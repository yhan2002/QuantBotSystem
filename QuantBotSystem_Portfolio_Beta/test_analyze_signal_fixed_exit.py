import contextlib
import io
import unittest
from unittest import mock

import analyze_signal_fixed_exit


class FixedExitAnalysisTests(unittest.TestCase):
    def test_preview_does_not_load_strategy_runners(self):
        with mock.patch.object(
            analyze_signal_fixed_exit,
            "_strategy_runners",
        ) as runners, contextlib.redirect_stdout(io.StringIO()) as output:
            code = analyze_signal_fixed_exit.main(
                ["--strategy", "Reversion", "--fixed-exit-sessions", "5"]
            )

        self.assertEqual(0, code)
        runners.assert_not_called()
        self.assertIn("next-open entry -> D+5 close", output.getvalue())
        self.assertIn("Not run", output.getvalue())

    def test_confirmed_run_passes_fixed_exit_to_selected_strategy(self):
        runner = mock.Mock(return_value="REPORT")
        with mock.patch.object(
            analyze_signal_fixed_exit,
            "_strategy_runners",
            return_value={"Reversion": runner},
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            code = analyze_signal_fixed_exit.main(
                [
                    "--strategy",
                    "Reversion",
                    "--group",
                    "NASDAQ_TOP_30_20260731",
                    "--fixed-exit-sessions",
                    "5",
                    "--confirm-long-run",
                ]
            )

        self.assertEqual(0, code)
        config_universe = analyze_signal_fixed_exit.config.universe_groups[
            "NASDAQ_TOP_30_20260731"
        ]
        runner.assert_called_once_with(
            config_universe,
            position_mode="LONG_SHORT_NEUTRAL",
            fixed_exit_sessions=5,
        )
        self.assertEqual(30, len(config_universe))
        self.assertIn("REPORT", output.getvalue())


if __name__ == "__main__":
    unittest.main()
