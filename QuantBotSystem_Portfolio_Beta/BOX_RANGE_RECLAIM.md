# Box Range V2

## Scope

This is the Beta bot's directional price-structure strategy. The deployment
folder retains the previous engine and is not modified.

- Signals use information available through the signal-day close.
- Entries occur no earlier than the next trading day's open.
- The active Beta implementation is `strategy_box_range.py`.
- LONG/SHORT availability follows the selected position mode.

## Price structure

The engine fits a robust parallel channel to the prior 40 completed bars.
The signal bar is excluded from channel fitting.

- The center line is a linear fit to normalized daily mid-prices.
- Lower and upper lines are parallel residual quantiles of daily lows and highs.
- `slope_ratio` is the fitted 40-day center-line move divided by channel width.
- `FLAT`: absolute slope ratio at most 0.35.
- `RISING`: slope ratio at least 0.75 and trend efficiency at least 0.20.
- `FALLING`: slope ratio at most -0.75 and trend efficiency at least 0.20.
- Values between the flat and directional thresholds are `TRANSITION`.
- `RISING` permits trend-following LONG entries, `FALLING` permits
  trend-following SHORT entries, and `TRANSITION` permits neither.
- RISING and FALLING quality scores apply the same trend-efficiency component.
- Exported trade diagnostics include signed flat/trend threshold margins and
  both lower/upper spaced-touch counts. These fields are diagnostic only and do
  not delay entries or add a new filter.

## Channel quality

A tradeable channel requires:

- at least 85% close-price containment;
- width between 4 and 12 trailing ATR;
- at least two same-side boundary touches separated by five trading days;
- signal-day turnover below 2.0 times its trailing 20-day average.

Touches use a 0.5 ATR tolerance. A single spike cannot satisfy the spaced-touch
requirement.

## Long entry events

### Flat range reclaim

- Structure is `FLAT`.
- Signal-day low breaks the projected lower line.
- The close returns above the lower line and remains below the center line.
- The close finishes in the upper 40% of the signal-day range.

### Rising-channel pullback

- Structure is `RISING`.
- Signal-day low touches or slightly breaches projected channel support.
- The close finishes above support, above the open, and in the upper 40% of the
  signal-day range.
- The close remains below the first target.

Both setups enter only when the next open remains above projected support and
below the setup-specific first target. Otherwise the signal is cancelled.

## Short entry events

### Flat range reclaim

- Structure is `FLAT`.
- Signal-day high breaks the projected upper line.
- The close returns below the upper line and remains above the center line.
- The close finishes in the lower 40% of the signal-day range.

### Falling-channel rally rejection

- Structure is `FALLING`.
- Signal-day high touches or slightly breaches projected channel resistance.
- The close finishes below resistance, below the open, and in the lower 40% of
  the signal-day range.
- The close remains above the first downside target.

Both setups enter only when the next open remains below projected resistance
and above the setup-specific first target. Otherwise the signal is cancelled.

The optional Box-only `Box Breakout Fill + TP/SL` policy replaces those
next-open fills with an exact fill at the next session's projected same-side
boundary: lower for LONG and upper for SHORT. The next session's daily range
must include that boundary or the signal is not filled. Signal timing remains
close-known, and native targets, stops, and time exits remain unchanged. Since
daily OHLC cannot establish whether a same-day target occurred after the
intraday boundary fill, entry-day stops remain active but targets begin on the
following session.

## Exits

Beta Exit Policy provides four choices. `Strategy Default` uses the native
rules below. `D+5 Close` disables them and closes the full position at the D+5
close. The Box-only `Box TP/SL + D+5` keeps native target and stop events first,
then closes the remaining quantity from D+5 onward on the first session without
a native exit event. A partial target or stop on D+5 therefore defers the
remaining close to the next event-free session. `Box Breakout Fill + TP/SL`
changes only the Box entry fill described above and keeps these native exits.

- Flat reclaim: take 50% at the projected channel center and the balance at the
  projected upper line.
- Rising pullback: take 50% at the greater of the projected center or available
  prior swing high, capped at the projected upper line; take the balance at the
  projected upper line.
- Flat short reclaim: take 50% at the projected channel center and the balance
  at the projected lower line.
- Falling rally rejection: take 50% at the lower of the projected center or
  available prior swing low, floored at the projected lower line; take the
  balance at the projected lower line.
- The channel fit is frozen on the signal day. During the trade, levels advance
  only by the frozen channel slope.
- If the first target is not reached within 10 trading days, exit the remainder.
- After the first target, allow 10 trading days for the upper target.
- At 7% adverse movement, close half of the remaining quantity; at 10%, close
  all remaining quantity, mirrored for LONG and SHORT.
- When a daily bar satisfies the final stop and a target, the final-stop branch
  closes the trade and no target is recorded. When it satisfies only the first
  stop and targets, record the half stop first, then apply targets to the
  remaining quantity on that same bar.
- Backtest details retain entry date, setup, structure, quality metrics, expected
  reward/risk, and every partial-exit date and reason.

## Validation discipline

- Keep the deployment engine as the comparison baseline.
- Compare every structure/setup/direction combination separately.
- Primary evaluation requires cumulative portfolio ROI to strictly exceed the
  positive same-window iShares `ACWI` ETF adjusted-price ROI. Equality is
  `FAIL`; missing or nonpositive ACWI ROI is `REVIEW`. Universe EW, win rate,
  CAGR, MDD, and Sharpe are secondary diagnostics.
- Review target, time-exit, -7% stop, and -10% stop contributions separately.
- The OOS start remains fixed at 2026-01-01, while its end rolls to the latest
  usable cached ACWI session at scan time. Do not tune thresholds from OOS.

## TradingView reference

`strategy_box_range_tradingview.pine` mirrors the close-known channel and entry
conditions for daily-chart inspection. Its thin live channel is recalculated
from the prior 40 completed bars and is the channel used by signal markers. A
frozen channel starts only on the signal-confirmation bar and extends forward;
it is never drawn backward over the formation window. The Pine file is an
indicator and does not replace the Python portfolio backtest or exit ledger.
To keep the chart bounded, a new signal replaces the previous frozen-channel
drawing, so only the latest confirmed frozen channel remains visible. The
script rejects chart timeframes other than one day.

The default chart view shows only the latest confirmed frozen channel and
next-open entry markers. Live rolling channels, touch thresholds, and setup
labels remain available as opt-in diagnostics, with live history capped at 80
bars by default. Inputs and plot values are hidden from the chart status line.
