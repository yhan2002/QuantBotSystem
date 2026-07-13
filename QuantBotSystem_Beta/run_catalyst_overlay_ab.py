"""A/B test a point-in-time catalyst rank overlay against QuantBot momentum."""
from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict

import numpy as np
import pandas as pd

import config
from catalyst_pit_overlay import PointInTimeCatalystOverlay
from scalping_catalyst_engine import load_scalping_events
from tune_momentum_no_lookahead import build_signal_frame, load_price_map, selected_universe


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_WORKBOOK = getattr(config, "SCALPING_WORKBOOK_PATH", "")
DEFAULT_WINDOWS = (5, 20, 60)
DEFAULT_HOLDING_DAYS = 20


def _country_from_ticker(ticker):
    if ticker.endswith((".KS", ".KQ")):
        return "KR"
    if ticker.endswith(".T"):
        return "JP"
    if ticker.endswith((".HK", ".SS", ".SZ")):
        return "CH"
    return "US"


def _load_sector_map(path):
    if not path:
        return {}
    df = pd.read_csv(path)
    required = {"ticker", "sector"}
    if not required.issubset(df.columns):
        raise ValueError("sector map must contain ticker,sector columns")
    return {
        str(row.ticker).upper(): {"sector": row.sector, "country": getattr(row, "country", None)}
        for row in df.itertuples(index=False)
    }


def _candidate_rows(price_map, overlay, holding_days, windows, threshold, sector_map):
    rows = defaultdict(list)
    for ticker, prices in price_map.items():
        signals = build_signal_frame(prices, windows, threshold)
        start = max(windows)
        for idx in range(start, len(signals) - holding_days):
            side = "LONG" if bool(signals["LONG_SIGNAL"].iloc[idx]) else "SHORT" if bool(signals["SHORT_SIGNAL"].iloc[idx]) else None
            if side is None:
                continue
            signal_date = pd.Timestamp(signals.index[idx]).date()
            factor = overlay.factor_on(ticker, signal_date)
            entry_idx, exit_idx = idx + 1, idx + holding_days
            metadata = sector_map.get(ticker, {})
            rows[signal_date].append({
                "ticker": ticker,
                "side": side,
                "base_score": float(signals["LONG_SCORE"].iloc[idx] if side == "LONG" else signals["SHORT_SCORE"].iloc[idx]),
                "signal_date": signal_date,
                "entry_idx": entry_idx,
                "exit_idx": exit_idx,
                "entry_date": pd.Timestamp(signals.index[entry_idx]).date(),
                "exit_date": pd.Timestamp(signals.index[exit_idx]).date(),
                "prices": prices,
                "tier": factor["catalyst_long_tier"],
                "country": metadata.get("country") or factor.get("country") or _country_from_ticker(ticker),
                "sector": metadata.get("sector") or "Unknown",
                "diagnostics": factor,
            })
    return rows


def _rank(candidates, side, long_tilt, short_penalties):
    ordered = sorted(candidates, key=lambda row: (-row["base_score"], row["ticker"]))
    n = max(1, len(ordered) - 1)
    ranked = []
    for idx, row in enumerate(ordered):
        base_percentile = 1.0 - idx / n if len(ordered) > 1 else 1.0
        tier = row["tier"]
        adjusted = base_percentile
        if side == "LONG" and long_tilt:
            adjusted = (1.0 - long_tilt) * base_percentile + long_tilt * (tier / 2.0)
        elif side == "SHORT":
            adjusted = base_percentile - short_penalties.get(tier, 0.0)
        ranked.append((adjusted, base_percentile, row))
    return [row for _, _, row in sorted(ranked, key=lambda item: (-item[0], -item[1], item[2]["ticker"]))]


def _daily_trade_returns(trade, weight, cost_rate):
    prices = trade["prices"]
    side = 1.0 if trade["side"] == "LONG" else -1.0
    result = []
    for idx in range(trade["entry_idx"], trade["exit_idx"] + 1):
        close = float(prices["Close"].iloc[idx])
        if idx == trade["entry_idx"]:
            base = float(prices["Open"].iloc[idx])
        else:
            base = float(prices["Close"].iloc[idx - 1])
        ret = side * (close / base - 1.0) * weight
        if idx == trade["entry_idx"] or idx == trade["exit_idx"]:
            ret -= cost_rate * weight
        result.append((pd.Timestamp(prices.index[idx]).date(), ret))
    return result


def _metrics(daily_returns, trades, max_positions, label):
    if not daily_returns:
        return {"label": label, "total_return": 0.0, "cagr": 0.0, "sharpe": 0.0, "mdd": 0.0, "volatility": 0.0, "turnover": 0.0, "avg_holdings": 0.0, "max_holdings": 0, "trades": 0}
    values = np.array(list(daily_returns.values()), dtype=float)
    equity = np.cumprod(1.0 + values)
    annual = 252 / len(values)
    total = float(equity[-1] - 1.0)
    cagr = float(equity[-1] ** annual - 1.0) if equity[-1] > 0 else -1.0
    vol = float(np.std(values, ddof=1) * math.sqrt(252)) if len(values) > 1 else 0.0
    sharpe = float(np.mean(values) / np.std(values, ddof=1) * math.sqrt(252)) if len(values) > 1 and np.std(values, ddof=1) else 0.0
    drawdown = equity / np.maximum.accumulate(equity) - 1.0
    counts = defaultdict(int)
    for trade in trades:
        for date_value, _ in _daily_trade_returns(trade, 1.0, 0.0):
            counts[date_value] += 1
    gross_turnover = len(trades) / max(1, len(values))
    return {"label": label, "total_return": total, "cagr": cagr, "sharpe": sharpe, "mdd": float(drawdown.min()), "volatility": vol, "turnover": gross_turnover, "avg_holdings": float(np.mean(list(counts.values()))) if counts else 0.0, "max_holdings": max(counts.values()) if counts else 0, "trades": len(trades)}


def _group_summary(trades, group_key, max_positions, calendar_days):
    grouped = defaultdict(lambda: {"trades": 0, "avg_gross_exposure": 0.0, "return_contribution": 0.0})
    for trade in trades:
        group = trade[group_key]
        gross = (float(trade["prices"]["Close"].iloc[trade["exit_idx"]]) / float(trade["prices"]["Open"].iloc[trade["entry_idx"]]) - 1.0)
        if trade["side"] == "SHORT":
            gross = -gross
        grouped[group]["trades"] += 1
        weight = 0.5 / max_positions
        holding_days = trade["exit_idx"] - trade["entry_idx"] + 1
        grouped[group]["avg_gross_exposure"] += weight * holding_days / max(1, calendar_days)
        grouped[group]["return_contribution"] += gross * weight
    return dict(grouped)


def simulate(candidates_by_date, long_tilt, short_penalties, max_positions, cost_bps):
    booked, trades = {}, []
    cost_rate = cost_bps / 10_000.0
    for signal_date in sorted(candidates_by_date):
        # A trade that exits on the signal close has freed capacity for t+1 open.
        booked = {ticker: row for ticker, row in booked.items() if row["exit_date"] > signal_date}
        day = candidates_by_date[signal_date]
        for side in ("LONG", "SHORT"):
            available = max_positions - sum(1 for row in booked.values() if row["side"] == side)
            if available <= 0:
                continue
            ranked = _rank([row for row in day if row["side"] == side and row["ticker"] not in booked], side, long_tilt if side == "LONG" else 0.0, short_penalties)
            for row in ranked[:available]:
                booked[row["ticker"]] = row
                trades.append(row)

    total_daily, long_daily, short_daily = defaultdict(float), defaultdict(float), defaultdict(float)
    weight = 0.5 / max_positions
    for trade in trades:
        target = long_daily if trade["side"] == "LONG" else short_daily
        for date_value, value in _daily_trade_returns(trade, weight, cost_rate):
            target[date_value] += value
            total_daily[date_value] += value
    tiers = {}
    for tier in (0, 1, 2):
        selected = [row for row in trades if row["tier"] == tier]
        returns = []
        for row in selected:
            gross = float(row["prices"]["Close"].iloc[row["exit_idx"]]) / float(row["prices"]["Open"].iloc[row["entry_idx"]]) - 1.0
            returns.append(gross if row["side"] == "LONG" else -gross)
        tiers[str(tier)] = {"count": len(selected), "avg_trade_return": float(np.mean(returns)) if returns else 0.0}
    calendar_days = len(set(total_daily) | set(long_daily) | set(short_daily))
    return {
        "overall": _metrics(total_daily, trades, max_positions, "overall"),
        "long_leg": _metrics(long_daily, [row for row in trades if row["side"] == "LONG"], max_positions, "long"),
        "short_leg": _metrics(short_daily, [row for row in trades if row["side"] == "SHORT"], max_positions, "short"),
        "country": _group_summary(trades, "country", max_positions, calendar_days),
        "sector": _group_summary(trades, "sector", max_positions, calendar_days),
        "tiers": tiers,
    }


def _segments(dates, train_end=None, validation_end=None):
    dates = sorted(dates)
    if train_end and validation_end:
        train_end, validation_end = pd.Timestamp(train_end).date(), pd.Timestamp(validation_end).date()
    else:
        train_end = dates[int(len(dates) * 0.60)]
        validation_end = dates[int(len(dates) * 0.80)]
    return {"train": lambda d: d <= train_end, "validation": lambda d: train_end < d <= validation_end, "test": lambda d: d > validation_end}


def main():
    parser = argparse.ArgumentParser(description="Point-in-time catalyst overlay A/B backtest")
    parser.add_argument("workbook", nargs="?", default=DEFAULT_WORKBOOK, help="scalping research workbook")
    parser.add_argument("--top-n", type=int, default=5, help="existing per-side capacity")
    parser.add_argument("--cost-bps", type=float, default=10.0, help="one-way transaction cost")
    parser.add_argument("--tilts", default="0.00,0.05,0.10,0.15")
    parser.add_argument("--short-penalty-tier1", type=float, default=0.03)
    parser.add_argument("--short-penalty-tier2", type=float, default=0.08)
    parser.add_argument("--train-end")
    parser.add_argument("--validation-end")
    parser.add_argument("--sector-map", help="optional CSV: ticker,sector[,country]")
    args = parser.parse_args()
    if not args.workbook or not os.path.exists(args.workbook):
        raise SystemExit("Workbook not found. Pass its local path as the first argument; it is never written to Git.")

    price_map = load_price_map(selected_universe())
    records = load_scalping_events(args.workbook)
    overlay = PointInTimeCatalystOverlay(records, price_map)
    candidates = _candidate_rows(price_map, overlay, DEFAULT_HOLDING_DAYS, DEFAULT_WINDOWS, 0.0, _load_sector_map(args.sector_map))
    if not candidates:
        raise SystemExit("No valid candidates. Verify cached price history and workbook ticker mapping.")
    penalties = {0: 0.0, 1: args.short_penalty_tier1, 2: args.short_penalty_tier2}
    models = {"A_baseline": (0.0, {0: 0.0, 1: 0.0, 2: 0.0})}
    for tilt in (float(value) for value in args.tilts.split(",")):
        if tilt <= 0:
            continue
        models[f"B_long_overlay_{tilt:.2f}"] = (tilt, {0: 0.0, 1: 0.0, 2: 0.0})
        models[f"C_short_penalty_{tilt:.2f}"] = (0.0, penalties)
        models[f"D_combined_{tilt:.2f}"] = (tilt, penalties)
    output = {"assumptions": {"signal": "t close", "entry": "t+1 open", "holding_days": 20, "author_outcome_rule": "exit_date < current_signal_date", "cost_bps_one_way": args.cost_bps}, "segments": {}}
    for segment, selector in _segments(candidates, args.train_end, args.validation_end).items():
        scoped = {date_value: rows for date_value, rows in candidates.items() if selector(date_value)}
        results = {name: simulate(scoped, tilt, penalties_for_model, args.top_n, args.cost_bps) for name, (tilt, penalties_for_model) in models.items()}
        baseline = results["A_baseline"]["overall"]
        for name, result in results.items():
            result["incremental_vs_baseline"] = {key: result["overall"][key] - baseline[key] for key in ("total_return", "cagr", "sharpe", "mdd", "volatility", "turnover")}
        output["segments"][segment] = results
    print(json.dumps(output, ensure_ascii=False, indent=2, default=float))


if __name__ == "__main__":
    main()
