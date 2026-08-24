# QuantBot Portfolio Beta

## Purpose

`QuantBotSystem_Portfolio_Beta` is an isolated code clone of the current
`QuantBotSystem_Beta`. The existing Beta remains the rollback version.

This version adds:

- deterministic cross-sectional signal scoring;
- score-proportional position sizing with a 5% per-security cap;
- cash, quantity, partial-exit, gross/net exposure, and NAV accounting;
- separate Box FLAT and TREND attribution;
- a separate 15-bar compressed-box breakout engine;
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

Interactive scans for Momentum, Box Range, Box Range (New one), and Reversion, plus Admin
price/backfill/metadata downloads, run through one process-local
FIFO background worker. The Telegram callback returns immediately and edits
one loading message in monotonic 5% steps. A frozen request snapshot keeps the
strategy, universe, exit policy, neutralization, and position mode unchanged
while a job is queued. The existing operation lock remains the database
consistency boundary.

## Common signal contract

Strategy-specific analyzers are adapted into `Signal` records:

```text
ticker, strategy, side, signal_date, raw_score,
setup, structure, metadata, entry_execution
```

The four strategy engines keep their entry rules. Their native exit
rules remain the default, while the optional Beta D+5 policy replaces every
native target, stop, partial, and time exit with a full close at the fifth
session close after next-open entry. Box Range V2 also exposes a hybrid policy
that preserves its native stop/target events and, from D+5, closes the remainder
on the first session without a native exit event. The adapter is the boundary
between strategy research and portfolio construction.

`analyze_research_analytics.py` is a top-layer, offline-only sidecar. It sends
uniform-horizon, precomputed `rank_score` panels to Alphalens Reloaded and the
existing net portfolio daily return plus local ACWI series to QuantStats. It is
not imported by strategy, scheduler, Telegram, ledger, or market-data modules.

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
- maximum simultaneous positions: 30;
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
With native exits, positions whose normal exit lies after an IS/OOS boundary
are marked at the last available close rather than omitted from end-of-window
NAV. With the D+5 policy, entries without all five post-entry sessions inside
the evaluation window are excluded instead of marked. The Box hybrid policy
also excludes an incomplete remainder rather than marking it.

## Strategy boundaries

- Momentum remains a directional trend strategy.
- Box Range V2 remains one strategy, with separate attribution:
  - `FLAT / RANGE_RECLAIM`;
  - `TREND / RISING_PULLBACK`;
  - `TREND / FALLING_RALLY`;
  - `TRANSITION` remains non-tradable.
- Box Range (New one) is a separate compression-breakout strategy. Its signal
  freezes the prior 15 completed bars as the box, requires width no greater
  than 3 ATR20 and signal-day turnover at least 2x its prior-20-session mean,
  confirms a close outside the box, and enters only at the next open inside a
  half-box gap gate. It exits at a one-box target, at the next open after a
  close back inside the box, or at the tenth entry-inclusive session close.
- Reversion keeps its shock entry and exit lifecycle, but its production
  candidate score is the country-cohort BRAIN unit-safe residual. The normal
  score-weighted allocator converts that score to entry size.

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
scale and one non-negative multiplier per exact trade id. Default `None` preserves
the production calculation. Existing quantities, exits, signal direction, and
all gross/side/name hard caps remain outside the hook.

`portfolio_rl_overlay.py` uses this boundary for a continuous LONG/SHORT
policy trained against exact daily portfolio NAV and ACWI-relative return with
a baseline-CVaR constraint. It is research infrastructure only and has no
Telegram, scheduled-scan, or deployment path.

`portfolio_hmm_overlay.py` uses the same boundary with `hmmlearn`'s two-state
Gaussian HMM. It builds close-known ACWI and universe features, fits only on
earlier observations, and returns filtered `TREND / NON_TREND` probabilities.
Momentum receives the trend preference while Box Range and Reversion receive
the non-trend preference. The policy preserves each side's requested entry
gross while redistributing it among eligible strategies; missing probabilities
pass through at `1.0`. `experiment_portfolio_hmm_overlay.py` compares the
combined three-strategy portfolio under `BASELINE`, a past-only rule, and HMM.
It reads the local cache only, requires an explicit long-run confirmation, and
is not imported by Telegram, scheduled scans, Classic Beta, or deployment.

## Reversion BRAIN score weighting

Reversion applies the unit-safe BRAIN residual before entry-date neutralization
and ranking. The existing shock, close confirmation, next-open entry, exits,
allocator, and ledger remain unchanged. The residual replaces the baseline
shock magnitude for score-proportional sizing; missing, zero, or
direction-inconsistent residuals do not enter. The compatibility variants are:

- `APAC_UNIT_SAFE_FILTER`: CH/JP/KR country cohorts use the signed residual of
  long-window return z-score against short-window return z-score;
- `APAC_EXACT_FILTER`: CH/JP/KR country cohorts reproduce the price-rank versus
  price-volatility vector projection and retain its known price-unit warning.

The production score-weighted policy covers US/CH/JP/KR in separate country
cohorts. The older APAC filter variants keep US passthrough for research
compatibility. Each factor uses only raw `Close`
observations through the signal date, requires 252 observations and at least
20 eligible securities in the same listing-country cohort. Compatibility
filters fall back to baseline when the cohort is insufficient; the production
score-weighted policy skips that entry.
Finite alpha must agree with the existing Reversion side; zero or opposite
alpha rejects the candidate. The experiment uses the configured static group
intersected with securities that have a valid signal-date close. Historical
universe-membership snapshots are not stored, so results must not be described
as point-in-time constituent membership beyond that static-group contract.
