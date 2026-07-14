# File name: box_range_engine.py
# Box false-breakout/reclaim strategy. Signals use the close and enter next open.
import pandas as pd

import market_data_store


BOX_MIN_WIDTH_PCT = 0.11
BOX_MAX_WIDTH_PCT = 0.20
BOUNCE_BUFFER_PCT = 0.01
VOLUME_LOOKBACK_DAYS = 20
VOLUME_SURGE_MULTIPLE = 2.0
TIME_EXIT_DAYS = 10
DEFAULT_EXIT_MODE = "reclaim_scale_out"
EXIT_MODE_LABELS = {
    DEFAULT_EXIT_MODE: "Reclaim: 50% center / 50% opposite edge, -7%/-10% stops",
}


def _normalize_ohlcv(df):
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    required = ["Open", "High", "Low", "Close", "Volume"]
    if any(column not in df.columns for column in required):
        return pd.DataFrame()
    return df.dropna(subset=required).copy()


def _period_to_rows(period):
    return {"1mo": 35, "3mo": 90, "6mo": 130, "1y": 260}.get(period)


def _load_ohlcv(ticker, period="3mo", allow_download=False):
    df = market_data_store.get_price_data(ticker, period_days=_period_to_rows(period))
    if not df.empty:
        return _normalize_ohlcv(df)
    if allow_download:
        market_data_store.ensure_price_data([ticker], min_period="1y")
        return _normalize_ohlcv(
            market_data_store.get_price_data(ticker, period_days=_period_to_rows(period))
        )
    return pd.DataFrame()


def _box_stats(history):
    high = float(history["High"].max())
    low = float(history["Low"].min())
    width_pct = (high - low) / low if low > 0 else float("inf")
    return {
        "highest": high,
        "lowest": low,
        "mid": (high + low) / 2.0,
        "box_width_pct": width_pct,
    }


def _boundary_bounces(history, stats, bounce_buffer_pct=BOUNCE_BUFFER_PCT):
    lower = stats["lowest"]
    upper = stats["highest"]
    lower_bounces = ((history["Low"] <= lower * (1 + bounce_buffer_pct)) & (history["Close"] > lower)).sum()
    upper_bounces = ((history["High"] >= upper * (1 - bounce_buffer_pct)) & (history["Close"] < upper)).sum()
    return int(lower_bounces), int(upper_bounces)


def _turnover_ratio(history, current):
    average = (history["Volume"] * history["Close"]).tail(VOLUME_LOOKBACK_DAYS).mean()
    current_turnover = float(current["Volume"] * current["Close"])
    if not average or pd.isna(average):
        return float("inf")
    return current_turnover / float(average)


def _entry_signal(history, current, min_width_pct=BOX_MIN_WIDTH_PCT, max_width_pct=BOX_MAX_WIDTH_PCT):
    """Return a close-known false-breakout reclaim signal for the next open."""
    stats = _box_stats(history)
    lower_bounces, upper_bounces = _boundary_bounces(history, stats)
    turnover_ratio = _turnover_ratio(history, current)
    stats.update(
        current_price=float(current["Close"]),
        lower_bounces=lower_bounces,
        upper_bounces=upper_bounces,
        turnover_ratio=turnover_ratio,
    )

    if not min_width_pct <= stats["box_width_pct"] <= max_width_pct:
        return "NORMAL", stats
    if turnover_ratio >= VOLUME_SURGE_MULTIPLE:
        return "VOLUME_VETO", stats

    lower_reclaim = current["Low"] < stats["lowest"] and stats["lowest"] < current["Close"] <= stats["highest"]
    upper_reclaim = current["High"] > stats["highest"] and stats["lowest"] <= current["Close"] < stats["highest"]
    if lower_reclaim and lower_bounces >= 2:
        return "LONG_RECLAIM", stats
    if upper_reclaim and upper_bounces >= 2:
        return "SHORT_RECLAIM", stats
    return "BOX_QUALIFIED", stats


def _pnl_pct(position, exit_price, entry_price):
    direction = 1.0 if position == "LONG" else -1.0
    return direction * (exit_price - entry_price) / entry_price * 100.0


def _stop_fill(position, day_open, level):
    return min(day_open, level) if position == "LONG" else max(day_open, level)


def _target_hit(position, day, level):
    return day["High"] >= level if position == "LONG" else day["Low"] <= level


def _stop_hit(position, day, level):
    return day["Low"] <= level if position == "LONG" else day["High"] >= level


def analyze_box_range(ticker, lookback_days=20, df=None):
    """Live signal: prior box, today's intraday breach and close reclaim."""
    try:
        df = _load_ohlcv(ticker, period="3mo") if df is None else _normalize_ohlcv(df)
        if df.empty or len(df) < lookback_days + 1:
            return {"status": "DATA_INSUFFICIENT"}

        history = df.iloc[-lookback_days - 1:-1]
        current = df.iloc[-1]
        status, stats = _entry_signal(history, current)
        box_text = f"{stats['lowest']:,.2f}~{stats['highest']:,.2f}"
        if status == "LONG_RECLAIM":
            message = f"lower false-break reclaim; box {box_text}, lower bounces {stats['lower_bounces']}, turnover {stats['turnover_ratio']:.1f}x"
        elif status == "SHORT_RECLAIM":
            message = f"upper false-break reclaim; box {box_text}, upper bounces {stats['upper_bounces']}, turnover {stats['turnover_ratio']:.1f}x"
        elif status == "VOLUME_VETO":
            message = f"qualified box {box_text}, but turnover {stats['turnover_ratio']:.1f}x blocks entry"
        elif status == "BOX_QUALIFIED":
            message = f"qualified box {box_text}; waiting for intraday breach and close reclaim"
        else:
            message = "box width must be 11%~20%"
        return {"status": status, "message": message, **stats}
    except Exception as exc:
        return {"status": "ERROR", "message": str(exc)}


def run_box_backtest(
    ticker,
    lookback_days=20,
    box_threshold_pct=BOX_MAX_WIDTH_PCT,
    touch_buffer_pct=BOUNCE_BUFFER_PCT,
    exit_mode=DEFAULT_EXIT_MODE,
    center_band_pct=0.10,
    fixed_tp_pct=4.0,
    fixed_sl_pct=2.0,
    box_break_buffer_pct=0.00,
    max_holding_days=None,
    side_filter=None,
    df=None,
    period="1y",
    include_trades=False,
):
    """Backtest the reclaim strategy using only the signal-day close and next open.

    Intraday target/stop collisions are resolved stop-first. Gap-through stops use
    the opening price when it is worse than the stop level.
    """
    del box_threshold_pct, touch_buffer_pct, center_band_pct, fixed_tp_pct, fixed_sl_pct, box_break_buffer_pct
    try:
        if exit_mode != DEFAULT_EXIT_MODE:
            raise ValueError(f"Unsupported exit_mode: {exit_mode}")
        df = _load_ohlcv(ticker, period=period) if df is None else _normalize_ohlcv(df)
        if df.empty or len(df) < lookback_days + 2:
            result = (0.0, 0.0, 0, "N/A", [])
            return result if include_trades else result[:4]
        period_label = f"{df.index[0].date()} to {df.index[-1].date()}"
        trades = []
        trade_details = []
        position = None

        def close_trade(current_position, exit_date):
            trades.append(current_position["realized"])
            trade_details.append(
                {
                    "side": current_position["side"],
                    "entry_date": current_position["entry_date"],
                    "exit_events": current_position["exit_events"],
                    "return": current_position["realized"],
                }
            )

        for i in range(lookback_days, len(df) - 1):
            day = df.iloc[i]
            if position is None:
                history = df.iloc[i - lookback_days:i]
                signal, stats = _entry_signal(history, day)
                side = "LONG" if signal == "LONG_RECLAIM" else "SHORT" if signal == "SHORT_RECLAIM" else None
                if side and side_filter in (None, side):
                    position = {
                        "side": side,
                        "entry_price": float(df["Open"].iloc[i + 1]),
                        "entry_index": i + 1,
                        "entry_date": df.index[i + 1].date().isoformat(),
                        "stats": stats,
                        "remaining": 1.0,
                        "realized": 0.0,
                        "center_taken": False,
                        "first_stop_taken": False,
                        "exit_events": [],
                    }
                continue

            side = position["side"]
            entry = position["entry_price"]
            remaining = position["remaining"]
            first_stop = entry * (0.93 if side == "LONG" else 1.07)
            final_stop = entry * (0.90 if side == "LONG" else 1.10)

            # Conservative daily-bar convention: protective stops take priority.
            if _stop_hit(side, day, final_stop):
                if not position["first_stop_taken"]:
                    half = remaining * 0.5
                    fill = _stop_fill(side, float(day["Open"]), first_stop)
                    position["realized"] += half * _pnl_pct(side, fill, entry)
                    position["exit_events"].append({"date": df.index[i].date().isoformat(), "reason": "-7% stop", "weight": half, "price": fill})
                    remaining -= half
                fill = _stop_fill(side, float(day["Open"]), final_stop)
                position["realized"] += remaining * _pnl_pct(side, fill, entry)
                position["exit_events"].append({"date": df.index[i].date().isoformat(), "reason": "-10% stop", "weight": remaining, "price": fill})
                close_trade(position, df.index[i].date().isoformat())
                position = None
                continue
            if not position["first_stop_taken"] and _stop_hit(side, day, first_stop):
                half = remaining * 0.5
                fill = _stop_fill(side, float(day["Open"]), first_stop)
                position["realized"] += half * _pnl_pct(side, fill, entry)
                position["exit_events"].append({"date": df.index[i].date().isoformat(), "reason": "-7% stop", "weight": half, "price": fill})
                position["remaining"] -= half
                position["first_stop_taken"] = True
                remaining = position["remaining"]

            if not position["center_taken"] and _target_hit(side, day, position["stats"]["mid"]):
                half = position["remaining"] * 0.5
                position["realized"] += half * _pnl_pct(side, position["stats"]["mid"], entry)
                position["exit_events"].append({"date": df.index[i].date().isoformat(), "reason": "center 50%", "weight": half, "price": position["stats"]["mid"]})
                position["remaining"] -= half
                position["center_taken"] = True

            if position["center_taken"] and _target_hit(side, day, position["stats"]["highest"] if side == "LONG" else position["stats"]["lowest"]):
                target = position["stats"]["highest"] if side == "LONG" else position["stats"]["lowest"]
                position["realized"] += position["remaining"] * _pnl_pct(side, target, entry)
                position["exit_events"].append({"date": df.index[i].date().isoformat(), "reason": "opposite edge", "weight": position["remaining"], "price": target})
                close_trade(position, df.index[i].date().isoformat())
                position = None
                continue

            held_days = i - position["entry_index"] + 1
            if not position["center_taken"] and held_days >= TIME_EXIT_DAYS:
                position["realized"] += position["remaining"] * _pnl_pct(side, float(day["Close"]), entry)
                position["exit_events"].append({"date": df.index[i].date().isoformat(), "reason": "10d time exit", "weight": position["remaining"], "price": float(day["Close"])})
                close_trade(position, df.index[i].date().isoformat())
                position = None
                continue
        if not trades:
            result = (0.0, 0.0, 0, period_label, [])
            return result if include_trades else result[:4]
        avg_return = sum(trades) / len(trades)
        win_rate = sum(pnl > 0 for pnl in trades) / len(trades) * 100.0
        result = (avg_return, win_rate, len(trades), period_label, trade_details)
        return result if include_trades else result[:4]
    except Exception:
        result = (0.0, 0.0, 0, "N/A", [])
        return result if include_trades else result[:4]


def compare_box_exit_modes(ticker, **kwargs):
    avg_return, win_rate, count, _ = run_box_backtest(ticker, **kwargs)
    return [{"mode": DEFAULT_EXIT_MODE, "label": EXIT_MODE_LABELS[DEFAULT_EXIT_MODE], "pnl": avg_return, "avg_return": avg_return, "win_rate": win_rate, "count": count}]


def _box_row(idx, ticker, res, action):
    return f"{idx}. {ticker:<6} | {action} | width {res['box_width_pct'] * 100:.1f}% | turnover {res['turnover_ratio']:.1f}x"


def _format_trade_dates(trade):
    exits = ", ".join(
        f"{event['date']} ({event['reason']})" for event in trade["exit_events"]
    )
    return f"{trade['entry_date']} -> {exits} {trade['return']:+.1f}%"


def scan_total_universe(universe_list):
    long_reclaim, short_reclaim, failed_tickers = [], [], []
    for ticker in universe_list:
        res = analyze_box_range(ticker)
        if res["status"] in ["DATA_INSUFFICIENT", "ERROR"]:
            failed_tickers.append(ticker)
        elif res["status"] == "LONG_RECLAIM":
            long_reclaim.append((ticker, res))
        elif res["status"] == "SHORT_RECLAIM":
            short_reclaim.append((ticker, res))

    report = "**Box Range Scan Report**\n=============================\n\n"
    report += "**LONG Candidates (next open)**\n" + ("\n".join(_box_row(i, ticker, res, "lower reclaim") for i, (ticker, res) in enumerate(long_reclaim[:10], 1)) or "None")
    report += "\n\n**SHORT Candidates (next open)**\n" + ("\n".join(_box_row(i, ticker, res, "upper reclaim") for i, (ticker, res) in enumerate(short_reclaim[:10], 1)) or "None")
    report += "\n\nBacktest Summary\n-----------------------------------------\n"
    report += "Entry: prior 20-day box width 11%~20%, 2+ same-side bounces, intraday boundary breach then close reclaim; turnover <2x 20d average; enter next open.\n"
    report += "Exit: 50% at center, balance at opposite edge; no center within 10d exits at close; -7% sells half of remaining, -10% closes all.\n"

    targets = [(ticker, "LONG") for ticker, _ in long_reclaim] + [(ticker, "SHORT") for ticker, _ in short_reclaim]
    total_return_sum = total_wins = 0.0
    total_trades = 0
    for ticker, side in targets[:15]:
        avg_return, win_rate, count, period_label, trade_details = run_box_backtest(
            ticker, side_filter=side, include_trades=True
        )
        total_return_sum += avg_return * count
        total_wins += win_rate / 100.0 * count
        total_trades += count
        if count:
            report += (
                f"{side} *{ticker}*: avg *{avg_return:+.1f}%* | "
                f"win {win_rate:.1f}% ({round(win_rate / 100.0 * count)}/{count})\n"
                f"  trades: {'; '.join(_format_trade_dates(trade) for trade in trade_details)}\n"
            )
    if targets:
        report += "\nSelected Candidate History Summary\n"
        avg = total_return_sum / total_trades if total_trades else 0.0
        win = total_wins / total_trades * 100.0 if total_trades else 0.0
        report += f"*Avg trade return {avg:+.1f}%* | win {win:.1f}% ({round(total_wins)}/{total_trades})\n"
    if failed_tickers:
        report += "\n**Data failed / insufficient**\n`" + ", ".join(sorted(set(failed_tickers))) + "`"
    return report


def scan_box_range(universe_list):
    return scan_total_universe(universe_list)
