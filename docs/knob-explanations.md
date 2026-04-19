# Knob & control explanations

One block per thing the UI shows. Each block has a short title, a range
description, and the reason *why* — so a non-coder can hover any control and
read a plain-English answer.

---

## pair

- title: Currency pair
- range: Forex pairs discovered on disk, e.g. EUR/USD, USD/JPY.
- why: Which market to trade. EUR/USD is calm and liquid; JPY pairs move in bigger jumps; crosses (AUD/NZD) can be quieter.

## main_tf

- title: Main timeframe (signals)
- range: M1, M5, M15, M30, H1, H4, D.
- why: The candle size used to generate trade signals. Smaller = more trades, faster, noisier. Larger = fewer trades, slower, usually more reliable.

## sub_tf

- title: Sub timeframe (fills)
- range: M1 (usually) — must be finer than the main TF.
- why: The finer candle size used to fill stop-loss / take-profit / trailing at realistic prices. M1 gives the most accurate fills.

## complexity

- title: Complexity 1–10
- range: Slider from 1 (bare-bones) to 10 (everything on, finest steps).
- why: A single knob that switches features on or off and chooses step sizes. 6 is a good default; crank it up for overnight runs.

## step_granularity

- title: Step granularity
- range: Coarse · Medium · Fine · Finest.
- why: Multiplies every step size. Finer = more combinations = more compute. Use Coarse while exploring, Fine once you've narrowed in.

## feature_set

- title: Feature set preset
- range: Minimal · Balanced · All on.
- why: Bulk-toggles the optional exit features (trailing, breakeven, partial, stale, session, max-bars). Per-row tickboxes still win.

## trials

- title: Trials
- range: 10 to 50,000 random parameter draws per run.
- why: How many different combinations to try. 2,000 is a sensible default. More = slower but more thorough.

## seed

- title: Random seed
- range: Any integer. Default 42.
- why: Controls which random combinations get picked. Same seed = same run, every time. Change it to explore a different slice.

## layer_name

- title: Layer name
- range: Free text. Leave blank for an auto-generated name.
- why: A label for this run in the history list and comparison dashboard. Useful when comparing several variations.

## library_size

- title: Signal library size
- range: Number of indicator variants (EMA, MACD, Donchian grids).
- why: The pool of entry signals the backtester will try. Each run picks the best one. More variants = slower build, wider search.

## effective_dims

- title: Tunable knobs per trial
- range: Usually a range (e.g. 12–20) because on/off features sample ON or OFF per trial, which changes how many knobs matter that trial.
- why: Lower number = less to search, faster to find a good combo. Higher number = more flexibility, but the optimiser needs more trials to cover it. The big number is the average; the small text shows the spread.

## estimated_runtime

- title: Estimated runtime
- range: Seconds, shown as a low–high band.
- why: Rough forecast of how long this backtest will take. Actual time depends on your CPU and cold-start loading.

## min

- title: Minimum value
- range: Lower bound for this parameter.
- why: The smallest value the optimiser will try. Trim this in when you've learned the good range sits elsewhere.

## max

- title: Maximum value
- range: Upper bound.
- why: The largest value the optimiser will try.

## step

- title: Step size
- range: Grid spacing between tested values.
- why: Small step = fine-grained search (more combinations). Large step = coarse sweep. Leave it blank for continuous sampling.

## enabled

- title: Include in optimisation
- range: Tick to vary this parameter, untick to freeze it at its minimum.
- why: Lets you lock a parameter out of the search so you can focus compute on the ones you're unsure about.

---

## Features

## stop_loss

- title: Stop loss
- range: Fixed pip distance from entry, or a multiple of ATR (Average True Range).
- why: Every trade needs an exit on the wrong side. Fixed SLs are predictable; ATR SLs adapt to recent volatility so quiet markets use tight stops and busy ones use wide stops.

## take_profit

- title: Take profit
- range: Risk:reward ratio, or a multiple of ATR, or a fixed pip distance.
- why: The other side of the trade. RR-based targets automatically scale with the SL; ATR-based scale with volatility; fixed is the simplest but can cut winners short.

## trailing

- title: Trailing stop
- range: Off, or a fixed pip distance / ATR multiple, plus how many pips of profit to wait before it activates.
- why: Lets winners run without immediately capping them. Only activates once the trade is already in profit, so losses are still capped at the original SL.

## breakeven

- title: Move SL to breakeven
- range: Off, or a pip profit at which SL jumps to entry (plus a small offset).
- why: Free insurance — once the trade is in healthy profit, the SL moves to (roughly) the entry price so the worst case becomes zero.

## partial

- title: Partial close
- range: Off, or a percent of position to close (20–75%) at a profit trigger (5–80 pips).
- why: Take something off the table at an interim target. Reduces risk on the remaining runner but also reduces upside.

## stale

- title: Stale exit (time + volatility)
- range: Off, or max bars in trade + an ATR-stall threshold.
- why: Kill trades that have gone nowhere for too long — the setup was wrong even if the SL hasn't hit.

## session

- title: Session (hours-of-day filter)
- range: Off, or inclusive start hour (0–23) and end hour (0–23), UTC.
- why: Forex trades around the clock but not all hours are equally liquid. Restricting to London or NY hours can filter noise.

## max_bars

- title: Max bars in trade
- range: Off, or a hard cap on how long a trade is allowed to live (48–500 bars).
- why: A belt-and-braces against stuck trades. Usually redundant once stale is tuned.

## days

- title: Days of week
- range: 31 = Mon-Fri · 63 = Mon-Sat · 127 = all week.
- why: Weekend gaps on Sunday open can be brutal. Most forex EAs trade only on weekdays.

## signals.ema_cross

- title: EMA crossover signal
- range: Fast EMA period × slow EMA period (fast < slow).
- why: The canonical momentum signal. Fast above slow = long bias; fast below slow = short bias.

## signals.macd_cross

- title: MACD crossover signal
- range: Fast EMA, slow EMA, signal EMA.
- why: MACD line crosses signal line. Similar intent to EMA cross but smoother thanks to the signal smoothing.

## signals.donchian

- title: Donchian channel breakout
- range: Lookback bars (20–120).
- why: Enter long when price breaks above the N-bar high, short when below the N-bar low. Trend-following, slow but robust.

---

## KPIs (result tiles)

## trades

- title: Trades
- range: Total number of round-trip trades the best variant opened.
- why: Sanity check. Very few trades → the result may be luck.

## win_rate_pct

- title: Win rate
- range: Percent of trades that ended in profit.
- why: Not a goal in itself — a 30% win rate with 3:1 RR is great. Read alongside expectancy.

## total_pips

- title: Total pips
- range: Sum of pips across every trade of the best variant.
- why: Raw profitability in pip terms, before commissions / position sizing.

## expectancy_pips

- title: Expectancy (pips/trade)
- range: Average pips per trade.
- why: The number to optimise. Positive = the strategy has an edge; negative = it bleeds.

## max_dd_pct

- title: Max drawdown %
- range: Peak-to-trough equity drop, in percent.
- why: Heart-attack factor. 100% means the account blew up at some point. Aim for < 30% for anything you'd actually trade.

## profit_factor

- title: Profit factor
- range: Gross wins / gross losses. > 1 means net profitable.
- why: 1.0 = break-even; 1.3 is okay; > 2 is strong. Above 3 usually smells like overfitting.

## sharpe

- title: Sharpe ratio
- range: Risk-adjusted return. Higher is smoother returns per unit of risk.
- why: A smoothness score. Strategies with high Sharpe feel less stressful to trade than bumpier ones with the same return.

## return_pct

- title: Return %
- range: Total percent return on starting equity.
- why: What the account would have grown by over the whole backtest.
