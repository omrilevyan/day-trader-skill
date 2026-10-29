---
name: trader-chart
description: >-
  Setup Charts. Draws an annotated candlestick chart for every high-quality
  opening-range breakout, on 5-minute bars and a 1-minute zoom, with every
  level computed from the bars: the 15-minute ORB High and Low, fair value
  gaps (FVG), inverse fair value gaps (iFVG), buy-side and sell-side
  liquidity (swing highs and lows, equal highs and lows, sweeps), the
  previous day's high and low (PDH / PDL), breaks of structure (BOS) and
  market structure shifts (MSS), the proposed entry, the stop, TP1 (1R) and
  TP2 (2R), each labeled on the chart. Grades the setup A, B or C on whether
  the ORB and the ICT read agree, and writes the explanation under the
  chart. Analysis only, never an order. Use when the user says "chart it",
  "show me the setup", "mark the FVGs", "draw the levels", "annotate the
  chart", "is there confluence", "ICT", "liquidity sweep", "market structure
  shift", or whenever /trader-entry triggers or /trader-scanner finds ORB hits.
argument-hint: "[TICKER ...] [--date YYYY-MM-DD] [--interval 5m|1m|both]"
---

# trader-chart

The Setup Charts skill puts one breakout on one picture. It reads the day's
bars, finds the opening range and the ICT levels around it, takes the entry
the way `/trader-entry` does (the first closed bar over the ORB High), sets
the stop the way `/trader-exit` does (the range midpoint by default), and
draws all of it, every line named with its price. Then it says in plain
words whether the ICT read backs the breakout. It never buys anything.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/chart.py" check TSLA                      # today: 5m chart + 1m zoom
python3 "${CLAUDE_SKILL_DIR}/chart.py" check TSLA NVDA --interval 5m
python3 "${CLAUDE_SKILL_DIR}/chart.py" check TSLA --date 2026-10-19    # a past session
python3 "${CLAUDE_SKILL_DIR}/chart.py" check TSLA --now "2026-10-19 10:20"   # replay to a moment
python3 "${CLAUDE_SKILL_DIR}/chart.py" watchlist                       # every scanner hit, charts A and B
python3 "${CLAUDE_SKILL_DIR}/chart.py" check TSLA --stop fvg           # stop under the breakout's FVG
```

Other flags: `--plan 1m` (take the entry on 1-minute bars), `--or-minutes N`
(default 15), `--stop or-mid|or-low|fvg|swing`, `--min-fvg-pct` (default
0.03 % of price), `--eq-pct` (equal highs / lows, default 0.03 %),
`--swing N` / `--swing-1m N` (bars each side of a swing, 2 and 3),
`--offline PATH` (a saved Yahoo chart JSON, or a folder of
`<TICKER>-5m.json` / `<TICKER>-1m.json`), `--json`, `--all` (watchlist: chart
C grades too).

## What it reads and writes

| file | what |
| --- | --- |
| Yahoo chart API | `range=5d`, 5-minute and 1-minute bars, regular hours: today plus the previous session for PDH / PDL |
| `trader/config.json` | `no_entries_before`, `no_entries_after`, `flat_by` (optional, same keys as the rest of the pack) |
| `trader/watchlist.json` | the scanner's hits, for `watchlist` |
| `trader/charts/<SYM>-<date>-5m.svg` | the full chart: the open through the end of the trade |
| `trader/charts/<SYM>-<date>-1m.svg` | the 1-minute zoom: the open to an hour after the entry |
| `trader/charts/<SYM>-<date>.md` | the charts with the explanation under them |
| `trader/charts/<SYM>-<date>.json` | every number on the chart, for the next skill |

## How each level is found

Nothing is typed in. Every line is a price from a bar.

- **ORB High / Low.** The high and low of the first 15 minutes (three 5m
  bars, fifteen 1m bars). Shaded at the open, drawn across the day.
- **PDH / PDL.** The high and low of the previous session's regular-hours
  bars. When one is far off the chart it is printed at the edge with its price.
- **FVG.** Three bars where the third bar's low is above the first bar's high
  (bull) or its high is under the first bar's low (bear). The box is the gap
  and runs until it is filled. Gaps under 0.03 % of price are noise.
- **iFVG.** An FVG that a later bar CLOSES through. A bear gap closed over
  turns into a bullish iFVG (support), a bull gap closed under into a
  bearish one. Dashed box from the close that inverted it.
- **Liquidity.** Swing highs are buy-side liquidity (BSL), swing lows
  sell-side (SSL): a bar whose high beats the 2 bars each side (3 on 1m).
  EQH / EQL when two sit within 0.03 %. "Swept" means a wick took the level
  and that bar closed back inside; "taken" means it closed through.
- **BOS / MSS.** A bar closes beyond the last confirmed swing. In the
  direction of the last break it is a BOS; against it, an MSS.
- **Entry.** The first closed bar after the range, inside the entry window,
  that closes above the ORB High. The entry is that close.
- **Stop.** The ORB midpoint, rounded down to the cent (`--stop` for the
  ORB Low, under the breakout's FVG, or under the last swing low).
- **TP1 / TP2.** Entry plus one and two times the risk.

## The grade

Four ICT checks, each with the bar that proves it, on the 5m and the 1m:

1. a bull FVG left by the breakout (displacement),
2. a bullish MSS or BOS into the entry,
3. sell-side liquidity swept before it (a swing low, the ORB Low or the PDL),
4. a bullish iFVG under the entry.

Plus room: no PDH and no untaken buy-side liquidity between the entry and TP1.

- **A**: two or more checks and room to TP1.
- **B**: one check and room, or three or more without room.
- **C**: the rest.

High quality means A or B. Confluence is "yes" at two or more checks,
"partial" at one, "no" at none.

## Steps

1. Get the tickers from the user, or run `chart.py watchlist` after
   `/trader-scanner orb`, or `chart.py check TICKER` after `/trader-entry`
   triggers.
2. For every A or B setup, show the chart: send the 5m SVG (and the 1m when
   there is one) to the user as a file. In a terminal, give the paths.
3. Under the chart, in a few lines: the setup (entry, stop, TP1, TP2, risk a
   share), which ICT checks passed and the bar that proves each, what is in
   the way of TP1, and the confluence verdict in one sentence. Say "paper
   trading / analysis only".
4. A C grade gets one line (why), not a chart, unless the user asks.
5. Hand off: `/trader-timemachine` for the odds, then `/trader-budget`. The
   chart is not a yes.

## Example: the reel

TSLA, Monday 2026-10-19, 5-minute bars:

```
TSLA 2026-10-19: ORB long, grade A (high quality)
ORB 354.47-362.18; PDH 370.58 / PDL 349.33 (2026-10-16); last close 366.68 at 10:50.
The 5m 10:10 bar closed at 364.18, 0.55% over the ORB High. Entry 364.18, stop 358.32 (ORB midpoint), risk 5.86 a share, TP1 370.04, TP2 375.90.
  [x] displacement FVG: 5m bull FVG 361.88-363.00 on the breakout (10:10 bar)
  [x] MSS/BOS: 5m bullish BOS over 362.92 at 10:15
  [ ] liquidity sweep
  [ ] bullish iFVG
  [x] room to TP1
Confluence: yes. The ORB break and the ICT read agree.
Note: PDH 370.58 sits between TP1 and TP2
```

## Rules

- Every level from the bars. Never draw a level that is not in the data,
  never move one after the fact, never round one the wrong way.
- Only closed bars count. Live, the chart stops at the last closed bar.
- Long side only, like the rest of v2. A break under the ORB Low is reported
  as a note, not planned.
- "Hindsight" lines (what happened after the entry on a past day) are a
  record, not a signal. Say so.
- Yahoo's free bars are unofficial and can lag. Say so when a minute matters.

## What it never does

It never places an order, never sizes a position, never calls
`/trader-broker`, and never says a grade is a reason to buy. An A grade is a
picture of agreement between two methods, not a promise.

## Safety

- PAPER TRADING / ANALYSIS ONLY. Education and tooling, not financial advice.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
