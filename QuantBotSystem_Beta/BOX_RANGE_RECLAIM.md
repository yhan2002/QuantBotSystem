# Box Range Reclaim

## Scope and timing

This is the Beta bot's daily-box false-breakout/reclaim strategy. It is separate from personal trendline experiments.

- Box and signal inputs use only information available at the signal-day close.
- A qualified signal enters no earlier than the next trading day's open.
- The active implementation is `strategy_box_range.py`.

## Current entry rules

1. Use the **prior 20 completed trading days** to define the box high, low, midpoint, and width.
2. The box width must be 11% to 20% of the lower boundary.
3. A boundary must show at least two same-side reactions:
   - lower: low within 1% of the lower boundary and close above it;
   - upper: high within 1% of the upper boundary and close below it.
4. Signal-day turnover is `Volume × Close`. If it is at least 2.0 times the prior 20-day average turnover, entry is vetoed.
5. LONG only: intraday low breaks below the lower boundary, then close returns inside the box.
6. An upper-boundary reclaim is not an entry signal.
7. Enter at the next trading day's open only when that open is above the lower boundary and below the box midpoint. Otherwise cancel the signal.

## Current exit rules

- Take 50% at the box midpoint.
- Take the remaining quantity at the opposite box boundary.
- If the midpoint is not reached within 10 trading days, close the remaining position at that day's close.
- After the midpoint is reached, allow 10 trading days for the opposite boundary. If it is not reached, close the remaining quantity at the tenth day's close.
- At -7%, close half of the remaining quantity; at -10%, close all remaining quantity.
- If a daily bar can satisfy a protective stop and a target, handle the stop first. Gap-through stops use the worse opening fill.
- Backtest output retains the actual entry date and every partial-exit date/reason.

## Validation record

### 2026-07-15: baseline review

- Full-universe one-year result: 1,144 trades, average -0.34%, median about -1.36%, win rate 40.4%.
- LONG was materially stronger than SHORT; aggregate performance was still negative.
- A temporary entry relaxation reduced the sample to 761 trades and worsened results to average -0.45%, median -1.44%, win rate 39.4%.
- The relaxation was reverted. Do not loosen the boundary-touch or post-signal price conditions without an out-of-sample comparison.

### 2026-07-15: long-only conversion

- Disabled upper-boundary short reclaims in the live signal, universe scan, and backtest.
- Hypothesis: remove the persistently weaker short leg from the equity-universe portfolio; validate against the unchanged long/short baseline with the same data window and costs.

### 2026-07-15: next-open gate and post-center time limit

- Cancel entry when the next open is at or below the reclaimed lower boundary or at/above the midpoint.
- Limit the residual position to 10 trading days from the midpoint-hit date.
- Market inclusion remains controlled by the supplied universe; the strategy engine does not exclude China/Hong Kong tickers.

## Reporting and evaluation

- Live scan reports LONG candidates, box width, and turnover ratio. Upper-boundary reclaims remain informational only.
- Universe summary evaluates one year inside a two-year local cache window, including aggregate return, win rate, and trade dates.
- Judge changes at portfolio level as well as per ticker: trade count, average and median return, win rate, loss distribution, and overlapping positions.

## Change discipline

Record future Box-only hypotheses, parameter changes, and focused validation outcomes here. Do not mix personal trendline/channel experiments into this document or the bot strategy.
