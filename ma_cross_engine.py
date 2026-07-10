# File name: ma_cross_engine.py
# Generates moving-average momentum signals and backtest reports.
import numpy as np
import pandas as pd
import catalyst_factor
import market_data_store


MOMENTUM_STRATEGY_WEIGHT = 0.80
MOMENTUM_CATALYST_WEIGHT = 0.20
MOMENTUM_MA_WINDOWS = (5, 20, 60)
MOMENTUM_SLOPE_THRESHOLD = 0.0
MOMENTUM_HOLDING_DAYS = 20


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
    if period == "4mo":
        return 120
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


def _add_ma_signal_columns(df, windows=MOMENTUM_MA_WINDOWS, threshold_pct=MOMENTUM_SLOPE_THRESHOLD):
    fast, mid, slow = windows
    out = df.copy()
    out["MA_FAST"] = out["Close"].rolling(window=fast).mean()
    out["MA_MID"] = out["Close"].rolling(window=mid).mean()
    out["MA_SLOW"] = out["Close"].rolling(window=slow).mean()

    out["S_FAST"] = out["MA_FAST"].pct_change() * 100
    out["S_MID"] = out["MA_MID"].pct_change() * 100
    out["S_SLOW"] = out["MA_SLOW"].pct_change() * 100

    out["LONG_SIGNAL"] = (
        (out["MA_FAST"] > out["MA_MID"])
        & (out["MA_MID"] > out["MA_SLOW"])
        & (out["S_FAST"] > threshold_pct)
        & (out["S_MID"] > threshold_pct)
        & (out["S_SLOW"] > threshold_pct)
    )
    out["SHORT_SIGNAL"] = (
        (out["MA_FAST"] < out["MA_MID"])
        & (out["MA_MID"] < out["MA_SLOW"])
        & (out["S_FAST"] < -threshold_pct)
        & (out["S_MID"] < -threshold_pct)
        & (out["S_SLOW"] < -threshold_pct)
    )
    out["SIGNAL_STRENGTH"] = (out["S_FAST"] + out["S_MID"] + out["S_SLOW"]) / 3.0
    return out


def _position_state(df, side_filter=None):
    position = None
    entry_idx = None
    for idx in range(len(df) - 1):
        if position is None:
            if df["LONG_SIGNAL"].iloc[idx] and side_filter in (None, "LONG"):
                position = "LONG"
                entry_idx = idx + 1
            elif df["SHORT_SIGNAL"].iloc[idx] and side_filter in (None, "SHORT"):
                position = "SHORT"
                entry_idx = idx + 1
        elif idx - entry_idx >= MOMENTUM_HOLDING_DAYS:
            position = None
            entry_idx = None
    return position, entry_idx


def analyze_trend_strict_signals(
    ticker,
    threshold_pct=MOMENTUM_SLOPE_THRESHOLD,
    period="4mo",
    windows=MOMENTUM_MA_WINDOWS,
):
    """Calculate MA alignment and slope signal for one ticker."""
    try:
        df = _load_ohlcv(ticker, period=period)

        if df.empty or len(df) < max(windows) + 2:
            return "INSUFFICIENT_DATA", 0.0

        df = _add_ma_signal_columns(df, windows=windows, threshold_pct=threshold_pct)
        latest = df.iloc[-1]
        required = ["MA_FAST", "MA_MID", "MA_SLOW", "S_FAST", "S_MID", "S_SLOW"]
        if pd.isna([latest[name] for name in required]).any():
            return "NO_SIGNAL", 0.0

        signal_strength = float(latest["SIGNAL_STRENGTH"])

        if bool(latest["LONG_SIGNAL"]):
            return "LONG_ENTRY", signal_strength
        if bool(latest["SHORT_SIGNAL"]):
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


def run_isolated_backtest(
    ticker,
    threshold_pct=MOMENTUM_SLOPE_THRESHOLD,
    side_filter=None,
    period="1y",
    windows=MOMENTUM_MA_WINDOWS,
    holding_days=MOMENTUM_HOLDING_DAYS,
):
    """Run a simple isolated MA momentum backtest."""
    try:
        df = _load_ohlcv(ticker, period=period)
        if df.empty or len(df) < max(windows) + holding_days + 2:
            return 0.0, 0.0, 0, "N/A"

        start_date = df.index[0].date().isoformat()
        end_date = df.index[-1].date().isoformat()
        period_label = f"{start_date} to {end_date}"

        df = _add_ma_signal_columns(df, windows=windows, threshold_pct=threshold_pct)

        trades = []

        next_available_idx = 0
        for idx in range(max(windows), len(df) - holding_days):
            if idx + 1 < next_available_idx:
                continue
            entry_idx = idx + 1
            exit_idx = idx + holding_days
            entry_price = float(df["Open"].iloc[entry_idx])
            exit_price = float(df["Close"].iloc[exit_idx])
            if entry_price <= 0:
                continue
            if df["LONG_SIGNAL"].iloc[idx] and side_filter in (None, "LONG"):
                trades.append((exit_price / entry_price - 1.0) * 100.0)
                next_available_idx = exit_idx + 1
            elif df["SHORT_SIGNAL"].iloc[idx] and side_filter in (None, "SHORT"):
                trades.append((entry_price - exit_price) / entry_price * 100.0)
                next_available_idx = exit_idx + 1

        if not trades:
            return 0.0, 0.0, 0, period_label

        avg_return = sum(trades) / len(trades)
        win_rate = sum(1 for pnl in trades if pnl > 0) / len(trades) * 100.0
        return avg_return, win_rate, len(trades), period_label
    except Exception:
        return 0.0, 0.0, 0, "N/A"


def check_new_signal_status(
    ticker,
    signal_type,
    threshold_pct=MOMENTUM_SLOPE_THRESHOLD,
    windows=MOMENTUM_MA_WINDOWS,
):
    """Detect whether today's signal is the first day of a new entry signal."""
    try:
        df = _load_ohlcv(ticker, period="4mo")
        if df.empty or len(df) < max(windows) + 2:
            return False

        df = _add_ma_signal_columns(df, windows=windows, threshold_pct=threshold_pct)

        if signal_type == "LONG":
            return bool(df["LONG_SIGNAL"].iloc[-1] and not df["LONG_SIGNAL"].iloc[-2])
        return bool(df["SHORT_SIGNAL"].iloc[-1] and not df["SHORT_SIGNAL"].iloc[-2])
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
            signal, strength = analyze_trend_strict_signals(ticker)

            if signal in ["INSUFFICIENT_DATA", "ERROR", "NO_SIGNAL"]:
                failed_tickers.append(ticker)
                continue

            if signal == "LONG_ENTRY":
                long_list.append((ticker, strength))
                if check_new_signal_status(ticker, "LONG"):
                    new_longs.append(ticker)
            elif signal == "SHORT_ENTRY":
                short_list.append((ticker, strength))
                if check_new_signal_status(ticker, "SHORT"):
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
                side_filter=side,
            )
            total_return_sum += avg_return * count
            total_wins += (win_rate / 100.0) * count
            total_trades += count
            period_text = f" | period {period_label}" if period_label != "N/A" else ""
            if count:
                backtest_report += (
                    f"{side} *{ticker} ({tag})*: "
                    f"avg *{avg_return:+.1f}%* | win {win_rate:.1f}% | "
                    f"{count} trades{period_text}\n"
                )
            else:
                backtest_report += (
                    f"{side} *{ticker} ({tag})*: "
                    f"no closed trades{period_text}\n"
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
        "Signal rule = MA 5/20/60 alignment with same-direction slopes; "
        "default test hold = 20 trading days.\n"
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
