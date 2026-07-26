import unittest
from unittest.mock import patch

import pandas as pd

import core_backtest_periods
import core_universe_backtest
import strategy_residual_stat_arb_app as subject


def _price_frame(last_date):
    index = pd.date_range(end=last_date, periods=3, freq="B")
    return pd.DataFrame(
        {
            "Open": [100.0, 101.0, 102.0],
            "High": [101.0, 102.0, 103.0],
            "Low": [99.0, 100.0, 101.0],
            "Close": [100.5, 101.5, 102.5],
            "Volume": [1000.0, 1000.0, 1000.0],
        },
        index=index,
    )


def _signal(ticker, side, signal_date, score):
    return {
        "status": "OK",
        "ticker": ticker,
        "side": side,
        "signal_date": signal_date,
        "signal_score": score,
        "entry_execution": "NEXT_OPEN",
        "residual_z": -1.5 if side == "LONG" else 1.5,
        "previous_residual_z": -2.5 if side == "LONG" else 2.5,
        "selected_peers": ["PEER"],
    }


class ResidualStatArbAppTests(unittest.TestCase):
    def test_default_as_of_excludes_current_kst_date_but_explicit_is_inclusive(self):
        price_map = {"AAA": _price_frame("2026-01-06")}
        metadata = {"AAA": {"listing_country": "US"}}
        runtime_cutoff = pd.Timestamp("2026-01-05") - pd.Timedelta(
            nanoseconds=1
        )

        with patch.object(
            subject,
            "_default_runtime_as_of",
            return_value=runtime_cutoff,
        ):
            default_dates = subject._country_latest_dates(price_map, metadata)
            normalized_default = subject._normalize_as_of(None)
        explicit_dates = subject._country_latest_dates(
            price_map,
            metadata,
            as_of="2026-01-05",
        )

        self.assertEqual(normalized_default, runtime_cutoff)
        self.assertEqual(
            default_dates["US"],
            pd.Timestamp("2026-01-02"),
        )
        self.assertEqual(
            explicit_dates["US"],
            pd.Timestamp("2026-01-05"),
        )

    def test_country_feed_older_than_seven_calendar_days_is_excluded(self):
        price_map = {"OLD": _price_frame("2026-01-02")}
        metadata = {"OLD": {"listing_country": "US"}}
        structured = {
            "signals": [_signal("OLD", "LONG", "2026-01-02", 2.5)],
            "longs": [],
            "shorts": [],
            "no_signal": [],
            "insufficient": [],
        }

        filtered, stale, latest, stale_countries = (
            subject._filter_stale_signals(
                structured,
                price_map,
                metadata,
                as_of="2026-01-12",
                max_stale_calendar_days=7,
            )
        )

        self.assertEqual(filtered["signals"], [])
        self.assertEqual([row["ticker"] for row in stale], ["OLD"])
        self.assertEqual(latest, {})
        self.assertEqual(
            stale_countries,
            {"US": pd.Timestamp("2026-01-02")},
        )
        self.assertEqual(
            subject._current_score_cohorts(filtered, metadata, latest),
            {},
        )

    def test_score_cohorts_are_current_eligible_and_split_by_signal_date(self):
        price_map = {
            "AAA": _price_frame("2026-01-05"),
            "USZERO": _price_frame("2026-01-05"),
            "USSHORT": _price_frame("2026-01-05"),
            "STALE": _price_frame("2026-01-02"),
            "JPN": _price_frame("2026-01-02"),
            "JPNZERO": _price_frame("2026-01-02"),
            "MISS": _price_frame("2026-01-05"),
        }
        metadata = {
            "AAA": {"listing_country": "US", "sector": "Technology"},
            "USZERO": {"listing_country": "US", "sector": "Technology"},
            "USSHORT": {"listing_country": "US", "sector": "Technology"},
            "STALE": {"listing_country": "US", "sector": "Technology"},
            "JPN": {"listing_country": "JP", "sector": "Technology"},
            "JPNZERO": {"listing_country": "JP", "sector": "Technology"},
            "MISS": {"listing_country": "US", "sector": "Technology"},
        }
        latest_by_country = subject._country_latest_dates(price_map, metadata)
        structured = {
            "signals": [
                _signal("AAA", "LONG", "2026-01-05", 2.5),
                _signal("USSHORT", "SHORT", "2026-01-05", -2.0),
                _signal("JPN", "SHORT", "2026-01-02", -2.4),
            ],
            "no_signal": [
                {
                    "ticker": "USZERO",
                    "signal_date": "2026-01-05",
                    "status": "NO_SIGNAL",
                },
                {
                    "ticker": "STALE",
                    "signal_date": "2026-01-02",
                    "status": "NO_SIGNAL",
                },
                {
                    "ticker": "JPNZERO",
                    "signal_date": "2026-01-02",
                    "status": "NO_SIGNAL",
                },
            ],
            "insufficient": [
                {"ticker": "MISS", "status": "DATA_INSUFFICIENT"}
            ],
        }
        score_cohorts = subject._current_score_cohorts(
            structured,
            metadata,
            latest_by_country,
        )
        neutralize = (
            subject.core_cross_sectional_neutralization.neutralize_scores
        )

        with patch.object(
            subject.core_cross_sectional_neutralization,
            "neutralize_scores",
            wraps=neutralize,
        ) as mocked_neutralize:
            candidates = subject._scored_candidates(
                structured,
                "MARKET",
                "LONG_SHORT_NEUTRAL",
                score_cohorts,
                metadata,
            )

        raw_cohorts = [call.args[0] for call in mocked_neutralize.call_args_list]
        self.assertEqual(
            score_cohorts,
            {
                "2026-01-05": ["AAA", "USSHORT", "USZERO"],
                "2026-01-02": ["JPN", "JPNZERO"],
            },
        )
        self.assertCountEqual(
            raw_cohorts,
            [
                {"AAA": 2.5, "USSHORT": -2.0, "USZERO": 0.0},
                {"JPN": -2.4, "JPNZERO": 0.0},
            ],
        )
        self.assertCountEqual(
            [row["ticker"] for row in candidates],
            ["AAA", "USSHORT", "JPN"],
        )

        with patch.object(
            subject.core_cross_sectional_neutralization,
            "neutralize_scores",
            wraps=neutralize,
        ) as long_only_neutralize:
            long_only = subject._scored_candidates(
                structured,
                "MARKET",
                "LONG_ONLY",
                score_cohorts,
                metadata,
            )

        self.assertEqual(
            long_only_neutralize.call_args.args[0],
            {"AAA": 2.5, "USSHORT": 0.0, "USZERO": 0.0},
        )
        self.assertEqual([row["ticker"] for row in long_only], ["AAA"])

    def test_adapter_uses_oos_peer_cutoff_and_filters_stale_by_country(self):
        price_map = {
            # Explicit as_of must ignore this frame's future cache row.
            "AAA": _price_frame("2026-01-06"),
            "BBB": _price_frame("2026-01-02"),
            "JPN": _price_frame("2026-01-02"),
        }
        metadata = {
            "AAA": {"listing_country": "US", "sector": "Technology"},
            "BBB": {"listing_country": "US", "sector": "Technology"},
            "JPN": {"listing_country": "JP", "sector": "Technology"},
        }
        structured = {
            "strategy": "residual_stat_arb",
            "signals": [
                _signal("AAA", "LONG", "2026-01-05", 2.5),
                _signal("BBB", "LONG", "2026-01-02", 2.2),
                _signal("JPN", "SHORT", "2026-01-02", -2.4),
            ],
            "longs": [],
            "shorts": [],
            "insufficient": [{"ticker": "MISS", "status": "DATA_INSUFFICIENT"}],
            "no_signal": [],
        }
        selection_result = {
            "trades": [],
            "qualified_trades": [
                {
                    "exit_execution_status": "MARKED_AT_EVALUATION_END",
                    "exit_execution_failures": [],
                }
            ],
        }
        oos_result = {
            "trades": [],
            "qualified_trades": [
                {
                    "exit_execution_status": "FILLED_DELAYED_AFTER_MISSING_OPEN",
                    "exit_execution_failures": [{"missing_legs": ["PEER"]}],
                }
            ],
        }
        payload = (
            "BACKTEST SUMMARY"
            + core_universe_backtest.DETAIL_MARKER
            + "BACKTEST DETAIL"
        )

        with patch.object(
            subject, "_load_local_inputs", return_value=(price_map, metadata)
        ), patch.object(
            subject.engine, "scan_residual_stat_arb", return_value=structured
        ) as scan, patch.object(
            subject.core_universe_backtest,
            "aggregate_universe_backtest",
            side_effect=[selection_result, oos_result],
        ) as aggregate, patch.object(
            subject.core_universe_backtest,
            "format_fixed_is_oos_backtest",
            return_value=payload,
        ):
            report = subject.scan_residual_stat_arb(
                ["AAA", "BBB", "JPN", "MISS"],
                as_of="2026-01-05",
            )

        self.assertEqual(
            scan.call_args.kwargs["peer_selection_as_of"],
            core_backtest_periods.SELECTION_END,
        )
        self.assertEqual(
            scan.call_args.kwargs["as_of"],
            pd.Timestamp("2026-01-05"),
        )
        self.assertEqual(
            aggregate.call_args_list[1].kwargs["evaluation_end"],
            "2026-01-05",
        )
        self.assertIn("Peer cohort cutoff: 2025-12-31", report)
        self.assertIn("1 stale signals removed", report)
        self.assertIn(
            "0 country feeds older than 7 calendar days excluded",
            report,
        )
        self.assertIn("1 insufficient tickers", report)
        self.assertIn("2 listing-country calendars", report)
        self.assertIn("AAA", report)
        self.assertIn("signal 2026-01-05", report)
        self.assertIn("JPN", report)
        self.assertNotIn("BBB", report)
        self.assertIn("1 missing-next-open leg events", report)
        self.assertIn("1 evaluation-end marks", report)


if __name__ == "__main__":
    unittest.main()
