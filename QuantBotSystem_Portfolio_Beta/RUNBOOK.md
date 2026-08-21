# Portfolio Beta Runbook

Run all commands from the repository root.

## Inspect and select

```powershell
python .\RUN_quantbot_version_switch.py status
python .\RUN_quantbot_version_switch.py select BETA
```

## Start the selected version

```powershell
python .\RUN_quantbot_version_switch.py run
```

The launcher stays in the foreground and owns the child process. Stop it with
`Ctrl+C`.

## Switch with one command

```powershell
python .\RUN_quantbot_version_switch.py run BETA --replace
```

`--replace` only manages processes previously started through this selector.
If a Beta server was started directly from VSCode, stop that console first.

Classic Beta and Portfolio Beta now resolve to this same unified runtime.
The legacy names remain accepted only for saved command compatibility.

Within Portfolio Beta, manual scans, Quick Check, scheduled scans, refreshes,
and Admin data updates are serialized. A second operation waits for the first
one instead of reading a half-refreshed database. See Detail buttons carry
their own report id and remain tied to the scan message that created them.

All three interactive strategy scans and the Admin latest-price, four-year
backfill, and classification downloads are background jobs. Telegram shows a
single 1-100% loading message in 5% increments, including stage, completed
ticker count, and elapsed time. One process executes these heavy jobs in FIFO
order; repeated taps in the same chat do not start a duplicate job. The old
inline menu is removed while the message is a loading screen and the result
menu is restored after completion.

## Storage

- Credentials: existing ignored `QuantBotSystem_Beta/config.local.py`
- Market data: existing `QuantBotSystem_Beta/data/market_data.sqlite`
- Manual/research exports: `QuantBotSystem_Portfolio_Beta/data/backtest_exports`
- Runtime/state: `QuantBotSystem_Portfolio_Beta/data/runtime` and `data/state`

The Portfolio version does not migrate the shared market-data schema.

## Backtest exports

`See Detail` displays text only. `Download Export` creates the selected scan's
ZIP in the background, sends it, and removes the temporary files afterward.
Export snapshots are compressed and immutable; a chat retains at most eight
snapshots for 24 hours with a 32 MB soft memory cap.
Portfolio-accounted ZIPs contain:

- trade events;
- actual per-leg entry/exit ledger;
- daily NAV, cash, long/short market value, gross, and net exposure.
- benchmark summary with the ACWI boundary dates, levels, ROI, market
  multiple, Universe EW ROI, and stock-selection excess.

## ACWI benchmark data

The primary market benchmark is ticker `ACWI`. Choose
`Admin -> Refresh Benchmark` to download ACWI history from the pre-IS seed
through the latest available date using the existing Yahoo market-data path.
The refresh runs in the shared background queue and uses the same runtime lock
as scans and scheduled market updates.

The full fixed-start window is refreshed each time because new distributions
can revise historical adjusted prices. The downloaded replacement must cover
the pre-IS seed, pass a business-day coverage check, and be current before the
existing cache is replaced. Reports use `Adj Close` and fall back to `Close`
only when adjusted prices are unavailable. ACWI is excluded from trading
universes and security metadata refreshes. No CSV or private benchmark source
configuration is required.

The developer target is `Strategy ROI > ACWI ROI` when same-window ACWI ROI is
positive. Strategy, Universe EW, and ACWI metrics stop at the latest common
ACWI trading date; equality is `FAIL`. If valid ACWI boundary prices are missing or
stale, or ACWI ROI is nonpositive, target status is `REVIEW`. Win rate, CAGR,
MDD, and Sharpe remain diagnostics.

The common ACWI end date is passed into each strategy runner before trades are
generated. Any direct performance caller with a later planned exit replaces
that exit with an explicit close-price `evaluation-end mark`; reports and
exports count these marks separately from completed exits.

## Validation only

The implementation does not automatically run a full-universe backtest. Use
the developer analysis script when a long local run is intended:

```powershell
Set-Location .\QuantBotSystem_Portfolio_Beta
python .\analyze_strategy_universe_direction.py
```

The independent portfolio-aware RL sizing experiment is also never started by
the bot or scheduler. Its point-in-time contract, risk objective, limitations,
and pilot/full commands are documented in
`QuantBotSystem_Portfolio_Beta/PORTFOLIO_RL_OVERLAY.md`.

The HMM regime router uses a separate Python 3.12 environment because the
current `hmmlearn` wheel is not published for Python 3.14. Prepare it once:

```powershell
py -3.12 -m venv .venv-hmm
.\.venv-hmm\Scripts\python.exe -m pip install -r .\QuantBotSystem_Portfolio_Beta\requirements-hmm.txt
```

Inspect the local-only workload before authorizing the comparison:

```powershell
Set-Location .\QuantBotSystem_Portfolio_Beta
..\.venv-hmm\Scripts\python.exe .\experiment_portfolio_hmm_overlay.py --estimate-only
```

The full `BASELINE / RULE / HMM` comparison requires the explicit
`--confirm-long-run` flag. It is never invoked by Telegram or the scheduler.

Inspect the local-only Reversion BRAIN alpha comparison before authorizing it:

```powershell
Set-Location .\QuantBotSystem_Portfolio_Beta
python .\experiment_reversion_brain_alpha.py --group CSQ_UNIV --estimate-only
```

The guarded full comparison is never called by Telegram or the scheduler:

```powershell
python .\experiment_reversion_brain_alpha.py `
  --group CSQ_UNIV `
  --confirm-long-run `
  --output .\data\backtest_exports\reversion_brain_alpha.json
```

It reads only the existing local cache and compares `BASELINE`,
`APAC_EXACT_FILTER`, and `APAC_UNIT_SAFE_FILTER`. The exact filter retains the
BRAIN price-unit warning; both filters remain research-only and default OFF.

## Signal D+5 fixed-exit comparison

Preview the bounded Nasdaq Top 30 workload without running it:

```powershell
Set-Location .\QuantBotSystem_Portfolio_Beta
python .\analyze_signal_fixed_exit.py --strategy ALL
```

Run it only after reviewing the preview:

```powershell
python .\analyze_signal_fixed_exit.py --strategy ALL --confirm-long-run
```

The comparison confirms the signal at the close, enters at the next open, and
closes the entire position at the D+5 close. Native targets, stops, and partial
exits are disabled, and entries without a complete D+5 observation inside the
evaluation window are excluded. It reads the local cache only.

Beta Telegram Settings -> Exit Policy exposes `Strategy Default`, `D+5 Close`,
and the Box-only `Box TP/SL + D+5`. The Box hybrid keeps native targets and
stops, then closes the remainder from D+5 on the first session without a native
exit event. The selected policy is frozen when a manual scan is submitted and
is also used by scheduled scans. It applies to both LONG and SHORT trades in
the IS/OOS portfolio calculation; the default remains `Strategy Default`.
