import contextlib
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest

from _load import FIX, load

C = load("trader-chart", "chart")
TSLA = os.path.join(FIX, "trader-timemachine", "TSLA-60d5m.json.gz")
DEMO = os.path.join(FIX, "trader-entry", "DEMO-1m-synthetic.json")


def bars(rows, start="2026-10-19 09:30", step=300):
    """[(o, h, l, c), ...] -> session bars on a step grid, New York time."""
    t0 = datetime.datetime.fromisoformat(start)
    out = []
    for i, (o, h, l, c) in enumerate(rows):
        et = t0 + datetime.timedelta(seconds=i * step)
        out.append({"t": C.ts_of(C.eastern_to_utc(et)), "o": o, "h": h, "l": l, "c": c, "v": 1000,
                    "et": et, "end": et + datetime.timedelta(seconds=step)})
    return out


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = C.main(argv)
    return code, out.getvalue(), err.getvalue()


class Levels(unittest.TestCase):
    def test_bull_fvg_and_its_inversion(self):
        b = bars([(10, 10.5, 9.9, 10.4), (10.4, 11.5, 10.3, 11.4), (11.4, 12, 11.0, 11.8),
                  (11.8, 11.9, 10.6, 10.7), (10.7, 10.8, 10.2, 10.3)])
        g = [x for x in C.find_fvgs(b, 0.01) if x["dir"] == "bull"]
        self.assertEqual(len(g), 1)
        self.assertEqual((g[0]["bot"], g[0]["top"]), (10.5, 11.0))
        self.assertEqual(g[0]["filled"], 4)        # the low 10.2 goes through 10.5
        self.assertEqual(g[0]["inverted"], 4)      # and the close 10.3 is under it: a bearish iFVG

    def test_bear_fvg_closed_over_is_a_bullish_ifvg(self):
        b = bars([(12, 12.1, 11.6, 11.7), (11.7, 11.7, 10.6, 10.7), (10.7, 11.0, 10.5, 10.9),
                  (10.9, 11.5, 10.8, 11.4), (11.4, 12.0, 11.3, 11.9)])
        g = [x for x in C.find_fvgs(b, 0.01) if x["dir"] == "bear"][0]
        self.assertEqual((g["bot"], g["top"]), (11.0, 11.6))
        self.assertEqual(g["inverted"], 4)         # 11.9 closes over 11.6

    def test_small_gaps_are_noise(self):
        b = bars([(10, 10.5, 9.9, 10.4), (10.4, 10.8, 10.3, 10.7), (10.7, 10.9, 10.505, 10.8)])
        self.assertEqual(C.find_fvgs(b, 0.01), [])

    def test_swings_sweeps_and_structure(self):
        rows = [(10, 10.2, 9.8, 10.0), (10.0, 10.6, 9.9, 10.5), (10.5, 11.0, 10.4, 10.8),   # swing high 11.0
                (10.8, 10.9, 10.2, 10.3), (10.3, 10.4, 9.5, 9.6), (9.6, 9.7, 9.0, 9.2),      # swing low 9.0
                (9.2, 9.8, 9.1, 9.7), (9.7, 9.9, 9.3, 9.8), (9.8, 10.9, 9.7, 10.8),
                (10.8, 11.2, 10.7, 11.1)]                                                    # closes over 11.0
        b = bars(rows)
        sw = C.find_swings(b, 2)
        self.assertIn(("high", 2, 11.0), [(s["kind"], s["i"], s["price"]) for s in sw])
        self.assertIn(("low", 5, 9.0), [(s["kind"], s["i"], s["price"]) for s in sw])
        ev = C.find_structure(b, sw)
        self.assertEqual([(e["kind"], e["dir"], e["price"], e["at"]) for e in ev], [("BOS", "up", 11.0, 9)])
        pools = C.liquidity(b, sw, 0.03)
        high = [p for p in pools if p["side"] == "BSL" and p["price"] == 11.0][0]
        self.assertEqual((high["taken"], high["swept"]), (9, False))   # closed through: taken, not swept

    def test_mss_is_a_break_against_the_last_one(self):
        rows = [(10, 10.2, 9.8, 10.0), (10.0, 10.6, 9.9, 10.5), (10.5, 11.0, 10.4, 10.8),
                (10.8, 10.9, 10.2, 10.3), (10.3, 10.4, 9.5, 9.6), (9.6, 9.7, 9.0, 9.2),
                (9.2, 9.8, 9.1, 9.7), (9.7, 9.9, 9.3, 9.8), (9.8, 9.85, 9.4, 9.5),
                (9.5, 9.6, 8.7, 8.8), (8.8, 9.0, 8.6, 8.9), (8.9, 11.3, 8.9, 11.2)]
        b = bars(rows)
        ev = C.find_structure(b, C.find_swings(b, 2))
        self.assertEqual([(e["kind"], e["dir"]) for e in ev][:2], [("BOS", "down"), ("MSS", "up")])

    def test_prices_snap_to_the_cent(self):
        self.assertEqual(C.tick(361.8799133300781), 361.88)
        self.assertEqual(C.tick(0.123456), 0.1235)
        self.assertEqual(C.floor_cents(358.325), 358.32)


class Reel(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def check(self, *extra):
        return run(["check", "TSLA", "--offline", TSLA, "--config", os.path.join(self.dir, "none.json"),
                    "--out", self.dir] + list(extra))

    def test_the_reel_trade_matches_entry_and_exit(self):
        code, out, _ = self.check("--date", "2026-10-19", "--json")
        self.assertEqual(code, 0)
        res = json.loads(out)[0]
        self.assertEqual((res["orb_low"], res["orb_high"]), (354.47, 362.18))
        self.assertEqual((res["pdh"], res["pdl"], res["prev_date"]), (370.58, 349.33, "2026-10-16"))
        st = res["setup"]
        self.assertEqual((st["entry_time"], st["entry"], st["stop"], st["tp1"], st["tp2"]),
                         ("10:15", 364.18, 358.32, 370.04, 375.90))   # trader-entry and trader-exit agree
        self.assertEqual(res["grade"], "A")
        self.assertIn("fvg", res["confluence"]["ict_checks"])
        self.assertIn("structure", res["confluence"]["ict_checks"])
        self.assertEqual(res["confluence"]["verdict"], "yes")

    def test_the_chart_labels_every_level(self):
        self.check("--date", "2026-10-19")
        with open(os.path.join(self.dir, "TSLA-2026-10-19-5m.svg"), encoding="utf-8") as fh:
            svg = fh.read()
        for text in ("ORB High 362.18", "ORB Low 354.47", "PDH 370.58", "PDL 349.33", "Entry 364.18",
                     "Stop 358.32", "TP1 370.04", "TP2 375.90", "Bull FVG 361.88-363.00", "BOS up 362.92",
                     "liquidity", "PAPER TRADING / ANALYSIS ONLY"):
            self.assertIn(text, svg, text)
        with open(os.path.join(self.dir, "TSLA-2026-10-19.md"), encoding="utf-8") as fh:
            md = fh.read()
        self.assertIn("![TSLA-2026-10-19-5m.svg](TSLA-2026-10-19-5m.svg)", md)
        self.assertIn("Confluence: yes", md)

    def test_only_closed_bars_count(self):
        code, out, _ = self.check("--date", "2026-10-19", "--now", "2026-10-19 10:12", "--json")
        res = json.loads(out)[0]
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "waiting")      # the 10:10 bar is still forming
        self.assertIsNone(res["setup"])

    def test_a_full_day_with_inversions(self):
        code, out, _ = self.check("--date", "2026-10-15", "--json")
        res = json.loads(out)[0]
        self.assertEqual(res["setup"]["entry"], 360.77)
        self.assertIn("ifvg", res["confluence"]["ict_checks"])
        self.assertEqual(res["after"]["tp1"], "11:10")

    def test_no_long_is_said_plainly(self):
        code, out, _ = self.check("--date", "2026-10-16", "--json")
        res = json.loads(out)[0]
        self.assertEqual((code, res["status"]), (1, "no-trigger"))
        self.assertTrue(any("short setup" in n for n in res["notes"]))

    def test_stop_rules(self):
        code, out, _ = self.check("--date", "2026-10-19", "--stop", "fvg", "--json")
        self.assertEqual(json.loads(out)[0]["setup"]["stop"], 361.87)    # a cent under the gap's 361.88
        code, out, _ = self.check("--date", "2026-10-19", "--stop", "or-low", "--json")
        self.assertEqual(json.loads(out)[0]["setup"]["stop"], 354.47)

    def test_one_minute_bars(self):
        code, out, _ = run(["check", "DEMO", "--offline", DEMO, "--interval", "1m", "--plan", "1m",
                            "--date", "2026-10-19", "--out", self.dir, "--config", "none", "--json"])
        res = json.loads(out)[0]
        self.assertEqual(res["plan_interval"], "1m")
        self.assertEqual(res["setup"]["entry"], 100.62)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "DEMO-2026-10-19-1m.svg")))

    def test_watchlist_charts_only_high_quality(self):
        wl = os.path.join(self.dir, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"hits": [{"symbol": "TSLA"}]}, fh)
        code, _, _ = run(["watchlist", "--file", wl, "--offline", TSLA, "--date", "2026-10-16",
                          "--out", self.dir, "--config", "none"])
        self.assertEqual(code, 1)
        self.assertFalse(any(f.endswith(".svg") for f in os.listdir(self.dir)))


if __name__ == "__main__":
    unittest.main()
