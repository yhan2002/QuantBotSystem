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
- The action is continuous: scale each new trade and the whole new-entry
  cohort from `0` to `1`.
- Existing positions are never rebalanced by the overlay.
- LONG and SHORT use separate policy heads.
- The reward is portfolio log return minus same-window ACWI log return.
- Daily downside CVaR at the configured confidence (95% by default) may not
  exceed the baseline CVaR limit.
- The selected seed checkpoint must also retain at least 25% of baseline
  average gross exposure, at least 25% average effective scale on every
  feature-eligible candidate, and the configured total/eligible/side
  filled-trade coverage.
- Every reward evaluation replays cash, quantities, partial exits, gross,
  side, and name limits through the normal Portfolio Beta ledger.
- Five fixed seeds can be ensembled; there is no 58% win-rate training gate.

The policy can only reduce a score-proportional requested weight. It cannot
create a signal, flip a side, enlarge a position beyond the original request,
change an exit, or bypass the existing portfolio hard caps.

If a signal lacks enough point-in-time feature history, it remains in both
baseline and RL ledger replays at its original requested size. The policy does
not silently delete it. Its individual multiplier stays at `1`, and the cohort
gross head is disabled for any cohort containing such a passthrough signal.

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
  --groups CSQ_UNIV `
  --strategies REVERSION `
  --seeds 11 23 37 `
  --iterations 6 `
  --population 3 `
  --minimum-filled-trades 60 `
  --minimum-feature-eligible-filled-trades 60 `
  --estimate-only
```

If the printed upper-bound replay count is acceptable, run the focused
Reversion pilot:

```powershell
python .\experiment_portfolio_rl_overlay.py `
  --groups CSQ_UNIV `
  --strategies REVERSION `
  --seeds 11 23 37 `
  --iterations 6 `
  --population 3 `
  --minimum-filled-trades 60 `
  --minimum-feature-eligible-filled-trades 60 `
  --confirm-long-run
```

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
