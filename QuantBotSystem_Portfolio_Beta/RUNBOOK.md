# Portfolio Beta Runbook

Run all commands from the repository root.

## Inspect and select

```powershell
python .\RUN_quantbot_version_switch.py status
python .\RUN_quantbot_version_switch.py select PORTFOLIO_BETA
```

## Start the selected version

```powershell
python .\RUN_quantbot_version_switch.py run
```

The launcher stays in the foreground and owns the child process. Stop it with
`Ctrl+C`.

## Switch with one command

```powershell
python .\RUN_quantbot_version_switch.py run CLASSIC_BETA --replace
python .\RUN_quantbot_version_switch.py run PORTFOLIO_BETA --replace
```

`--replace` only manages processes previously started through this selector.
If a Beta server was started directly from VSCode, stop that console first.

Never run Classic Beta and Portfolio Beta with the same Telegram token at the
same time.

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
