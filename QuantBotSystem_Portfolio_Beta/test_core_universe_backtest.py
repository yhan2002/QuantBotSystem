import unittest
from unittest import mock
import tempfile
from pathlib import Path
import zipfile

import pandas as pd

import core_universe_backtest


class UniverseBacktestTests(unittest.TestCase):
    def test_progress_callback_reports_each_ticker_completion_once(self):
        progress = []

        def runner(ticker, include_trades=False, **_kwargs):
            if ticker == "ERROR":
                raise RuntimeError("runner failed")
            period = (
                "2026-01-01 to 2026-07-16"
                if ticker == "OK"
                else "N/A"
            )
            return 0.0, 0.0, 0, period, []

        result = core_universe_backtest.aggregate_universe_backtest(
            ["OK", "NO_DATA", "ERROR"],
            runner,
            progress_callback=lambda completed, total, ticker, succeeded: progress.append(
                (completed, total, ticker, succeeded)
            ),
        )

        self.assertEqual(
            progress,
            [
                (1, 3, "OK", True),
                (2, 3, "NO_DATA", False),
                (3, 3, "ERROR", False),
            ],
        )
        self.assertEqual(result["eligible_tickers"], 1)
        self.assertEqual(result["failed_tickers"], ["NO_DATA", "ERROR"])

    def test_progress_callback_errors_do_not_change_backtest_results(self):
        callback_calls = []

        def runner(ticker, include_trades=False, **_kwargs):
            return 1.0, 100.0, 1, "2026-01-01 to 2026-07-16", [{
                "side": "LONG",
                "entry_date": "2026-02-02",
                "exit_date": "2026-02-06",
                "return": 1.0,
            }]

        def broken_callback(completed, total, ticker, succeeded):
            callback_calls.append((completed, total, ticker, succeeded))
            raise RuntimeError("Telegram edit failed")

        result = core_universe_backtest.aggregate_universe_backtest(
            ["AAA", "BBB"],
            runner,
            progress_callback=broken_callback,
        )

        self.assertEqual(len(callback_calls), 2)
        self.assertEqual(result["eligible_tickers"], 2)
        self.assertEqual(result["failed_tickers"], [])
        self.assertEqual(result["all"]["count"], 2)

    def test_aggregates_every_ticker_and_splits_summary_detail(self):
        calls = []

        def runner(ticker, include_trades=False, **kwargs):
            calls.append((ticker, include_trades, kwargs.get("evaluation_days")))
            side = "LONG" if ticker == "AAA" else "SHORT"
            value = 2.0 if ticker == "AAA" else -1.0
            details = [
                {
                    "side": side,
                    "entry_date": "2025-08-01",
                    "exit_date": "2025-08-08",
                    "return": value,
                }
            ]
            return value, 100.0 if value > 0 else 0.0, 1, "2025-07-15 to 2026-07-15", details

        result = core_universe_backtest.aggregate_universe_backtest(
            ["AAA", "BBB"], runner, evaluation_days=252
        )
        report = core_universe_backtest.format_universe_backtest(
            result, "entry rule", "exit rule"
        )
        summary, detail = core_universe_backtest.split_summary_detail(report)

        self.assertEqual(len(calls), 2)
        self.assertTrue(all(include_trades for _, include_trades, _ in calls))
        self.assertEqual(result["all"]["count"], 2)
        self.assertIn("Entry: entry rule", summary)
        self.assertIn("Exit: exit rule", summary)
        self.assertIn("ALL: trades 2", summary)
        self.assertNotIn("2025-08-01", summary)
        self.assertIn("LONG AAA: 2025-08-01", detail)
        self.assertIn("SHORT BBB: 2025-08-01", detail)

    def test_fixed_is_oos_report_keeps_windows_separate(self):
        def runner(ticker, include_trades=False, **kwargs):
            start = kwargs["evaluation_start"]
            end = kwargs["evaluation_end"]
            details = [
                {
                    "side": "LONG",
                    "entry_date": start,
                    "exit_date": end,
                    "return": 1.0,
                }
            ]
            return 1.0, 100.0, 1, f"{start} to {end}", details

        selection = core_universe_backtest.aggregate_universe_backtest(
            ["AAA"], runner, evaluation_start="2024-01-01", evaluation_end="2025-12-31"
        )
        oos = core_universe_backtest.aggregate_universe_backtest(
            ["AAA"], runner, evaluation_start="2026-01-01", evaluation_end="2026-07-31"
        )
        summary, detail = core_universe_backtest.split_summary_detail(
            core_universe_backtest.format_fixed_is_oos_backtest(
                selection, oos, "entry rule", "exit rule"
            )
        )

        self.assertIn("Candidate selection (IS: 2024-01 to 2025-12):", summary)
        self.assertIn("Fixed OOS (2026-01 ~ 2026-07-31):", summary)
        self.assertIn("Closed trades: 1", detail)

    def test_portfolio_summary_uses_cagr_while_side_detail_says_avg_trade(self):
        stats = {
            "count": 2,
            "avg": 0.5,
            "median": 0.5,
            "win_rate": 50.0,
            "wins": 1,
        }
        long_stats = {
            "count": 1,
            "avg": 2.0,
            "median": 2.0,
            "win_rate": 100.0,
            "wins": 1,
        }
        short_stats = {
            "count": 1,
            "avg": -1.0,
            "median": -1.0,
            "win_rate": 0.0,
            "wins": 0,
        }
        result = {
            "period": "2026-01-01 to 2026-07-31",
            "universe_count": 10,
            "eligible_tickers": 9,
            "traded_tickers": 2,
            "trades": [],
            "all": stats,
            "long": long_stats,
            "short": short_stats,
            "box_attribution": {},
            "portfolio_performance": {
                "strategy": {
                    "roi_pct": 6.0,
                    "cagr_pct": 12.34,
                    "mdd_pct": -2.5,
                    "sharpe": None,
                },
                "benchmark": {"roi_pct": 3.0},
                "market_benchmark": {"roi_pct": 3.0},
                "universe_ew_benchmark": {"roi_pct": 4.0},
                "market_benchmark_multiple": 2.0,
                "stock_selection_excess_pct": 2.0,
                "daily": pd.DataFrame(
                    {"gross_exposure_pct": [10.0, 20.0]}
                ),
                "ledger": {
                    "filled_trades": 2,
                    "skipped_trades": 0,
                    "policy": {
                        "max_gross": 1.0,
                        "max_name_weight": 0.05,
                        "long_cap": 0.5,
                        "short_cap": 0.5,
                    },
                },
            },
        }

        summary, detail = core_universe_backtest.split_summary_detail(
            core_universe_backtest.format_fixed_is_oos_backtest(
                result,
                result,
                "entry rule",
                "exit rule",
            )
        )

        self.assertIn(
            "ALL: trades 2 | portfolio CAGR +12.34% | "
            "trade win 50.0% (1/2)",
            summary,
        )
        self.assertNotIn("avg trade", summary)
        self.assertIn(
            "LONG: trades 1 | avg trade +2.00%",
            detail,
        )
        self.assertIn(
            "MSCI ACWI ROI: +3.0% | Strategy ROI: +6.0% | Multiple: 2.00x",
            detail,
        )
        self.assertIn(
            "Universe EW ROI: +4.0% | Stock-selection excess: +2.0%p",
            detail,
        )
        self.assertIn("Portfolio risk: CAGR +12.34% | MDD -2.50%", detail)
        self.assertIn(
            "CAGR: annualized from daily portfolio NAV using 252 trading days.",
            summary,
        )

    def test_portfolio_lines_show_review_when_msci_acwi_is_missing(self):
        result = {
            "portfolio_performance": {
                "strategy": {
                    "roi_pct": 5.0,
                    "cagr_pct": 10.0,
                    "mdd_pct": -3.0,
                    "sharpe": 1.0,
                },
                "market_benchmark": {"roi_pct": None},
                "universe_ew_benchmark": {"roi_pct": 4.0},
                "stock_selection_excess_pct": 1.0,
                "market_benchmark_unavailable_reason": "No local MIWD00000PUS data",
                "daily": pd.DataFrame({"gross_exposure_pct": [10.0]}),
                "ledger": {
                    "filled_trades": 1,
                    "skipped_trades": 0,
                    "policy": {
                        "max_gross": 1.0,
                        "max_name_weight": 0.05,
                        "long_cap": 0.5,
                        "short_cap": 0.5,
                    },
                },
            }
        }

        lines = core_universe_backtest._portfolio_lines(result)

        self.assertIn(
            "MSCI ACWI ROI: N/A | Strategy ROI: +5.0% | Multiple: N/A",
            lines,
        )
        self.assertTrue(any("Benchmark status: REVIEW" in line for line in lines))

    def test_trade_event_csv_keeps_partial_exits_and_is_oos_windows(self):
        selection = {
            "period": "2024-01-01 to 2025-12-31",
            "trades": [{
                "ticker": "AAA", "side": "LONG", "entry_date": "2024-02-01",
                "entry_price": 100.0, "return": 3.0,
                "exit_events": [
                    {"date": "2024-02-05", "reason": "partial", "weight": 0.5, "price": 102.0},
                    {"date": "2024-02-08", "reason": "final", "weight": 0.5, "price": 104.0},
                ],
            }],
        }
        oos = {
            "period": "2026-01-01 to 2026-07-16",
            "trades": [{
                "ticker": "BBB", "side": "SHORT", "entry_date": "2026-02-01",
                "entry_price": 100.0, "exit_date": "2026-02-06", "exit_price": 95.0,
                "return": 5.0,
            }],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = core_universe_backtest.export_trade_events_csv(
                selection, oos, "box_range", temp_dir
            )
            lines = Path(path).read_text(encoding="utf-8-sig").splitlines()

        self.assertEqual(len(lines), 4)  # header + two partial IS exits + one OOS exit
        self.assertIn("IS,2024-01-01,2025-12-31,box_range,IS-00001,AAA,LONG", lines[1])
        self.assertIn("OOS,2026-01-01,2026-07-16,box_range,OOS-00001,BBB,SHORT", lines[3])

    def test_portfolio_bundle_contains_trade_ledger_and_daily_nav_files(self):
        daily = pd.DataFrame(
            {
                "strategy_return": [0.0],
                "strategy_nav": [1.0],
                "market_benchmark_return": [0.001],
                "market_benchmark_nav": [1.001],
                "universe_ew_return": [0.002],
                "universe_ew_nav": [1.002],
                "universe_ew_constituents": [10],
                "cash": [100.0],
                "gross_exposure_pct": [0.0],
            },
            index=pd.to_datetime(["2026-01-02"]),
        )
        result = {
            "period": "2026-01-02 to 2026-01-02",
            "trades": [
                {
                    "trade_id": "T000777",
                    "ticker": "AAA",
                    "side": "LONG",
                    "entry_date": "2026-01-02",
                    "entry_price": 100.0,
                    "exit_date": "2026-01-02",
                    "exit_price": 101.0,
                    "return": 1.0,
                }
            ],
            "portfolio_performance": {
                "daily": daily,
                "strategy": {"roi_pct": 2.0},
                "market_benchmark": {"roi_pct": 1.0},
                "market_benchmark_label": "MSCI ACWI",
                "market_benchmark_ticker": "MIWD00000PUS",
                "market_benchmark_available": True,
                "market_benchmark_start_level_date": "2025-12-31",
                "market_benchmark_start_level": 100.0,
                "market_benchmark_end_level_date": "2026-01-02",
                "market_benchmark_end_level": 101.0,
                "market_benchmark_multiple": 2.0,
                "universe_ew_benchmark": {"roi_pct": 1.5},
                "stock_selection_excess_pct": 0.5,
                "market_benchmark_daily_curve_available": True,
                "ledger": {
                    "events": [
                        {
                            "date": "2026-01-02",
                            "event": "ENTRY",
                            "trade_id": "T000777",
                            "ticker": "AAA",
                        }
                    ]
                },
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = core_universe_backtest.export_portfolio_bundle(
                result,
                result,
                "box_range",
                temp_dir,
            )
            with zipfile.ZipFile(bundle) as archive:
                names = archive.namelist()
                trade_name = next(
                    name for name in names if name.endswith("_trade_events.csv")
                )
                ledger_name = next(
                    name for name in names if name.endswith("_ledger_events.csv")
                )
                daily_name = next(
                    name for name in names if name.endswith("_portfolio_daily.csv")
                )
                benchmark_name = next(
                    name for name in names if name.endswith("_benchmark_summary.csv")
                )
                trade_rows = pd.read_csv(archive.open(trade_name))
                ledger_rows = pd.read_csv(archive.open(ledger_name))
                daily_rows = pd.read_csv(archive.open(daily_name))
                benchmark_rows = pd.read_csv(archive.open(benchmark_name))

        self.assertEqual(len(names), 4)
        self.assertTrue(any(name.endswith("_trade_events.csv") for name in names))
        self.assertTrue(any(name.endswith("_portfolio_daily.csv") for name in names))
        self.assertTrue(any(name.endswith("_ledger_events.csv") for name in names))
        self.assertTrue(any(name.endswith("_benchmark_summary.csv") for name in names))
        self.assertEqual({"T000777"}, set(trade_rows["trade_id"]))
        self.assertEqual({"T000777"}, set(ledger_rows["trade_id"]))
        self.assertTrue(
            {
                "market_benchmark_return",
                "market_benchmark_nav",
                "universe_ew_return",
                "universe_ew_nav",
                "universe_ew_constituents",
            }.issubset(daily_rows.columns)
        )
        self.assertEqual(set(benchmark_rows["market_benchmark_ticker"]), {"MIWD00000PUS"})
        self.assertEqual(set(benchmark_rows["start_level_date"]), {"2025-12-31"})
        self.assertEqual(set(benchmark_rows["market_multiple"]), {2.0})

    def test_csv_marker_preserves_trailing_strategy_report_text(self):
        payload = (
            "summary" + core_universe_backtest.DETAIL_MARKER + "detail"
            + core_universe_backtest.CSV_MARKER + "C:\\tmp\\events.csv\nfooter"
        )
        summary, detail, csv_path = core_universe_backtest.split_report_payload(payload)

        self.assertEqual(summary, "summary")
        self.assertEqual(csv_path, "C:\\tmp\\events.csv")
        self.assertIn("footer", detail)

    def test_market_neutralization_filters_entry_date_cross_section(self):
        def runner(ticker, include_trades=False, **_kwargs):
            score = 3.0 if ticker == "AAA" else 1.0
            details = [{
                "side": "LONG",
                "entry_date": "2026-02-02",
                "exit_date": "2026-02-06",
                "signal_score": score,
                "return": score,
            }]
            return score, 100.0, 1, "2026-01-01 to 2026-07-16", details

        result = core_universe_backtest.aggregate_universe_backtest(
            ["AAA", "BBB"], runner, neutralization="MARKET"
        )

        self.assertEqual(result["all"]["count"], 1)
        self.assertEqual(result["trades"][0]["ticker"], "AAA")
        self.assertAlmostEqual(result["trades"][0]["neutralized_score"], 1.0)

    def test_neutralization_uses_only_securities_live_on_the_entry_date(self):
        entry_date = "2025-01-06"
        frames = {
            "AAA": pd.DataFrame(
                {"Close": [100.0, 101.0]},
                index=pd.to_datetime(["2025-01-02", "2025-01-10"]),
            ),
            "BBB": pd.DataFrame(
                {"Close": [50.0, 51.0]},
                index=pd.to_datetime(["2025-01-03", "2025-01-09"]),
            ),
            "FUTURE": pd.DataFrame(
                {"Close": [20.0, 21.0]},
                index=pd.to_datetime(["2025-02-03", "2025-03-03"]),
            ),
            "PAST": pd.DataFrame(
                {"Close": [30.0, 31.0]},
                index=pd.to_datetime(["2024-11-01", "2024-12-31"]),
            ),
        }
        metadata = {
            ticker: {
                "listing_country": "US",
                "sector": "Technology",
                "industry": "Software",
            }
            for ticker in frames
        }
        eligible = list(frames)

        with mock.patch.object(
            core_universe_backtest.core_market_data_store,
            "get_security_metadata",
            return_value=metadata,
        ):
            for mode in ("MARKET", "COUNTRY", "SECTOR", "INDUSTRY"):
                with self.subTest(mode=mode):
                    result = core_universe_backtest._neutralize_trade_entries(
                        [
                            {
                                "ticker": "AAA",
                                "side": "LONG",
                                "entry_date": entry_date,
                                "signal_score": 1.0,
                            }
                        ],
                        eligible,
                        mode,
                        price_availability_loader=lambda ticker: frames[ticker],
                    )

                    self.assertEqual(len(result), 1)
                    # BBB is live and remains a zero-score cross-sectional
                    # member. FUTURE and PAST must not dilute the cohort.
                    self.assertAlmostEqual(
                        result[0]["neutralized_score"],
                        0.5,
                    )


if __name__ == "__main__":
    unittest.main()
