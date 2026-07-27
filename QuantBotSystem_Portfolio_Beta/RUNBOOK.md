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

All four interactive strategy scans and the Admin latest-price, four-year
backfill, and classification downloads are background jobs. Telegram shows a
single 1-100% loading message in 5% increments, including stage, completed
ticker count, and elapsed time. One process executes these heavy jobs in FIFO
order; repeated taps in the same chat do not start a duplicate job. The old
inline menu is removed while the message is a loading screen and the result
menu is restored after completion.

## Storage

- Credentials: existing ignored `QuantBotSystem_Beta/config.local.py`
- Market data: existing `QuantBotSystem_Beta/data/market_data.sqlite`
- Portfolio exports: `QuantBotSystem_Portfolio_Beta/data/backtest_exports`
- Runtime/state: `QuantBotSystem_Portfolio_Beta/data/runtime` and `data/state`
- Residual compute cache:
  `QuantBotSystem_Portfolio_Beta/data/cache/residual_stat_arb_cache.sqlite`

The Portfolio version does not migrate the shared market-data schema.
Deleting the Residual compute cache is safe; the next scan recreates it and
will be slower on the first cold run. It also prunes older fingerprints and
keeps approximately the newest 4,000 entries.

## Backtest exports

See Detail sends a ZIP for portfolio-accounted scans:

- trade events;
- actual per-leg entry/exit ledger;
- daily NAV, cash, long/short market value, gross, and net exposure.

## Validation only

The implementation does not automatically run a full-universe backtest. Use
the developer analysis script when a long local run is intended:

```powershell
Set-Location .\QuantBotSystem_Portfolio_Beta
python .\analyze_strategy_universe_direction.py
```

The fourth Residual Stat-Arb pass is materially heavier than the three
single-name strategies. Run this analysis intentionally and do not treat the
Telegram request timeout as an execution benchmark.
