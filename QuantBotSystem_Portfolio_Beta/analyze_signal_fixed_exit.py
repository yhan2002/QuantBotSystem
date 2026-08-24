"""Run an opt-in local-cache signal-to-D+N-close Beta comparison."""

import argparse

import config
import core_entry_execution
import core_position_mode


DEFAULT_GROUP = "NASDAQ_TOP_30_20260731"
DEFAULT_EXIT_SESSIONS = 5
STRATEGY_NAMES = ("Momentum", "Box Range", "Box Range (New one)", "Reversion")


def _strategy_runners():
    import strategy_box_range
    import strategy_box_range_new
    import strategy_ma_cross
    import strategy_price_liquidity_reversion

    return {
        "Momentum": strategy_ma_cross.scan_ma_cross,
        "Box Range": strategy_box_range.scan_box_range,
        "Box Range (New one)": strategy_box_range_new.scan_box_range_new,
        "Reversion": strategy_price_liquidity_reversion.scan_value_reversion,
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strategy",
        choices=("ALL", *STRATEGY_NAMES),
        default="ALL",
    )
    parser.add_argument(
        "--group",
        choices=sorted(config.universe_groups),
        default=DEFAULT_GROUP,
    )
    parser.add_argument(
        "--position-mode",
        choices=tuple(core_position_mode.POSITION_MODE_LABELS),
        default=core_position_mode.LONG_SHORT_NEUTRAL,
    )
    parser.add_argument(
        "--fixed-exit-sessions",
        type=int,
        default=DEFAULT_EXIT_SESSIONS,
    )
    parser.add_argument("--confirm-long-run", action="store_true")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        sessions = core_entry_execution.normalize_fixed_exit_sessions(
            args.fixed_exit_sessions
        )
    except ValueError as exc:
        parser.error(str(exc))

    universe = config.universe_groups[args.group]
    strategies = STRATEGY_NAMES if args.strategy == "ALL" else (args.strategy,)
    print(
        f"Fixed exit preview: {len(universe)} tickers x {len(strategies)} strategies | "
        f"next-open entry -> D+{sessions} close | local cache only"
    )
    print(
        "Native targets, stops, partial exits, and incomplete D+N holdings are excluded."
    )
    if not args.confirm_long_run:
        print("Not run. Re-run with --confirm-long-run to execute the comparison.")
        return 0

    runners = _strategy_runners()
    for strategy in strategies:
        print(f"\n===== {strategy} | {args.group} =====", flush=True)
        report = runners[strategy](
            universe,
            position_mode=args.position_mode,
            fixed_exit_sessions=sessions,
        )
        print(report, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
