#!/usr/bin/env python3
"""Adversarial tests for the bold bot: fuzzing + failure scenarios. Run: python3 tests/test_bold.py [bot.py]"""
import copy
import importlib.util
import json
import math
import os
import random
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "bot.py")


def load():
    spec = importlib.util.spec_from_file_location(f"b{random.random()}", PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def obs(cash=10.0, vol=0.0, cyc=0, pos=None, secs=200, mid="m1", ts=1.79e9, yes=(0.95, 0.949), no=(0.13, 0.12), depth=1000):
    def side(a, b):
        return {"asks": [[a, depth]], "bids": [[b, depth]], "minOrderSize": 5}
    return {"schemaVersion": 1, "timestamp": ts, "market": {"id": mid, "secondsToClose": secs},
            "account": {"cashUsd": cash, "eligibleVolumeUsd": vol, "completedCycles": cyc, "position": pos},
            "rules": {"maximumBuyCashUsd": 5, "targetVolumeUsd": 1000},
            "books": {"YES": side(*yes), "NO": side(*no)}}


FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


def valid(out, o):
    if not isinstance(out, dict) or out.get("action") not in ("HOLD", "BUY", "SELL"):
        return False
    if out["action"] == "BUY":
        acct = o.get("account") if isinstance(o, dict) else None
        try:
            cash = float(acct["cashUsd"]); vol = float(acct.get("eligibleVolumeUsd") or 0)
        except Exception:
            return False  # BUY on an unreadable account
        x = out.get("maxCashUsd")
        return (out.get("outcome") in ("YES", "NO") and isinstance(x, float) and math.isfinite(x)
                and 0 < x <= min(5.0, cash) + 1e-12 and acct.get("position") is None and vol < 1000)
    return set(out) == {"action"}


def junk(r):
    return r.choice([None, "x", -1, 0, 1e308, float("nan"), float("inf"), [], {}, [[0.5]], [["a", "b"]], True, 0.999999])


def fuzz(n=30000):
    m = load()
    r = random.Random(7)
    bad = 0
    for i in range(n):
        o = obs(cash=r.choice([10, 0.5, 4.99, 5.0000004, 37, 0]), vol=r.choice([0, 300, 999.9, 1000, 1200]),
                secs=r.uniform(-5, 300), yes=(r.uniform(0.01, 0.999), r.uniform(0.0, 0.999)),
                no=(r.uniform(0.01, 0.999), r.uniform(0.0, 0.999)), depth=r.choice([0.1, 3, 5, 1000]), ts=1.79e9 + i)
        if r.random() < 0.3:  # corrupt a random field
            path = r.choice([("market", "secondsToClose"), ("account", "cashUsd"), ("books", "YES"), ("books", "NO", "asks"),
                             ("rules", "maximumBuyCashUsd"), ("account", "position"), ("timestamp",), ("market",), ("books",)])
            d = o
            for k in path[:-1]:
                d = d[k]
            d[path[-1]] = junk(r)
        if r.random() < 0.02:
            o = junk(r)
        out = m.decide(copy.deepcopy(o))
        if not valid(out, o):
            bad += 1
            if bad < 4:
                print("  bad:", out, json.dumps(o, default=str)[:300])
    check(f"fuzz {n} observations -> valid, capped, flat-only BUYs", bad == 0, f"{bad} bad")


def stale_state_file():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "s.json")
    json.dump({"bet_market": "old", "refueled": True, "goal": 35, "c0": 40, "v0": 0, "t_start": 1.7e9, "last": [800, 90, 1.7e9]}, open(p, "w"))
    m = load(); m.BOT.enable_persistence(p)
    out = m.decide(obs())  # brand-new run: $10, no volume
    check("stale state file from a previous run is discarded (bets, not churns)",
          out.get("action") == "BUY" and out.get("outcome") == "NO" and not m.BOT.refueled, out)


def reused_process():
    m = load()
    m.decide(obs(cash=40, vol=0)); m.decide(obs(cash=38, vol=300, cyc=30, ts=1.79e9 + 3600))
    out = m.decide(obs(cash=10, vol=0, cyc=0, ts=1.80e9))  # evaluator reuses the endpoint for a new window
    check("one process reused for a second run resets state", out.get("outcome") == "NO" and m.BOT.t_start == 1.80e9, out)


def rerefuel_oscillation():
    m = load(); b = m.BOT
    b._fresh = False
    b.refueled, b.bet_market, b.c0, b.v0, b.t_start = True, "old", 40.0, 0.0, 1.79e9
    # measured cost 3%/$ -> need >> cash, but cash >= 0.85 * (need + 3): old code flipped back to churn without betting
    o = obs(cash=20.0, vol=500, cyc=50, mid="m9", ts=1.79e9 + 7200)
    b.last = [500, 50, 1.79e9 + 7199]
    b.c0 = 20.0 + 0.02626 * 500  # need ~20.3 > cash 20 >= 0.85*(20.3+3)
    m.decide(o)
    st1 = (b.refueled, b.goal)
    o2 = obs(cash=20.0, vol=500, cyc=50, mid="m10", secs=400, ts=1.79e9 + 7210)
    m.decide(o2)
    check("re-refuel does not flip back to churn using an old bet", st1[0] is False and b.refueled is False, (st1, b.refueled, b.goal))


def restart_with_open_bet():
    m = load()
    pos = {"market_id": "m1", "outcome": "NO", "shares": 40, "buy_gross_usd": 4.9, "buy_fees_usd": 0.1, "sell_gross_usd": 0, "sell_fees_usd": 0}
    out = m.decide(obs(cash=5, vol=120, cyc=10, pos=pos, secs=150))
    check("restart without state keeps an open underdog bet (no dump)", out == {"action": "HOLD"}, out)
    pos2 = dict(pos, outcome="YES", shares=5.2)
    m2 = load()
    out2 = m2.decide(obs(cash=30, vol=400, cyc=40, pos=pos2, secs=150))
    check("restart without state still sells a churn favourite", out2 == {"action": "SELL"}, out2)


def thin_book():
    m = load(); m.BOT._fresh = False; m.BOT.refueled = True; m.BOT.c0, m.BOT.v0 = 40, 0
    out = m.decide(obs(cash=40, vol=10, cyc=1, depth=3))
    check("churn skips books too thin for the minimum order", out == {"action": "HOLD"}, out)
    o = obs(cash=40, vol=10, cyc=1); o["books"]["YES"]["minOrderSize"] = 10
    out = m.decide(o)
    check("churn respects a larger minOrderSize ($5 buys < 10 shares)", out == {"action": "HOLD"}, out)


def full_cycle():
    m = load()
    o = obs(); out = m.decide(o)
    check("fresh $10: bets an underdog toward the fuel goal", out.get("action") == "BUY" and out["outcome"] == "NO" and out["maxCashUsd"] <= 5, out)
    pos = {"market_id": "m1", "outcome": "NO", "shares": 80, "buy_gross_usd": 4.9, "buy_fees_usd": 0.1, "sell_gross_usd": 0, "sell_fees_usd": 0}
    check("holds the bet mid-market", m.decide(obs(cash=5, pos=pos, secs=150)) == {"action": "HOLD"})
    check("sells a clear winner just before close", m.decide(obs(cash=5, pos=pos, secs=8, no=(0.995, 0.99), yes=(0.02, 0.01))) == {"action": "SELL"})
    out = m.decide(obs(cash=39, vol=0, mid="m2", secs=250))
    check("after a win: churns the tight favourite", out.get("outcome") == "YES" and m.BOT.refueled, out)
    check("stays flat at target", m.decide(obs(cash=30, vol=1000.5, mid="m3")) == {"action": "HOLD"})


def http_and_latency():
    import subprocess, urllib.request
    port = 18000 + random.randint(0, 999)
    d = tempfile.mkdtemp()
    pr = subprocess.Popen([sys.executable, PATH], env=dict(os.environ, PORT=str(port), BOT_STATE=os.path.join(d, "s.json")),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/decide", b"{}", method="POST"), timeout=1); break
            except Exception:
                time.sleep(0.1)
        lat, outs = [], []
        for body in [json.dumps(obs()).encode(), b"not json", b"", json.dumps([1, 2]).encode()]:
            t = time.perf_counter()
            r = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/decide", body, method="POST",
                                                              headers={"Content-Type": "application/json"}), timeout=2)
            lat.append((time.perf_counter() - t) * 1000); outs.append(json.loads(r.read()))
        check("HTTP: valid JSON replies for good and garbage bodies", outs[0]["action"] == "BUY" and all(o == {"action": "HOLD"} for o in outs[1:]), outs)
        check(f"HTTP latency max {max(lat):.1f} ms < 50 ms", max(lat) < 50)
        check("state file written", os.path.exists(os.path.join(d, "s.json")))
    finally:
        pr.kill()


def opus_review_regressions():
    """Findings of the 2026-11-05 Opus review (each failed on the previous build)."""
    # 1. rejected re-refuel bet must not count as refuelled; churn in that market must still be sold
    m = load(); b = m.BOT; b._fresh = False
    b.refueled, b.goal, b.c0, b.v0, b.t_start, b.last = False, 23.3, None, None, 1.79e9, [500, 50, 1.79e9]
    out = m.decide(obs(cash=20.0, vol=500, cyc=50, mid="m5", secs=200, no=(0.55, 0.54), ts=1.79e9 + 5))
    rejected_flat = obs(cash=20.0, vol=500, cyc=50, mid="m5", secs=150, ts=1.79e9 + 10)  # BUY was rejected: still flat
    m.decide(rejected_flat)
    check("rejected bet does not count as refuel", out.get("action") == "BUY" and not b.refueled, (out, b.refueled))
    pos = {"market_id": "m5", "outcome": "YES", "shares": 5.2, "buy_gross_usd": 4.94, "buy_fees_usd": 0.06, "sell_gross_usd": 0, "sell_fees_usd": 0}
    check("churn-priced position in the bet market is sold", m.decide(obs(cash=15, vol=500, cyc=50, pos=pos, mid="m5", secs=120, ts=1.79e9 + 20)) == {"action": "SELL"})
    # 2. position ids in another format than market ids
    m = load(); m.BOT._fresh = False; m.BOT.refueled, m.BOT.c0, m.BOT.v0, m.BOT.t_start = True, 40, 0, 1.79e9
    out = m.decide(obs(cash=40, vol=10, cyc=1, mid="btc-5m-1"))
    pos = {"market_id": "0xCONDITION", "outcome": "YES", "shares": 5.2, "buy_gross_usd": 4.94, "buy_fees_usd": 0.06, "sell_gross_usd": 0, "sell_fees_usd": 0}
    check("position id format differs from market id: churn still sold", out.get("action") == "BUY" and m.decide(obs(cash=35, vol=10, cyc=1, pos=pos, mid="btc-5m-1")) == {"action": "SELL"})
    check("...but a position from an earlier market is left to settle", m.decide(obs(cash=35, vol=10, cyc=1, pos=pos, mid="btc-5m-2")) == {"action": "HOLD"})
    # 3. restart without state during the first refuel (vol 0) keeps the bet
    m = load()
    pos = {"market_id": "m1", "outcome": "NO", "shares": 38, "buy_gross_usd": 4.94, "buy_fees_usd": 0.06, "sell_gross_usd": 0, "sell_fees_usd": 0}
    check("restart at vol 0 keeps an open underdog bet", m.decide(obs(cash=5, vol=0, pos=pos, secs=150, no=(0.8, 0.79))) == {"action": "HOLD"})
    # 5. corrupt / foreign state files are ignored
    ok = True
    for junk_state in ("5", "null", '{"last":[1,2]}', '{"goal":null}', '{"refueled":"yes"}', "[1,2,3]", "{bad json"):
        d = tempfile.mkdtemp(); p = os.path.join(d, "s.json"); open(p, "w").write(junk_state)
        m = load(); m.BOT.enable_persistence(p)
        out = m.decide(obs())
        ok &= out.get("action") == "BUY"
    check("corrupt state files are ignored (bot still bets)", ok)
    # 6. null fields
    m = load(); o = obs(); o["timestamp"] = None; o["rules"] = None; o["books"]["YES"] = None
    out = m.decide(o)
    check("null timestamp/rules/YES book: still bets NO", out.get("action") == "BUY" and out.get("outcome") == "NO", out)
    # 7. endgame: one cycle finishes, do not gamble
    m = load(); b = m.BOT; b._fresh = False; b.refueled, b.c0, b.v0, b.t_start, b.last = True, 20.0, 0.0, 1.79e9, [995, 99, 1.79e9]
    out = m.decide(obs(cash=5.15, vol=995, cyc=99, ts=1.79e9 + 60))
    check("endgame: last cycle churns instead of re-betting", out.get("outcome") == "YES" and b.refueled, (out, b.refueled))
    # 8. a new run at $10/0/0 soon after a refuelled run resets
    m = load(); b = m.BOT; b._fresh = False; b.refueled, b.c0, b.v0, b.t_start, b.last = True, 36.0, 0.0, 1.79e9, [0, 0, 1.79e9]
    out = m.decide(obs(cash=10, vol=0, cyc=0, ts=1.79e9 + 600))
    check("new run at $10 flat after a refuelled run resets to fuel_goal", out.get("outcome") == "NO" and b.goal == m.CFG["fuel_goal"], (out, b.goal))


def http_chunked():
    import socket, subprocess
    port = 19000 + random.randint(0, 999)
    d = tempfile.mkdtemp()
    pr = subprocess.Popen([sys.executable, PATH], env=dict(os.environ, PORT=str(port), BOT_STATE=os.path.join(d, "s.json")),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                c = socket.create_connection(("127.0.0.1", port), timeout=1); break
            except OSError:
                time.sleep(0.1)
        body = json.dumps(obs()).encode()
        chunks = b"".join(b"%x\r\n%s\r\n" % (len(body[i:i + 100]), body[i:i + 100]) for i in range(0, len(body), 100)) + b"0\r\n\r\n"
        req = b"POST /decide HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\nContent-Type: application/json\r\n\r\n" + chunks
        c.sendall(req + req)  # two chunked requests on one keep-alive connection
        time.sleep(0.5)
        data = c.recv(65536).decode()
        n_buy = data.count('"BUY"')
        check("HTTP chunked bodies parsed, keep-alive survives (2 replies)", data.count("HTTP/1.1 200") == 2 and n_buy >= 1, data[:300])
    finally:
        pr.kill()


if __name__ == "__main__":
    for t in (fuzz, stale_state_file, reused_process, rerefuel_oscillation, restart_with_open_bet, thin_book, full_cycle, http_and_latency, opus_review_regressions, http_chunked):
        t()
    print(f"\n{len(FAILS)} failed" if FAILS else "\nALL PASS")
    sys.exit(1 if FAILS else 0)
