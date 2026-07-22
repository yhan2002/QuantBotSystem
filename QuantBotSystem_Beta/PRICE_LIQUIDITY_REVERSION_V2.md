# Price/Liquidity Reversion v2

## Classification and timing

This is a price/liquidity reversion strategy, not a fundamental Value factor backtest. Signals use data known at the close; entries and indicator-based exits fill at the next trading day's open. ATR stops are intraday protective exits and retain conservative gap fills.

## Current rules

- Shock: 5-day return divided by 20-day daily volatility times square root of 5.
- LONG: shock at or below -1.40 sigma within the prior three sessions, MA60 near-flat or rising, a higher close with close location at least 55%, a positive intraday candle, and turnover below 2.0 times the prior 20-day average.
- SHORT: shock at or above +1.60 sigma within the prior three sessions, MA60 near-flat or falling, a lower close with close location at most 45%, and turnover below 2.0 times the prior 20-day average.
- Planned payoff filter: at the signal close, the 50/50 blended reward to the fixed plus/minus 0.5-sigma and zero-sigma targets must be at least 1.75 times the initial 1.5 ATR risk. Reward to a target already passed is zero.
- Exit: close-confirmed 50% scale-out at plus/minus 0.5 sigma; the residual stop moves to the entry price after that fill; close the remainder at zero-sigma mean reversion. An initial 1.5 ATR protective stop, 3-day no-progress exit, and 10-day maximum holding period remain in force.

## Planned payoff calculation

The zero-sigma target is the close from five trading sessions earlier, which is the price anchor in the current shock definition. The plus/minus 0.5-sigma target uses that same anchor and the signal day's 20-day daily volatility. Planned reward is 50% of the directional distance to the partial target plus 50% of the directional distance to the mean target. Planned risk is 1.5 times the signal day's ATR.

This qualification uses only information available at the signal close, so the next trading day's open is not used to decide whether a signal exists. It is a planning estimate rather than a guaranteed realized payoff: an entry gap and the rolling shock inputs used by the actual exit logic can change the eventual reward-to-risk ratio.

## 2026-07-22 payoff rationale

The filter targets entry quality rather than a broad threshold search. It rejects late confirmations with little reversion distance left and high-ATR setups whose initial stop is large relative to the two-stage profit objective. The 1.75 cutoff is a single economic hypothesis and must be compared with the unfiltered baseline by trade count, average return, win rate, LONG/SHORT results, region, costs, and exit reason before promotion.

## 2026-07-15 risk and LONG rationale

The 2026-07-14 selected-candidate history contained 12 gross trades. LONG contributed -8.8% over seven trades while SHORT contributed +17.0% over five trades. Three large losses occurred after a partial exit and then an ATR stop. Moving the residual stop to entry after the partial exit addresses this asymmetry while preserving conservative gap treatment. Requiring a positive LONG candle rejects gap-up/fading rebounds that close above the previous day but show no same-day buying pressure.

This is a limited structural adjustment, not parameter optimization. Evaluate costs, trade counts, LONG/SHORT results, and regional breakdowns out of sample before changing shock thresholds or the 50% scale-out size.
