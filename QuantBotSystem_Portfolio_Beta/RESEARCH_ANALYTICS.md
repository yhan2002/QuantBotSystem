# Research analytics sidecar

`analyze_research_analytics.py` is an offline research-only adapter. Telegram,
the scheduler, strategies, and runtime modules do not import it. Alphalens and
QuantStats are imported only when their functions are called.

Install the pinned optional packages in a separate environment:

```powershell
python -m pip install -r .\QuantBotSystem_Portfolio_Beta\requirements-research.txt
```

## Alphalens Reloaded

Provide one uniform forward horizon per CSV with exactly
`date,asset,factor,forward_return` (and optionally a group column). Returns are
decimal returns, not percentage points. Build `factor` from the existing signed
`ScoredSignal.rank_score`; compute `forward_return` independently for the same
horizon. Variable trade holding-period outcomes are not valid inputs. The
adapter calls `alphalens.utils.get_clean_factor` with `max_loss=0`, so missing,
non-finite, duplicate, unaligned, or unbinnable rows fail instead of being
silently dropped.

```powershell
Set-Location .\QuantBotSystem_Portfolio_Beta
python .\analyze_research_analytics.py factor .\data\backtest_exports\factor_panel_5d.csv `
  --period 5D --quantiles 5 --group-column sector --binning-by-group `
  --output .\data\backtest_exports\factor_data_5d.csv
```

Library callers can use `factor_panel_from_scored_signals(...)` followed by
`clean_alphalens_factor(...)`.

## QuantStats

Use the existing `*_portfolio_daily.csv` export. `strategy_return` is the
score-weighted ledger's net daily portfolio return and
`market_benchmark_return` is the already-loaded local ACWI return. The adapter
always passes the latter as a `pandas.Series`; ticker strings and network
benchmark lookup are not accepted. Reports explicitly use 252 observations per
year, compounding, and a zero risk-free rate. Bundles contain IS and OOS, so
select one.

```powershell
python .\analyze_research_analytics.py performance .\data\backtest_exports\example_portfolio_daily.csv `
  --window OOS --mode basic
```

HTML is opt-in and is written only when `--html` is supplied:

```powershell
python .\analyze_research_analytics.py performance .\data\backtest_exports\example_portfolio_daily.csv `
  --window OOS --html .\data\backtest_exports\quantstats_oos.html
```

These commands do not download data or run a backtest. Generated CSV/HTML
remain local research artifacts and must not be committed.
