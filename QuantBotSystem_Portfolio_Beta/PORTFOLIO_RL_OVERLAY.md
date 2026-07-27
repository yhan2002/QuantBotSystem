# Portfolio-aware RL overlay

This is a developer-only experiment in `QuantBotSystem_Portfolio_Beta`.
It is not connected to Telegram, scheduled scans, or the deployment line.

## What changed from the held RL pilot

The held pilot treated each signal as an isolated `ENTER / SKIP` prediction
and rewarded trade-level R-multiples. That setup could improve reported
portfolio ROI merely by changing position overlap while excluding trades that
were individually better.

This experiment instead optimizes the actual portfolio decision:

- A NumPy-only antithetic evolution strategy searches the policy directly;
  it does not train a return classifier.
- Momentum, Box Range, and Reversion signals and exits remain unchanged.
- The action is continuous: tilt candidate weights up or down inside each
  new-entry LONG and SHORT cohort.
- Each cohort's requested LONG gross and SHORT gross are preserved
  separately. The overlay reallocates capital; it does not shrink it.
- Existing positions are never rebalanced by the overlay.
- LONG and SHORT use separate policy heads.
- The reward is portfolio log return minus same-window ACWI log return.
- Daily downside CVaR at the configured confidence (95% by default) may not
  exceed the baseline CVaR limit.
- The selected seed checkpoint must preserve eligible requested capital,
  retain the configured total/eligible/side filled-trade coverage, and pass
  the portfolio CVaR constraint.
- Every reward evaluation replays cash, quantities, partial exits, gross,
  side, and name limits through the normal Portfolio Beta ledger.
- Five fixed seeds can be ensembled; there is no 58% win-rate training gate.

The policy may enlarge one requested position only by reducing peers on the
same side and entry cohort by the same total amount. It cannot create a
signal, flip a side, change an exit, alter total requested side gross, or
bypass the existing 5% name and portfolio hard caps.

For an A/B comparison, `BASELINE` is the current Portfolio Beta strategy with
no RL sizing overlay. `RL_ENSEMBLE` receives the exact same signals, scores,
next-open entries, and strategy exits, and changes only the requested entry
weights within each side. Zero policy parameters reproduce the baseline
requested weights exactly. The experiment explicitly uses
`LONG_SHORT_NEUTRAL`: LONG and SHORT are both allowed with the normal 50% side
caps, but the ledger does not force exact daily dollar neutrality.

If a signal lacks enough point-in-time feature history, it remains in both
baseline and RL ledger replays at its original requested size. The policy does
not silently delete it. Its individual multiplier stays at `1`; eligible peers
redistribute only their own side capital.

## Point-in-time boundary

The action receives only:

- signal-close OHLCV features;
- the strategy score and setup metadata;
- ACWI features whose last observation is strictly earlier than the signal
  date;
- the portfolio state after the previous trading day's close.

It does not receive the next open, entry price, gap, exit, realized return, or
same-day open portfolio state. Every strategy trade now records its explicit
`signal_date`, which must be earlier than `entry_date`.

The feature scaler and policy parameters are fitted only on fixed IS
(`2024-01` through `2025-12`). The 2026 result is labelled
`OBSERVED_OOS`: it is useful research validation, but it has already been
reviewed during prior development and is not a fresh final holdout.
The safe runtime cutoff is no later than the most recently clock-safe US
trading date (conservatively after 18:00 New York time). Local OHLCV rows do
not record whether they were overwritten after the close, so the experiment
also excludes the latest clock-safe ACWI session. This one-session provenance
lag is applied separately to every ticker as well: each ticker's latest
clock-safe local session is removed before signals and ledger replay. This
prevents a stale persisted partial daily bar or future-dated cache row from
entering the experiment.

The first version uses the normal Long/Short Portfolio Beta caps, no
cross-sectional neutralization overlay, zero trading costs, and the current
local universe/classifications. Survivorship and classification bias
therefore remain and are recorded in the model metadata.

## Run locally

Run this only when a long local experiment is intended. It uses the local
SQLite cache and writes ignored research artifacts under
`data/backtest_exports`; it does not download data or send Telegram messages.

First print the scope without loading market data:

```powershell
Set-Location .\QuantBotSystem_Portfolio_Beta
python .\experiment_portfolio_rl_overlay.py `
  --groups CSQ_UNIV US_LARGE_CAP_100_20260630_PROXY `
  --combine-groups `
  --combined-universe-name CSQ_US100 `
  --strategies BOX `
  --seeds 11 23 37 `
  --iterations 6 `
  --population 3 `
  --minimum-filled-trades 60 `
  --minimum-feature-eligible-filled-trades 60 `
  --estimate-only
```

If the printed upper-bound replay count is acceptable, run the focused
Box Range A/B:

```powershell
python .\experiment_portfolio_rl_overlay.py `
  --groups CSQ_UNIV US_LARGE_CAP_100_20260630_PROXY `
  --combine-groups `
  --combined-universe-name CSQ_US100 `
  --strategies BOX `
  --seeds 11 23 37 `
  --iterations 6 `
  --population 3 `
  --minimum-filled-trades 60 `
  --minimum-feature-eligible-filled-trades 60 `
  --confirm-long-run
```

Without `--combine-groups`, multiple group names remain separate portfolio
experiments. With the flag, their members are sorted and deduplicated before
one portfolio is trained and evaluated. At the current configuration,
`CSQ_UNIV` and US Top 100 contain 44 overlapping names and resolve to one
207-name `CSQ_US100` universe.

The pilot performs many exact ledger replays, so runtime depends on universe
size and trade count. The command above has an upper bound of 139 full ledger
replays. Use one strategy/universe pilot first.

A 4-universe × 3-strategy × 5-seed run with the full defaults is roughly
8,868 full ledger replays before signal generation. Do not launch it as the
first test. If later required, run each strategy/universe separately so a
failed gate does not discard the remaining queue.

Before loading market data, a confirmed run validates the export location and
refuses to overwrite an existing run id. Seed metrics, full policy parameters,
the frozen scaler, training history, and checkpoint status are written
atomically during the run so completed seed models survive an interruption.

## Outputs and decision rule

The experiment writes:

- baseline versus ensemble summary;
- daily strategy and ACWI NAV;
- per-trade size decisions and executed weights;
- eligible capital-preservation ratio and redistribution turnover, including
  separate LONG and SHORT diagnostics;
- every portfolio ledger entry/partial exit/final exit event;
- per-seed IS metrics;
- optimization history;
- frozen scaler and policy parameters;
- feature passthrough observations with exact join IDs and reasons;
- ticker-level strategy failures, data coverage, git state, and cache-date
  snapshot.

Do not adopt the overlay because its fitted IS improves. Adoption requires
consistent improvement in the observed validation window by country and side,
no material deterioration in MDD/CVaR, non-collapsing gross exposure, and a
future untouched holdout or paper-trading period. The primary business target
remains strict `Strategy ROI > positive same-window ACWI ROI`.
