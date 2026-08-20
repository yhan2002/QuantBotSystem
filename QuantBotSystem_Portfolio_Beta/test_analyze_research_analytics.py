from __future__ import annotations

import types
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import analyze_research_analytics as subject


class ResearchAnalyticsTests(unittest.TestCase):
    def test_momentum_panel_uses_signal_close_and_next_open_to_fixed_close(self):
        dates = pd.bdate_range("2025-01-02", periods=260)
        close = pd.Series([100.0 + index for index in range(260)], index=dates)
        prices = pd.DataFrame(
            {
                "Open": close + 0.5,
                "Close": close,
                "Volume": 1_000_000.0,
            },
            index=dates,
        )
        start = dates[252]
        end = dates[254]

        panel = subject.build_momentum_factor_panel(
            {"aaa": prices}, holding_days=2, start=start, end=end
        )

        self.assertEqual(len(panel), 1)
        self.assertEqual(panel.loc[0, "date"], start)
        self.assertEqual(panel.loc[0, "asset"], "AAA")
        self.assertEqual(panel.loc[0, "group"], "CSQ_UNIV")
        self.assertAlmostEqual(
            panel.loc[0, "forward_return"],
            prices["Close"].iloc[254] / prices["Open"].iloc[253] - 1.0,
        )

        changed = prices.copy()
        changed.iloc[254, changed.columns.get_loc("Close")] *= 1.5
        changed_panel = subject.build_momentum_factor_panel(
            {"aaa": changed}, holding_days=2, start=start, end=end
        )
        self.assertEqual(changed_panel.loc[0, "factor"], panel.loc[0, "factor"])
        self.assertNotEqual(
            changed_panel.loc[0, "forward_return"], panel.loc[0, "forward_return"]
        )

    def test_alphalens_uses_precomputed_panel_and_zero_max_loss(self):
        panel = pd.DataFrame(
            {
                "date": ["2026-01-02", "2026-01-02"],
                "asset": ["aaa", "bbb"],
                "factor": [-1.0, 1.0],
                "forward_return": [-0.02, 0.03],
            }
        )
        expected = pd.DataFrame({"clean": [1]})
        get_clean_factor = mock.Mock(return_value=expected)
        fake_utils = types.SimpleNamespace(get_clean_factor=get_clean_factor)

        with mock.patch.object(subject.importlib, "import_module", return_value=fake_utils):
            actual = subject.clean_alphalens_factor(panel, period="5D", quantiles=2)

        self.assertIs(actual, expected)
        factor, forward = get_clean_factor.call_args.args
        self.assertEqual(factor.index.names, ["date", "asset"])
        self.assertEqual(list(factor), [-1.0, 1.0])
        self.assertEqual(list(forward.columns), ["5D"])
        self.assertEqual(list(forward["5D"]), [-0.02, 0.03])
        self.assertEqual(get_clean_factor.call_args.kwargs["max_loss"], 0)
        self.assertIsNone(get_clean_factor.call_args.kwargs["groupby"])

    def test_alphalens_rejects_missing_before_optional_import(self):
        panel = pd.DataFrame(
            {
                "date": ["2026-01-02"],
                "asset": ["AAA"],
                "factor": [1.0],
                "forward_return": [float("nan")],
            }
        )
        with mock.patch.object(subject.importlib, "import_module") as import_module:
            with self.assertRaisesRegex(ValueError, "missing"):
                subject.clean_alphalens_factor(panel, period="1D")
        import_module.assert_not_called()

    def test_quantstats_uses_net_strategy_and_local_acwi_series(self):
        daily = pd.DataFrame(
            {
                "strategy_return": [0.01, -0.005],
                "market_benchmark_return": [0.002, 0.003],
            },
            index=pd.to_datetime(["2026-01-02", "2026-01-05"]),
        )
        metrics = pd.DataFrame({"Strategy": [1.0]})
        fake_reports = types.SimpleNamespace(
            metrics=mock.Mock(return_value=metrics), html=mock.Mock()
        )
        performance = {
            "daily": daily,
            "market_benchmark_daily_curve_available": True,
        }

        with mock.patch.object(
            subject.importlib, "import_module", return_value=fake_reports
        ):
            actual = subject.quantstats_metrics(performance)
            subject.write_quantstats_html(performance, Path("report.html"))

        self.assertIs(actual, metrics)
        metric_call = fake_reports.metrics.call_args.kwargs
        self.assertEqual(list(metric_call["returns"]), [0.01, -0.005])
        self.assertEqual(list(metric_call["benchmark"]), [0.002, 0.003])
        self.assertIsInstance(metric_call["benchmark"], pd.Series)
        self.assertEqual(metric_call["periods_per_year"], 252)
        self.assertTrue(metric_call["compounded"])
        self.assertEqual(metric_call["rf"], 0.0)
        html_call = fake_reports.html.call_args.kwargs
        self.assertEqual(html_call["output"], "report.html")
        self.assertIsInstance(html_call["benchmark"], pd.Series)
        self.assertEqual(html_call["periods_per_year"], 252)

    def test_alphalens_rejects_percent_units(self):
        panel = pd.DataFrame(
            {
                "date": ["2026-01-02"],
                "asset": ["AAA"],
                "factor": [1.0],
                "forward_return": [-2.0],
            }
        )
        with self.assertRaisesRegex(ValueError, "decimal units"):
            subject.clean_alphalens_factor(panel, period="5D")

    def test_alphalens_allows_total_loss_and_rejects_missing_group(self):
        total_loss = pd.DataFrame(
            {
                "date": ["2026-01-02"],
                "asset": ["AAA"],
                "factor": [1.0],
                "forward_return": [-1.0],
            }
        )
        fake_utils = types.SimpleNamespace(
            get_clean_factor=mock.Mock(return_value=pd.DataFrame())
        )
        with mock.patch.object(subject.importlib, "import_module", return_value=fake_utils):
            subject.clean_alphalens_factor(total_loss, period="5D", quantiles=None, bins=1)

        missing_group = total_loss.assign(sector=None)
        with self.assertRaisesRegex(ValueError, "group cannot be missing"):
            subject.clean_alphalens_factor(
                missing_group, period="5D", group_column="sector"
            )

    def test_quantstats_fails_closed_on_incomplete_local_acwi(self):
        performance = {
            "daily": pd.DataFrame(
                {
                    "strategy_return": [0.0],
                    "market_benchmark_return": [float("nan")],
                },
                index=pd.to_datetime(["2026-01-02"]),
            ),
            "market_benchmark_daily_curve_available": False,
            "market_benchmark_daily_curve_reason": "missing date",
        }
        with self.assertRaisesRegex(ValueError, "complete local ACWI"):
            subject.quantstats_metrics(performance)

    def test_daily_csv_rejects_empty_window_column(self):
        daily = pd.DataFrame(
            {
                "window": [None],
                "strategy_return": [0.0],
                "market_benchmark_return": [0.0],
            }
        )
        with mock.patch.object(subject.pd, "read_csv", return_value=daily):
            with self.assertRaisesRegex(ValueError, "no usable values"):
                subject._read_daily_csv("unused.csv", None)


if __name__ == "__main__":
    unittest.main()
