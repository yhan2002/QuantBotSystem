# QuantBot Portfolio Beta

## Purpose

`QuantBotSystem_Portfolio_Beta` is an isolated code clone of the current
`QuantBotSystem_Beta`. The existing Beta remains the rollback version.

This version adds:

- deterministic cross-sectional signal scoring;
- score-proportional position sizing with a 5% per-security cap;
- cash, quantity, partial-exit, gross/net exposure, and NAV accounting;
- separate Box FLAT and TREND attribution;
- a stop/start version selector for Telegram operation.

## Non-negotiable invariants

- A signal uses information available at the close.
- A new position is filled no earlier than the next open.
- Position quantity changes only on an entry or an explicit partial/full exit.
- No daily equal-weight rebalance is assumed.
- Transaction cost defaults to zero, but turnover and gross exposure remain
  observable.
- `QuantBotSystem_Beta` and `QuantBotSystem_Portfolio_Beta` must not poll the
  same Telegram token at the same time.
- The shared market-data database is not migrated by the portfolio engine.

## Runtime isolation

| Resource | Policy |
|---|---|
| Python code | Separate version folders |
| Telegram credentials | Read from ignored local config |
| Market OHLCV and metadata | Shared existing Beta SQLite path |
| Backtest exports | Separate folder under Portfolio Beta |
| Portfolio ledger and logs | Separate folder under Portfolio Beta |
| Telegram in-memory sessions | Separate per running process |

`RUN_quantbot_version_switch.py` selects one version, acquires a common
single-process lock, and starts that version as a child process. Switching is
stop-then-start, not hot reload.

The Portfolio process serializes scans, quick checks, scheduled refreshes, and
Admin data mutations through one re-entrant operation lock. Each completed
manual scan stores an immutable report id, so an older See Detail button
cannot be redirected to a newer scan.

Interactive scans for Momentum, Box Range, and Reversion, plus Admin
price/backfill/metadata downloads, run through one process-local
FIFO background worker. The Telegram callback returns immediately and edits
one loading message in monotonic 5% steps. A frozen request snapshot keeps the
strategy, universe, neutralization, and position mode unchanged while a job is
queued. The existing operation lock remains the database consistency boundary.

## Common signal contract

Strategy-specific analyzers are adapted into `Signal` records:

```text
ticker, strategy, side, signal_date, raw_score,
setup, structure, metadata, entry_execution
```

The three existing strategy engines keep their entry and exit rules. The
adapter is the boundary between strategy research and portfolio construction.

## Score pipeline

For each signal-date cohort:

1. reject invalid/non-finite scores;
2. apply the selected group-neutralization rule using only securities whose
   local price history was live on that cohort date;
3. winsorize score magnitude within each side;
4. percentile-rank score magnitude within each side;
5. restore LONG/SHORT sign, producing `rank_score` in `[-1, +1]`;
6. map absolute rank to a requested weight up to 5% of current NAV.

The pipeline never changes a strategy's intended side. A signal that becomes
inconsistent with the selected neutralization direction is rejected before
allocation.

## Portfolio allocation

Default limits:

- initial NAV: 100;
- maximum total gross at new-entry sizing: 100% of NAV;
- maximum security gross: 5% of NAV;
- Long Only: long 100%, short 0%;
- Short Only: long 0%, short 100%;
- Long/Short: long 50%, short 50%.

Weak or sparse signals leave cash uninvested. Existing positions are not
trimmed merely because market moves push gross above its entry-time cap; the
cap prevents new exposure until capacity returns.

The allocator scales every leg of a multi-leg trade by the same factor. Gross
includes both the alpha leg and every hedge leg.

## Ledger

The ledger processes:

1. opening marks and next-open entries;
2. explicit intraday/close exit events;
3. closing marks;
4. daily cash, long value, short liability, NAV, gross, net, and active lots.

For a short entry, sale proceeds increase cash and the short market value is a
liability. Partial exits reduce the original quantity by the event weight.
Positions whose normal exit lies after an IS/OOS boundary are marked at the
last available close, rather than omitted from end-of-window NAV.

## Strategy boundaries

- Momentum remains a directional trend strategy.
- Box remains one strategy, with separate attribution:
  - `FLAT / RANGE_RECLAIM`;
  - `TREND / RISING_PULLBACK`;
  - `TREND / FALLING_RALLY`;
  - `TRANSITION` remains non-tradable.
- Reversion remains unchanged as a raw-price mean-reversion strategy.

## Reporting

Portfolio reports expose:

- raw, neutralized, and ranked signal score;
- requested and allocated entry weight;
- quantity and each partial/full exit;
- daily NAV, cash, gross, net, long, short, and active positions;
- Box FLAT/TREND attribution;
- portfolio ROI, CAGR, MDD, and Sharpe;
- exact `MIWD00000PUS` MSCI ACWI boundary-level ROI as the primary market
  benchmark and PASS/FAIL input;
- daily equal-weight selected-universe ROI as the secondary stock-selection
  diagnostic.

The primary comparison is reported as:

```text
MSCI ACWI ROI: ... | Strategy ROI: ... | Multiple: ...x
Universe EW ROI: ... | Stock-selection excess: ...%p
```

ACWI ROI needs only the last valid index close before the evaluation window
and the last valid close through its end. It never substitutes an ETF or
Universe EW when exact ACWI data is absent; the report then shows `N/A` and the
target status is `REVIEW`. ACWI MDD, Sharpe, and a daily curve are exposed only
when complete daily index levels are available.

`Admin -> Refresh Benchmark` feeds only validated `Date,Close` data for
`MIWD00000PUS` into the shared market database. Its source is either a configured
local CSV or an authorized HTTPS CSV; both use the background job queue,
runtime serialization lock, and the existing atomic close-series upsert.

The IS/OOS headline return is portfolio CAGR annualized from daily ledger NAV
with 252 trading days. Trade-level averages remain explicitly labelled
``avg trade`` in LONG/SHORT and quick diagnostics and are not treated as
portfolio returns.
