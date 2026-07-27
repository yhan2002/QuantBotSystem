"""Cash/quantity portfolio simulation for score-weighted strategy trades."""
from __future__ import annotations

from collections.abc import Mapping
from collections import defaultdict
from copy import deepcopy
import math

import pandas as pd

import core_market_data_store
from core_portfolio_allocator import (
    PortfolioPolicy,
    cohort_capacity_scale,
    exposure_snapshot,
)


_ENTRY_POLICY_SIGNAL_FIELDS = (
    "trade_id",
    "strategy",
    "ticker",
    "side",
    "signal_date",
    "entry_date",
    "signal_score",
    "neutralized_score",
    "selected_score",
    "winsorized_score",
    "percentile_rank",
    "rank_score",
    "requested_weight",
    "setup",
    "structure",
    "policy_observation_id",
    "policy_feature_eligible",
    "policy_feature_skip_reason",
    "policy_features",
)


def _normalize_frame(frame):
    if frame is None or frame.empty:
        return None
    result = frame.copy()
    result.index = pd.to_datetime(result.index).normalize()
    result = result[~result.index.duplicated(keep="last")].sort_index()
    columns = {str(column).lower(): column for column in result.columns}
    if "close" not in columns:
        return None
    normalized = pd.DataFrame(index=result.index)
    normalized["Close"] = pd.to_numeric(result[columns["close"]], errors="coerce")
    if "open" in columns:
        normalized["Open"] = pd.to_numeric(result[columns["open"]], errors="coerce")
    else:
        normalized["Open"] = float("nan")
    return normalized.dropna(subset=["Close"])


def _timestamp(value):
    return pd.Timestamp(value).normalize()


def _trade_events(trade):
    events = [dict(event) for event in (trade.get("exit_events") or [])]
    if not events and trade.get("exit_date") is not None:
        events = [
            {
                "date": trade["exit_date"],
                "weight": 1.0,
                "price": trade.get("exit_price"),
                "reason": trade.get("exit_reason", "final exit"),
            }
        ]
    for event in events:
        event["date"] = _timestamp(event["date"])
        event["weight"] = float(event.get("weight", 1.0))
    return sorted(events, key=lambda event: event["date"])


def _leg_specs(trade):
    """Normalize a single-name or multi-leg trade into signed ratios."""
    supplied = trade.get("legs") or []
    if supplied:
        legs = []
        for raw in supplied:
            leg = dict(raw)
            ticker = str(leg.get("ticker") or "").strip().upper()
            if not ticker:
                raise ValueError("Trade leg is missing ticker")
            if leg.get("signed_ratio") is not None:
                signed_ratio = float(leg["signed_ratio"])
            else:
                direction = leg.get("direction", leg.get("side"))
                if isinstance(direction, str):
                    direction = 1.0 if direction.upper() == "LONG" else -1.0
                signed_ratio = float(direction) * float(
                    leg.get("ratio", leg.get("notional_ratio", 1.0))
                )
            if not math.isfinite(signed_ratio) or abs(signed_ratio) <= 1e-12:
                raise ValueError(f"Invalid leg ratio for {ticker}")
            leg["ticker"] = ticker
            leg["signed_ratio"] = signed_ratio
            legs.append(leg)
        return legs

    side = str(trade.get("side") or "").upper()
    if side not in ("LONG", "SHORT"):
        raise ValueError("Trade is missing LONG/SHORT side")
    return [
        {
            "ticker": str(trade["ticker"]).upper(),
            "signed_ratio": 1.0 if side == "LONG" else -1.0,
            "role": "ALPHA",
            "entry_price": trade.get("entry_price"),
        }
    ]


def _price_frames(trades, price_loader):
    tickers = set()
    for trade in trades:
        for leg in _leg_specs(trade):
            tickers.add(leg["ticker"])
    frames = {}
    for ticker in sorted(tickers):
        frame = _normalize_frame(price_loader(ticker))
        if frame is not None:
            frames[ticker] = frame
    return frames


def _calendar(frames, start_date, end_date, supplied=None):
    start = _timestamp(start_date)
    end = _timestamp(end_date)
    if supplied is not None:
        dates = pd.DatetimeIndex(pd.to_datetime(supplied)).normalize()
        dates = dates[(dates >= start) & (dates <= end)]
    else:
        dates = pd.DatetimeIndex(
            sorted(
                {
                    date
                    for frame in frames.values()
                    for date in frame.index
                    if start <= date <= end
                }
            )
        )
    if dates.empty:
        raise ValueError("No trading dates are available for the portfolio period")
    return dates


def _build_marks(frames, calendar):
    closes = pd.DataFrame(
        {ticker: frame["Close"] for ticker, frame in frames.items()}
    ).reindex(calendar)
    observed_opens = pd.DataFrame(
        {ticker: frame["Open"] for ticker, frame in frames.items()}
    ).reindex(calendar)
    close_marks = closes.ffill()
    prior_close = closes.ffill().shift(1)
    open_marks = observed_opens.where(observed_opens.notna(), prior_close)
    return observed_opens, open_marks, close_marks


def _mark_map(lots, frame, date):
    result = {}
    for lot in lots:
        ticker = lot["ticker"]
        if ticker in result:
            continue
        if ticker not in frame or pd.isna(frame.at[date, ticker]):
            raise ValueError(f"Missing mark for active position: {ticker} {date.date()}")
        result[ticker] = float(frame.at[date, ticker])
    return result


def _entry_price(trade, leg, observed_opens, date):
    explicit = leg.get("entry_price")
    if explicit is None and len(trade.get("legs") or []) == 0:
        explicit = trade.get("entry_price")
    if explicit is not None and math.isfinite(float(explicit)) and float(explicit) > 0:
        return float(explicit)
    ticker = leg["ticker"]
    if ticker not in observed_opens or pd.isna(observed_opens.at[date, ticker]):
        raise ValueError(f"Missing next-open entry price: {ticker} {date.date()}")
    return float(observed_opens.at[date, ticker])


def _exit_price(event, lot, observed_opens, close_marks, date):
    leg_prices = event.get("leg_prices") or event.get("prices") or {}
    if lot["ticker"] in leg_prices:
        return float(leg_prices[lot["ticker"]])
    if event.get("price") is not None and lot.get("single_leg"):
        return float(event["price"])
    execution = str(event.get("execution") or "").upper()
    frame = observed_opens if execution == "NEXT_OPEN" else close_marks
    if lot["ticker"] not in frame or pd.isna(frame.at[date, lot["ticker"]]):
        raise ValueError(
            f"Missing exit price for {lot['ticker']} on {date.date()}"
        )
    return float(frame.at[date, lot["ticker"]])


def _annotate_scores(trades, max_name_weight):
    normalized = []
    for trade in trades:
        item = dict(trade)
        if (
            item.get("neutralized_score") is None
            and item.get("signal_score") is None
        ):
            item["signal_score"] = (
                1.0 if str(item.get("side")).upper() == "LONG" else -1.0
            )
        normalized.append(item)
    try:
        from core_signal_scoring import annotate_trades_by_entry_date

        return annotate_trades_by_entry_date(
            normalized,
            max_name_weight=max_name_weight,
        )
    except ImportError:
        result = []
        for trade in normalized:
            item = dict(trade)
            score = float(
                item.get("neutralized_score", item.get("signal_score", 0.0))
            )
            if not math.isfinite(score) or score == 0:
                continue
            side_sign = 1.0 if item.get("side") == "LONG" else -1.0
            if score * side_sign <= 0:
                continue
            item["rank_score"] = side_sign
            item["requested_weight"] = max_name_weight
            result.append(item)
        return result


def _portfolio_nav(cash, lots, price_map):
    return float(cash) + sum(
        float(lot["quantity"]) * float(price_map[lot["ticker"]])
        for lot in lots
    )


def _entry_policy_signal(trade):
    """Expose only signal-time fields to an entry sizing policy."""

    return {
        field: deepcopy(trade[field])
        for field in _ENTRY_POLICY_SIGNAL_FIELDS
        if field in trade
    }


def _entry_policy_scale(value, label):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a finite number from 0 to 1") from exc
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError(f"{label} must be a finite number from 0 to 1")
    return result


def _entry_policy_multiplier(value, label):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            f"{label} must be a finite non-negative number"
        ) from exc
    if not math.isfinite(result) or result < 0.0:
        raise ValueError(f"{label} must be a finite non-negative number")
    return result


def _entry_policy_decision(cohort, entry_sizing_policy, portfolio_state):
    trade_ids = [trade["trade_id"] for trade in cohort]
    if len(set(trade_ids)) != len(trade_ids):
        raise ValueError("Entry sizing policy requires unique cohort trade IDs")
    if entry_sizing_policy is None:
        return 1.0, {trade_id: 1.0 for trade_id in trade_ids}

    decision = entry_sizing_policy(
        signals=[_entry_policy_signal(trade) for trade in cohort],
        portfolio_state=deepcopy(portfolio_state),
    )
    if not isinstance(decision, Mapping):
        raise ValueError("Entry sizing policy must return a mapping")
    if "gross_scale" not in decision:
        raise ValueError("Entry sizing policy result is missing gross_scale")
    multipliers = decision.get("trade_multipliers")
    if not isinstance(multipliers, Mapping):
        raise ValueError(
            "Entry sizing policy result must contain trade_multipliers"
        )

    expected_ids = set(trade_ids)
    supplied_ids = set(multipliers)
    if supplied_ids != expected_ids:
        missing = sorted(str(value) for value in expected_ids - supplied_ids)
        extra = sorted(str(value) for value in supplied_ids - expected_ids)
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if extra:
            details.append("extra=" + ",".join(extra))
        raise ValueError(
            "Entry sizing policy trade_multipliers must match the exact "
            "cohort trade IDs"
            + (": " + "; ".join(details) if details else "")
        )

    gross_scale = _entry_policy_scale(
        decision["gross_scale"],
        "gross_scale",
    )
    validated_multipliers = {
        trade_id: _entry_policy_multiplier(
            multipliers[trade_id],
            f"trade multiplier for {trade_id}",
        )
        for trade_id in trade_ids
    }
    return gross_scale, validated_multipliers


def _apply_entry_sizing_policy(
    cohort,
    entry_sizing_policy,
    portfolio_state,
):
    if not cohort:
        return
    gross_scale, multipliers = _entry_policy_decision(
        cohort,
        entry_sizing_policy,
        portfolio_state,
    )
    for trade in cohort:
        base_weight = float(trade.get("base_requested_weight", 0.0))
        multiplier = multipliers[trade["trade_id"]]
        trade["policy_gross_scale"] = gross_scale
        trade["policy_trade_multiplier"] = multiplier
        trade["requested_weight"] = base_weight * gross_scale * multiplier


def _portfolio_state_snapshot(asof_date, nav, cash, exposure, active_positions):
    if nav > 0:
        long_ratio = float(exposure["long"]) / float(nav)
        short_ratio = float(exposure["short"]) / float(nav)
        gross_ratio = float(exposure["gross"]) / float(nav)
        net_ratio = (
            float(exposure["long"]) - float(exposure["short"])
        ) / float(nav)
    else:
        long_ratio = short_ratio = gross_ratio = net_ratio = None
    return {
        "asof_date": (
            None
            if asof_date is None
            else pd.Timestamp(asof_date).date().isoformat()
        ),
        "nav": float(nav),
        "cash": float(cash),
        "long_fraction": long_ratio,
        "short_fraction": short_ratio,
        "gross_fraction": gross_ratio,
        "net_fraction": net_ratio,
        "active_positions": int(active_positions),
    }


def simulate_portfolio(
    trades,
    start_date,
    end_date,
    price_loader=core_market_data_store.get_price_data,
    policy=None,
    calendar=None,
    entry_sizing_policy=None,
):
    """Replay completed trades through a non-rebalancing portfolio ledger.

    Entries are sized together at the open. Exit proceeds dated on the same
    day are processed only after new-open sizing, so an intraday exit cannot be
    anticipated and reused at that morning's open.

    ``entry_sizing_policy`` is called once per entry cohort with keyword-only
    ``signals`` (a signal-time allowlist) and ``portfolio_state`` (the previous
    ledger close). It must return one ``gross_scale`` and an exact
    ``trade_multipliers`` mapping. Gross scale remains constrained to
    ``[0, 1]``; non-negative trade multipliers may exceed one so a policy can
    redistribute cohort capital. Final name, side, and gross caps are still
    enforced by the normal allocator.
    """
    policy = policy or PortfolioPolicy()
    scored_trades = _annotate_scores(
        [dict(trade) for trade in trades],
        policy.max_name_weight,
    )
    for sequence, trade in enumerate(
        sorted(
            scored_trades,
            key=lambda item: (
                str(item.get("entry_date") or ""),
                str(item.get("ticker") or ""),
            ),
        ),
        start=1,
    ):
        trade.setdefault("trade_id", f"T{sequence:06d}")
        trade["entry_date"] = _timestamp(trade["entry_date"])
        trade["_events"] = _trade_events(trade)
        trade["base_requested_weight"] = float(
            trade.get("requested_weight", 0.0)
        )
        trade["policy_gross_scale"] = 1.0
        trade["policy_trade_multiplier"] = 1.0
    trade_ids = [trade["trade_id"] for trade in scored_trades]
    if len(set(trade_ids)) != len(trade_ids):
        raise ValueError(
            "Portfolio ledger requires globally unique trade IDs"
        )

    frames = _price_frames(scored_trades, price_loader)
    dates = _calendar(frames, start_date, end_date, supplied=calendar)
    observed_opens, open_marks, close_marks = _build_marks(frames, dates)

    entries_by_date = defaultdict(list)
    exits_by_date = defaultdict(list)
    for trade in scored_trades:
        entries_by_date[trade["entry_date"]].append(trade)
        for event in trade["_events"]:
            exits_by_date[event["date"]].append((trade, event))

    cash = float(policy.initial_nav)
    previous_nav = float(policy.initial_nav)
    open_lots = []
    ledger_events = []
    daily_rows = []
    allocation_by_id = {}
    realized_pnl_by_trade = defaultdict(float)
    prior_close_state = _portfolio_state_snapshot(
        None,
        policy.initial_nav,
        cash,
        {"long": 0.0, "short": 0.0, "gross": 0.0},
        0,
    )

    for date in dates:
        existing_marks = _mark_map(open_lots, open_marks, date) if open_lots else {}
        open_nav = _portfolio_nav(cash, open_lots, existing_marks)
        existing_exposure = exposure_snapshot(
            open_lots, existing_marks
        ) if open_lots else {
            "long": 0.0,
            "short": 0.0,
            "gross": 0.0,
            "name_gross": {},
        }

        cohort = sorted(
            entries_by_date.get(date, []),
            key=lambda item: (str(item.get("strategy") or ""), item["ticker"]),
        )
        _apply_entry_sizing_policy(
            cohort,
            entry_sizing_policy,
            prior_close_state,
        )
        prepared = []
        requested_legs = []
        for trade in cohort:
            requested_weight = min(
                policy.max_name_weight,
                max(0.0, float(trade.get("requested_weight", 0.0))),
            )
            desired_base = open_nav * requested_weight
            if desired_base <= 0:
                allocation_by_id[trade["trade_id"]] = {
                    "allocation_status": "SKIPPED_ZERO_SCORE",
                    "allocated_base_notional": 0.0,
                }
                continue
            try:
                legs = []
                for leg in _leg_specs(trade):
                    price = _entry_price(trade, leg, observed_opens, date)
                    notional = desired_base * abs(float(leg["signed_ratio"]))
                    direction = 1.0 if float(leg["signed_ratio"]) > 0 else -1.0
                    prepared_leg = {
                        **leg,
                        "price": price,
                        "notional": notional,
                        "direction": direction,
                    }
                    legs.append(prepared_leg)
                requested_legs.extend(legs)
                prepared.append((trade, desired_base, legs))
            except ValueError as exc:
                allocation_by_id[trade["trade_id"]] = {
                    "allocation_status": "SKIPPED_MISSING_OPEN",
                    "allocated_base_notional": 0.0,
                    "reason": str(exc),
                }

        scale = cohort_capacity_scale(
            requested_legs,
            open_nav,
            existing_exposure["long"],
            existing_exposure["short"],
            existing_exposure["name_gross"],
            policy,
        )
        for trade, desired_base, legs in prepared:
            allocated_base = desired_base * scale
            if allocated_base <= 1e-12:
                allocation_by_id[trade["trade_id"]] = {
                    "allocation_status": "SKIPPED_CAPACITY",
                    "allocated_base_notional": 0.0,
                }
                continue

            trade_lots = []
            for leg in legs:
                notional = float(leg["notional"]) * scale
                signed_quantity = (
                    float(leg["direction"]) * notional / float(leg["price"])
                )
                cash -= signed_quantity * float(leg["price"])
                lot = {
                    "trade_id": trade["trade_id"],
                    "strategy": trade.get("strategy"),
                    "ticker": leg["ticker"],
                    "role": leg.get("role", "LEG"),
                    "quantity": signed_quantity,
                    "original_quantity": signed_quantity,
                    "entry_price": float(leg["price"]),
                    "remaining_fraction": 1.0,
                    "single_leg": len(legs) == 1,
                }
                open_lots.append(lot)
                trade_lots.append(lot)
                ledger_events.append(
                    {
                        "date": date,
                        "event": "ENTRY",
                        "trade_id": trade["trade_id"],
                        "ticker": leg["ticker"],
                        "role": leg.get("role", "LEG"),
                        "price": float(leg["price"]),
                        "quantity": signed_quantity,
                        "notional": notional,
                        "cash_after": cash,
                    }
                )
            allocation_by_id[trade["trade_id"]] = {
                "allocation_status": "FILLED",
                "requested_weight": float(trade.get("requested_weight", 0.0)),
                "allocated_base_notional": allocated_base,
                "entry_weight_pct": allocated_base / open_nav * 100.0,
                "allocated_gross_notional": sum(
                    abs(lot["original_quantity"] * lot["entry_price"])
                    for lot in trade_lots
                ),
                "quantity": (
                    trade_lots[0]["original_quantity"]
                    if len(trade_lots) == 1
                    else None
                ),
            }

        for trade, event in sorted(
            exits_by_date.get(date, []),
            key=lambda pair: pair[0]["trade_id"],
        ):
            allocation = allocation_by_id.get(trade["trade_id"], {})
            if allocation.get("allocation_status") != "FILLED":
                continue
            trade_lots = [
                lot for lot in open_lots if lot["trade_id"] == trade["trade_id"]
            ]
            if not trade_lots:
                continue
            weight = float(event["weight"])
            remaining = min(lot["remaining_fraction"] for lot in trade_lots)
            if weight < -1e-12 or weight > remaining + 1e-9:
                raise ValueError(
                    f"Invalid exit weight for {trade['trade_id']} on {date.date()}"
                )
            for lot in trade_lots:
                price = _exit_price(
                    event, lot, observed_opens, close_marks, date
                )
                exited_quantity = lot["original_quantity"] * weight
                cash += exited_quantity * price
                lot["quantity"] -= exited_quantity
                lot["remaining_fraction"] = max(
                    0.0, lot["remaining_fraction"] - weight
                )
                realized_pnl = exited_quantity * (
                    price - float(lot["entry_price"])
                )
                realized_pnl_by_trade[trade["trade_id"]] += realized_pnl
                ledger_events.append(
                    {
                        "date": date,
                        "event": "EXIT",
                        "trade_id": trade["trade_id"],
                        "ticker": lot["ticker"],
                        "role": lot["role"],
                        "reason": event.get("reason", "exit"),
                        "price": price,
                        "quantity": exited_quantity,
                        "weight": weight,
                        "realized_pnl": realized_pnl,
                        "cash_after": cash,
                    }
                )
            open_lots = [
                lot for lot in open_lots if abs(float(lot["quantity"])) > 1e-12
            ]

        close_price_map = (
            _mark_map(open_lots, close_marks, date) if open_lots else {}
        )
        nav = _portfolio_nav(cash, open_lots, close_price_map)
        exposure = exposure_snapshot(
            open_lots, close_price_map
        ) if open_lots else {
            "long": 0.0,
            "short": 0.0,
            "gross": 0.0,
            "name_gross": {},
        }
        active_trade_ids = {lot["trade_id"] for lot in open_lots}
        daily_rows.append(
            {
                "date": date,
                "portfolio_return": nav / previous_nav - 1.0,
                "portfolio_nav": nav,
                "cash": cash,
                "long_market_value": exposure["long"],
                "short_market_value": exposure["short"],
                "gross_exposure_pct": (
                    exposure["gross"] / nav * 100.0 if nav > 0 else float("nan")
                ),
                "net_exposure_pct": (
                    (exposure["long"] - exposure["short"]) / nav * 100.0
                    if nav > 0
                    else float("nan")
                ),
                "active_positions": len(active_trade_ids),
                "active_security_lots": len(open_lots),
            }
        )
        prior_close_state = _portfolio_state_snapshot(
            date,
            nav,
            cash,
            exposure,
            len(active_trade_ids),
        )
        previous_nav = nav

    annotated_trades = []
    for trade in scored_trades:
        item = {
            key: value
            for key, value in trade.items()
            if key != "_events"
        }
        item["entry_date"] = str(trade["entry_date"].date())
        item.update(allocation_by_id.get(trade["trade_id"], {}))
        item["realized_pnl"] = realized_pnl_by_trade.get(
            trade["trade_id"], 0.0
        )
        item["nav_contribution_pct"] = (
            item["realized_pnl"] / float(policy.initial_nav) * 100.0
        )
        annotated_trades.append(item)

    still_open = {lot["trade_id"] for lot in open_lots}
    if still_open:
        raise ValueError(
            "Allocated trades are not fully exited: " + ", ".join(sorted(still_open))
        )

    daily = pd.DataFrame(daily_rows).set_index("date")
    return {
        "daily": daily,
        "trades": annotated_trades,
        "events": ledger_events,
        "policy": policy,
        "filled_trades": sum(
            row.get("allocation_status") == "FILLED"
            for row in allocation_by_id.values()
        ),
        "skipped_trades": sum(
            row.get("allocation_status") != "FILLED"
            for row in allocation_by_id.values()
        ),
    }
