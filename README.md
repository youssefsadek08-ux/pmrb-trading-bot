# Pre-Market Range Breakout (PMRB) Trading Bot

An automated intraday trading bot for SPY, written in Python, that trades breakouts of the pre-market
range on the 5-minute timeframe with a fixed 1:3 risk-to-reward. It places orders through the
[Alpaca](https://alpaca.markets) API (paper or live) and includes a backtester that runs on years of
1-minute data.

## Strategy

1. **Levels:** the highest high and lowest low of SPY between 04:00 and 09:30 New York time (pre-market).
2. **Signal:** from 09:30, 1-minute bars are grouped into 5-minute candles. The first candle to *close*
   above the pre-market high is a buy; the first to close below the pre-market low is a short.
   One trade per day.
3. **Stop-loss:** the breakout candle's low (long) or high (short).
4. **Take-profit:** 3x the stop distance (1:3 risk to reward).
5. **Exit:** stop or target, placed together on Alpaca as an OCO order, or at the close if neither is hit.

## Backtest results

SPY, 5-minute candles, 1:3, stop at the breakout candle, with spread/slippage modelled as about 2 cents
per market or stop fill (take-profit limit fills free):

| Period | Trading days | Trades | Win rate | Before costs | After costs |
|---|---|---|---|---|---|
| Nov 2022 – Sep 2025 | 700 | 661 | 29% | +47.1R | +13.8R |
| Sep 2025 – Nov 2026 | 300 | 285 | 31% | +26.5R | +12.1R |

R = the amount risked per trade. Break-even win rate at 1:3 is 25% before costs.

About 100 variants were tested (1/5/15/30-minute candles, stop placements, 1:3 vs 1:4, an indicator
filter, swing-level breakouts, minimum stop sizes). Settings that looked best on one period, such as
15-minute candles, did not hold up on the other. The edge is thin: with about 5 cents of slippage per
stop it disappears, and drawdowns of 20% or more (risking 0.5% per trade) occurred along the way.

## Files

| File | What it is |
|---|---|
| `alpaca_breakout_bot.py` | The whole bot in one file: live/paper trading, backtest, replay |
| `alpaca.env.example` | Template for your Alpaca API keys |

## Setup

Requires Python 3.9+ and no extra packages.

1. Create a free [Alpaca](https://alpaca.markets) account and get paper-trading API keys.
2. Copy `alpaca.env.example` to `alpaca.env` and paste your keys into it.

## Usage

```bash
python3 alpaca_breakout_bot.py --backtest 20          # last 20 days (Yahoo data, no keys needed)
python3 alpaca_breakout_bot.py --backtest 300         # 300 days (Alpaca data, needs keys)
python3 alpaca_breakout_bot.py --replay 4             # run the live logic over 4 past days
python3 alpaca_breakout_bot.py --check                # test keys and order placement
python3 alpaca_breakout_bot.py --dry-run              # simulated trades on live data
python3 alpaca_breakout_bot.py --qty 100              # paper trading, 100 shares per trade
```

Options include `--timeframe`, `--rr`, `--stop candle|level|range` and `--levels premarket|swing`.
Run with `--help` for all of them.

## Disclaimer

For education and research only. This is not financial advice, and backtest results do not predict
future returns. Trading involves risk of loss.
