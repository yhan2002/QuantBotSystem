"""Local-DB and report adapter for the Residual Stat-Arb research engine."""
from functools import partial

import pandas as pd

import core_backtest_periods
import core_cross_sectional_neutralization
import core_market_data_store
from core_portfolio_allocator import PortfolioPolicy
import core_position_mode
import core_signal_scoring
from core_signal_model import Signal
import core_universe_backtest
import strategy_residual_stat_arb as engine


STRATEGY_KEY = "RESIDUAL_STAT_ARB"
STRATEGY_LABEL = "Residual Stat-Arb"
DEFAULT_MAX_STALE_CALENDAR_DAYS = 7


def _notify_progress(
    progress_callback,
    stage,
    completed,
    total,
    ticker=None,
    succeeded=True,
):
    if progress_callback is None:
        return
    try:
        progress_callback(
            stage,
            int(completed),
            int(total),
            ticker,
            bool(succeeded),
        )
    except Exception:
        pass


def _load_local_inputs(universe_list, progress_callback=None):
    price_map = {}
    universe_list = list(universe_list)
    total = len(universe_list)
    for completed, ticker in enumerate(universe_list, start=1):
        succeeded = False
        prices = core_market_data_store.get_price_data(ticker)
        if prices is not None and not prices.empty:
            price_map[ticker] = prices
            succeeded = True
        _notify_progress(
            progress_callback,
            "DATA_LOAD",
            completed,
            total,
            ticker,
            succeeded,
        )
    metadata = core_market_data_store.get_security_metadata(price_map)
    return engine.normalize_price_map(price_map), metadata


def _default_runtime_as_of():
    kst_today = pd.Timestamp.now(tz="Asia/Seoul").normalize().tz_localize(None)
    return kst_today - pd.Timedelta(nanoseconds=1)


def _normalize_as_of(as_of):
    value = (
        _default_runtime_as_of()
        if as_of is None
        else pd.Timestamp(as_of)
    )
    if value.tzinfo is not None:
        value = value.tz_convert(None)
    return value


def _latest_price_date(prices, as_of):
    if prices is None or prices.empty:
        return None
    eligible = prices.index[prices.index <= as_of]
    if not len(eligible):
        return None
    return pd.Timestamp(eligible[-1]).normalize()


def _country_latest_dates(price_map, metadata, as_of=None):
    as_of = _normalize_as_of(as_of)
    latest_by_country = {}
    for ticker, prices in price_map.items():
        country = str(
            (metadata.get(ticker) or {}).get("listing_country") or ""
        ).strip()
        if not country or prices is None or prices.empty:
            continue
        latest = _latest_price_date(prices, as_of)
        if latest is None:
            continue
        if country not in latest_by_country or latest > latest_by_country[country]:
            latest_by_country[country] = latest
    return latest_by_country


def _filter_stale_signals(
    scan_result,
    price_map,
    metadata,
    as_of=None,
    max_stale_calendar_days=DEFAULT_MAX_STALE_CALENDAR_DAYS,
):
    max_stale_calendar_days = int(max_stale_calendar_days)
    if max_stale_calendar_days < 0:
        raise ValueError("max_stale_calendar_days cannot be negative")
    resolved_as_of = _normalize_as_of(as_of)
    as_of_date = resolved_as_of.normalize()
    observed_latest = _country_latest_dates(
        price_map,
        metadata,
        as_of=resolved_as_of,
    )
    stale_country_dates = {
        country: latest
        for country, latest in observed_latest.items()
        if (as_of_date - latest).days > max_stale_calendar_days
    }
    latest_by_country = {
        country: latest
        for country, latest in observed_latest.items()
        if country not in stale_country_dates
    }
    current, stale = [], []
    for row in scan_result["signals"]:
        country = str(
            (metadata.get(row["ticker"]) or {}).get("listing_country") or ""
        ).strip()
        expected = latest_by_country.get(country)
        signal_date = pd.Timestamp(row["signal_date"]).normalize()
        if expected is not None and signal_date == expected:
            current.append(row)
        else:
            stale.append(row)
    filtered = dict(scan_result)
    filtered["signals"] = current
    filtered["longs"] = [row for row in current if row["side"] == "LONG"]
    filtered["shorts"] = [row for row in current if row["side"] == "SHORT"]
    return filtered, stale, latest_by_country, stale_country_dates


def _current_score_cohorts(scan_result, metadata, latest_by_country):
    cohorts = {}
    eligible_rows = list(scan_result.get("signals") or []) + list(
        scan_result.get("no_signal") or []
    )
    for row in eligible_rows:
        ticker = row.get("ticker")
        signal_date_value = row.get("signal_date")
        if not ticker or signal_date_value is None:
            continue
        country = str(
            (metadata.get(ticker) or {}).get("listing_country") or ""
        ).strip()
        expected = latest_by_country.get(country)
        signal_date = pd.Timestamp(signal_date_value).normalize()
        if expected is None or signal_date != expected:
            continue
        key = signal_date.date().isoformat()
        cohorts.setdefault(key, []).append(ticker)
    return {
        key: list(dict.fromkeys(tickers))
        for key, tickers in cohorts.items()
    }


def _scored_candidates(
    scan_result,
    neutralization,
    position_mode,
    score_cohorts,
    metadata,
):
    allowed_by_date = {}
    for row in scan_result["signals"]:
        side = row["side"]
        if side == "LONG" and not core_position_mode.allows_long(position_mode):
            continue
        if side == "SHORT" and not core_position_mode.allows_short(position_mode):
            continue
        key = pd.Timestamp(row["signal_date"]).date().isoformat()
        allowed_by_date.setdefault(key, []).append(row)

    result = []
    for signal_date, rows in sorted(allowed_by_date.items()):
        raw_scores = {
            ticker: 0.0 for ticker in score_cohorts.get(signal_date, [])
        }
        by_ticker = {}
        for row in rows:
            ticker = row["ticker"]
            raw_scores.setdefault(ticker, 0.0)
            raw_scores[ticker] = float(row["signal_score"])
            by_ticker[ticker] = row
        neutralized = core_cross_sectional_neutralization.neutralize_scores(
            raw_scores,
            neutralization,
            metadata_by_ticker=metadata,
        )
        candidates = []
        for ticker, row in by_ticker.items():
            signal = Signal(
                strategy=STRATEGY_KEY,
                ticker=ticker,
                side=row["side"],
                signal_date=row["signal_date"],
                raw_score=row["signal_score"],
                status=row["status"],
                setup="RESIDUAL_REENTRY",
                structure="PEER_HEDGED",
                entry_execution=row["entry_execution"],
            )
            candidates.append((signal, float(neutralized.get(ticker, 0.0))))

        for item in core_signal_scoring.score_signal_cohort(candidates):
            row = dict(by_ticker[item.signal.ticker])
            row.update(
                {
                    "neutralized_score": item.selected_score,
                    "rank_score": item.rank_score,
                    "requested_weight": item.requested_weight,
                }
            )
            result.append(row)
    return result


def _window_through_as_of(window, as_of):
    bounded = dict(window)
    end = min(pd.Timestamp(bounded["evaluation_end"]), as_of.normalize())
    bounded["evaluation_end"] = end.date().isoformat()
    return bounded


def _candidate_line(index, row):
    peers = ",".join(row.get("selected_peers") or [])
    return (
        f"{index}. {row['ticker']:<8} | signal {row['signal_date']} | "
        f"residual z {row['residual_z']:+.2f} "
        f"(prior {row['previous_residual_z']:+.2f}) | "
        f"score {row['rank_score']:+.2f} | alpha weight "
        f"{row['requested_weight'] * 100:.1f}% | peers {peers}"
    )


def _runner(
    price_map,
    metadata,
    cache_context,
    ticker,
    include_trades=False,
    **kwargs,
):
    return engine.run_residual_stat_arb_backtest(
        ticker,
        price_map,
        metadata,
        include_trades=include_trades,
        cache_context=cache_context,
        **kwargs,
    )


def scan_residual_stat_arb(
    universe_list,
    neutralization="NONE",
    position_mode=core_position_mode.LONG_SHORT_NEUTRAL,
    config=None,
    as_of=None,
    max_stale_calendar_days=DEFAULT_MAX_STALE_CALENDAR_DAYS,
    progress_callback=None,
):
    """Scan and backtest the fourth strategy entirely from the local cache."""
    neutralization = core_cross_sectional_neutralization.normalize_mode(
        neutralization
    )
    position_mode = core_position_mode.normalize_mode(position_mode)
    scan_as_of = _normalize_as_of(as_of)
    price_map, metadata = _load_local_inputs(
        universe_list,
        progress_callback=progress_callback,
    )
    cache_context = engine.create_residual_cache_context(price_map, metadata)
    structured = engine.scan_residual_stat_arb(
        universe_list,
        price_map,
        metadata,
        as_of=scan_as_of,
        peer_selection_as_of=core_backtest_periods.SELECTION_END,
        config=config,
        limit_per_side=None,
        cache_context=cache_context,
        progress_callback=lambda completed, total, ticker, succeeded: (
            _notify_progress(
                progress_callback,
                "CURRENT_SCAN",
                completed,
                total,
                ticker,
                succeeded,
            )
        ),
    )
    (
        structured,
        stale_signals,
        latest_by_country,
        stale_country_dates,
    ) = _filter_stale_signals(
        structured,
        price_map,
        metadata,
        as_of=scan_as_of,
        max_stale_calendar_days=max_stale_calendar_days,
    )
    score_cohorts = _current_score_cohorts(
        structured,
        metadata,
        latest_by_country,
    )
    candidates = _scored_candidates(
        structured,
        neutralization,
        position_mode,
        score_cohorts,
        metadata,
    )
    longs = sorted(
        (row for row in candidates if row["side"] == "LONG"),
        key=lambda row: (-row["rank_score"], row["ticker"]),
    )
    shorts = sorted(
        (row for row in candidates if row["side"] == "SHORT"),
        key=lambda row: (row["rank_score"], row["ticker"]),
    )

    report = "**Residual Stat-Arb Scan Report**\n"
    report += "=============================\n\n"
    report += (
        f"Peer cohort cutoff: {core_backtest_periods.SELECTION_END} "
        "(same pre-OOS cohort as the OOS backtest)\n"
        f"Scan as_of: {scan_as_of.isoformat()} "
        "(default runtime excludes the current KST date)\n"
        f"Freshness gate: {len(stale_signals)} stale signals removed | "
        f"{len(stale_country_dates)} country feeds older than "
        f"{int(max_stale_calendar_days)} calendar days excluded | "
        f"{len(structured['insufficient'])} insufficient tickers | "
        f"{len(latest_by_country)} listing-country calendars | "
        f"{sum(map(len, score_cohorts.values()))} current eligible names "
        f"across {len(score_cohorts)} signal-date cohorts\n"
        "Freshness basis: local cache dates through scan as_of; no external "
        "exchange-calendar or feed-outage validation.\n"
        "PIT limitation: historical tests reuse the current local universe "
        "and classifications, so survivorship/classification bias remains.\n"
        "Compute profile: full-universe peer ranking/backtest scales "
        "approximately O(N^2 x T).\n\n"
    )
    report += "LONG Candidates (next open, peer-hedged)\n"
    if longs:
        report += "\n".join(
            _candidate_line(index, row)
            for index, row in enumerate(longs[:5], start=1)
        )
    else:
        report += "No LONG candidates."
    report += "\n\nSHORT Candidates (next open, peer-hedged)\n"
    if shorts:
        report += "\n".join(
            _candidate_line(index, row)
            for index, row in enumerate(shorts[:5], start=1)
        )
    else:
        report += "No SHORT candidates."

    runner = partial(_runner, price_map, metadata, cache_context)
    # Position mode controls the alpha-leg direction. Both hedge directions
    # remain available because an unhedged residual trade is not stat-arb.
    hedge_policy = PortfolioPolicy(position_mode="LONG_SHORT_NEUTRAL")
    selection_result = core_universe_backtest.aggregate_universe_backtest(
        universe_list,
        runner,
        neutralization=neutralization,
        position_mode=position_mode,
        portfolio_accounting=True,
        portfolio_policy=hedge_policy,
        side=core_position_mode.side_filter(position_mode),
        config=config,
        progress_callback=lambda completed, total, ticker, succeeded: (
            _notify_progress(
                progress_callback,
                "IS_BACKTEST",
                completed,
                total,
                ticker,
                succeeded,
            )
        ),
        **_window_through_as_of(
            core_backtest_periods.selection_window(),
            scan_as_of,
        ),
    )
    oos_result = core_universe_backtest.aggregate_universe_backtest(
        universe_list,
        runner,
        neutralization=neutralization,
        position_mode=position_mode,
        portfolio_accounting=True,
        portfolio_policy=hedge_policy,
        side=core_position_mode.side_filter(position_mode),
        config=config,
        progress_callback=lambda completed, total, ticker, succeeded: (
            _notify_progress(
                progress_callback,
                "OOS_BACKTEST",
                completed,
                total,
                ticker,
                succeeded,
            )
        ),
        **_window_through_as_of(
            core_backtest_periods.oos_window(),
            scan_as_of,
        ),
    )
    backtest_payload = core_universe_backtest.format_fixed_is_oos_backtest(
        selection_result,
        oos_result,
        entry_text=(
            "same-country/sector residual crosses back inside ±2σ at close; "
            "enter target and beta-adjusted peer hedge together at next open"
        ),
        exit_text=(
            "residual reaches zero, widens to ±3.5σ, or reaches 10 trading "
            "days; close every leg at the following open"
        ),
        strategy_key="residual_stat_arb",
    )
    qualified_rows = list(
        selection_result.get("qualified_trades", selection_result["trades"])
    ) + list(oos_result.get("qualified_trades", oos_result["trades"]))
    exit_failure_count = sum(
        len(row.get("exit_execution_failures") or [])
        for row in qualified_rows
    )
    evaluation_mark_count = sum(
        row.get("exit_execution_status") == "MARKED_AT_EVALUATION_END"
        for row in qualified_rows
    )
    execution_line = (
        "\nExit execution audit: "
        f"{exit_failure_count} missing-next-open leg events | "
        f"{evaluation_mark_count} evaluation-end marks\n"
    )
    if core_universe_backtest.DETAIL_MARKER in backtest_payload:
        backtest_payload = backtest_payload.replace(
            core_universe_backtest.DETAIL_MARKER,
            execution_line + core_universe_backtest.DETAIL_MARKER,
            1,
        )
    else:
        backtest_payload += execution_line
    cache_stats = cache_context.stats
    cache_line = (
        "\nResidual cache: "
        f"{cache_stats['hits']} hits "
        f"(memory {cache_stats['memory_hits']}, disk {cache_stats['disk_hits']}) | "
        f"{cache_stats['misses']} misses | "
        f"{cache_stats['disk_writes']} disk writes | "
        f"{cache_stats['errors']} errors\n"
    )
    if core_universe_backtest.DETAIL_MARKER in backtest_payload:
        backtest_payload = backtest_payload.replace(
            core_universe_backtest.DETAIL_MARKER,
            cache_line + core_universe_backtest.DETAIL_MARKER,
            1,
        )
    else:
        backtest_payload += cache_line
    report += backtest_payload
    report += (
        "\n"
        + core_cross_sectional_neutralization.report_line(
            neutralization, universe_list
        )
        + "\n"
        + core_position_mode.report_line(position_mode)
        + " (mode applies to alpha leg; hedge remains enabled)"
    )
    return report
