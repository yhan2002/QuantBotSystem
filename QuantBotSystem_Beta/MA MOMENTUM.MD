# MA Momentum v1

## Classification and timing

This is a price-and-volume moving-average momentum strategy. Signals use information available at the close. Entries are filled at the next trading day's open; the strategy must not use the next day's open or close when constructing a signal.

## Current rules

- MA alignment: long when MA5 > MA20 > MA60; short when MA5 < MA20 < MA60.
- Slope confirmation: all three moving averages must slope in the same direction. The current default threshold is 0.0% per trading day.
- Liquidity confirmation: turnover proxy is `Volume × Close`, and the current signal requires at least 3.0 times the 252-day rolling average.
- Holding period: 20 trading days by default, with no overlapping entry for the same isolated backtest position.
- Exit: close at the end of the holding period.
- Backtest records: each closed trade includes side, actual entry date, actual exit date, and gross return.

## Alpha 1 / positive-day status

The historical Alpha 1 / positive-day condition is not part of the live MA signal engine. It exists only in the separate momentum-volume experiment as a reversion-neutralized positive-day alpha overlay. It must remain an experiment until it improves out-of-sample results after costs without materially reducing trade count or stability.

## Known measurement gaps

- The turnover rolling mean currently includes the signal day's turnover; the intended comparison is the signal day's turnover against the prior 252 completed sessions.
- The MA engine currently reports trade count, average gross return, and win rate, but not t-statistic or transaction-cost-adjusted return.
- Existing A/B experiment code does not replace the production MA signal rules and should not be treated as production evidence by itself.

## Evaluation protocol

Compare a small, pre-declared candidate set using the same dates and universe:

1. Baseline: current MA5/20/60, 3.0x turnover surge, 20-day holding period.
2. Candidate A: exclude the signal day from the 252-day turnover average.
3. Candidate B: Candidate A plus one fixed positive-day filter from the Alpha 1 experiment.

For every candidate, report trades, mean return, trade win rate, cohort/portfolio return where applicable, t-statistic, and return after explicitly stated round-trip costs. Preserve actual entry and exit dates for every trade. Reject a candidate if the improvement depends on a broad parameter sweep, materially reduces observations, or appears only in-sample.

## Change log

### 2026-07-15

- Added this MA-specific strategy log.
- Recorded the current MA, turnover, timing, holding-period, and trade-recording rules.
- Recorded the Alpha 1 / positive-day separation and the two main measurement gaps for the next approved implementation.

## Future entry format

For each material strategy change, append a dated entry with:

- hypothesis and exact rule change;
- baseline and candidate definitions;
- data period, universe, and cost assumptions;
- trade count, average return, win rate, t-statistic, and after-cost result;
- entry/exit-date coverage and look-ahead checks;
- decision: keep, reject, or require out-of-sample follow-up.
