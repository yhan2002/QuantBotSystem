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
| Telegram backtest exports | Compressed immutable snapshots; on-demand background temp files removed after send |
| Manual/research exports | Separate folder under Portfolio Beta |
| Portfolio ledger and logs | Separate folder under Portfolio Beta |
| Telegram in-memory sessions | Separate per running process |

`RUN_quantbot_version_switch.py` selects one version, acquires a common
single-process lock, and starts that version as a child process. Switching is
stop-then-start, not hot reload.

The Portfolio process serializes scans, quick checks, scheduled refreshes, and
Admin data mutations through one re-entrant operation lock. Each completed
manual scan stores an immutable report id, so an older See Detail button
cannot be redirected to a newer scan. Per-chat export snapshots are retained
for at most 24 hours, eight results, and a 32 MB soft cap.

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
- iShares `ACWI` ETF adjusted-price boundary ROI as the primary market benchmark and
  PASS/FAIL input: strategy ROI must strictly exceed a positive same-window
  ACWI ROI, and equality is `FAIL`;
- daily equal-weight selected-universe ROI as the secondary stock-selection
  diagnostic.

The primary comparison is reported as:

```text
ACWI ROI: ... | Strategy ROI: ... | Multiple: ...x
Universe EW ROI: ... | Stock-selection excess: ...%p
```

ACWI ROI uses the last valid adjusted price before the evaluation window and
the last valid adjusted price through its end. Strategy and Universe EW metrics
are truncated to that same latest ACWI trading date. It does not substitute Universe
EW when ACWI data is absent. Missing or nonpositive ACWI ROI produces
`REVIEW`; ACWI MDD, Sharpe, and a daily curve are exposed only when complete
daily prices are available. Win rate, CAGR, MDD, and Sharpe remain diagnostics
and do not change the ROI PASS/FAIL result.

`Admin -> Refresh Benchmark` downloads the full fixed-start ACWI history from a
pre-IS seed through the latest available date so historical `Adj Close`
revisions are captured. A coverage and freshness gate protects the existing
cache from partial Yahoo responses. The job uses the background queue and
runtime serialization lock.

The IS/OOS headline return is portfolio CAGR annualized from daily ledger NAV
with 252 trading days. Trade-level averages remain explicitly labelled
``avg trade`` in LONG/SHORT and quick diagnostics and are not treated as
portfolio returns.

## Experimental entry-sizing policy hook

The portfolio ledger has an optional developer-only hook immediately before a
new entry cohort is allocated. It receives an allowlisted copy of signal-time
fields plus the prior-close portfolio state and may return only a cohort gross
scale and one `0..1` multiplier per exact trade id. Default `None` preserves
the production calculation. Existing quantities, exits, signal direction, and
all gross/side/name hard caps remain outside the hook.

`portfolio_rl_overlay.py` uses this boundary for a continuous LONG/SHORT
policy trained against exact daily portfolio NAV and ACWI-relative return with
a baseline-CVaR constraint. It is research infrastructure only and has no
Telegram, scheduled-scan, or deployment path.
