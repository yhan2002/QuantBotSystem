# File name: ma_cross_engine.py
# Generates moving-average momentum signals and backtest reports.
import numpy as np
import pandas as pd
import market_data_store


def _normalize_ohlcv(df):
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna(subset=["Open", "Close"]).copy()


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


def analyze_trend_strict_signals(ticker, threshold_pct=1.0, period="3mo"):
    """Calculate MA alignment and slope signal for one ticker."""
    try:
        df = _load_ohlcv(ticker, period=period)

        if df.empty or len(df) < 22:
            return "INSUFFICIENT_DATA", 0.0

        df["MA5"] = df["Close"].rolling(window=5).mean()
        df["MA10"] = df["Close"].rolling(window=10).mean()
        df["MA20"] = df["Close"].rolling(window=20).mean()

        df["slope_pct5"] = df["MA5"].pct_change() * 100
        df["slope_pct10"] = df["MA10"].pct_change() * 100
        df["slope_pct20"] = df["MA20"].pct_change() * 100

        latest = df.iloc[-1]
        ma5, ma10, ma20 = latest["MA5"], latest["MA10"], latest["MA20"]
        sp5, sp10, sp20 = (
            latest["slope_pct5"],
            latest["slope_pct10"],
            latest["slope_pct20"],
        )

        if pd.isna([ma5, ma10, ma20, sp5, sp10, sp20]).any():
            return "NO_SIGNAL", 0.0

        signal_strength = (sp5 + sp10 + sp20) / 3.0
        all_slopes_positive = (
            sp5 > threshold_pct and sp10 > threshold_pct and sp20 > threshold_pct
        )
        all_slopes_negative = (
            sp5 < -threshold_pct and sp10 < -threshold_pct and sp20 < -threshold_pct
        )

        if (ma5 > ma10 > ma20) and all_slopes_positive:
            return "LONG_ENTRY", signal_strength
        if (ma5 < ma10 < ma20) and all_slopes_negative:
            return "SHORT_ENTRY", signal_strength
        return "EXIT_SIGNAL", signal_strength
    except Exception:
        return "ERROR", 0.0


def calculate_portfolio_weights(top_longs, top_shorts):
    """Allocate 50% gross long and 50% gross short by rank-weighted top names."""
    weights = {}
    if top_longs:
        long_ranks = np.arange(len(top_longs), 0, -1)
        total_rank = np.sum(long_ranks)
        for idx, (ticker, _) in enumerate(top_longs):
            weights[ticker] = (long_ranks[idx] / total_rank) * 50.0

    if top_shorts:
        short_ranks = np.arange(len(top_shorts), 0, -1)
        total_rank = np.sum(short_ranks)
        for idx, (ticker, _) in enumerate(top_shorts):
            weights[ticker] = (short_ranks[idx] / total_rank) * 50.0

    return weights


def run_isolated_backtest(ticker, threshold_pct=1.0):
    """Run a simple one-year isolated MA momentum backtest."""
    try:
        df = _load_ohlcv(ticker, period="1y")
        if df.empty or len(df) < 22:
            return 0.0, 0.0, 0

        df["MA5"] = df["Close"].rolling(window=5).mean()
        df["MA10"] = df["Close"].rolling(window=10).mean()
        df["MA20"] = df["Close"].rolling(window=20).mean()

        df["slope_pct5"] = df["MA5"].pct_change() * 100
        df["slope_pct10"] = df["MA10"].pct_change() * 100
        df["slope_pct20"] = df["MA20"].pct_change() * 100

        df["all_slopes_positive"] = (
            (df["slope_pct5"] > threshold_pct)
            & (df["slope_pct10"] > threshold_pct)
            & (df["slope_pct20"] > threshold_pct)
        )
        df["all_slopes_negative"] = (
            (df["slope_pct5"] < -threshold_pct)
            & (df["slope_pct10"] < -threshold_pct)
            & (df["slope_pct20"] < -threshold_pct)
        )

        df["LONG_SIGNAL"] = (
            (df["MA5"] > df["MA10"])
            & (df["MA10"] > df["MA20"])
            & df["all_slopes_positive"]
        )
        df["SHORT_SIGNAL"] = (
            (df["MA5"] < df["MA10"])
            & (df["MA10"] < df["MA20"])
            & df["all_slopes_negative"]
        )

        position = None
        entry_price = 0.0
        trades = []

        for idx in range(len(df) - 1):
            next_open = float(df["Open"].iloc[idx + 1])
            if position is None:
                if df["LONG_SIGNAL"].iloc[idx]:
                    position = "LONG"
                    entry_price = next_open
                elif df["SHORT_SIGNAL"].iloc[idx]:
                    position = "SHORT"
                    entry_price = next_open
            elif position == "LONG" and not df["LONG_SIGNAL"].iloc[idx]:
                trades.append((next_open - entry_price) / entry_price * 100.0)
                position = None
            elif position == "SHORT" and not df["SHORT_SIGNAL"].iloc[idx]:
                trades.append((entry_price - next_open) / entry_price * 100.0)
                position = None

        if not trades:
            return 0.0, 0.0, 0

        total_pnl = sum(trades)
        win_rate = sum(1 for pnl in trades if pnl > 0) / len(trades) * 100.0
        return total_pnl, win_rate, len(trades)
    except Exception:
        return 0.0, 0.0, 0


def check_new_signal_status(ticker, signal_type, threshold_pct=1.0):
    """Detect whether today's signal is the first day of a new entry signal."""
    try:
        df = _load_ohlcv(ticker, period="1mo")
        if df.empty or len(df) < 22:
            return False

        df["MA5"] = df["Close"].rolling(window=5).mean()
        df["MA10"] = df["Close"].rolling(window=10).mean()
        df["MA20"] = df["Close"].rolling(window=20).mean()
        df["slope_pct5"] = df["MA5"].pct_change() * 100
        df["slope_pct10"] = df["MA10"].pct_change() * 100
        df["slope_pct20"] = df["MA20"].pct_change() * 100
        df["all_slopes_positive"] = (
            (df["slope_pct5"] > threshold_pct)
            & (df["slope_pct10"] > threshold_pct)
            & (df["slope_pct20"] > threshold_pct)
        )
        df["all_slopes_negative"] = (
            (df["slope_pct5"] < -threshold_pct)
            & (df["slope_pct10"] < -threshold_pct)
            & (df["slope_pct20"] < -threshold_pct)
        )
        df["LONG"] = (
            (df["MA5"] > df["MA10"])
            & (df["MA10"] > df["MA20"])
            & df["all_slopes_positive"]
        )
        df["SHORT"] = (
            (df["MA5"] < df["MA10"])
            & (df["MA10"] < df["MA20"])
            & df["all_slopes_negative"]
        )

        if signal_type == "LONG":
            return bool(df["LONG"].iloc[-1] and not df["LONG"].iloc[-2])
        return bool(df["SHORT"].iloc[-1] and not df["SHORT"].iloc[-2])
    except Exception:
        return False


def _format_ranked_rows(items, weights, long_side=True):
    if not items:
        return "None\n"

    rows = []
    for idx, (ticker, strength) in enumerate(items, start=1):
        sign = "+" if long_side else ""
        rows.append(
            f"{idx}. {ticker:<6} | strength {sign}{strength:.2f}% | "
            f"target weight {weights.get(ticker, 0):.1f}%"
        )
    return "\n".join(rows) + "\n"


def scan_ma_cross(universe_list):
    long_list, short_list, exit_list = [], [], []
    new_longs, new_shorts = [], []
    failed_tickers = []

    for ticker in universe_list:
        try:
            signal, strength = analyze_trend_strict_signals(ticker, threshold_pct=1.0)

            if signal in ["INSUFFICIENT_DATA", "ERROR", "NO_SIGNAL"]:
                failed_tickers.append(ticker)
                continue

            if signal == "LONG_ENTRY":
                long_list.append((ticker, strength))
                if check_new_signal_status(ticker, "LONG", threshold_pct=1.0):
                    new_longs.append(ticker)
            elif signal == "SHORT_ENTRY":
                short_list.append((ticker, strength))
                if check_new_signal_status(ticker, "SHORT", threshold_pct=1.0):
                    new_shorts.append(ticker)
            elif signal == "EXIT_SIGNAL":
                exit_list.append((ticker, strength))
        except Exception:
            failed_tickers.append(ticker)

    long_list.sort(key=lambda item: item[1], reverse=True)
    short_list.sort(key=lambda item: item[1])
    exit_list.sort(key=lambda item: item[0])

    top_longs = long_list[:5]
    top_shorts = short_list[:5]
    portfolio_weights = calculate_portfolio_weights(top_longs, top_shorts)

    long_rows = _format_ranked_rows(top_longs, portfolio_weights, long_side=True)
    short_rows = _format_ranked_rows(top_shorts, portfolio_weights, long_side=False)

    long_watch = ", ".join(f"{t}(+{s:.2f}%)" for t, s in long_list[5:]) or "None"
    short_watch = ", ".join(f"{t}({s:.2f}%)" for t, s in short_list[5:]) or "None"
    exit_names = ", ".join(t for t, _ in exit_list) or "None"

    target_tickers = [(t, "LONG NEW") for t in new_longs] + [
        (t, "SHORT NEW") for t in new_shorts
    ]
    for ticker, _ in top_longs:
        if (ticker, "LONG NEW") not in target_tickers:
            target_tickers.append((ticker, "LONG"))
    for ticker, _ in top_shorts:
        if (ticker, "SHORT NEW") not in target_tickers:
            target_tickers.append((ticker, "SHORT"))

    backtest_report = ""
    if target_tickers:
        backtest_report += "\nBacktest Summary\n"
        backtest_report += "-----------------------------------------\n"
        for ticker, tag in target_tickers:
            pnl, win_rate, count = run_isolated_backtest(ticker, threshold_pct=1.0)
            side = "LONG" if "LONG" in tag else "SHORT"
            backtest_report += (
                f"{side} *{ticker} ({tag})*: total *{pnl:+.1f}%* | "
                f"win rate {win_rate:.1f}% ({count} trades)\n"
            )
        backtest_report += "-----------------------------------------\n"

    msg = (
        "**[MA Momentum Scan Report]**\n"
        "=============================\n\n"
        "**New Signals Today**\n"
        f"- New LONG: {', '.join(new_longs) if new_longs else 'None'}\n"
        f"- New SHORT: {', '.join(new_shorts) if new_shorts else 'None'}\n\n"
        "-----------------------------\n"
        "**LONG Candidates**\n"
        "Top 5 target allocation:\n"
        f"{long_rows}"
        f"Watch list: {long_watch}\n\n"
        "-----------------------------\n"
        "**SHORT Candidates**\n"
        "Top 5 target allocation:\n"
        f"{short_rows}"
        f"Watch list: {short_watch}\n\n"
        "-----------------------------\n"
        "**Exit / No-Position Watch**\n"
        f"{exit_names}\n"
        f"{backtest_report}"
        "============================="
    )

    if failed_tickers:
        failed_str = ", ".join(sorted(set(failed_tickers)))
        msg += (
            "\n\n----------------------------------------\n"
            "**Data Failed / Insufficient**\n"
            f"`{failed_str}`"
        )

    return msg
