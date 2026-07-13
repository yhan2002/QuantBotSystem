# File name: box_range_engine.py
# Runs box-range signal analysis and box strategy backtests.
import pandas as pd
import market_data_store


DEFAULT_EXIT_MODE = "center_band"
EXIT_MODE_LABELS = {
    "center_band": "Center band +/-10%",
    "opposite_touch": "Opposite box edge touch",
    "fixed_return": "Fixed return TP/SL",
}


def _normalize_ohlcv(df):
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna(subset=["Open", "High", "Low", "Close"]).copy()


def _period_to_rows(period):
    if period == "1mo":
        return 35
    if period == "3mo":
        return 90
    if period == "1y":
        return 260
    return None


def _load_ohlcv(ticker, period="3mo", allow_download=False):
    df = market_data_store.get_price_data(ticker, period_days=_period_to_rows(period))
    if not df.empty:
        return _normalize_ohlcv(df)

    if allow_download:
        market_data_store.ensure_price_data([ticker], min_period="1y")
        df = market_data_store.get_price_data(ticker, period_days=_period_to_rows(period))
        if not df.empty:
            return _normalize_ohlcv(df)

    return pd.DataFrame()


def _box_stats(df):
    high = float(df["High"].max())
    low = float(df["Low"].min())
    close = float(df["Close"].iloc[-1])
    width_pct = (high - low) / low if low > 0 else float("inf")
    mid = (high + low) / 2.0
    return {
        "current_price": close,
        "highest": high,
        "lowest": low,
        "mid": mid,
        "box_width_pct": width_pct,
    }


def _entry_signal(stats, box_threshold_pct, touch_buffer_pct):
    if stats["box_width_pct"] > box_threshold_pct:
        return "NORMAL"

    upper_touch = stats["highest"] * (1 - touch_buffer_pct)
    lower_touch = stats["lowest"] * (1 + touch_buffer_pct)
    price = stats["current_price"]

    if price >= upper_touch:
        return "TOUCH_UPPER"
    if price <= lower_touch:
        return "TOUCH_LOWER"
    return "BOX_FORMED"


def _close_position(
    position,
    c_price,
    entry_price,
    stats,
    exit_mode,
    center_band_pct,
    fixed_tp_pct,
    fixed_sl_pct,
):
    if position == "LONG":
        pnl_pct = (c_price - entry_price) / entry_price * 100.0
    else:
        pnl_pct = (entry_price - c_price) / entry_price * 100.0

    if exit_mode == "center_band":
        lower = stats["mid"] * (1 - center_band_pct)
        upper = stats["mid"] * (1 + center_band_pct)
        return lower <= c_price <= upper or pnl_pct <= -5.0

    if exit_mode == "opposite_touch":
        if position == "LONG":
            return c_price >= stats["highest"]
        return c_price <= stats["lowest"]

    if exit_mode == "fixed_return":
        return pnl_pct >= fixed_tp_pct or pnl_pct <= -fixed_sl_pct

    raise ValueError(f"Unsupported exit_mode: {exit_mode}")


def analyze_box_range(
    ticker,
    lookback_days=20,
    box_threshold_pct=0.20,
    touch_buffer_pct=0.01,
    df=None,
):
    """
    Current box-range signal.

    The current production definition remains Donchian-style:
    a box exists when the recent high-low range is within box_threshold_pct.
    """
    try:
        if df is None:
            df = _load_ohlcv(ticker, period="3mo")
        else:
            df = _normalize_ohlcv(df)

        if df.empty or len(df) < lookback_days:
            return {"status": "DATA_INSUFFICIENT"}

        window = df.tail(lookback_days)
        stats = _box_stats(window)
        status = _entry_signal(stats, box_threshold_pct, touch_buffer_pct)

        message = ""
        if status == "TOUCH_UPPER":
            message = (
                f"upper touch: current {stats['current_price']:,.2f}, "
                f"box {stats['lowest']:,.2f}~{stats['highest']:,.2f}, "
                f"width {stats['box_width_pct'] * 100:.1f}%"
            )
        elif status == "TOUCH_LOWER":
            message = (
                f"lower touch: current {stats['current_price']:,.2f}, "
                f"box {stats['lowest']:,.2f}~{stats['highest']:,.2f}, "
                f"width {stats['box_width_pct'] * 100:.1f}%"
            )
        elif status == "BOX_FORMED":
            message = (
                f"box formed: width {stats['box_width_pct'] * 100:.1f}% "
                f"({stats['lowest']:,.2f}~{stats['highest']:,.2f})"
            )

        return {"status": status, "message": message, **stats}

    except Exception as e:
        return {"status": "ERROR", "message": str(e)}


def run_box_backtest(
    ticker,
    lookback_days=20,
    box_threshold_pct=0.20,
    touch_buffer_pct=0.01,
    exit_mode=DEFAULT_EXIT_MODE,
    center_band_pct=0.10,
    fixed_tp_pct=4.0,
    fixed_sl_pct=2.0,
    df=None,
):
    """
    One-year box strategy backtest.

    exit_mode:
    - center_band: close when price reaches the box midpoint +/- center_band_pct.
    - opposite_touch: close when price touches the opposite side of the box.
    - fixed_return: close at fixed_tp_pct profit or fixed_sl_pct loss.
    """
    try:
        if df is None:
            df = _load_ohlcv(ticker, period="1y")
        else:
            df = _normalize_ohlcv(df)

        if df.empty or len(df) < lookback_days + 5:
            return 0.0, 0.0, 0

        position = None
        entry_price = 0.0
        trades = []

        for i in range(lookback_days, len(df) - 1):
            window = df.iloc[i - lookback_days + 1 : i + 1]
            stats = _box_stats(window)
            c_price = float(df["Close"].iloc[i])
            next_open = float(df["Open"].iloc[i + 1])
            signal = _entry_signal(stats, box_threshold_pct, touch_buffer_pct)

            if position is None:
                if signal == "TOUCH_LOWER":
                    position = "LONG"
                    entry_price = next_open
                elif signal == "TOUCH_UPPER":
                    position = "SHORT"
                    entry_price = next_open
                continue

            should_close = _close_position(
                position,
                c_price,
                entry_price,
                stats,
                exit_mode,
                center_band_pct,
                fixed_tp_pct,
                fixed_sl_pct,
            )

            if should_close:
                if position == "LONG":
                    trades.append((next_open - entry_price) / entry_price * 100.0)
                else:
                    trades.append((entry_price - next_open) / entry_price * 100.0)
                position = None

        if not trades:
            return 0.0, 0.0, 0

        total_pnl = sum(trades)
        win_rate = sum(1 for pnl in trades if pnl > 0) / len(trades) * 100.0
        return total_pnl, win_rate, len(trades)

    except Exception:
        return 0.0, 0.0, 0


def compare_box_exit_modes(ticker, **kwargs):
    df = _load_ohlcv(ticker, period="1y")
    results = []
    for mode in EXIT_MODE_LABELS:
        pnl, win_rate, count = run_box_backtest(ticker, exit_mode=mode, df=df, **kwargs)
        results.append(
            {
                "mode": mode,
                "label": EXIT_MODE_LABELS[mode],
                "pnl": pnl,
                "win_rate": win_rate,
                "count": count,
            }
        )
    return results


def scan_total_universe(universe_list):
    boxed_items = []
    touch_alerts = []
    failed_tickers = []
    backtest_targets = []

    for ticker in universe_list:
        res = analyze_box_range(ticker)

        if res["status"] in ["DATA_INSUFFICIENT", "ERROR"]:
            failed_tickers.append(ticker)
            continue

        if res["status"] in ["TOUCH_UPPER", "TOUCH_LOWER"]:
            touch_alerts.append(f"- *{ticker}*: {res['message']}")
            backtest_targets.append((ticker, res["status"]))
        elif res["status"] == "BOX_FORMED":
            boxed_items.append(f"- *{ticker}*: {res['message']}")

    report = "**[Box Range Strategy Scan]**\n"
    report += "=============================\n\n"

    report += "**Entry watch: boundary-touch names**\n"
    report += ("\n".join(touch_alerts) if touch_alerts else "None") + "\n\n"

    report += "**Stable box candidates**\n"
    report += ("\n".join(boxed_items) if boxed_items else "None") + "\n\n"

    if backtest_targets:
        report += "Backtest Summary\n"
        report += "-----------------------------\n"
        report += (
            f"Default exit: {EXIT_MODE_LABELS[DEFAULT_EXIT_MODE]} "
            "(test other modes with /boxbt TICKER)\n"
        )
        for ticker, status in backtest_targets:
            tag = "LONG" if status == "TOUCH_LOWER" else "SHORT"
            pnl, win_rate, count = run_box_backtest(ticker)
            report += (
                f"- *{ticker} ({tag})*: total {pnl:+.1f}% | "
                f"win {win_rate:.1f}% ({count} trades)\n"
            )
        report += "============================="

    if failed_tickers:
        failed_str = ", ".join(sorted(set(failed_tickers)))
        report += (
            "\n\n----------------------------------------\n"
            "**Data failed / insufficient**\n"
            f"`{failed_str}`"
        )

    return report


def scan_box_range(universe_list):
    return scan_total_universe(universe_list)
