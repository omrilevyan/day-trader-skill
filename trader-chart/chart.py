#!/usr/bin/env python3
"""Setup Charts for trader-chart: an annotated candlestick chart for every
opening-range breakout worth a look, with the ICT levels around it.
Standard library only. Long side only (v2). It never places an order.

    python3 chart.py check TSLA                           # today, 5m and 1m charts
    python3 chart.py check TSLA --date 2026-10-19 --interval 5m
    python3 chart.py check TSLA --offline TSLA-5d5m.json --interval 5m
    python3 chart.py watchlist                            # every hit in trader/watchlist.json
    python3 chart.py watchlist --all                      # chart the C grades too

Every level comes from the bars Yahoo sends. Nothing is typed in:
  ORB High / Low   high and low of the first --or-minutes (default 15) of the session
  PDH / PDL        high and low of the previous session's regular-hours bars
  FVG              three bars where bar 3's low is above bar 1's high (bull) or
                   bar 3's high is under bar 1's low (bear); the gap between them.
                   Gaps under --min-fvg-pct of price (default 0.03) are noise and skipped.
  iFVG             an FVG that a later bar CLOSES through: a bull FVG closed under
                   becomes a bearish iFVG, a bear FVG closed over becomes a bullish one
  Liquidity        swing highs (buy-side, BSL) and swing lows (sell-side, SSL): a bar
                   whose high (low) beats the --swing bars on each side. EQH / EQL when
                   two sit within --eq-pct of each other. Swept = a later wick takes the
                   level and that bar closes back on the original side.
  BOS / MSS        a bar CLOSES beyond the last confirmed swing. With the trend it is a
                   break of structure (BOS); against the last break it is a market
                   structure shift (MSS)
  Entry            the first closed bar after the range that closes above the ORB High,
                   inside the config's entry window (the trader-entry rule)
  Stop             --stop or-mid (default, the trader-exit rule), or-low, fvg (bottom of
                   the breakout's bull FVG) or swing (last swing low before the entry),
                   rounded down to the cent
  TP1 / TP2        entry + 1R and entry + 2R

Grade: four ICT checks (displacement FVG on the breakout, bullish MSS / BOS into
it, sell-side liquidity swept before it, a bullish iFVG under it) plus room to
TP1 (no PDH or untaken buy-side liquidity in the way).
  A  two or more ICT checks and room to TP1
  B  one ICT check and room to TP1, or three or more without
  C  the rest. Only A and B are charted by watchlist unless --all.

Writes trader/charts/<SYM>-<date>-<interval>.svg, <SYM>-<date>.json and
<SYM>-<date>.md (the charts with the explanation under them).

Data: Yahoo's free chart JSON (unofficial, can lag), range=5d so the previous
session is there; query2 then query1. --offline PATH reads a saved chart JSON
(.json or .json.gz) or PATH/<TICKER>-<interval>.json / PATH/<TICKER>.json.

Exit codes: 0 when at least one A or B setup was found, 1 otherwise or on an error.
"""

import argparse
import datetime
import gzip
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
PAUSE = 0.5
BACKOFF = 5.0
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"no_entries_before": "09:45", "no_entries_after": "15:30", "flat_by": "15:55"}
EPOCH = datetime.datetime(1970, 1, 1)
OUT_DIR = os.path.join("trader", "charts")
NOTE = "PAPER TRADING / ANALYSIS ONLY. Nothing here places an order. Education and tooling, not financial advice."

NYSE_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
NYSE_EARLY_CLOSE = {"2026-11-27", "2026-12-24", "2027-11-26"}  # 1:00 p.m. ET
OPEN = datetime.time(9, 30)
CLOSE = datetime.time(16, 0)
EARLY = datetime.time(13, 0)


class ChartError(Exception):
    pass


# ---------------------------------------------------------------- time

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """-4 in New York daylight time, -5 otherwise (US rule since 2007)."""
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    return -4 if start <= utc.replace(tzinfo=None) < end else -5


def to_eastern(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def eastern_to_utc(local):
    for off in (-4, -5):
        utc = local - datetime.timedelta(hours=off)
        if eastern_offset(utc) == off:
            return utc
    return local + datetime.timedelta(hours=5)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def ts_of(utc):
    return int((utc.replace(tzinfo=None) - EPOCH).total_seconds())


def parse_now(text):
    """--now: New York time like "2026-10-19 10:15" (or ...Z for UTC) -> naive UTC."""
    if not text:
        return utc_now()
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise ChartError('--now wants a New York time like "2026-10-19 10:15"')
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ChartError("%s wants a New York time like 15:30, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def hm(t):
    return "%d:%02d" % (t.hour, t.minute)


def session(d):
    """(open, close) New York time for a trading day, else None."""
    if d.weekday() >= 5 or d.isoformat() in NYSE_HOLIDAYS:
        return None
    return OPEN, (EARLY if d.isoformat() in NYSE_EARLY_CLOSE else CLOSE)


# ---------------------------------------------------------------- Yahoo

def yahoo_symbol(sym):
    return sym.upper().replace(".", "-")


def chart_range(day, today, interval):
    """A Yahoo range that holds `day` and the session before it."""
    back = (today - day).days
    if back <= 3:
        return "5d"
    if interval == "1m":
        if back <= 6:
            return "5d" if back <= 4 else "7d"
        raise ChartError("Yahoo keeps 1-minute bars for about a week: use --interval 5m for %s" % day)
    if back <= 25:
        return "1mo"
    if back <= 57:
        return "60d"
    raise ChartError("Yahoo keeps 5-minute bars for about 60 days: %s is too far back" % day)


def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def yahoo_get(path):
    last = "no answer"
    for host in HOSTS:
        url = "https://%s.finance.yahoo.com%s" % (host, path)
        for attempt in (0, 1):
            try:
                return _open_url(url)
            except urllib.error.HTTPError as e:
                code = e.code
                e.close()
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    time.sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise ChartError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as e:
                last = "could not connect: %s" % (e.reason,)
                break
            except OSError as e:
                last = "network error: %s" % (e,)
                break
    raise ChartError("Yahoo did not answer (%s on query2 and query1). In a Claude cloud session, "
                     "allow query1.finance.yahoo.com and query2.finance.yahoo.com in the "
                     "environment's network settings" % last)


def parse_chart(text, sym):
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise ChartError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise ChartError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown ticker"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        o, h, lo = at("open", i), at("high", i), at("low", i)
        bars.append({"t": int(t), "o": tick(c if o is None else o), "h": tick(c if h is None else h),
                     "l": tick(c if lo is None else lo), "c": tick(c), "v": int(at("volume", i) or 0)})
    return {"symbol": str(meta.get("symbol") or sym).upper(), "meta": meta, "bars": bars,
            "interval": meta.get("dataGranularity"), "data_time": meta.get("regularMarketTime")}


def tick(x):
    """Yahoo sends float32 noise (361.8799133); US stocks trade in cents (4 places under $1)."""
    x = float(x)
    return round(x, 2) if x >= 1 else round(x, 4)


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def offline_file(path, sym, interval):
    if os.path.isdir(path):
        for name in ("%s-%s.json" % (sym, interval), "%s-%s.json.gz" % (sym, interval)):
            p = os.path.join(path, name)
            if os.path.exists(p):
                return p
        for name in (sym + ".json", sym + ".json.gz"):
            p = os.path.join(path, name)
            if os.path.exists(p):
                return p
        return None
    return path if os.path.exists(path) else None


def get_chart(sym, interval, day, today, offline=None):
    """One ticker's bars at one interval, or None when an offline source lacks them."""
    if offline:
        f = offline_file(offline, sym, interval)
        if not f:
            return None
        ch = parse_chart(read_text(f), sym)
        if ch["interval"] and ch["interval"] != interval:
            return None
    else:
        path = "/v8/finance/chart/%s?range=%s&interval=%s&includePrePost=false" % (
            yahoo_symbol(sym), chart_range(day, today, interval), interval)
        ch = parse_chart(yahoo_get(path), sym)
        time.sleep(PAUSE)
    if ch["symbol"] != yahoo_symbol(sym):
        raise ChartError("the chart holds %s, not %s" % (ch["symbol"], sym))
    return ch


def sessions(bars, step, now_utc=None):
    """{date: [regular-hours bars on the grid, closed by now_utc]} in New York time."""
    out = {}
    now_ts = ts_of(now_utc) if now_utc else None
    for b in bars:
        if b["t"] % step:
            continue                                   # Yahoo's live tick is not a bar
        if now_ts is not None and b["t"] + step > now_ts:
            continue                                   # still forming
        et = to_eastern(utc_of(b["t"]))
        s = session(et.date())
        if not s:
            continue
        o, c = s
        if not (datetime.datetime.combine(et.date(), o) <= et < datetime.datetime.combine(et.date(), c)):
            continue
        out.setdefault(et.date(), {})[b["t"]] = dict(b, et=et, end=et + datetime.timedelta(seconds=step))
    return {d: [v[k] for k in sorted(v)] for d, v in out.items()}


# ---------------------------------------------------------------- the levels

def r2(x):
    return None if x is None else round(float(x) + 1e-9, 2) + 0.0


def floor_cents(x):
    return math.floor(round(x * 100, 6)) / 100.0


def find_swings(bars, k):
    """Fractal swings: a high above the k bars before it and not beaten by the k after."""
    out = []
    for i in range(k, len(bars) - k):
        h, lo = bars[i]["h"], bars[i]["l"]
        if all(h > bars[j]["h"] for j in range(i - k, i)) and all(h >= bars[j]["h"] for j in range(i + 1, i + k + 1)):
            out.append({"kind": "high", "i": i, "price": h, "confirmed": i + k})
        if all(lo < bars[j]["l"] for j in range(i - k, i)) and all(lo <= bars[j]["l"] for j in range(i + 1, i + k + 1)):
            out.append({"kind": "low", "i": i, "price": lo, "confirmed": i + k})
    return out


def liquidity(bars, swings, eq_pct):
    """Each swing as a liquidity pool: when it was taken, and whether that was a sweep."""
    pools = []
    for s in swings:
        p = dict(s, side="BSL" if s["kind"] == "high" else "SSL", taken=None, swept=False, equal=False)
        for j in range(s["confirmed"] + 1, len(bars)):
            b = bars[j]
            if s["kind"] == "high" and b["h"] > s["price"]:
                p["taken"], p["swept"] = j, b["c"] < s["price"]
                break
            if s["kind"] == "low" and b["l"] < s["price"]:
                p["taken"], p["swept"] = j, b["c"] > s["price"]
                break
        pools.append(p)
    for a in pools:
        for b in pools:
            if a is not b and a["kind"] == b["kind"] and abs(a["price"] - b["price"]) <= a["price"] * eq_pct / 100.0:
                a["equal"] = True
    return pools


def find_fvgs(bars, min_gap):
    """Three-bar fair value gaps, with when each was filled and inverted."""
    out = []
    for i in range(1, len(bars) - 1):
        a, c = bars[i - 1], bars[i + 1]
        if c["l"] - a["h"] >= min_gap:
            out.append({"dir": "bull", "i": i, "bot": a["h"], "top": c["l"]})
        elif a["l"] - c["h"] >= min_gap:
            out.append({"dir": "bear", "i": i, "bot": c["h"], "top": a["l"]})
    for g in out:
        g.update(filled=None, inverted=None, inv_end=None)
        for j in range(g["i"] + 2, len(bars)):
            b = bars[j]
            if g["dir"] == "bull":
                if g["filled"] is None and b["l"] <= g["bot"]:
                    g["filled"] = j
                if b["c"] < g["bot"]:
                    g["inverted"] = j
                    break
            else:
                if g["filled"] is None and b["h"] >= g["top"]:
                    g["filled"] = j
                if b["c"] > g["top"]:
                    g["inverted"] = j
                    break
        if g["inverted"] is not None:              # the iFVG holds until a close back through
            for j in range(g["inverted"] + 1, len(bars)):
                b = bars[j]
                if (g["dir"] == "bull" and b["c"] > g["top"]) or (g["dir"] == "bear" and b["c"] < g["bot"]):
                    g["inv_end"] = j
                    break
    return out


def find_structure(bars, swings):
    """BOS / MSS: a close beyond the last confirmed, unbroken swing."""
    events, trend = [], None
    last = {"high": None, "low": None}
    by_conf = sorted(swings, key=lambda s: (s["confirmed"], s["i"]))
    n = 0
    for j, b in enumerate(bars):
        while n < len(by_conf) and by_conf[n]["confirmed"] < j:
            s = by_conf[n]
            last[s["kind"]] = s
            n += 1
        for kind, d in (("high", "up"), ("low", "down")):
            s = last[kind]
            if s is None:
                continue
            if (d == "up" and b["c"] > s["price"]) or (d == "down" and b["c"] < s["price"]):
                label = "MSS" if trend and trend != d else "BOS"
                events.append({"kind": label, "dir": d, "price": s["price"], "from": s["i"], "at": j})
                trend = d
                last[kind] = None
    return events


# ---------------------------------------------------------------- one timeframe

def analyze_tf(bars, prev, step, or_minutes, min_fvg_pct, eq_pct, swing_k):
    if not bars:
        return None
    n_or = or_minutes * 60 // step
    or_bars = [b for b in bars if b["et"] < bars[0]["et"].replace(hour=9, minute=30) + datetime.timedelta(minutes=or_minutes)]
    ref = bars[0]["o"]
    sw = find_swings(bars, swing_k)
    tf = {"step": step, "bars": bars, "n_or": n_or,
          "or_complete": len(or_bars) == n_or and bars[0]["et"].time() == OPEN,
          "orh": max(b["h"] for b in or_bars) if or_bars else None,
          "orl": min(b["l"] for b in or_bars) if or_bars else None,
          "pdh": max(b["h"] for b in prev) if prev else None,
          "pdl": min(b["l"] for b in prev) if prev else None,
          "prev_date": prev[0]["et"].date().isoformat() if prev else None,
          "swings": sw, "pools": liquidity(bars, sw, eq_pct),
          "fvgs": find_fvgs(bars, max(0.01, ref * min_fvg_pct / 100.0)),
          "structure": find_structure(bars, sw)}
    return tf


def index_at(bars, when):
    """Index of the bar that closes at `when` (or the last one closing before it)."""
    k = None
    for i, b in enumerate(bars):
        if b["end"] <= when:
            k = i
    return k


def trigger_of(tf, cfg):
    bars = tf["bars"]
    if not tf["or_complete"]:
        return None
    day = bars[0]["et"].date()
    before = datetime.datetime.combine(day, parse_hhmm(cfg["no_entries_before"], "no_entries_before"))
    cutoff = datetime.datetime.combine(day, parse_hhmm(cfg["no_entries_after"], "no_entries_after"))
    for i in range(tf["n_or"], len(bars)):
        b = bars[i]
        if b["end"] < before or b["end"] > cutoff:
            continue
        if b["c"] > tf["orh"]:
            return i
    return None


def breakdown_of(tf, cfg):
    """First close under the ORB Low (reported only: v2 plans longs)."""
    if not tf["or_complete"]:
        return None
    for i in range(tf["n_or"], len(tf["bars"])):
        if tf["bars"][i]["c"] < tf["orl"]:
            return i
    return None


def ict_checks(tf, trig, entry):
    """The four ICT checks on one timeframe, up to the trigger bar `trig`."""
    bars, step = tf["bars"], tf["step"]
    name = "5m" if step == 300 else "1m"
    win = max(3, 900 // step)
    out = {}
    disp = [g for g in tf["fvgs"] if g["dir"] == "bull" and trig - win <= g["i"] <= trig + 1
            and (g["inverted"] is None or g["inverted"] > trig + 1)]
    if disp:
        g = disp[-1]
        out["fvg"] = "%s bull FVG %.2f-%.2f on the breakout (%s bar)" % (name, g["bot"], g["top"], hm(bars[g["i"]]["et"]))
    ups = [e for e in tf["structure"] if e["at"] <= trig]
    if ups and ups[-1]["dir"] == "up":
        e = ups[-1]
        out["structure"] = "%s bullish %s over %.2f at %s" % (name, e["kind"], e["price"], hm(bars[e["at"]]["end"]))
    sweeps = []
    for p in tf["pools"]:
        if p["side"] == "SSL" and p["swept"] and p["taken"] is not None and p["taken"] <= trig:
            sweeps.append((p["taken"], "%s sell-side swept under swing low %.2f at %s" % (
                name, p["price"], hm(bars[p["taken"]]["end"]))))
    for lvl, nm in ((tf["pdl"], "PDL"), (tf["orl"], "ORB Low")):
        if lvl is None:
            continue
        start = tf["n_or"] if nm == "ORB Low" else 0
        for j in range(start, trig + 1):
            if bars[j]["l"] < lvl:
                if bars[j]["c"] > lvl:
                    sweeps.append((j, "%s sell-side swept under %s %.2f at %s" % (name, nm, lvl, hm(bars[j]["end"]))))
                break
    if sweeps:
        out["sweep"] = sorted(sweeps)[-1][1]
    inv = [g for g in tf["fvgs"] if g["dir"] == "bear" and g["inverted"] is not None and g["inverted"] <= trig
           and (g["inv_end"] is None or g["inv_end"] > trig) and g["bot"] < entry]
    if inv:
        g = inv[-1]
        out["ifvg"] = "%s bullish iFVG %.2f-%.2f (bear FVG closed over at %s)" % (
            name, g["bot"], g["top"], hm(bars[g["inverted"]]["end"]))
    return out


def stop_for(mode, tf, trig, entry):
    bars = tf["bars"]
    if mode == "or-mid":
        return floor_cents((tf["orh"] + tf["orl"]) / 2.0), "ORB midpoint"
    if mode == "or-low":
        return floor_cents(tf["orl"]), "ORB Low"
    if mode == "fvg":
        gs = [g for g in tf["fvgs"] if g["dir"] == "bull" and g["i"] <= trig + 1 and g["bot"] < entry
              and (g["inverted"] is None or g["inverted"] > trig + 1)]
        if gs:
            return floor_cents(gs[-1]["bot"] - 0.01), "under the breakout's bull FVG"
    if mode == "swing":
        lows = [s for s in tf["swings"] if s["kind"] == "low" and s["confirmed"] < trig and s["price"] < entry]
        if lows:
            return floor_cents(lows[-1]["price"] - 0.01), "under the last swing low"
    return floor_cents((tf["orh"] + tf["orl"]) / 2.0), "ORB midpoint, no %s level found" % mode


def outcome(tf, trig, stop, tp1, tp2, flat_by):
    """What happened after the entry, from the bars that exist. Hindsight, not a signal."""
    bars = tf["bars"]
    day = bars[0]["et"].date()
    flat = datetime.datetime.combine(day, flat_by)
    res = {"tp1": None, "tp2": None, "stop": None, "open": True}
    for b in bars[trig + 1:]:
        if b["et"] >= flat:
            break
        if b["l"] <= stop:                         # a bar that touches both counts as the stop
            res["stop"] = hm(b["end"])
            res["open"] = False
            break
        if res["tp1"] is None and b["h"] >= tp1:
            res["tp1"] = hm(b["end"])
        if b["h"] >= tp2:
            res["tp2"] = hm(b["end"])
            res["open"] = False
            break
    res["last_bar"] = hm(bars[-1]["end"])
    return res


def analyze(sym, charts, day, cfg, opts):
    """charts = {"5m": chart or None, "1m": chart or None}. Pure apart from the inputs."""
    tfs = {}
    for name in ("5m", "1m"):
        ch = charts.get(name)
        if not ch:
            continue
        step = INTERVALS[name]
        by_day = sessions(ch["bars"], step, opts.get("now"))
        if day not in by_day:
            continue
        prev_days = [d for d in sorted(by_day) if d < day]
        prev = by_day[prev_days[-1]] if prev_days else []
        tfs[name] = analyze_tf(by_day[day], prev, step, opts["or_minutes"], opts["min_fvg_pct"],
                               opts["eq_pct"], opts["swing_5m"] if name == "5m" else opts["swing_1m"])
    if not tfs:
        raise ChartError("no %s regular-hours bars for %s" % (day.isoformat(), sym))
    plan_tf = opts["plan"] if opts["plan"] in tfs else sorted(tfs)[-1]
    tf = tfs[plan_tf]
    # the previous day's levels come from the 5m data when both are loaded (it reaches further back)
    pd_src = tfs.get("5m") if tfs.get("5m") and tfs["5m"]["pdh"] is not None else tf
    for t in tfs.values():
        t["pdh"], t["pdl"], t["prev_date"] = pd_src["pdh"], pd_src["pdl"], pd_src["prev_date"]
    res = {"symbol": sym, "date": day.isoformat(), "plan_interval": plan_tf, "intervals": sorted(tfs),
           "orb_high": r2(tf["orh"]), "orb_low": r2(tf["orl"]), "pdh": r2(tf["pdh"]), "pdl": r2(tf["pdl"]),
           "prev_date": tf["prev_date"], "last_close": r2(tf["bars"][-1]["c"]),
           "last_bar": hm(tf["bars"][-1]["end"]), "setup": None, "grade": None, "high_quality": False,
           "notes": [], "note": NOTE}
    if not tf["or_complete"]:
        res["status"] = "forming"
        res["notes"].append("the %d-minute opening range is not complete yet" % opts["or_minutes"])
        return res, tfs
    trig = trigger_of(tf, cfg)
    down = breakdown_of(tf, cfg)
    if down is not None and (trig is None or down < trig):
        res["notes"].append("the %s bar closed under the ORB Low first (%.2f): a short setup, which v2 does not plan"
                            % (hm(tf["bars"][down]["et"]), tf["bars"][down]["c"]))
    if trig is None:
        last_end = tf["bars"][-1]["end"]
        cutoff = datetime.datetime.combine(last_end.date(), parse_hhmm(cfg["no_entries_after"], "no_entries_after"))
        if last_end < cutoff:
            res["status"] = "waiting"
            res["notes"].append("no close above the ORB High %.2f yet (last close %.2f, %.2f under)"
                                % (tf["orh"], tf["bars"][-1]["c"], tf["orh"] - tf["bars"][-1]["c"]))
        else:
            res["status"] = "no-trigger"
            res["notes"].append("no close above the ORB High %.2f inside the entry window" % tf["orh"])
        return res, tfs
    tb = tf["bars"][trig]
    entry = r2(tb["c"])
    stop, stop_why = stop_for(opts["stop"], tf, trig, entry)
    if stop >= entry:
        res["status"] = "no-plan"
        res["notes"].append("the stop %.2f is not under the entry %.2f" % (stop, entry))
        return res, tfs
    risk = round(entry - stop, 2)
    tp1, tp2 = r2(entry + risk), r2(entry + 2 * risk)
    checks = {}
    for name, t in tfs.items():
        k = trig if t is tf else index_at(t["bars"], tb["end"])
        if k is None:
            continue
        for key, text in ict_checks(t, k, entry).items():
            checks.setdefault(key, []).append(text)
    blockers = []
    if tf["pdh"] is not None and entry < tf["pdh"] < tp1:
        blockers.append("PDH %.2f sits under TP1" % tf["pdh"])
    for p in tf["pools"]:
        if p["side"] == "BSL" and p["confirmed"] < trig and (p["taken"] is None or p["taken"] > trig) \
                and entry < p["price"] < tp1:
            blockers.append("untaken buy-side liquidity %.2f (swing high %s) sits under TP1"
                            % (p["price"], hm(tf["bars"][p["i"]]["et"])))
    n_ict = len(checks)
    room = not blockers
    grade = "A" if n_ict >= 2 and room else "B" if (n_ict >= 1 and room) or n_ict >= 3 else "C"
    res.update(status="triggered", grade=grade, high_quality=grade in ("A", "B"),
               setup={"side": "long", "kind": "15-minute ORB breakout" if opts["or_minutes"] == 15
                      else "%d-minute ORB breakout" % opts["or_minutes"],
                      "trigger_bar": hm(tb["et"]), "entry_time": hm(tb["end"]), "entry": entry,
                      "stop": stop, "stop_rule": stop_why, "risk": risk, "tp1": tp1, "tp2": tp2,
                      "pct_over_orh": r2(100.0 * (entry / tf["orh"] - 1))},
               confluence={"ict_checks": checks, "count": n_ict, "room_to_tp1": room, "blockers": blockers,
                           "verdict": "yes" if n_ict >= 2 else "partial" if n_ict == 1 else "no"},
               trigger_index=trig)
    if tf["pdh"] is not None and tp1 <= tf["pdh"] < tp2:
        res["notes"].append("PDH %.2f sits between TP1 and TP2" % tf["pdh"])
    if trig < len(tf["bars"]) - 1:
        res["after"] = outcome(tf, trig, stop, tp1, tp2, parse_hhmm(cfg["flat_by"], "flat_by"))
    return res, tfs


# ---------------------------------------------------------------- the chart (SVG)

COL = {"bg": "#ffffff", "grid": "#eceff1", "text": "#263238", "muted": "#78909c", "up": "#26a69a", "down": "#ef5350",
       "orb": "#1e63d6", "pd": "#7b1fa2", "entry": "#111111", "stop": "#d32f2f", "tp": "#2e7d32",
       "bull": "#26a69a", "bear": "#ef5350", "ibull": "#0097a7", "ibear": "#f57c00", "bsl": "#455a64",
       "ssl": "#455a64", "bos": "#ef6c00", "mss": "#c2185b"}


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def nice_step(span, target=8):
    raw = span / float(target)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def spread(items, lo, hi, gap):
    """Labels at their price's y, pushed apart by `gap` px and kept inside [lo, hi]."""
    items.sort(key=lambda it: it["y"])
    for i, it in enumerate(items):
        it["ly"] = max(it["y"], items[i - 1]["ly"] + gap) if i else max(it["y"], lo)
    over = items[-1]["ly"] - hi if items else 0
    if over > 0:
        for it in items:
            it["ly"] -= over
        for i in range(len(items) - 2, -1, -1):
            items[i]["ly"] = min(items[i]["ly"], items[i + 1]["ly"] - gap)
    return items


def render(res, tf, interval, window, path):
    bars_all = tf["bars"]
    a, z = window
    bars = bars_all[a:z]
    n = len(bars)
    W, top, left, lab_w = 1320, 64, 64, 300
    ph, vh = 470, 70
    right = W - lab_w
    pw = right - left
    cw = pw / float(max(n, 1))
    setup = res.get("setup")
    trig = res.get("trigger_index") if res["plan_interval"] == interval else None
    if setup and trig is None:
        trig = index_at(bars_all, datetime.datetime.combine(bars_all[0]["et"].date(),
                                                            parse_hhmm(setup["entry_time"], "entry")))
    lo = min(b["l"] for b in bars)
    hi = max(b["h"] for b in bars)
    must = [tf["orh"], tf["orl"]]
    if setup:
        must += [setup["entry"], setup["stop"], setup["tp1"], setup["tp2"]]
    lo, hi = min([lo] + must), max([hi] + must)
    span = hi - lo
    off = []
    for nm, v in (("PDH", tf["pdh"]), ("PDL", tf["pdl"])):
        if v is None:
            continue
        if lo - 0.6 * span <= v <= hi + 0.6 * span:
            lo, hi = min(lo, v), max(hi, v)
        else:
            off.append((nm, v))
    pad = (hi - lo) * 0.04 or 0.5
    lo, hi = lo - pad, hi + pad

    def X(i):            # i is an index into bars_all
        return left + (i - a + 0.5) * cw

    def Y(p):
        return top + (hi - p) / (hi - lo) * ph

    foot_lines = footer_lines(res, interval)
    H = top + ph + 12 + vh + 34 + 20 * len(foot_lines) + 20
    s = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
         'font-family="Helvetica, Arial, sans-serif">' % (W, H, W, H),
         '<rect width="100%%" height="100%%" fill="%s"/>' % COL["bg"]]
    title = "%s  %s  %s" % (res["symbol"], interval, res["date"])
    if setup:
        title += "   |   %s, grade %s" % (setup["kind"], res["grade"])
    else:
        title += "   |   no ORB long (%s)" % res.get("status")
    s.append('<text x="%d" y="28" font-size="20" font-weight="bold" fill="%s">%s</text>' % (left, COL["text"], esc(title)))
    s.append('<text x="%d" y="48" font-size="12" fill="%s">%s</text>' % (left, COL["stop"], esc(NOTE)))
    # grid and price axis
    step = nice_step(hi - lo)
    p = math.ceil(lo / step) * step
    while p <= hi:
        y = Y(p)
        s.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="%s"/>' % (left, right, y, y, COL["grid"]))
        s.append('<text x="%d" y="%.1f" font-size="11" text-anchor="end" fill="%s">%.2f</text>'
                 % (left - 6, y + 4, COL["muted"], p))
        p += step
    every = 30 if interval == "5m" else 15
    for i in range(a, z):
        et = bars_all[i]["et"]
        if (et.hour * 60 + et.minute - 570) % every == 0:
            x = X(i)
            s.append('<line x1="%.1f" x2="%.1f" y1="%d" y2="%d" stroke="%s"/>' % (x, x, top, top + ph, COL["grid"]))
            s.append('<text x="%.1f" y="%d" font-size="11" text-anchor="middle" fill="%s">%s</text>'
                     % (x, top + ph + vh + 28, COL["muted"], hm(et)))
    s.append('<rect x="%d" y="%d" width="%d" height="%d" fill="none" stroke="%s"/>' % (left, top, pw, ph, COL["muted"]))
    labels, inline, tags = [], [], []

    def hline(price, color, text, x0=None, dash=None, width=1.4):
        x0 = left if x0 is None else x0
        y = Y(price)
        s.append('<line x1="%.1f" x2="%d" y1="%.1f" y2="%.1f" stroke="%s" stroke-width="%s"%s/>'
                 % (x0, right, y, y, color, width, ' stroke-dasharray="%s"' % dash if dash else ""))
        labels.append({"y": y, "text": text, "color": color})

    def tag(x, y, text, color, anchor="start", size=10.5):
        w = len(text) * size * 0.56
        bx = x if anchor == "start" else x - w / 2.0
        for _ in range(12):
            if not any(bx < o[0] + o[2] and o[0] < bx + w and y - size < o[1] and o[1] - size < y for o in inline):
                break
            y += size + 2
        inline.append((bx, y, w))
        tags.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#ffffff" fill-opacity="0.8"/>'
                 % (bx - 2, y - size, w + 4, size + 3))
        tags.append('<text x="%.1f" y="%.1f" font-size="%s" fill="%s" text-anchor="%s" font-weight="bold">%s</text>'
                     % (x, y, size, color, anchor, esc(text)))

    # the opening range band
    n_or = min(tf["n_or"], z) - a
    if n_or > 0 and tf["orh"] is not None:
        s.append('<rect x="%d" y="%.1f" width="%.1f" height="%.1f" fill="%s" fill-opacity="0.07"/>'
                 % (left, Y(tf["orh"]), n_or * cw, Y(tf["orl"]) - Y(tf["orh"]), COL["orb"]))
    # fair value gaps and their inversions
    for g in pick_fvgs(tf, res, a, z, trig):
        x0 = X(g["i"]) - cw / 2
        end = g["inverted"] if g["inverted"] is not None else (g["filled"] if g["filled"] is not None else z - 1)
        end = min(end, z - 1)
        color = COL["bull"] if g["dir"] == "bull" else COL["bear"]
        y0, y1 = Y(g["top"]), Y(g["bot"])
        s.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" fill-opacity="0.18" stroke="%s" '
                 'stroke-opacity="0.6"/>' % (x0, y0, max(X(end) + cw / 2 - x0, cw), max(y1 - y0, 1), color, color))
        state = " filled" if g["filled"] is not None and g["inverted"] is None else ""
        tag(x0 + 2, y0 - 3, "%s FVG %.2f-%.2f%s" % ("Bull" if g["dir"] == "bull" else "Bear", g["bot"], g["top"], state),
            color)
        if g["inverted"] is not None and g["inverted"] < z:
            xi = X(g["inverted"]) - cw / 2
            ie = min(g["inv_end"] if g["inv_end"] is not None else z - 1, z - 1)
            icol = COL["ibull"] if g["dir"] == "bear" else COL["ibear"]
            s.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" fill-opacity="0.22" stroke="%s" '
                     'stroke-dasharray="4 3"/>' % (xi, y0, max(X(ie) + cw / 2 - xi, cw), max(y1 - y0, 1), icol, icol))
            tag(xi + 2, y1 + 12, "%s iFVG %.2f-%.2f" % ("Bull" if g["dir"] == "bear" else "Bear", g["bot"], g["top"]), icol)
    # liquidity
    for p in pick_pools(tf, a, z, trig):
        x0 = X(p["i"])
        y = Y(p["price"])
        eq = (" EQH" if p["kind"] == "high" else " EQL") if p["equal"] else ""
        if p["taken"] is not None and p["taken"] < z:
            x1 = X(p["taken"])
            s.append('<line x1="%.1f" x2="%.1f" y1="%.1f" y2="%.1f" stroke="%s" stroke-dasharray="2 3"/>'
                     % (x0, x1, y, y, COL["bsl"]))
            word = "swept" if p["swept"] else "taken"
            tag(x1 + 3, y + (-4 if p["kind"] == "high" else 13),
                "%s%s %s %.2f" % (p["side"], eq, word, p["price"]), COL["bsl"], size=9.5)
        else:
            s.append('<line x1="%.1f" x2="%d" y1="%.1f" y2="%.1f" stroke="%s" stroke-dasharray="2 3"/>'
                     % (x0, right, y, y, COL["bsl"]))
            labels.append({"y": y, "text": "%s%s liquidity %.2f (swing %s)" % (
                p["side"], eq, p["price"], hm(tf["bars"][p["i"]]["et"])), "color": COL["bsl"]})
    # structure
    for e in tf["structure"]:
        if not (a <= e["at"] < z):
            continue
        x0, x1 = X(max(e["from"], a)), X(e["at"])
        y = Y(e["price"])
        col = COL["mss"] if e["kind"] == "MSS" else COL["bos"]
        s.append('<line x1="%.1f" x2="%.1f" y1="%.1f" y2="%.1f" stroke="%s" stroke-width="1.6"/>' % (x0, x1, y, y, col))
        tag((x0 + x1) / 2.0, y + (-4 if e["dir"] == "up" else 13),
            "%s %s %.2f" % (e["kind"], "up" if e["dir"] == "up" else "down", e["price"]), col, anchor="middle")
    # candles
    body_w = max(1.0, cw * 0.62)
    vmax = max(b["v"] for b in bars) or 1
    vtop = top + ph + 12
    for i in range(a, z):
        b = bars_all[i]
        x = X(i)
        col = COL["up"] if b["c"] >= b["o"] else COL["down"]
        s.append('<line x1="%.1f" x2="%.1f" y1="%.1f" y2="%.1f" stroke="%s"/>' % (x, x, Y(b["h"]), Y(b["l"]), col))
        yt, yb = Y(max(b["o"], b["c"])), Y(min(b["o"], b["c"]))
        s.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"/>'
                 % (x - body_w / 2, yt, body_w, max(yb - yt, 0.8), col))
        vh_i = vh * b["v"] / float(vmax)
        s.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" fill-opacity="0.45"/>'
                 % (x - body_w / 2, vtop + vh - vh_i, body_w, vh_i, col))
    s.append('<text x="%d" y="%d" font-size="10" fill="%s">volume</text>' % (left + 4, vtop + 10, COL["muted"]))
    # the day's levels
    hline(tf["orh"], COL["orb"], "ORB High %.2f" % tf["orh"], width=1.6)
    hline(tf["orl"], COL["orb"], "ORB Low %.2f" % tf["orl"], width=1.6)
    for nm, v in (("PDH", tf["pdh"]), ("PDL", tf["pdl"])):
        if v is not None and (nm, v) not in off:
            hline(v, COL["pd"], "%s %.2f (%s)" % (nm, v, tf["prev_date"]), dash="7 4", width=1.4)
    for i, (nm, v) in enumerate(off):
        y = top + 14 if v > hi else top + ph - 6
        s.append('<text x="%d" y="%.1f" font-size="11" font-weight="bold" fill="%s" text-anchor="end">%s %.2f (%s, off chart %s)</text>'
                 % (right - 6, y, COL["pd"], nm, v, tf["prev_date"], "above" if v > hi else "below"))
    # the plan
    if setup and trig is not None and a <= trig < z:
        xe = X(trig)
        s.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" fill-opacity="0.07"/>'
                 % (xe, Y(setup["entry"]), right - xe, Y(setup["stop"]) - Y(setup["entry"]), COL["stop"]))
        s.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" fill-opacity="0.07"/>'
                 % (xe, Y(setup["tp2"]), right - xe, Y(setup["entry"]) - Y(setup["tp2"]), COL["tp"]))
        hline(setup["entry"], COL["entry"], "Entry %.2f (%s close)" % (setup["entry"], setup["entry_time"]), x0=xe, width=1.8)
        hline(setup["stop"], COL["stop"], "Stop %.2f (-1R, %s)" % (setup["stop"], setup["stop_rule"].split(",")[0]),
              x0=xe, width=1.8)
        hline(setup["tp1"], COL["tp"], "TP1 %.2f (+1R)" % setup["tp1"], x0=xe, width=1.8)
        hline(setup["tp2"], COL["tp"], "TP2 %.2f (+2R)" % setup["tp2"], x0=xe, width=1.8)
        yb = Y(bars_all[trig]["l"]) + 6
        s.append('<path d="M %.1f %.1f l -6 11 l 12 0 z" fill="%s"/>' % (xe, yb, COL["entry"]))
        tag(xe, yb + 24, "ENTRY", COL["entry"], anchor="middle")
    s.extend(tags)                      # inline names on top of the candles
    # the label column, every level named at its price
    for it in spread(labels, top + 6, top + ph - 2, 14):
        s.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="%s" stroke-width="0.8"/>'
                 % (right, right + 10, it["y"], it["ly"] - 4, it["color"]))
        s.append('<text x="%d" y="%.1f" font-size="11.5" font-weight="bold" fill="%s">%s</text>'
                 % (right + 13, it["ly"], it["color"], esc(it["text"])))
    y = top + ph + vh + 56
    for line in foot_lines:
        s.append('<text x="%d" y="%d" font-size="12.5" fill="%s">%s</text>' % (left, y, COL["text"], esc(line)))
        y += 20
    s.append("</svg>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(s) + "\n")


def pick_fvgs(tf, res, a, z, trig):
    """The gaps worth drawing, at most six: the ones around the entry and the inversions
    up to it first, then the gaps still open at the right edge nearest the last price."""
    last = tf["bars"][z - 1]["c"]
    near_w = max(3, 900 // tf["step"])
    ranked = []
    for g in tf["fvgs"]:
        if not (a <= g["i"] < z - 1):
            continue
        if trig is not None and abs(g["i"] - trig) <= near_w:
            rank = 0
        elif g["inverted"] is not None and (trig is None or g["inverted"] <= trig) and g["inverted"] < z:
            rank = 1
        elif g["filled"] is None or g["filled"] >= z:
            rank = 2
        else:
            continue
        ranked.append((rank, abs((g["top"] + g["bot"]) / 2 - last), g["i"], g))
    ranked.sort(key=lambda r: r[:3])
    return sorted([r[3] for r in ranked[:6]], key=lambda g: g["i"])


def pick_pools(tf, a, z, trig):
    """Untaken pools nearest the last price (3 a side) and the sweeps up to the entry."""
    last = tf["bars"][z - 1]["c"]
    live = [p for p in tf["pools"] if a <= p["i"] < z and (p["taken"] is None or p["taken"] >= z)]
    above = sorted([p for p in live if p["side"] == "BSL"], key=lambda p: abs(p["price"] - last))[:3]
    below = sorted([p for p in live if p["side"] == "SSL"], key=lambda p: abs(p["price"] - last))[:3]
    cut = trig if trig is not None else z
    swept = [p for p in tf["pools"] if p["swept"] and p["taken"] is not None and a <= p["i"] and p["taken"] <= cut]
    return above + below + swept[-3:]


def footer_lines(res, interval):
    out = []
    st = res.get("setup")
    if st:
        out.append("Long %s: entry %.2f at %s, stop %.2f (%s), risk %.2f a share, TP1 %.2f (1R), TP2 %.2f (2R)."
                   % (st["kind"], st["entry"], st["entry_time"], st["stop"], st["stop_rule"], st["risk"], st["tp1"], st["tp2"]))
        c = res["confluence"]
        out.append("ICT confluence %d of 4 (%s): %s" % (c["count"], c["verdict"], "; ".join(
            "%s yes" % k for k in ("fvg", "structure", "sweep", "ifvg") if k in c["ict_checks"]) or "none"))
        out.append("Room to TP1: %s" % ("clear" if c["room_to_tp1"] else "; ".join(c["blockers"])))
    else:
        out.append("No long setup: %s." % "; ".join(res["notes"]))
    out.append("Levels from Yahoo %s bars, regular hours, New York time. PDH/PDL from %s. Plan on %s bars."
               % (interval, res.get("prev_date") or "n/a", res["plan_interval"]))
    return out


# ---------------------------------------------------------------- words

def explain(res):
    lines = []
    st = res.get("setup")
    lines.append("%s %s: %s" % (res["symbol"], res["date"], {
        "triggered": "ORB long, grade %s%s" % (res.get("grade"), " (high quality)" if res.get("high_quality") else ""),
        "no-trigger": "no ORB long", "waiting": "waiting for an ORB break", "forming": "opening range still forming", "no-plan": "no plan"}.get(res["status"], res["status"])))
    lines.append("ORB %.2f-%.2f; PDH %s / PDL %s (%s); last close %.2f at %s."
                 % (res["orb_low"], res["orb_high"], "%.2f" % res["pdh"] if res["pdh"] is not None else "n/a",
                    "%.2f" % res["pdl"] if res["pdl"] is not None else "n/a", res.get("prev_date") or "no prior session",
                    res["last_close"], res["last_bar"]))
    if st:
        lines.append("The %s %s bar closed at %.2f, %.2f%% over the ORB High. Entry %.2f, stop %.2f (%s), "
                     "risk %.2f a share, TP1 %.2f, TP2 %.2f."
                     % (res["plan_interval"], st["trigger_bar"], st["entry"], st["pct_over_orh"], st["entry"],
                        st["stop"], st["stop_rule"], st["risk"], st["tp1"], st["tp2"]))
        c = res["confluence"]
        names = {"fvg": "displacement FVG", "structure": "MSS/BOS", "sweep": "liquidity sweep", "ifvg": "bullish iFVG"}
        for k in ("fvg", "structure", "sweep", "ifvg"):
            lines.append("  [%s] %s%s" % ("x" if k in c["ict_checks"] else " ", names[k],
                                         ": " + "; ".join(c["ict_checks"][k]) if k in c["ict_checks"] else ""))
        lines.append("  [%s] room to TP1%s" % ("x" if c["room_to_tp1"] else " ",
                                               "" if c["room_to_tp1"] else ": " + "; ".join(c["blockers"])))
        lines.append("Confluence: %s. %s" % (c["verdict"], {
            "yes": "The ORB break and the ICT read agree.",
            "partial": "One ICT signal backs the ORB break; the rest do not.",
            "no": "The ORB broke, but no ICT signal backs it."}[c["verdict"]]
            + ("" if c["room_to_tp1"] else " But liquidity sits under TP1, so the grade drops.")))
        if res.get("after"):
            a = res["after"]
            what = ("stopped at %s" % a["stop"]) if a["stop"] else ("TP2 at %s" % a["tp2"]) if a["tp2"] else \
                ("TP1 at %s, then neither TP2 nor the stop by %s" % (a["tp1"], a["last_bar"])) if a["tp1"] else \
                "neither the stop nor TP1 by %s" % a["last_bar"]
            if a["stop"] and a["tp1"]:
                what = "TP1 at %s, then stopped at %s" % (a["tp1"], a["stop"])
            lines.append("Hindsight (bars after the entry, not a signal): %s." % what)
    for n in res["notes"]:
        lines.append("Note: " + n)
    lines.append(NOTE)
    return lines


def write_outputs(res, tfs, out_dir, intervals):
    os.makedirs(out_dir, exist_ok=True)
    base = "%s-%s" % (res["symbol"], res["date"])
    files = []
    for name in intervals:
        tf = tfs.get(name)
        if not tf:
            continue
        n = len(tf["bars"])
        z = n
        st = res.get("setup")
        if name == "5m" and st:                # the open through the trade's end, not the whole afternoon
            trig = index_at(tf["bars"], datetime.datetime.combine(tf["bars"][0]["et"].date(),
                                                                  parse_hhmm(st["entry_time"], "entry")))
            a_ = res.get("after") or {}
            done = a_.get("stop") or a_.get("tp2")
            if done:
                k = index_at(tf["bars"], datetime.datetime.combine(tf["bars"][0]["et"].date(), parse_hhmm(done, "end")))
                z = min(n, k + 7)
            else:
                z = min(n, trig + 31)
        if name == "1m":                      # 1m zooms on the open and the hour after the entry
            end_t = None
            if res.get("setup"):
                end_t = datetime.datetime.combine(tf["bars"][0]["et"].date(),
                                                  parse_hhmm(res["setup"]["entry_time"], "entry")) + datetime.timedelta(minutes=60)
            else:
                end_t = tf["bars"][0]["et"] + datetime.timedelta(minutes=90)
            z = max(i + 1 for i, b in enumerate(tf["bars"]) if b["et"] < end_t)
        path = os.path.join(out_dir, "%s-%s.svg" % (base, name))
        render(res, tf, name, (0, z), path)
        files.append(path)
    doc = dict(res, charts=files)
    with open(os.path.join(out_dir, base + ".json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    md = ["# %s %s" % (res["symbol"], res["date"]), ""]
    for f in files:
        md += ["![%s](%s)" % (os.path.basename(f), os.path.basename(f)), ""]
    md += explain(res)
    with open(os.path.join(out_dir, base + ".md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")
    return files + [os.path.join(out_dir, base + ".json"), os.path.join(out_dir, base + ".md")]


# ---------------------------------------------------------------- CLI

def read_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        for k in DEFAULTS:
            if doc.get(k):
                cfg[k] = doc[k]
    return cfg


def run_one(sym, args, cfg, today, now):
    day = datetime.date.fromisoformat(args.date) if args.date else to_eastern(now).date()
    want = ["5m", "1m"] if args.interval == "both" else [args.interval]
    charts = {}
    for name in want:
        try:
            charts[name] = get_chart(sym, name, day, today, args.offline)
        except ChartError as e:
            if name == "1m" and len(want) > 1:
                charts[name] = None
                print("note: no 1m chart (%s)" % e, file=sys.stderr)
            else:
                raise
    # only closed bars count: cut at --now, or at the clock for live data; a saved file or a past day is whole
    cut = now if args.now or (not args.offline and day >= today) else None
    opts = {"or_minutes": args.or_minutes, "min_fvg_pct": args.min_fvg_pct, "eq_pct": args.eq_pct,
            "swing_5m": args.swing, "swing_1m": args.swing_1m, "stop": args.stop, "plan": args.plan,
            "now": cut}
    return analyze(sym.upper(), charts, day, cfg, opts)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Setup Charts (trader-chart): annotated ORB + ICT charts. Never places orders.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "watchlist"):
        p = sub.add_parser(name)
        if name == "check":
            p.add_argument("symbols", nargs="+")
        else:
            p.add_argument("--file", default=os.path.join("trader", "watchlist.json"))
            p.add_argument("--all", action="store_true", help="chart C grades too")
        p.add_argument("--date", help="session date YYYY-MM-DD (default today, New York)")
        p.add_argument("--now", help='New York time to replay up to, e.g. "2026-10-19 10:20"')
        p.add_argument("--interval", choices=["5m", "1m", "both"], default="both")
        p.add_argument("--plan", choices=["5m", "1m"], default="5m", help="timeframe the entry is taken on")
        p.add_argument("--or-minutes", type=int, default=15)
        p.add_argument("--stop", choices=["or-mid", "or-low", "fvg", "swing"], default="or-mid")
        p.add_argument("--min-fvg-pct", type=float, default=0.03)
        p.add_argument("--eq-pct", type=float, default=0.03)
        p.add_argument("--swing", type=int, default=2, help="bars each side for a 5m swing")
        p.add_argument("--swing-1m", type=int, default=3, help="bars each side for a 1m swing")
        p.add_argument("--config", default=os.path.join("trader", "config.json"))
        p.add_argument("--offline")
        p.add_argument("--out", default=OUT_DIR)
        p.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        if (args.or_minutes * 60) % 300:
            raise ChartError("--or-minutes must be a multiple of 5")
        cfg = read_config(args.config)
        now = parse_now(args.now)
        today = to_eastern(utc_now()).date()
        if args.cmd == "check":
            syms = [s.upper() for s in args.symbols]
        else:
            with open(args.file, encoding="utf-8") as fh:
                syms = [h["symbol"].upper() for h in json.load(fh).get("hits", []) if h.get("symbol")]
            if not syms:
                raise ChartError("%s has no hits: run /trader-scanner first" % args.file)
    except (ChartError, OSError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    good, results = 0, []
    for sym in syms:
        try:
            res, tfs = run_one(sym, args, cfg, today, now)
        except ChartError as e:
            print("%s: %s" % (sym, e), file=sys.stderr)
            continue
        draw = args.cmd == "check" or res["high_quality"] or getattr(args, "all", False)
        if draw:
            res["files"] = write_outputs(res, tfs, args.out, sorted(tfs, reverse=True))
        good += 1 if res["high_quality"] else 0
        results.append(res)
        if not args.json:
            print("\n".join(explain(res)))
            for f in res.get("files", []):
                print("  wrote " + f)
            print("")
    if args.json:
        print(json.dumps(results, indent=2))
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
