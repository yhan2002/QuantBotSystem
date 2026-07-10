# File name: ma_cross_engine.py
# Generates moving-average momentum signals and backtest reports.
import numpy as np
import pandas as pd
import catalyst_factor
import market_data_store


MOMENTUM_STRATEGY_WEIGHT = 0.80
MOMENTUM_CATALYST_WEIGHT = 0.20


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

    def get_ticker(item):
        if isinstance(item, dict):
            return item["ticker"]
        return item[0]

    if top_longs:
        long_ranks = np.arange(len(top_longs), 0, -1)
        total_rank = np.sum(long_ranks)
        for idx, item in enumerate(top_longs):
            ticker = get_ticker(item)
            weights[ticker] = (long_ranks[idx] / total_rank) * 50.0

    if top_shorts:
        short_ranks = np.arange(len(top_shorts), 0, -1)
        total_rank = np.sum(short_ranks)
        for idx, item in enumerate(top_shorts):
            ticker = get_ticker(item)
            weights[ticker] = (short_ranks[idx] / total_rank) * 50.0

    return weights


def run_isolated_backtest(ticker, threshold_pct=1.0, side_filter=None):
    """Run a simple one-year isolated MA momentum backtest."""
    try:
        df = _load_ohlcv(ticker, period="1y")
        if df.empty or len(df) < 22:
            return 0.0, 0.0, 0, "N/A"

        start_date = df.index[0].date().isoformat()
        end_date = df.index[-1].date().isoformat()
        period_label = f"{start_date} to {end_date}"

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
                if df["LONG_SIGNAL"].iloc[idx] and side_filter in (None, "LONG"):
                    position = "LONG"
                    entry_price = next_open
                elif df["SHORT_SIGNAL"].iloc[idx] and side_filter in (None, "SHORT"):
                    position = "SHORT"
                    entry_price = next_open
            elif position == "LONG" and not df["LONG_SIGNAL"].iloc[idx]:
                trades.append((next_open - entry_price) / entry_price * 100.0)
                position = None
            elif position == "SHORT" and not df["SHORT_SIGNAL"].iloc[idx]:
                trades.append((entry_price - next_open) / entry_price * 100.0)
                position = None

        if not trades:
            return 0.0, 0.0, 0, period_label

        avg_return = sum(trades) / len(trades)
        win_rate = sum(1 for pnl in trades if pnl > 0) / len(trades) * 100.0
        return avg_return, win_rate, len(trades), period_label
    except Exception:
        return 0.0, 0.0, 0, "N/A"


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


def _enrich_with_catalyst(items, catalyst_map, side):
    if not items:
        return []
    max_strength = max(abs(item[1]) for item in items) or 1.0
    enriched = []
    for ticker, strength in items:
        strategy_norm = abs(strength) / max_strength * 100.0
        cat_score = catalyst_factor.confirmed_score(catalyst_map, ticker)
        if side == "SHORT":
            combined = (
                strategy_norm * MOMENTUM_STRATEGY_WEIGHT
                - cat_score * MOMENTUM_CATALYST_WEIGHT
            )
        else:
            combined = (
                strategy_norm * MOMENTUM_STRATEGY_WEIGHT
                + cat_score * MOMENTUM_CATALYST_WEIGHT
            )
        enriched.append(
            {
                "ticker": ticker,
                "strength": strength,
                "strategy_norm": strategy_norm,
                "catalyst_score": cat_score,
                "combined_score": combined,
            }
        )
    enriched.sort(key=lambda row: row["combined_score"], reverse=True)
    return enriched


def _format_ranked_rows(items, long_side=True):
    if not items:
        return "None\n"

    rows = []
    for idx, row in enumerate(items, start=1):
        ticker = row["ticker"]
        strength = row["strength"]
        sign = "+" if long_side else ""
        catalyst_text = (
            f" | catalyst {row['catalyst_score']:.1f}"
            if row["catalyst_score"] > 0
            else ""
        )
        rows.append(
            f"{idx}. {ticker:<6} | strength {sign}{strength:.2f}% | "
            f"combined {row['combined_score']:.1f}{catalyst_text}"
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

    catalyst_map = catalyst_factor.latest_catalyst_map(universe_list)
    long_list.sort(key=lambda item: item[1], reverse=True)
    short_list.sort(key=lambda item: item[1])
    exit_list.sort(key=lambda item: item[0])

    enriched_longs = _enrich_with_catalyst(long_list, catalyst_map, side="LONG")
    enriched_shorts = _enrich_with_catalyst(short_list, catalyst_map, side="SHORT")

    top_longs = enriched_longs[:5]
    top_shorts = enriched_shorts[:5]

    long_rows = _format_ranked_rows(top_longs, long_side=True)
    short_rows = _format_ranked_rows(top_shorts, long_side=False)

    long_watch = (
        ", ".join(
            f"{row['ticker']}(combo {row['combined_score']:.1f})"
            for row in enriched_longs[5:]
        )
        or "None"
    )
    short_watch = (
        ", ".join(
            f"{row['ticker']}(combo {row['combined_score']:.1f})"
            for row in enriched_shorts[5:]
        )
        or "None"
    )
    exit_names = ", ".join(t for t, _ in exit_list) or "None"

    target_tickers = [(t, "LONG NEW") for t in new_longs] + [
        (t, "SHORT NEW") for t in new_shorts
    ]
    for row in top_longs:
        ticker = row["ticker"]
        if (ticker, "LONG NEW") not in target_tickers:
            target_tickers.append((ticker, "LONG"))
    for row in top_shorts:
        ticker = row["ticker"]
        if (ticker, "SHORT NEW") not in target_tickers:
            target_tickers.append((ticker, "SHORT"))

    backtest_report = ""
    if target_tickers:
        backtest_report += "\nBacktest Summary\n"
        backtest_report += "-----------------------------------------\n"
        backtest_report += (
            "Rule: no-lookahead side-matched MA signals, signal close -> next open, "
            "average trade return\n"
            "Guard: catalyst affects today's ranking only, not historical entries.\n"
        )
        total_return_sum = 0.0
        total_wins = 0.0
        total_trades = 0
        for ticker, tag in target_tickers:
            side = "LONG" if "LONG" in tag else "SHORT"
            avg_return, win_rate, count, period_label = run_isolated_backtest(
                ticker,
                threshold_pct=1.0,
                side_filter=side,
            )
            total_return_sum += avg_return * count
            total_wins += (win_rate / 100.0) * count
            total_trades += count
            period_text = f" | period {period_label}" if period_label != "N/A" else ""
            backtest_report += (
                f"{side} *{ticker} ({tag})*: "
                f"avg *{avg_return:+.1f}%* | win {win_rate:.1f}% | "
                f"{count} trades{period_text}\n"
            )
        total_win_rate = total_wins / total_trades * 100.0 if total_trades else 0.0
        portfolio_avg = total_return_sum / total_trades if total_trades else 0.0
        backtest_report += "\nSelected Candidate History Summary\n"
        backtest_report += (
            f"*Avg trade return {portfolio_avg:+.1f}%* | "
            f"win {total_win_rate:.1f}% | {total_trades} trades\n"
        )
        backtest_report += "-----------------------------------------\n"

    msg = (
        "**[MA Momentum Scan Report]**\n"
        "=============================\n\n"
        "**Scoring**\n"
        "Final rank = strategy score 80% + catalyst factor 20%.\n"
        "For SHORT candidates, positive catalyst reduces rank.\n\n"
        "**New Signals Today**\n"
        f"- New LONG: {', '.join(new_longs) if new_longs else 'None'}\n"
        f"- New SHORT: {', '.join(new_shorts) if new_shorts else 'None'}\n\n"
        "-----------------------------\n"
        "**LONG Candidates**\n"
        "Top 5 by combined score:\n"
        f"{long_rows}"
        f"Watch list: {long_watch}\n\n"
        "-----------------------------\n"
        "**SHORT Candidates**\n"
        "Top 5 by combined score:\n"
        f"{short_rows}"
        f"Watch list: {short_watch}\n\n"
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
