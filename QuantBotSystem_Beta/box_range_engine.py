# File name: box_range_engine.py
# Runs box-range signal analysis and box strategy backtests.
import pandas as pd
import catalyst_factor
import market_data_store


DEFAULT_EXIT_MODE = "long_scale_short_center"
EXIT_MODE_LABELS = {
    "long_scale_short_center": "Long scale-out, short-risk center exit",
    "scale_center_opposite": "50% center + 50% opposite edge, breakeven guard",
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
    if period == "6mo":
        return 130
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


def _pnl_pct(position, exit_price, entry_price):
    if position == "LONG":
        return (exit_price - entry_price) / entry_price * 100.0
    return (entry_price - exit_price) / entry_price * 100.0


def _box_break_stop(position, c_price, entry_stats, box_break_buffer_pct):
    if position == "LONG":
        stop_level = entry_stats["lowest"] * (1 - box_break_buffer_pct)
        return c_price < stop_level
    stop_level = entry_stats["highest"] * (1 + box_break_buffer_pct)
    return c_price > stop_level


def _center_reached(position, c_price, entry_stats):
    if position == "LONG":
        return c_price >= entry_stats["mid"]
    return c_price <= entry_stats["mid"]


def _opposite_reached(position, c_price, entry_stats):
    if position == "LONG":
        return c_price >= entry_stats["highest"]
    return c_price <= entry_stats["lowest"]


def _breakeven_guard_reached(position, c_price, entry_price):
    if position == "LONG":
        return c_price <= entry_price
    return c_price >= entry_price


def _close_position(
    position,
    c_price,
    entry_price,
    entry_stats,
    exit_mode,
    center_band_pct,
    fixed_tp_pct,
    fixed_sl_pct,
    box_break_buffer_pct,
):
    pnl_pct = _pnl_pct(position, c_price, entry_price)

    if _box_break_stop(position, c_price, entry_stats, box_break_buffer_pct):
        return True

    if exit_mode == "center_band":
        lower = entry_stats["mid"] * (1 - center_band_pct)
        upper = entry_stats["mid"] * (1 + center_band_pct)
        return lower <= c_price <= upper

    if exit_mode == "opposite_touch":
        return _opposite_reached(position, c_price, entry_stats)

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
    box_break_buffer_pct=0.00,
    max_holding_days=20,
    side_filter=None,
    df=None,
    period="1y",
):
    """
    One-year box strategy backtest.

    exit_mode:
    - long_scale_short_center: use the scale-out rule for LONG rebound signals and
      center exit for SHORT/TRIM risk signals.
    - scale_center_opposite: close 50% at the box midpoint, then protect the rest
      near breakeven while aiming for the opposite side.
    - center_band: close when price reaches the box midpoint +/- center_band_pct.
    - opposite_touch: close when price touches the opposite side of the box.
    - fixed_return: close at fixed_tp_pct profit or fixed_sl_pct loss.

    Stop rule:
    - LONG exits when price closes below the entry box low.
    - SHORT exits when price closes above the entry box high.
    """
    try:
        if df is None:
            df = _load_ohlcv(ticker, period=period)
        else:
            df = _normalize_ohlcv(df)

        if df.empty or len(df) < lookback_days + 5:
            return 0.0, 0.0, 0, "N/A"

        period_label = f"{df.index[0].date()} to {df.index[-1].date()}"

        position = None
        entry_price = 0.0
        entry_stats = None
        entry_index = None
        center_taken = False
        realized_pnl = 0.0
        remaining_weight = 1.0
        trades = []

        for i in range(lookback_days, len(df) - 1):
            window = df.iloc[i - lookback_days + 1 : i + 1]
            stats = _box_stats(window)
            c_price = float(df["Close"].iloc[i])
            next_open = float(df["Open"].iloc[i + 1])
            signal = _entry_signal(stats, box_threshold_pct, touch_buffer_pct)

            if position is None:
                if signal == "TOUCH_LOWER" and side_filter in (None, "LONG"):
                    position = "LONG"
                    entry_price = next_open
                    entry_stats = stats.copy()
                    entry_index = i + 1
                    center_taken = False
                    realized_pnl = 0.0
                    remaining_weight = 1.0
                elif signal == "TOUCH_UPPER" and side_filter in (None, "SHORT"):
                    position = "SHORT"
                    entry_price = next_open
                    entry_stats = stats.copy()
                    entry_index = i + 1
                    center_taken = False
                    realized_pnl = 0.0
                    remaining_weight = 1.0
                continue

            effective_exit_mode = exit_mode
            if exit_mode == "long_scale_short_center":
                effective_exit_mode = (
                    "scale_center_opposite" if position == "LONG" else "center_band"
                )

            if effective_exit_mode == "scale_center_opposite":
                if _box_break_stop(
                    position,
                    c_price,
                    entry_stats,
                    box_break_buffer_pct,
                ):
                    pnl = realized_pnl + remaining_weight * _pnl_pct(
                        position,
                        next_open,
                        entry_price,
                    )
                    trades.append(pnl)
                    position = None
                    continue

                if not center_taken and _center_reached(position, c_price, entry_stats):
                    realized_pnl += 0.5 * _pnl_pct(position, next_open, entry_price)
                    remaining_weight = 0.5
                    center_taken = True

                if center_taken and _breakeven_guard_reached(
                    position,
                    c_price,
                    entry_price,
                ):
                    pnl = realized_pnl + remaining_weight * _pnl_pct(
                        position,
                        next_open,
                        entry_price,
                    )
                    trades.append(pnl)
                    position = None
                    continue

                if center_taken and _opposite_reached(position, c_price, entry_stats):
                    pnl = realized_pnl + remaining_weight * _pnl_pct(
                        position,
                        next_open,
                        entry_price,
                    )
                    trades.append(pnl)
                    position = None
                    continue

                if entry_index is not None and i - entry_index >= max_holding_days:
                    pnl = realized_pnl + remaining_weight * _pnl_pct(
                        position,
                        next_open,
                        entry_price,
                    )
                    trades.append(pnl)
                    position = None
                continue

            should_close = _close_position(
                position,
                c_price,
                entry_price,
                entry_stats,
                effective_exit_mode,
                center_band_pct,
                fixed_tp_pct,
                fixed_sl_pct,
                box_break_buffer_pct,
            )

            if should_close:
                trades.append(_pnl_pct(position, next_open, entry_price))
                position = None

        if not trades:
            return 0.0, 0.0, 0, period_label

        avg_return = sum(trades) / len(trades)
        win_rate = sum(1 for pnl in trades if pnl > 0) / len(trades) * 100.0
        return avg_return, win_rate, len(trades), period_label

    except Exception:
        return 0.0, 0.0, 0, "N/A"


def compare_box_exit_modes(ticker, **kwargs):
    df = _load_ohlcv(ticker, period="1y")
    results = []
    for mode in EXIT_MODE_LABELS:
        avg_return, win_rate, count, _ = run_box_backtest(
            ticker,
            exit_mode=mode,
            df=df,
            **kwargs,
        )
        results.append(
            {
                "mode": mode,
                "label": EXIT_MODE_LABELS[mode],
                "pnl": avg_return,
                "avg_return": avg_return,
                "win_rate": win_rate,
                "count": count,
            }
        )
    return results


def _catalyst_label(catalyst_map, ticker):
    row = catalyst_map.get(ticker)
    if not row:
        return "catalyst unavailable"
    if row.get("confirmed"):
        return "catalyst confirmed"
    return "catalyst unconfirmed"


def _box_row(idx, ticker, res, action, catalyst_text):
    width = res["box_width_pct"] * 100.0
    return (
        f"{idx}. {ticker:<6} | {action} | "
        f"width {width:.1f}% | {catalyst_text}"
    )


def scan_total_universe(universe_list):
    long_rebound = []
    breakout_watch = []
    trim_risk = []
    failed_tickers = []
    backtest_targets = []
    catalyst_map = catalyst_factor.latest_catalyst_map(universe_list)

    for ticker in universe_list:
        res = analyze_box_range(ticker)

        if res["status"] in ["DATA_INSUFFICIENT", "ERROR"]:
            failed_tickers.append(ticker)
            continue

        catalyst_score = catalyst_factor.confirmed_score(catalyst_map, ticker)
        catalyst_text = _catalyst_label(catalyst_map, ticker)

        if res["status"] == "TOUCH_LOWER":
            long_rebound.append((ticker, res, catalyst_text))
            backtest_targets.append((ticker, "LONG REBOUND", res["status"]))
        elif res["status"] == "TOUCH_UPPER" and catalyst_score > 0:
            breakout_watch.append((ticker, res, catalyst_text))
        elif res["status"] == "TOUCH_UPPER":
            trim_risk.append((ticker, res, catalyst_text))
            backtest_targets.append((ticker, "TRIM / SHORT-RISK", res["status"]))

    report = "**Box Range Scan Report**\n"
    report += "=============================\n\n"
    report += "**LONG Candidates**\n"
    report += (
        "\n".join(
            _box_row(idx, ticker, res, "near lower box", catalyst_text)
            for idx, (ticker, res, catalyst_text) in enumerate(long_rebound[:10], 1)
        )
        or "None"
    )
    report += "\n\n**Breakout Watch**\n"
    report += (
        "\n".join(
            _box_row(idx, ticker, res, "near upper box", catalyst_text)
            for idx, (ticker, res, catalyst_text) in enumerate(breakout_watch[:10], 1)
        )
        or "None"
    )
    report += "\n\n**SHORT Candidates**\n"
    report += (
        "\n".join(
            _box_row(idx, ticker, res, "near upper box", catalyst_text)
            for idx, (ticker, res, catalyst_text) in enumerate(trim_risk[:10], 1)
        )
        or "None"
    )
    report += "\n\n"

    if backtest_targets:
        report += "Backtest Summary\n"
        report += "-----------------------------------------\n"
        report += (
            "Entry: 20-day box width <=20%; lower touch for LONG, upper touch for "
            "SHORT; enter next open.\n"
            "Exit: LONG takes 50% at center and targets the opposite edge; SHORT exits "
            "near center. Both stop on box break.\n"
        )
        total_return_sum = 0.0
        total_wins = 0.0
        total_trades = 0
        for ticker, action, status in backtest_targets[:15]:
            side = "LONG" if status == "TOUCH_LOWER" else "SHORT"
            avg_return, win_rate, count, period_label = run_box_backtest(
                ticker,
                side_filter=side,
            )
            total_return_sum += avg_return * count
            total_wins += (win_rate / 100.0) * count
            total_trades += count
            period_text = f" | period {period_label}" if period_label != "N/A" else ""
            if count:
                wins = round((win_rate / 100.0) * count)
                report += (
                    f"{side} *{ticker} ({action})*: "
                    f"avg *{avg_return:+.1f}%* | win {win_rate:.1f}% "
                    f"({wins}/{count}){period_text}\n"
                )
            else:
                report += (
                    f"{side} *{ticker} ({action})*: "
                    f"no closed trades{period_text}\n"
                )
        total_win_rate = total_wins / total_trades * 100.0 if total_trades else 0.0
        portfolio_avg = total_return_sum / total_trades if total_trades else 0.0
        total_win_count = round(total_wins)
        report += "\nSelected Candidate History Summary\n"
        report += (
            f"*Avg trade return {portfolio_avg:+.1f}%* | "
            f"win {total_win_rate:.1f}% ({total_win_count}/{total_trades})\n"
        )
        report += "-----------------------------------------\n"
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
