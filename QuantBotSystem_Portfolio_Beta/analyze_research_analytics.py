"""Offline Alphalens/QuantStats adapters; never imported by the runtime."""

from __future__ import annotations

import argparse
import importlib
import math
from collections.abc import Iterable, Mapping
from pathlib import Path

import pandas as pd


FACTOR_COLUMNS = ("date", "asset", "factor", "forward_return")
PERFORMANCE_COLUMNS = ("strategy_return", "market_benchmark_return")


def _research_module(module: str, distribution: str):
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name == module or module.startswith(f"{exc.name}."):
            raise RuntimeError(
                f"Install research dependencies with "
                f"`python -m pip install -r requirements-research.txt` "
                f"({distribution} is missing)."
            ) from exc
        raise


def factor_panel_from_scored_signals(
    scored_signals: Iterable[object],
    forward_returns: Mapping[tuple[object, object], object] | pd.Series,
) -> pd.DataFrame:
    """Adapt existing ``ScoredSignal.rank_score`` values to the research panel."""

    returns = {
        (pd.Timestamp(date).normalize(), str(asset).strip().upper()): value
        for (date, asset), value in forward_returns.items()
    }
    rows = []
    for scored in scored_signals:
        signal = getattr(scored, "signal", None)
        if signal is None:
            raise TypeError("each item must expose ScoredSignal.signal")
        key = (
            pd.Timestamp(signal.signal_date).normalize(),
            str(signal.ticker).strip().upper(),
        )
        if key not in returns:
            raise ValueError(f"missing forward return for {key[0].date()} {key[1]}")
        rows.append(
            {
                "date": key[0],
                "asset": key[1],
                "factor": getattr(scored, "rank_score"),
                "forward_return": returns[key],
            }
        )
    return pd.DataFrame(rows, columns=FACTOR_COLUMNS)


def build_momentum_factor_panel(
    price_by_asset: Mapping[object, pd.DataFrame],
    *,
    group: str = "CSQ_UNIV",
    holding_days: int | None = None,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Build a dense, precomputed Momentum panel from cached OHLCV frames."""

    import strategy_ma_cross

    if not isinstance(price_by_asset, Mapping) or not price_by_asset:
        raise ValueError("price_by_asset cannot be empty")
    group = str(group).strip()
    if not group:
        raise ValueError("group cannot be blank")
    horizon = (
        strategy_ma_cross.MOMENTUM_HOLDING_DAYS
        if holding_days is None
        else holding_days
    )
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0:
        raise ValueError("holding_days must be a positive integer")
    start_at = pd.Timestamp(start).normalize() if start is not None else None
    end_at = pd.Timestamp(end).normalize() if end is not None else None
    if start_at is not None and end_at is not None and start_at > end_at:
        raise ValueError("start cannot be after end")

    windows = strategy_ma_cross.MOMENTUM_MA_WINDOWS
    warmup = max(max(windows), strategy_ma_cross.TURNOVER_LOOKBACK_DAYS)
    rows = []
    for raw_asset, raw_prices in price_by_asset.items():
        asset = str(raw_asset).strip().upper()
        if not asset:
            raise ValueError("asset cannot be blank")
        prices = strategy_ma_cross._normalize_ohlcv(raw_prices)
        if prices.empty:
            continue
        prices.index = pd.to_datetime(prices.index, errors="raise").normalize()
        if prices.index.has_duplicates:
            raise ValueError(f"{asset} price dates must be unique")
        prices = strategy_ma_cross._add_ma_signal_columns(prices.sort_index())
        for signal_idx in range(warmup, len(prices) - horizon):
            entry_idx = signal_idx + 1
            exit_idx = signal_idx + horizon
            signal_date = prices.index[signal_idx]
            exit_date = prices.index[exit_idx]
            if start_at is not None and signal_date < start_at:
                continue
            if end_at is not None and exit_date > end_at:
                continue
            factor = float(prices["SIGNAL_STRENGTH"].iloc[signal_idx])
            entry = float(prices["Open"].iloc[entry_idx])
            exit_price = float(prices["Close"].iloc[exit_idx])
            if not all(math.isfinite(value) for value in (factor, entry, exit_price)):
                raise ValueError(f"{asset} contains non-finite factor or execution prices")
            if entry <= 0 or exit_price < 0:
                raise ValueError(f"{asset} contains invalid execution prices")
            rows.append(
                {
                    "date": signal_date,
                    "asset": asset,
                    "factor": factor,
                    "forward_return": exit_price / entry - 1.0,
                    "group": group,
                }
            )

    panel = pd.DataFrame(rows, columns=(*FACTOR_COLUMNS, "group"))
    if panel.empty:
        raise ValueError("no complete Momentum factor observations")
    return panel.sort_values(["date", "asset"], ignore_index=True)


def clean_alphalens_factor(
    panel: pd.DataFrame,
    *,
    period: str,
    quantiles: int | None = 5,
    bins: int | list[float] | None = None,
    group_column: str | None = None,
    binning_by_group: bool = False,
) -> pd.DataFrame:
    """Validate a precomputed uniform-horizon panel with fail-closed Alphalens."""

    if not isinstance(panel, pd.DataFrame):
        raise TypeError("panel must be a pandas DataFrame")
    missing = set(FACTOR_COLUMNS) - set(panel.columns)
    if missing:
        raise ValueError(f"factor panel is missing columns: {sorted(missing)}")
    try:
        horizon = pd.Timedelta(period)
    except (TypeError, ValueError) as exc:
        raise ValueError("period must be a positive pandas Timedelta label") from exc
    if horizon <= pd.Timedelta(0):
        raise ValueError("period must be positive")
    if bins is not None and quantiles is not None:
        raise ValueError("provide quantiles or bins, not both")

    required_columns = [*FACTOR_COLUMNS]
    if group_column is not None:
        if group_column not in panel.columns:
            raise ValueError(f"factor panel is missing group column: {group_column}")
        required_columns.append(group_column)
    frame = panel.loc[:, required_columns].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.normalize()
    frame["asset"] = frame["asset"].astype(str).str.strip().str.upper()
    frame["factor"] = pd.to_numeric(frame["factor"], errors="raise")
    frame["forward_return"] = pd.to_numeric(
        frame["forward_return"], errors="raise"
    )
    if frame.empty:
        raise ValueError("factor panel cannot be empty")
    if (frame["asset"] == "").any():
        raise ValueError("asset cannot be blank")
    if frame[["date", "asset", "factor", "forward_return"]].isna().any().any():
        raise ValueError("factor panel cannot contain missing values")
    if not all(
        math.isfinite(float(value))
        for column in ("factor", "forward_return")
        for value in frame[column]
    ):
        raise ValueError("factor and forward_return must be finite")
    if frame.duplicated(["date", "asset"]).any():
        raise ValueError("factor panel contains duplicate (date, asset) rows")
    if (frame["forward_return"] < -1.0).any():
        raise ValueError("forward_return must be at least -1.0 in decimal units")
    if group_column is not None:
        if frame[group_column].isna().any():
            raise ValueError("group cannot be missing")
        frame[group_column] = frame[group_column].astype(str).str.strip()
        if (frame[group_column] == "").any():
            raise ValueError("group cannot be blank")

    indexed = frame.sort_values(["date", "asset"]).set_index(["date", "asset"])
    indexed.index = indexed.index.set_names(["date", "asset"])
    factor = indexed["factor"]
    forward = indexed[["forward_return"]].rename(columns={"forward_return": period})
    groupby = indexed[group_column] if group_column is not None else None
    utils = _research_module("alphalens.utils", "alphalens-reloaded")
    return utils.get_clean_factor(
        factor,
        forward,
        groupby=groupby,
        quantiles=quantiles,
        bins=bins,
        binning_by_group=binning_by_group,
        max_loss=0,
    )


def _performance_daily(performance_or_daily: Mapping[str, object] | pd.DataFrame):
    if isinstance(performance_or_daily, pd.DataFrame):
        daily = performance_or_daily.copy()
    elif isinstance(performance_or_daily, Mapping):
        if performance_or_daily.get("market_benchmark_daily_curve_available") is False:
            reason = performance_or_daily.get("market_benchmark_daily_curve_reason")
            raise ValueError(f"complete local ACWI daily returns are required: {reason}")
        daily = performance_or_daily.get("daily")
        if not isinstance(daily, pd.DataFrame):
            raise TypeError("performance['daily'] must be a pandas DataFrame")
        daily = daily.copy()
    else:
        raise TypeError("expected a performance mapping or daily DataFrame")

    missing = set(PERFORMANCE_COLUMNS) - set(daily.columns)
    if missing:
        raise ValueError(f"performance daily data is missing columns: {sorted(missing)}")
    if "date" in daily.columns:
        daily = daily.set_index("date")
    daily.index = pd.to_datetime(daily.index, errors="raise").normalize()
    if daily.index.has_duplicates:
        raise ValueError("performance daily dates must be unique")
    daily = daily.sort_index()
    selected = daily.loc[:, PERFORMANCE_COLUMNS].apply(pd.to_numeric, errors="raise")
    if selected.empty or selected.isna().any().any():
        raise ValueError("strategy and local ACWI returns must be complete")
    if not all(math.isfinite(float(value)) for value in selected.to_numpy().ravel()):
        raise ValueError("strategy and local ACWI returns must be finite")
    if (selected < -1.0).any().any():
        raise ValueError("strategy and local ACWI returns must be at least -1.0")
    strategy = selected["strategy_return"].rename("Strategy")
    benchmark = selected["market_benchmark_return"].rename("ACWI")
    return strategy, benchmark


def quantstats_metrics(
    performance_or_daily: Mapping[str, object] | pd.DataFrame,
    *,
    mode: str = "basic",
) -> pd.DataFrame:
    """Create QuantStats metrics from ledger returns and the local ACWI curve."""

    strategy, benchmark = _performance_daily(performance_or_daily)
    reports = _research_module("quantstats.reports", "quantstats")
    return reports.metrics(
        returns=strategy,
        benchmark=benchmark,
        display=False,
        mode=mode,
        benchmark_title="ACWI",
        strategy_title="Strategy",
        periods_per_year=252,
        compounded=True,
        rf=0.0,
    )


def write_quantstats_html(
    performance_or_daily: Mapping[str, object] | pd.DataFrame,
    output: str | Path,
    *,
    title: str = "QuantBot Research",
) -> Path:
    """Write an HTML tear sheet only when the caller supplies an output path."""

    path = Path(output)
    if path.suffix.lower() not in {".html", ".htm"}:
        raise ValueError("QuantStats output must be an .html or .htm file")
    strategy, benchmark = _performance_daily(performance_or_daily)
    reports = _research_module("quantstats.reports", "quantstats")
    reports.html(
        returns=strategy,
        benchmark=benchmark,
        output=str(path),
        title=title,
        benchmark_title="ACWI",
        strategy_title="Strategy",
        periods_per_year=252,
        compounded=True,
        rf=0.0,
    )
    return path


def _read_daily_csv(path: str | Path, window: str | None) -> pd.DataFrame:
    daily = pd.read_csv(path, encoding="utf-8-sig")
    if "window" in daily.columns:
        choices = [str(value) for value in daily["window"].dropna().unique()]
        if not choices:
            raise ValueError("daily CSV window column has no usable values")
        if window is None and len(choices) != 1:
            raise ValueError(f"choose --window from {choices}")
        selected = window or choices[0]
        daily = daily.loc[daily["window"].astype(str) == selected]
    return daily


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    factor_parser = subparsers.add_parser("factor", help="validate a factor panel")
    factor_parser.add_argument("panel_csv")
    factor_parser.add_argument("--period", required=True)
    factor_parser.add_argument("--quantiles", type=int, default=5)
    factor_parser.add_argument("--group-column")
    factor_parser.add_argument("--binning-by-group", action="store_true")
    factor_parser.add_argument("--output")

    momentum_parser = subparsers.add_parser(
        "momentum-panel", help="build a dense Momentum panel from the local cache"
    )
    momentum_parser.add_argument("--group", default="CSQ_UNIV")
    momentum_parser.add_argument("--holding-days", type=int)
    momentum_parser.add_argument("--start")
    momentum_parser.add_argument("--end")
    momentum_parser.add_argument("--output", required=True)

    performance_parser = subparsers.add_parser(
        "performance", help="print local portfolio/ACWI metrics"
    )
    performance_parser.add_argument("daily_csv")
    performance_parser.add_argument("--window")
    performance_parser.add_argument("--mode", choices=("basic", "full"), default="basic")
    performance_parser.add_argument("--html")

    args = parser.parse_args(argv)
    if args.command == "momentum-panel":
        import config
        import core_market_data_store

        if args.group not in config.universe_groups:
            raise ValueError(f"unknown universe group: {args.group}")
        panel = build_momentum_factor_panel(
            {
                ticker: core_market_data_store.get_price_data(ticker)
                for ticker in dict.fromkeys(config.universe_groups[args.group])
            },
            group=args.group,
            holding_days=args.holding_days,
            start=args.start,
            end=args.end,
        )
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        panel.to_csv(output, index=False, encoding="utf-8-sig")
        print(f"Wrote {len(panel)} rows to {output}")
        return 0

    if args.command == "factor":
        cleaned = clean_alphalens_factor(
            pd.read_csv(args.panel_csv, encoding="utf-8-sig"),
            period=args.period,
            quantiles=args.quantiles,
            group_column=args.group_column,
            binning_by_group=args.binning_by_group,
        )
        if args.output:
            cleaned.reset_index().to_csv(args.output, index=False, encoding="utf-8-sig")
        else:
            print(cleaned.to_string())
        return 0

    daily = _read_daily_csv(args.daily_csv, args.window)
    print(quantstats_metrics(daily, mode=args.mode).to_string())
    if args.html:
        write_quantstats_html(daily, args.html)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
