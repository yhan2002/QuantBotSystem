# Price/Liquidity Reversion v2

## Classification and timing

This is a price/liquidity reversion strategy, not a fundamental Value factor backtest. Signals use data known at the close; entries and indicator-based exits fill at the next trading day's open. ATR stops are intraday protective exits and retain conservative gap fills.

## Current rules

- Shock: 5-day return divided by 20-day daily volatility times square root of 5.
- LONG: shock at or below -1.40 sigma within the prior three sessions, MA60 near-flat or rising, a higher close with close location at least 55%, a positive intraday candle, and turnover below 2.0 times the prior 20-day average.
- SHORT: shock at or above +1.60 sigma within the prior three sessions, MA60 near-flat or falling, a lower close with close location at most 45%, and turnover below 2.0 times the prior 20-day average.
- Exit: close-confirmed 50% scale-out at plus/minus 0.5 sigma; the residual stop moves to the entry price after that fill; close the remainder at zero-sigma mean reversion. An initial 1.5 ATR protective stop, 3-day no-progress exit, and 10-day maximum holding period remain in force.

## 2026-07-15 risk and LONG rationale

The 2026-07-14 selected-candidate history contained 12 gross trades. LONG contributed -8.8% over seven trades while SHORT contributed +17.0% over five trades. Three large losses occurred after a partial exit and then an ATR stop. Moving the residual stop to entry after the partial exit addresses this asymmetry while preserving conservative gap treatment. Requiring a positive LONG candle rejects gap-up/fading rebounds that close above the previous day but show no same-day buying pressure.

This is a limited structural adjustment, not parameter optimization. Evaluate costs, trade counts, LONG/SHORT results, and regional breakdowns out of sample before changing shock thresholds or the 50% scale-out size.
