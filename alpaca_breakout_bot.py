"""
Pre-market range breakout bot for Alpaca: SPY, 5-minute candles, 1:3 risk to reward, flat by the close.

Rules:
    1. Key levels (--levels):
           swing      the last swing high and last swing low of the previous regular session (09:30-16:00 ET),
                      as seen on a regular-hours chart. A swing high is a 5-minute candle whose
                      high is above the 5 candles before and after it (near the close, at least 2 after);
                      a swing low is the same with lows. The "internal" high before the open, not the
                      day's overall extreme or a thin pre-market spike.
           premarket  the highest high and lowest low from 04:00 to 09:30 ET  [default].
    2. From 09:30, 1-minute bars are grouped into 5-minute candles. The first candle to CLOSE above
       the high -> go long; the first to CLOSE below the low -> go short.
       One trade per day: after the first breakout the bot is done for the day.
    3. Entry at market right after that candle closes.
    4. Stop-loss (--stop):
           candle  the breakout candle's low (long) / high (short)            [default]
           level   the high (long) / low (short) that was broken
           range   the other side: the low (long) / high (short)
       Take-profit = 3x the risk: entry + 3 * (entry - stop) for a long, mirrored for a short.
    5. If neither is hit, the position is closed 1 minute before the market closes.

On Alpaca, the take-profit and stop go in as one OCO order, measured from the actual fill price.

Why SPY on 5-minute candles: with the pre-market levels, over 20 trading days (Sep 23 - Oct 20, 2026)
it was the best of the combinations tested (SPY, QQQ, AAPL, TSLA x 1/5/15/30-minute candles x 3 stop placements):
20 trades, 45% winners, +10.2R. 1-minute candles put the stop so close that noise hits it
(0.0R on SPY); 15-minute candles enter late and rarely reach the 3x target (-0.5R).
With the swing levels (Sep 23 - Oct 20): SPY 5-min +5.7R (35% winners), 15-min +9.4R, 1-min -4.0R.
The swing levels sit close to the previous close, so the overnight gap usually puts the first
candle beyond one of them: 18 of 20 SPY trades entered at 09:35.

Data: prices come from Yahoo (all exchanges, pre-market from 04:00), the same data as the backtest;
orders go to Alpaca. The free Alpaca "iex" feed is not used by default because it barely covers the
pre-market: on Oct 20, 2026 it had 7 SPY bars before 09:30 (from 08:00) and missed the 04:02 high.
Use --data alpaca --feed sip if you pay for Alpaca's full data.

Setup:
    Put your keys in alpaca.env next to this file (see alpaca.env.example), or export
    APCA_API_KEY_ID and APCA_API_SECRET_KEY.

Usage:
    python3 alpaca_breakout_bot.py --backtest 20                     # SPY over the last 20 trading days
    python3 alpaca_breakout_bot.py --backtest 20 --symbol SPY,QQQ,AAPL,TSLA --sweep
    python3 alpaca_breakout_bot.py --backtest 1000                   # ~4 years, Alpaca data (needs keys)
    python3 alpaca_breakout_bot.py --replay 4        # run the live bot logic minute by minute over 4 past days
    python3 alpaca_breakout_bot.py --check           # test keys, data and order placement on Alpaca, then exit
    python3 alpaca_breakout_bot.py --dry-run         # simulated fills on live Yahoo data; no keys, no orders
    python3 alpaca_breakout_bot.py                   # Alpaca PAPER account
    python3 alpaca_breakout_bot.py --qty 5 --timeframe 15 --stop level
    python3 alpaca_breakout_bot.py --live            # REAL money (asks you to type LIVE first)

Ctrl+C stops the bot and closes its position (pass --keep-on-exit to leave it open).
"""

import argparse
import json
import logging
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("breakout-bot")


# ---------------------------------------------------------------------------------------
# HTTP and setup
# ---------------------------------------------------------------------------------------

_ssl_ctx = None


def ssl_context():
    """python.org builds on macOS ship without root certificates; fall back to the system keychain."""
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context()
        if not _ssl_ctx.get_ca_certs() and sys.platform == "darwin":
            pem = subprocess.run(["security", "find-certificate", "-a", "-p",
                                  "/System/Library/Keychains/SystemRootCertificates.keychain"],
                                 capture_output=True, text=True).stdout
            _ssl_ctx = ssl.create_default_context(cadata=pem)
    return _ssl_ctx


def http(method, url, headers=None, params=None, body=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json",
                                          **(headers or {})})
    with urllib.request.urlopen(req, timeout=20, context=ssl_context()) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def load_env():
    path = os.path.join(HERE, "alpaca.env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def completed(bars, now):
    """Drop the bar that is still forming (its start time is the current minute)."""
    minute = now.replace(second=0, microsecond=0)
    return [b for b in bars if b["ts"] < minute]


def market_clock_local(now):
    """Dry-run clock: weekdays 09:30-16:00 ET (ignores holidays)."""
    et = now.astimezone(ET)
    close = et.replace(hour=16, minute=0, second=0, microsecond=0)
    is_open = et.weekday() < 5 and (9, 30) <= (et.hour, et.minute) < (16, 0)
    return is_open, now, close.astimezone(timezone.utc)


# ---------------------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------------------

@dataclass
class Params:
    levels: str = "premarket"     # "swing": previous session's last swing high/low; "premarket": 04:00-09:30 range
    swing_len: int = 5            # a swing candle beats this many candles on each side
    swing_tf: int = 5             # candle size for finding swings, minutes
    session_start: str = "04:00"  # pre-market range window for levels="premarket", ET
    session_end: str = "09:30"
    timeframe: int = 5            # breakout candle size in minutes
    rr: float = 3.0               # take-profit = rr x the stop distance
    stop: str = "candle"          # "candle", "level" or "range"
    last_entry: str = "15:55"     # no new entries from this time, ET


def hhmm(s):
    h, m = s.split(":")
    return int(h), int(m)


def et_minute(b):
    et = b["ts"].astimezone(ET)
    return et.hour, et.minute


def et_day(b):
    return b["ts"].astimezone(ET).date()


def session_range(day_bars, p):
    """(high, low) of the pre-market window, or None if there are no bars in it."""
    start, end = hhmm(p.session_start), hhmm(p.session_end)
    pre = [b for b in day_bars if start <= et_minute(b) < end]
    if not pre:
        return None
    return max(b["high"] for b in pre), min(b["low"] for b in pre)


def last_swing(cs, n, key, min_right=2):
    """The latest candle whose `key` ("high" or "low") beats the n candles before it and the n after it
    (fewer after it at the end of the session, but at least min_right)."""
    better = (lambda a, b: a > b) if key == "high" else (lambda a, b: a < b)
    for i in range(len(cs) - 1 - min_right, n - 1, -1):
        if all(better(cs[i][key], c[key]) for c in cs[i - n:i] + cs[i + 1:i + 1 + n]):
            return cs[i]
    return None


def swing_levels(prev_bars, p):
    """(high, low, high candle, low candle) from the previous session's last swings, or None."""
    cs = candles(prev_bars or [], p.swing_tf)
    hi, lo = last_swing(cs, p.swing_len, "high"), last_swing(cs, p.swing_len, "low")
    if hi is None or lo is None:
        return None
    return hi["high"], lo["low"], hi, lo


def key_levels(day_bars, prev_bars, p):
    """(high, low) the day's breakout is measured against, or None if there's no data for them."""
    if p.levels == "premarket":
        return session_range(day_bars, p)
    lv = swing_levels(prev_bars, p)
    return lv[:2] if lv else None


def previous_session(bars, day):
    """The 1-minute bars of the last ET date before `day` that has regular-session bars."""
    prev = [b for b in bars if et_day(b) < day and (9, 30) <= et_minute(b) < (16, 0)]
    if not prev:
        return []
    d = et_day(prev[-1])
    return [b for b in prev if et_day(b) == d]


def candles(day_bars, minutes):
    """Group the day's 09:30-16:00 ET 1-minute bars into N-minute candles starting at 09:30."""
    out = OrderedDict()
    for b in day_bars:
        h, m = et_minute(b)
        k = h * 60 + m - 570  # minutes since 09:30
        if not 0 <= k < 390:
            continue
        c = out.get(k // minutes)
        if c is None:
            start = b["ts"].replace(second=0, microsecond=0) - timedelta(minutes=k % minutes)
            out[k // minutes] = {"start": start, "end": start + timedelta(minutes=minutes),
                                 "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"]}
        else:
            c["high"], c["low"], c["close"] = max(c["high"], b["high"]), min(c["low"], b["low"]), b["close"]
    return list(out.values())


def find_breakout(day_bars, p, now=None, prev_bars=None):
    """Returns ((high, low), (candle, side)) for the day's first breakout; the second item is None if there
    isn't one yet. With `now`, candles that haven't finished by then are ignored."""
    rng = key_levels(day_bars, prev_bars, p)
    if rng is None:
        return None, None
    hi, lo = rng
    last_entry = hhmm(p.last_entry)
    for c in candles(day_bars, p.timeframe):
        if now is not None and c["end"] > now:
            break
        end_et = c["end"].astimezone(ET)
        if (end_et.hour, end_et.minute) >= last_entry:
            break
        if c["close"] > hi:
            return rng, (c, 1)
        if c["close"] < lo:
            return rng, (c, -1)
    return rng, None


def stop_price(candle, side, rng, p):
    hi, lo = rng
    if p.stop == "candle":
        return candle["low"] if side > 0 else candle["high"]
    if p.stop == "level":
        return hi if side > 0 else lo
    return lo if side > 0 else hi


# ---------------------------------------------------------------------------------------
# Brokers
# ---------------------------------------------------------------------------------------

class Alpaca:
    def __init__(self, key, secret, paper, feed):
        self.base = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
        self.headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self.feed = feed
        self.side, self.qty, self.entry_time, self.entry, self.tp, self.sl = 0, 0, None, None, None, None

    def api(self, method, path, **kw):
        return http(method, self.base + path, self.headers, **kw)

    def account(self):
        return self.api("GET", "/v2/account")

    def clock(self):
        c = self.api("GET", "/v2/clock")
        return (c["is_open"], datetime.fromisoformat(c["timestamp"]).astimezone(timezone.utc),
                datetime.fromisoformat(c["next_close"]).astimezone(timezone.utc))

    def bars(self, symbol, now):
        bars, token = [], None
        params = {"symbols": symbol, "timeframe": "1Min", "limit": 10000, "adjustment": "raw",
                  "feed": self.feed, "start": (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        while True:
            if token:
                params["page_token"] = token
            d = http("GET", "https://data.alpaca.markets/v2/stocks/bars", self.headers, params=params)
            for b in d["bars"].get(symbol, []):
                bars.append({"ts": datetime.fromisoformat(b["t"].replace("Z", "+00:00")),
                             "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"]})
            token = d.get("next_page_token")
            if not token:
                return bars

    def position_qty(self, symbol):
        try:
            return float(self.api("GET", f"/v2/positions/{symbol}")["qty"])  # negative when short
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 0.0
            raise

    def wait_fill(self, order_id, timeout=30):
        deadline = time.time() + timeout
        while True:
            o = self.api("GET", f"/v2/orders/{order_id}")
            if o["status"] == "filled" or time.time() > deadline:
                return o
            if o["status"] in ("canceled", "rejected", "expired"):
                return o
            time.sleep(0.5)

    def cancel_open_orders(self, symbol):
        for o in self.api("GET", "/v2/orders", params={"status": "open", "symbols": symbol, "nested": "true"}):
            try:
                self.api("DELETE", f"/v2/orders/{o['id']}")
            except urllib.error.HTTPError as e:
                if e.code not in (404, 422):  # already filled / canceled
                    raise

    def sync(self, symbol, bar):
        """Notice when the take-profit or stop order closed the position on Alpaca's side."""
        if self.side and self.position_qty(symbol) == 0:
            log.info("EXIT  %s closed by take-profit/stop order on Alpaca", symbol)
            self.cancel_open_orders(symbol)
            self.side = 0

    def flatten(self, symbol, reason, ref_price=None):
        self.cancel_open_orders(symbol)
        if self.position_qty(symbol) != 0:
            try:
                self.api("DELETE", f"/v2/positions/{symbol}")
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
            for _ in range(20):
                if self.position_qty(symbol) == 0:
                    break
                time.sleep(0.5)
        log.info("EXIT  %s closed (%s)", symbol, reason)
        self.side = 0


class SimBroker:
    """Paper-trades in memory for --dry-run / --replay: fills at the last close, TP/stop checked against bar high/low."""

    def __init__(self, fee_pct=0.01):
        self.side, self.qty, self.entry_time, self.entry, self.tp, self.sl = 0, 0, None, None, None, None
        self.fee = fee_pct / 100
        self.trades = []

    def _close(self, price, reason):
        ret = (price / self.entry - 1) * self.side - 2 * self.fee
        self.trades.append({"entry_time": self.entry_time, "side": self.side, "entry": self.entry,
                            "exit": price, "ret": ret, "reason": reason})
        log.info("EXIT  %s %s @ %.2f (%s)  trade %+.3f%%", self.when(), "long" if self.side > 0 else "short",
                 price, reason, ret * 100)
        self.side = 0

    def sync(self, symbol, bar):
        if not self.side or bar["ts"] < self.entry_time.replace(second=0, microsecond=0):
            return
        hit_sl = bar["low"] <= self.sl if self.side > 0 else bar["high"] >= self.sl
        hit_tp = bar["high"] >= self.tp if self.side > 0 else bar["low"] <= self.tp
        if hit_sl or hit_tp:
            self.now = bar["ts"]
        if hit_sl:
            self._close(self.sl, "stop")
        elif hit_tp:
            self._close(self.tp, "take-profit")

    def when(self):
        return self.now.astimezone(ET).strftime("%H:%M") if getattr(self, "now", None) else ""

    def flatten(self, symbol, reason, ref_price=None):
        if self.side:
            self._close(ref_price, reason)


class AlpacaBreakout(Alpaca):
    def enter(self, symbol, side, qty, stop, rr, now, ref_price):
        o = self.api("POST", "/v2/orders", body={
            "symbol": symbol, "qty": str(qty), "side": "buy" if side > 0 else "sell",
            "type": "market", "time_in_force": "day", "client_order_id": f"orb-bot-{uuid.uuid4().hex[:16]}"})
        o = self.wait_fill(o["id"])
        filled = float(o.get("filled_qty") or 0)
        if o["status"] != "filled":
            if o["status"] not in ("canceled", "rejected", "expired"):
                self.api("DELETE", f"/v2/orders/{o['id']}")
            if filled == 0:
                log.warning("ENTRY not filled (status %s); skipping", o["status"])
                return
        fill = float(o["filled_avg_price"])
        self.side, self.qty, self.entry_time, self.entry = side, filled, now, fill
        risk = (fill - stop) * side
        if risk <= 0:
            log.warning("ENTRY filled @ %.2f, already past the stop %.2f; closing", fill, stop)
            self.flatten(symbol, "filled past the stop")
            return
        tp = round(fill + side * rr * risk, 2)
        sl = round(stop, 2)
        self.api("POST", "/v2/orders", body={
            "symbol": symbol, "qty": str(int(filled)), "side": "sell" if side > 0 else "buy",
            "type": "limit", "time_in_force": "day", "order_class": "oco",
            "take_profit": {"limit_price": f"{tp:.2f}"}, "stop_loss": {"stop_price": f"{sl:.2f}"},
            "client_order_id": f"orb-bot-{uuid.uuid4().hex[:16]}"})
        self.tp, self.sl = tp, sl
        log.info("ENTRY %s %s %g @ %.2f  take-profit %.2f  stop %.2f  (risk %.2f, reward %.2f)",
                 "LONG " if side > 0 else "SHORT", symbol, filled, fill, tp, sl, risk, rr * risk)


class SimBreakout(SimBroker):
    def enter(self, symbol, side, qty, stop, rr, now, ref_price):
        risk = (ref_price - stop) * side
        if risk <= 0:
            log.warning("ENTRY skipped: price %.2f is already past the stop %.2f", ref_price, stop)
            return
        self.side, self.qty, self.entry_time, self.entry = side, qty, now, ref_price
        self.sl, self.tp = stop, ref_price + side * rr * risk
        log.info("ENTRY %s %s %s %g @ %.2f  take-profit %.2f  stop %.2f (simulated)",
                 self.when(), "LONG " if side > 0 else "SHORT", symbol, qty, ref_price, self.tp, self.sl)


# ---------------------------------------------------------------------------------------
# Strategy step (one call per minute)
# ---------------------------------------------------------------------------------------

def step(broker, symbol, qty, p, bars, now, minutes_to_close, state, quiet=False):
    """`bars` are completed 1-minute bars including pre-market and the previous session;
    `state` remembers what was done today."""
    today = now.astimezone(ET).date()
    if state.get("day") != today:
        state.clear()
        state.update(day=today, done=False, range_logged=False)
    day = [b for b in bars if et_day(b) == today]
    broker.now = now
    if day:
        broker.last_price = day[-1]["close"]
        broker.sync(symbol, day[-1])

    if broker.side and minutes_to_close <= 1:
        broker.flatten(symbol, "end of day", day[-1]["close"] if day else None)
        return
    if state["done"]:
        return

    if not state.get("prev"):
        state["prev"] = previous_session(bars, today)
    rng, sig = find_breakout(day, p, now, state["prev"])
    if rng is None:
        if not state["range_logged"]:
            log.warning("no %s levels for today (missing data); no trade", p.levels)
            state["range_logged"] = True
        return
    if not state["range_logged"]:
        if p.levels == "premarket":
            log.info("%s pre-market range %s-%s ET: high %.2f  low %.2f", today, p.session_start, p.session_end, *rng)
        else:
            _, _, hc, lc = swing_levels(state["prev"], p)
            at = lambda c: c["start"].astimezone(ET).strftime("%b %d %H:%M")
            log.info("%s levels: last swing high %.2f (%s ET)  last swing low %.2f (%s ET)",
                     today, rng[0], at(hc), rng[1], at(lc))
        state["range_logged"] = True
    if not sig:
        if not quiet and day:
            log.info("bar %s  close %.2f  waiting for a %d-minute close above %.2f or below %.2f",
                     day[-1]["ts"].astimezone(ET).strftime("%H:%M"), day[-1]["close"], p.timeframe, *rng)
        return

    c, side = sig
    state["done"] = True  # one trade per day
    when = c["end"].astimezone(ET).strftime("%H:%M")
    if now - c["end"] > timedelta(minutes=3):
        log.info("the first breakout (candle closing %s) happened before the bot was watching; no trade today", when)
        return
    if minutes_to_close <= 2:
        return
    log.info("BREAKOUT %s: %d-minute candle closed %.2f, %s the %s %.2f", when, p.timeframe, c["close"],
             "above" if side > 0 else "below", "high" if side > 0 else "low", rng[0] if side > 0 else rng[1])
    broker.enter(symbol, side, qty, stop_price(c, side, rng, p), p.rr, now, c["close"])


# ---------------------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------------------

def yahoo_bars(symbol, period1=None, period2=None):
    """1-minute bars including pre- and post-market. Without periods: the last 5 days."""
    params = {"interval": "1m", "includePrePost": "true"}
    if period1:
        params.update(period1=int(period1.timestamp()), period2=int(period2.timestamp()))
    else:
        params["range"] = "5d"
    d = http("GET", f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}", params=params)["chart"]["result"][0]
    q = d["indicators"]["quote"][0]
    bars = []
    for k, ts in enumerate(d.get("timestamp") or []):
        vals = [q[f][k] for f in ("open", "high", "low", "close")]
        if None not in vals:
            bars.append({"ts": datetime.fromtimestamp(ts, timezone.utc),
                         "open": vals[0], "high": vals[1], "low": vals[2], "close": vals[3]})
    return bars


def yahoo_history(symbol, days):
    """Yahoo keeps ~30 days of 1-minute bars and serves them in chunks of at most 8 days."""
    end = datetime.now(timezone.utc)
    t = end - timedelta(days=min(days * 7 // 5 + 5, 29))
    bars = {}
    while t < end:
        t2 = min(t + timedelta(days=7), end)
        for b in yahoo_bars(symbol, t, t2):
            bars[b["ts"]] = b
        t = t2
        time.sleep(0.3)
    return [bars[k] for k in sorted(bars)]


def alpaca_history(symbol, days, feed="sip"):
    """1-minute bars incl. pre/post-market from Alpaca, going back years. The free plan serves the full
    "sip" feed for anything older than 15 minutes, which is all a backtest needs."""
    load_env()
    key, secret = os.environ.get("APCA_API_KEY_ID"), os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        sys.exit("--source alpaca needs API keys in alpaca.env (see alpaca.env.example).")
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    end = datetime.now(timezone.utc) - timedelta(minutes=16)
    start = end - timedelta(days=days * 365 // 252 + 10)
    params = {"symbols": symbol, "timeframe": "1Min", "limit": 10000, "adjustment": "raw", "feed": feed,
              "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": end.strftime("%Y-%m-%dT%H:%M:%SZ")}
    bars = []
    while True:
        d = http("GET", "https://data.alpaca.markets/v2/stocks/bars", headers, params=params)
        for b in d["bars"].get(symbol, []):
            bars.append({"ts": datetime.fromisoformat(b["t"].replace("Z", "+00:00")),
                         "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"]})
        if not d.get("next_page_token"):
            return bars
        params["page_token"] = d["next_page_token"]
        print(f"\r  downloading {symbol}: {len(bars):,} bars, up to {bars[-1]['ts']:%Y-%m-%d}", end="", file=sys.stderr)


def complete_days(bars):
    """Bars grouped by ET date, keeping days that have a full regular session (so not today before 16:00)."""
    by_day = OrderedDict()
    for b in bars:
        by_day.setdefault(et_day(b), []).append(b)
    return OrderedDict((d, bs) for d, bs in by_day.items()
                       if any(et_minute(b) >= (15, 59) and et_minute(b) < (16, 0) for b in bs))


# ---------------------------------------------------------------------------------------
# Backtest (1-minute bars decide the stop / take-profit, stop first if both are touched in one minute)
# ---------------------------------------------------------------------------------------

def backtest_day(day_bars, p, fee_pct, prev_bars=None):
    rng, sig = find_breakout(day_bars, p, prev_bars=prev_bars)
    if not sig:
        return rng, None
    c, side = sig
    after = [b for b in day_bars if b["ts"] >= c["end"] and et_minute(b) < (16, 0)]
    if not after:
        return rng, None
    entry, stop = after[0]["open"], stop_price(c, side, rng, p)
    risk = (entry - stop) * side
    if risk <= 0:
        return rng, {"side": side, "reason": "gapped past stop", "R": 0.0, "net": 0.0, "candle": c}
    tp = entry + side * p.rr * risk
    exit_price, reason, exit_bar = after[-1]["close"], "end of day", after[-1]
    for b in after:
        hit_sl = b["low"] <= stop if side > 0 else b["high"] >= stop
        hit_tp = b["high"] >= tp if side > 0 else b["low"] <= tp
        if hit_sl:
            gapped = b["open"] <= stop if side > 0 else b["open"] >= stop
            exit_price, reason, exit_bar = (b["open"] if gapped else stop), "stop", b
            break
        if hit_tp:
            gapped = b["open"] >= tp if side > 0 else b["open"] <= tp
            exit_price, reason, exit_bar = (b["open"] if gapped else tp), "take-profit", b
            break
    return rng, {"side": side, "candle": c, "entry": entry, "stop": stop, "tp": tp, "exit": exit_price,
                 "reason": reason, "entry_time": after[0]["ts"], "exit_time": exit_bar["ts"],
                 "R": (exit_price - entry) * side / risk,
                 "net": (exit_price / entry - 1) * side - 2 * fee_pct / 100}


def run_backtest(all_days, p, fee_pct, days):
    """Backtest the last `days` of `all_days` (ET date -> bars); earlier days only supply previous sessions."""
    keys = list(all_days)
    return [(d, *backtest_day(all_days[d], p, fee_pct, previous_session(all_days[keys[i - 1]], d) if i else None))
            for i, d in enumerate(keys) if i >= len(keys) - days]


def summarize(results):
    trades = [t for _, _, t in results if t and t["reason"] != "gapped past stop"]
    eq = 1.0
    for t in trades:
        eq *= 1 + t["net"]
    cum = peak = max_dd = 0.0
    streak = max_streak = 0
    for t in trades:
        cum += t["R"]
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
        streak = streak + 1 if t["R"] < 0 else 0
        max_streak = max(max_streak, streak)
    return {"days": len(results), "trades": len(trades),
            "wins": sum(t["R"] > 0 for t in trades),
            "R": sum(t["R"] for t in trades), "net": eq - 1,
            "max_dd": max_dd, "max_streak": max_streak,
            "reasons": Counter(t["reason"] for t in trades)}


def backtest(symbols, days, p, fee_pct, sweep, source="yahoo"):
    hm = lambda ts: ts.astimezone(ET).strftime("%H:%M")
    summary = []
    for sym in symbols:
        history = alpaca_history(sym, days + 1) if source == "alpaca" else yahoo_history(sym, days + 1)
        days_bars = complete_days(history)
        results = run_backtest(days_bars, p, fee_pct, days)
        levels = (f"pre-market {p.session_start}-{p.session_end} ET range" if p.levels == "premarket"
                  else "previous session's last swing high/low")
        print(f"\n=== {sym}  {levels}, first {p.timeframe}-min close "
              f"outside it, stop {p.stop}, 1:{p.rr:g} risk/reward, costs {fee_pct}%/side ===")
        if days <= 60:
            print(f"{'day':10}  {'high':>9}  {'low':>9}  {'side':5}  {'in':5}  {'entry':>9}  {'stop':>9}  "
                  f"{'target':>9}  {'out':5}  {'exit':>9}  {'R':>5}  {'net':>7}  result")
        for d, rng, t in (results if days <= 60 else []):
            hi, lo = rng or (float("nan"), float("nan"))
            if not t:
                print(f"{d}  {hi:9.2f}  {lo:9.2f}  {'no breakout' if rng else 'no level data'}")
            elif t["reason"] == "gapped past stop":
                print(f"{d}  {hi:9.2f}  {lo:9.2f}  {'long' if t['side'] > 0 else 'short':5}  skipped: opened past the stop")
            else:
                print(f"{d}  {hi:9.2f}  {lo:9.2f}  {'long' if t['side'] > 0 else 'short':5}  {hm(t['entry_time'])}  "
                      f"{t['entry']:9.2f}  {t['stop']:9.2f}  {t['tp']:9.2f}  {hm(t['exit_time'])}  {t['exit']:9.2f}  "
                      f"{t['R']:+5.2f}  {t['net']:+7.2%}  {t['reason']}")
        if days > 60:
            print(f"{'year':4}  {'days':>4}  {'trades':>6}  {'win%':>5}  {'total R':>7}  {'net':>8}  {'worst dip':>9}")
            for y in sorted({d.year for d, _, _ in results}):
                ys = summarize([r for r in results if r[0].year == y])
                print(f"{y:4}  {ys['days']:4}  {ys['trades']:6}  {ys['wins'] / (ys['trades'] or 1):5.0%}  "
                      f"{ys['R']:+7.1f}  {ys['net']:+8.2%}  {ys['max_dd']:+8.1f}R")
        s = summarize(results)
        s["symbol"] = sym
        summary.append(s)
        n = s["trades"] or 1
        print(f"total: {s['trades']} trades in {s['days']} days, win {s['wins'] / n:.0%} (need >{1 / (1 + p.rr):.0%} "
              f"to break even at 1:{p.rr:g}), {s['R']:+.1f}R, net {s['net']:+.2%}  exits: "
              + ", ".join(f"{k} {v}" for k, v in s["reasons"].most_common())
              + f"\n       worst dip {s['max_dd']:+.1f}R, longest losing streak {s['max_streak']} trades, "
                f"{results[0][0]} to {results[-1][0]}")
        if sweep:
            print("  sweep (total R / net):")
            for tf in (1, 5, 15, 30):
                cells = []
                for stop in ("candle", "level", "range"):
                    q = Params(**{**p.__dict__, "timeframe": tf, "stop": stop})
                    r = summarize(run_backtest(days_bars, q, fee_pct, days))
                    cells.append(f"{stop} {r['R']:+6.1f}R {r['net']:+6.2%}")
                print(f"    {tf:2}-min:  " + "   ".join(cells))

    print(f"\n=== Summary ===\n{'symbol':8}  {'days':>4}  {'trades':>6}  {'win%':>5}  {'total R':>7}  {'avg R':>6}  {'net':>8}")
    for s in summary:
        n = s["trades"] or 1
        print(f"{s['symbol']:8}  {s['days']:4}  {s['trades']:6}  {s['wins'] / n:5.0%}  {s['R']:+7.1f}  "
              f"{s['R'] / n:+6.2f}  {s['net']:+8.2%}")


# ---------------------------------------------------------------------------------------
# Replay: the live step() minute by minute over past days, to check it matches the backtest
# ---------------------------------------------------------------------------------------

def replay(symbol, qty, p, days):
    all_bars = yahoo_history(symbol, days + 1)
    days_bars = complete_days(all_bars)
    test_days = list(days_bars)[-days:]
    log.info("Replaying %s on %d day(s) with the live bot logic, simulated fills", symbol, len(test_days))
    total = 0.0
    for d in test_days:
        prev = [b for b in all_bars if et_day(b) < d]
        bars = prev + days_bars[d]
        broker, state = SimBreakout(), {}
        close = datetime.combine(d, dtime(16, 0), ET).astimezone(timezone.utc)
        log.info("\n=== %s ===", d)
        for i, b in enumerate(bars):
            if et_day(b) != d or not (9, 30) <= et_minute(b) < (16, 0):
                continue
            now = b["ts"] + timedelta(seconds=65)
            step(broker, symbol, qty, p, bars[: i + 1], now, (close - now).total_seconds() / 60, state, quiet=True)
        ret = sum(t["ret"] for t in broker.trades)
        log.info("%s: %d trade(s), %+.3f%%", d, len(broker.trades), ret * 100)
        total += ret
    log.info("\nTOTAL over %d day(s): %+.3f%%", len(test_days), total * 100)


def check(broker, symbol):
    """Test data access and order placement without trading: a far-away limit order is placed and cancelled."""
    is_open, now, next_close = broker.clock()
    log.info("Market is %s (next close %s ET)", "OPEN" if is_open else "closed",
             next_close.astimezone(ET).strftime("%Y-%m-%d %H:%M"))

    bars = broker.bars(symbol, now)
    if bars:
        log.info("Data OK: %d 1-minute bars from the %s feed, latest %s ET close %.2f", len(bars), broker.feed,
                 bars[-1]["ts"].astimezone(ET).strftime("%Y-%m-%d %H:%M"), bars[-1]["close"])
    else:
        log.warning("No bars returned from the %s feed", broker.feed)

    asset = broker.api("GET", f"/v2/assets/{symbol}")
    log.info("%s: tradable %s, shortable %s, easy to borrow %s", symbol, asset["tradable"],
             asset["shortable"], asset["easy_to_borrow"])

    price = round(bars[-1]["close"] * 0.5, 2) if bars else 1.00
    o = broker.api("POST", "/v2/orders", body={
        "symbol": symbol, "qty": "1", "side": "buy", "type": "limit", "limit_price": f"{price:.2f}",
        "time_in_force": "day", "client_order_id": f"orb-bot-check-{uuid.uuid4().hex[:12]}"})
    log.info("Test order accepted: buy 1 %s limit $%.2f (half the price, so it can't fill), status %s",
             symbol, price, o["status"])
    broker.api("DELETE", f"/v2/orders/{o['id']}")
    time.sleep(1)
    o = broker.api("GET", f"/v2/orders/{o['id']}")
    log.info("Test order cancelled: status %s", o["status"])
    log.info("CHECK PASSED: keys, data and order placement all work.")


# ---------------------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Pre-market range breakout bot for Alpaca (SPY, 5-minute candles, 1:3 risk/reward)")
    ap.add_argument("--symbol", default="SPY", help="symbol to trade (comma-separated list for --backtest)")
    ap.add_argument("--qty", type=int, default=1, help="shares per trade")
    ap.add_argument("--timeframe", type=int, default=5, help="breakout candle size in minutes")
    ap.add_argument("--rr", type=float, default=3.0, help="reward to risk: take-profit = RR x stop distance")
    ap.add_argument("--stop", default="candle", choices=["candle", "level", "range"],
                    help="candle: breakout candle's low/high; level: the broken pre-market level; "
                         "range: the other side of the pre-market range")
    ap.add_argument("--levels", default="premarket", choices=["swing", "premarket"],
                    help="swing: previous session's last swing high/low; premarket: 04:00-09:30 ET high/low")
    ap.add_argument("--swing-len", type=int, default=5, help="a swing candle beats this many 5-min candles each side")
    ap.add_argument("--session", default="04:00-09:30", help="pre-market range window for --levels premarket, ET")
    ap.add_argument("--last-entry", default="15:55", help="no new entries from this time, ET")
    ap.add_argument("--data", default="yahoo", choices=["yahoo", "alpaca"],
                    help="where prices come from when trading on Alpaca (orders always go to Alpaca)")
    ap.add_argument("--feed", default="iex", choices=["iex", "sip"],
                    help="Alpaca data feed for --data alpaca: iex is free but thin pre-market; sip needs a paid plan")
    ap.add_argument("--dry-run", action="store_true", help="simulate trades on live Yahoo data; no keys, no orders")
    ap.add_argument("--live", action="store_true", help="trade the REAL-money account")
    ap.add_argument("--keep-on-exit", action="store_true", help="leave the position open on Ctrl+C")
    ap.add_argument("--backtest", type=int, metavar="DAYS", help="backtest the last DAYS trading days, then exit")
    ap.add_argument("--source", choices=["yahoo", "alpaca"],
                    help="backtest data: yahoo (last ~20 days) or alpaca (years; needs keys). "
                         "Default: yahoo up to 20 days, alpaca beyond")
    ap.add_argument("--sweep", action="store_true", help="with --backtest: also try other candle sizes and stops")
    ap.add_argument("--replay", type=int, metavar="DAYS", help="replay the last DAYS trading days with the live logic")
    ap.add_argument("--check", action="store_true", help="test keys, data and order placement, then exit")
    a = ap.parse_args()
    start, end = a.session.split("-")
    p = Params(levels=a.levels, swing_len=a.swing_len, session_start=start, session_end=end,
               timeframe=a.timeframe, rr=a.rr, stop=a.stop, last_entry=a.last_entry)

    if a.backtest:
        source = a.source or ("yahoo" if a.backtest <= 20 else "alpaca")
        backtest([s.strip() for s in a.symbol.split(",")], a.backtest, p, 0.01, a.sweep, source)
        return
    if a.replay:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        replay(a.symbol, a.qty, p, a.replay)
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
                        handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(HERE, "breakout_bot.log"))])

    if a.dry_run:
        broker = SimBreakout()
        get_clock = lambda: market_clock_local(datetime.now(timezone.utc))
        get_bars = lambda now: yahoo_bars(a.symbol)
        mode = "DRY RUN (simulated, Yahoo data)"
    else:
        load_env()
        key, secret = os.environ.get("APCA_API_KEY_ID"), os.environ.get("APCA_API_SECRET_KEY")
        if not key or not secret:
            sys.exit("Missing API keys: create alpaca.env (see alpaca.env.example) or export "
                     "APCA_API_KEY_ID and APCA_API_SECRET_KEY.")
        if a.live and input("This trades REAL money. Type LIVE to continue: ").strip() != "LIVE":
            sys.exit("Aborted.")
        broker = AlpacaBreakout(key, secret, paper=not a.live, feed=a.feed)
        try:
            acct = broker.account()
        except urllib.error.HTTPError as e:
            sys.exit(f"Alpaca rejected the keys (HTTP {e.code}). Check they match the "
                     f"{'live' if a.live else 'paper'} account.")
        equity = float(acct["equity"])
        log.info("Account %s  equity $%.2f  buying power $%.2f  shorting %s",
                 acct["account_number"], equity, float(acct["buying_power"]),
                 "enabled" if acct.get("shorting_enabled") else "DISABLED")
        if equity < 25000:
            log.warning("Equity is under $25,000: the pattern day trader rule limits you to 3 day trades "
                        "per 5 days, and this strategy trades most days.")
        if a.check:
            try:
                check(broker, a.symbol)
            except urllib.error.HTTPError as e:
                sys.exit(f"CHECK FAILED: HTTP {e.code} {e.read().decode(errors='replace')}")
            return
        existing = broker.position_qty(a.symbol)
        if existing:
            log.warning("Existing %s position of %g shares found; the bot will close it at the end of the day "
                        "and won't open another today.", a.symbol, existing)
            broker.side, broker.qty, broker.entry_time = (1 if existing > 0 else -1), abs(existing), datetime.now(timezone.utc)
        get_clock = broker.clock
        if a.data == "alpaca":
            get_bars = lambda now: broker.bars(a.symbol, now)
        else:
            get_bars = lambda now: yahoo_bars(a.symbol)
        mode = ("LIVE MONEY" if a.live else "PAPER") + f", {a.data} data"

    levels = (f"pre-market {p.session_start}-{p.session_end} ET range" if p.levels == "premarket"
              else "previous session's last swing high/low")
    log.info("Starting %s: %s, %d share(s), %s, first %d-min close outside it, stop %s, 1:%g risk/reward",
             mode, a.symbol, a.qty, levels, p.timeframe, p.stop, p.rr)

    state = {}
    if broker.side:
        state.update(day=datetime.now(ET).date(), done=True, range_logged=True)
    last_closed_log = None
    try:
        while True:
            # Wake 5 seconds after each minute so the previous bar is complete.
            now = datetime.now(timezone.utc)
            time.sleep(60 - now.second - now.microsecond / 1e6 + 5)
            try:
                is_open, now, next_close = get_clock()
                if not is_open:
                    if last_closed_log is None or now - last_closed_log > timedelta(minutes=30):
                        log.info("market closed; waiting")
                        last_closed_log = now
                    continue
                bars = completed(get_bars(now), now)
                minutes_to_close = (next_close - now).total_seconds() / 60
                step(broker, a.symbol, a.qty, p, bars, now, minutes_to_close, state)
            except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as e:
                log.error("error this minute, will retry: %r", e)
    except KeyboardInterrupt:
        if broker.side and not a.keep_on_exit:
            log.info("stopping: closing position")
            broker.flatten(a.symbol, "bot stopped", getattr(broker, "last_price", None))
        log.info("stopped")
        if a.dry_run and broker.trades:
            total = sum(t["ret"] for t in broker.trades)
            log.info("simulated %d trade(s), total %+.3f%%", len(broker.trades), total * 100)


if __name__ == "__main__":
    main()
