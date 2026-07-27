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
- Portfolio exports: `QuantBotSystem_Portfolio_Beta/data/backtest_exports`
- Runtime/state: `QuantBotSystem_Portfolio_Beta/data/runtime` and `data/state`

The Portfolio version does not migrate the shared market-data schema.

## Backtest exports

See Detail sends a ZIP for portfolio-accounted scans:

- trade events;
- actual per-leg entry/exit ledger;
- daily NAV, cash, long/short market value, gross, and net exposure.
- benchmark summary with the exact ACWI boundary dates, levels, ROI, market
  multiple, Universe EW ROI, and stock-selection excess.

## MSCI ACWI benchmark data

The primary market benchmark is the exact `MIWD00000PUS` MSCI ACWI index
level. It is never downloaded through Yahoo and is never added to a trading
universe. Put a legally obtained `Date,Close` CSV at:

```text
QuantBotSystem_Portfolio_Beta/data/imports/msci_acwi.csv
```

Then choose `Admin -> Refresh Benchmark`. The refresh runs in the shared
background queue and uses the same runtime lock as scans and scheduled market
updates. Alternatively, configure one authorized source in the ignored
private config shown on the Admin screen. When Portfolio Beta is launched by
the root version selector, this is normally
`QuantBotSystem_Beta/config.local.py`:

```python
MSCI_ACWI_SOURCE_PATH = r"C:\path\to\legally_obtained_msci_acwi.csv"
# or
MSCI_ACWI_SOURCE_URL = "https://your-authorized-endpoint.example/acwi.csv"
MSCI_ACWI_SOURCE_HEADERS = {"Authorization": "Bearer YOUR_LOCAL_SECRET"}
```

The HTTPS endpoint must be an authorized CSV source. The bot does not scrape
public MSCI web pages and does not substitute an ETF. Manual import remains
available:

```powershell
Set-Location .\QuantBotSystem_Portfolio_Beta
python .\import_market_benchmark_csv.py "C:\path\to\msci_acwi.csv"
```

For ROI-only comparison, the file may contain only the valid closes needed at
the IS/OOS boundaries. The importer and Admin refresh validate the complete
file before one atomic upsert into the existing shared `ohlcv` table. Stop the
bot before a manual import; the Admin path is serialized automatically. If the
boundary levels are missing or stale, reports show `MSCI ACWI ROI: N/A` and
target status `REVIEW`.

## Validation only

The implementation does not automatically run a full-universe backtest. Use
the developer analysis script when a long local run is intended:

```powershell
Set-Location .\QuantBotSystem_Portfolio_Beta
python .\analyze_strategy_universe_direction.py
```
