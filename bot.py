#!/usr/bin/env python3
"""Bold-refuel + churn volume bot for BTC 5-minute up/down binary markets.

Why: fees (taker 0.07*p*(1-p) + 1% overlay per side) cost >= ~$11 per $1,000 of volume, so $10 of
starting cash cannot fund $1,000 of volume at any fair price. BUYs are capped at $5, but payouts are not.
  Phase 1 (refuel): stake up to $5 on an underdog priced so that a win lifts cash to `fuel_goal`, hold
           to settlement (a win is sold just before close for extra eligible volume). Underdog asks
           measured ~fair on 7 weeks of archived books, so P(reach goal) ~ cash/goal minus fees.
  Phase 2 (churn): buy the favourite with the tightest book and sell it straight back, repeating until
           $1,000 of eligible volume, then stay flat. Cost ~= spread + fees, ~1.2-1.7% of volume.
Pure book logic: no BTC reference needed.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CFG = {
    "fuel_goal": 35.0,        # refuel until cash >= this (sweeps: 30-35 best; 35 most robust to worse fills)
    "bet_band": 0.06,         # buy the underdog when its ask is within [q_max - band, q_max]
    "bet_min_ask": 0.05,
    "bet_secs_lo": 100.0,     # place bets with this much time left ...
    "bet_secs_hi": 280.0,     # ... and no earlier than this
    "bet_sell_bid": 0.98,     # sell a winning bet at/after this bid near close (eligible volume)
    "bet_sell_secs": 12.0,
    "min_bet_cash": 0.6,      # below this nothing useful can be done
    "churn_min_ask": 0.95,
    "churn_max_ask": 0.995,
    "churn_max_spread": 0.002,      # spread allowed while on pace (tight books = cheap churn)
    "churn_behind_spread": 0.005,   # allowed when behind the volume schedule
    "churn_late_spread": 0.03,     # allowed when far behind / late in the window
    "deadline_hours": 20.0,         # plan to reach the target by then (window is 24 h)
    "late_margin": 0.15,            # "far behind" = this fraction of the target below schedule
    "churn_secs_lo": 20.0,    # do not open churn positions closer to the close than this
    "churn_cash_floor": 0.0,  # stop churning if cash would fall below this
    "goal_frac": 0.85,        # a bet that lands within this fraction of the goal counts as refuelled
    "rerefuel": True,         # bet again if measured churn cost says cash will not reach the target
    "cost_prior": 0.013,      # churn cost per $ of volume assumed before it is measured
    "cost_min_vol": 60.0,     # volume of churn needed before trusting the measured cost
    "cost_margin": 1.15,
    "reserve": 5.2,           # cash needed for the last 5-share buy
    "rebet_buffer": 3.0,      # re-bet goal = projected need + this
    "taker": 0.07,
    "overlay": 0.01,
    "target_volume": 1000.0,
    # experimental (neutral defaults)
    "lock_cash": 1e9,         # sell a refuel bet early once cash + its sale value >= this (locks in the refuel)
    "lock_secs_min": 15.0,    # ... only with at least this much time left (late wins use bet_sell_bid)
    "step_mult": 1e9,
    "dust_q": 0.10,
    "bet_avg_max": 0.9,       # a held position bought below this average price is a refuel bet (churn buys >= 0.95)
    "bet_max_spread": 1.0,
    "bet_q_max": 1.0,         # cap the bet price: bet cheaper underdogs with a smaller need-sized stake (1.0 = off)
    "bet_pick": "high",       # "high": priciest ask in the band; "low": cheapest (smaller need-sized stake, keeps a 2nd shot)    # skip underdog books wider than this (experimental; 1.0 = off)
    "adapt_spread": False,    # spread limits relative to recently observed tightest spreads (wide-book regimes)
    "adapt_add": [0.001, 0.003, 0.02],  # on-pace / behind / late: allowed spread above the recent tightest one
    "adapt_markets": 48,      # recent tightest = min of the per-market minimum over this many markets (~4 h):
                              # only ~6% of markets ever show a <=0.002 book (median gap 1 h, p90 3 h, max 7 h)
    "adapt_min_markets": 24,  # observe this many markets before adapting
    "adapt_stat": "min",      # "min" keeps patience for rare tight books ($4 at the crossing); "median" = typical            # >0: if the goal needs an ask below this, climb in steps (bet to what a full stake reaches here)         # ladder: each bet aims for min(goal, cash * step_mult) instead of the goal in one shot
}
CFG.update(json.loads(os.environ.get("BOLD_CFG", "{}")))


def _levels(rows: Any, *, reverse: bool) -> list[tuple[float, float]]:
    out = []
    for row in rows if isinstance(rows, list) else []:
        try:
            p, s = float(row[0]), float(row[1])
        except (IndexError, TypeError, ValueError):
            continue
        if 0 < p < 1 and s > 0 and math.isfinite(p) and math.isfinite(s):
            out.append((p, s))
    return sorted(out, reverse=reverse)


def _fee_rate(p: float) -> float:
    return CFG["taker"] * (1 - p) + CFG["overlay"]  # fee per $ of notional


def _now(obs: dict) -> float:
    try:
        t = float(obs.get("timestamp") or 0)
        return t if math.isfinite(t) and t > 0 else time.time()
    except (TypeError, ValueError):
        return time.time()


def _floor6(x: float) -> float:
    return math.floor(x * 1e6) / 1e6  # never ask for more than we have because of rounding


def _min_size(book: Any) -> float:
    try:
        m = float(book.get("minOrderSize", 5))
        return m if math.isfinite(m) and m > 0 else 5.0
    except (AttributeError, TypeError, ValueError):
        return 5.0


def _buy_fill(asks, cash):
    """Walk the asks with `cash` (fees included). Returns (shares, gross paid incl. fees)."""
    shares = paid = 0.0
    for p, s in asks:
        per = p * (1 + _fee_rate(p))
        n = min(s, (cash - paid) / per)
        if n <= 0:
            break
        shares += n
        paid += n * per
    return shares, paid


def _sell_value(bids, shares):
    """Net proceeds of selling `shares` into the bids, or None if the bids are too thin."""
    got = 0.0
    for p, s in bids:
        n = min(s, shares)
        got += n * p * (1 - _fee_rate(p))
        shares -= n
        if shares <= 1e-12:
            return got
    return None


class BoldBot:
    def __init__(self) -> None:
        self.bet_market: str | None = None   # market id of an open refuel bet
        self.refueled = False
        self.goal = CFG["fuel_goal"]
        self.c0 = self.v0 = None  # cash / volume when churn started (for measured cost)
        self.state_path: str | None = None  # set when serving: survive a process restart mid-run
        self._fresh = True
        self.t_start: float | None = None  # first observation ~ window start
        self.buy_mid: str | None = None      # market where we last emitted a BUY (position ids may use another format)
        self.bet_cash0: float | None = None  # cash just before the current refuel bet
        self.bet_seen = False                # the current refuel bet actually filled (we held it)
        self.last: list | None = None  # [volume, cycles, timestamp] of the previous call: detects a new run
        self._errors = 0
        self._saved_at = 0.0
        self.mkt_min: dict[str, float] = {}  # market id -> tightest eligible churn spread seen (not persisted)
        self.lock = threading.Lock()

    _KEYS = ("bet_market", "refueled", "goal", "c0", "v0", "t_start", "buy_mid", "bet_cash0", "bet_seen", "last")  # "last" must stay last
    _NUM = (int, float)
    _TYPES = {"bet_market": (str, type(None)), "refueled": (bool,), "goal": _NUM, "c0": _NUM + (type(None),),
              "v0": _NUM + (type(None),), "t_start": _NUM + (type(None),), "buy_mid": (str, type(None)),
              "bet_cash0": _NUM + (type(None),), "bet_seen": (bool,), "last": (list, type(None))}

    def _reset(self) -> None:
        self.bet_market, self.refueled, self.goal = None, False, CFG["fuel_goal"]
        self.c0 = self.v0 = self.t_start = self.last = self.buy_mid = self.bet_cash0 = None
        self.bet_seen = False
        self._fresh = True

    def enable_persistence(self, path: str) -> None:
        self.state_path = path
        try:
            with open(path) as f:
                st = json.load(f)
            ok = isinstance(st, dict) and all(isinstance(st[k], self._TYPES[k]) for k in self._KEYS if k in st)
            last = st.get("last") if ok else None
            if last is not None and not (len(last) == 3 and all(isinstance(x, self._NUM) for x in last)):
                ok = False
            if ok:  # corrupt / foreign / old-schema files are ignored entirely
                for k in self._KEYS:
                    if k in st:
                        setattr(self, k, st[k])
                self._fresh = False
        except Exception:  # noqa: BLE001
            pass

    def _save(self) -> None:
        if not self.state_path:
            return
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                    json.dump({k: getattr(self, k) for k in self._KEYS}, f)
            os.replace(tmp, self.state_path)
            self._saved_at = time.monotonic()
        except OSError:
            pass

    def decide(self, obs: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            try:
                out = self._decide_locked(obs)
                self._errors = 0
                return out
            except Exception:  # noqa: BLE001 - never fail a request
                self._errors += 1
                if self._errors >= 50:  # persistent failure: drop state rather than HOLD forever
                    self._reset()
                    self._errors = 0
                return {"action": "HOLD"}

    def _decide_locked(self, obs: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(obs, dict):
            return {"action": "HOLD"}
        acct = obs.get("account") or {}
        vol = float(acct.get("eligibleVolumeUsd", 0) or 0)
        cyc = int(acct.get("completedCycles", 0) or 0)
        now = _now(obs)
        if self.last is not None:  # a new run (stale state file, or one process reused for several runs)?
            lv, lc, lt = self.last
            fresh_acct = vol == 0 and cyc == 0 and acct.get("position") is None and abs(float(acct.get("cashUsd", 0) or 0) - 10.0) < 1e-6
            if vol < lv - 1e-6 or cyc < lc or (now and lt and abs(now - lt) > 3 * 3600) or (fresh_acct and self.refueled):
                self._reset()
        before = tuple(getattr(self, k) for k in self._KEYS[:-1])
        if self._fresh:
            self._fresh = False
            if vol > 0:  # restarted mid-run without saved state: refuel is done
                self.refueled = True
                self.c0, self.v0 = float(acct["cashUsd"]), vol
                if now > 0:  # unknown start time: assume we are just behind schedule (safer than "on pace")
                    frac = min(1.0, vol / CFG["target_volume"] + CFG["late_margin"])
                    self.t_start = now - frac * CFG["deadline_hours"] * 3600
        self.last = [vol, cyc, now]
        out = self._decide(obs)
        if out.get("action") == "BUY":  # last line of defence: never emit an invalid BUY
            x = out.get("maxCashUsd")
            cash = float(acct.get("cashUsd", 0) or 0)
            if not (isinstance(x, float) and math.isfinite(x) and 0 < x <= min(5.0, cash) and out.get("outcome") in ("YES", "NO")):
                out = {"action": "HOLD"}
        if out.get("action") == "BUY":
            self.buy_mid = str((obs.get("market") or {}).get("id"))
        if tuple(getattr(self, k) for k in self._KEYS[:-1]) != before or time.monotonic() - self._saved_at > 10:
            self._save()
        return out

    def _decide(self, obs: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(obs, dict):
            return {"action": "HOLD"}
        mkt, acct, rules = obs.get("market") or {}, obs.get("account") or {}, obs.get("rules") or {}
        raw = obs.get("books") or {}
        books = {sd: raw.get(sd) if isinstance(raw.get(sd), dict) else {} for sd in ("YES", "NO")}
        now = _now(obs)
        if self.t_start is None and now > 0:
            self.t_start = now
        self.elapsed_h = (now - self.t_start) / 3600 if self.t_start else 0.0
        secs = float(mkt["secondsToClose"])
        mid = str(mkt["id"])
        cash = float(acct["cashUsd"])
        vol = float(acct.get("eligibleVolumeUsd", 0.0) or 0.0)
        if not all(math.isfinite(x) for x in (secs, cash, vol, now)):
            return {"action": "HOLD"}
        pos = acct.get("position")
        cap = min(float(rules.get("maximumBuyCashUsd", 5.0)), 5.0)
        target = float(rules.get("targetVolumeUsd", CFG["target_volume"]) or CFG["target_volume"])
        if not (math.isfinite(cap) and cap > 0):
            cap = 0.0  # unreadable cap: can still SELL/HOLD, never BUY

        if pos is not None:
            side = str(pos.get("outcome"))
            bids = _levels(books.get(side, {}).get("bids", []), reverse=True)
            if str(pos.get("market_id", mid)) != mid and self.buy_mid != mid:
                return {"action": "HOLD"}  # bought in an earlier market: settles on its own
            try:
                shares, paid = float(pos.get("shares") or 0), float(pos.get("buy_gross_usd") or 0)
            except (TypeError, ValueError):
                shares = paid = 0.0
            avg = paid / shares if shares > 0 and paid > 0 else None
            is_bet = avg < CFG["bet_avg_max"] if avg is not None else mid == self.bet_market
            if is_bet:                     # refuel bet: hold to settlement, sell a clear winner late
                self.bet_seen, self.bet_market = True, mid
                if bids and bids[0][0] >= CFG["bet_sell_bid"] and secs <= CFG["bet_sell_secs"]:
                    return {"action": "SELL"}
                if bids and secs >= CFG["lock_secs_min"] and CFG["lock_cash"] < 1e8:
                    back = _sell_value(bids, float(pos.get("shares", 0)))
                    if back is not None and cash + back >= CFG["lock_cash"]:
                        return {"action": "SELL"}
                return {"action": "HOLD"}
            return {"action": "SELL"} if bids else {"action": "HOLD"}  # churn: straight back out

        if vol >= target - 1e-9 or cap <= 0:
            return {"action": "HOLD"}
        bet_done = (self.bet_seen and cash > (self.bet_cash0 or 0) + 1e-9  # a bet that filled and won
                    and cash >= min(CFG["goal_frac"] * self.goal, CFG["lock_cash"]))
        if not self.refueled and (cash >= self.goal or bet_done):
            self.refueled = True
            self.c0, self.v0 = cash, vol
        if self.refueled and CFG["rerefuel"]:
            need = self._need(cash, vol, target)
            one = min(cap, cash)  # endgame: one min-size cycle finishes -> churn, do not gamble
            last_cycle = target - vol <= 1.9 * one and one >= 5 * CFG["churn_min_ask"] * (1 + _fee_rate(CFG["churn_min_ask"]))
            if cash < need and cash >= CFG["min_bet_cash"] and not last_cycle:
                self.refueled = False
                self.goal = need + CFG["rebet_buffer"]
                self.bet_market, self.bet_seen = None, False  # the old bet must not count toward this new goal
        self._observe_spread(books, mid)
        if not self.refueled:
            return self._bet(books, secs, mid, cash, cap)
        return self._churn(books, secs, cash, cap, vol, target)

    def _bet(self, books, secs, mid, cash, cap):
        if not (CFG["bet_secs_lo"] <= secs <= CFG["bet_secs_hi"]) or cash < CFG["min_bet_cash"]:
            return {"action": "HOLD"}
        if self.bet_market == mid:
            return {"action": "HOLD"}  # one bet per market
        stake = min(cap, cash)
        goal = min(self.goal, max(cash * CFG["step_mult"], cash + 0.5))  # this bet's target (ladder step)
        q_hi = self._q_max(cash, stake, goal)  # most probable price at which a win still reaches the target
        q_hi = min(q_hi, CFG["bet_avg_max"] - 0.05, CFG["bet_q_max"])  # stay classifiable as a bet; optional price cap
        dq = CFG["dust_q"]
        if dq > 0 and q_hi < dq:  # leftover cash cannot reach the goal at a sane price: climb a step instead of idling
            goal = cash - stake + stake / (dq * (1 + _fee_rate(dq)))
            q_hi = self._q_max(cash, stake, goal)
        q_lo = max(CFG["bet_min_ask"], q_hi - CFG["bet_band"])
        best = None
        for side in ("YES", "NO"):
            asks = _levels(books.get(side, {}).get("asks", []), reverse=False)
            bids = _levels(books.get(side, {}).get("bids", []), reverse=True)
            if asks and CFG["bet_max_spread"] < 1 and (not bids or asks[0][0] - bids[0][0] > CFG["bet_max_spread"] + 1e-9):
                continue
            better = best is None or (asks and (asks[0][0] < best[1] if CFG["bet_pick"] == "low" else asks[0][0] > best[1]))
            if asks and q_lo <= asks[0][0] <= q_hi and better:
                best = (side, asks[0][0])
        if best is None:
            return {"action": "HOLD"}
        side, q = best
        k = 1 / (q * (1 + _fee_rate(q)))      # shares per $ staked
        need = (goal - cash) / (k - 1)  # smallest stake whose win reaches the target
        use = min(stake, max(need * 1.03, 1.01 * _min_size(books.get(side, {})) / k))
        self.bet_market, self.bet_cash0, self.bet_seen = mid, cash, False
        return {"action": "BUY", "outcome": side, "maxCashUsd": _floor6(use)}

    def _q_max(self, cash, stake, goal):
        # highest price at which a full stake still reaches fuel_goal: stake/(q(1+f)) = goal - cash + stake
        lo, hi = 0.01, 0.99
        for _ in range(40):
            q = (lo + hi) / 2
            if cash - stake + stake / (q * (1 + _fee_rate(q))) >= goal:
                lo = q
            else:
                hi = q
        return lo

    def _need(self, cash, vol, target):
        dv = vol - (self.v0 or 0.0)
        rate = CFG["cost_prior"]
        if dv >= CFG["cost_min_vol"]:
            rate = max((self.c0 - cash) / dv, 0.0)
        return (target - vol) * rate * CFG["cost_margin"] + CFG["reserve"]

    def _observe_spread(self, books, mid):
        if not CFG["adapt_spread"]:
            return
        for side in ("YES", "NO"):
            a = _levels(books.get(side, {}).get("asks", []), reverse=False)
            b = _levels(books.get(side, {}).get("bids", []), reverse=True)
            if a and b and CFG["churn_min_ask"] <= a[0][0] <= CFG["churn_max_ask"]:
                sp = a[0][0] - b[0][0]
                if sp < self.mkt_min.get(mid, 1.0):
                    self.mkt_min.pop(mid, None)  # re-insert: dict order = recency
                    self.mkt_min[mid] = sp
        while len(self.mkt_min) > CFG["adapt_markets"]:
            self.mkt_min.pop(next(iter(self.mkt_min)))

    def _spread_limit(self, vol, target):
        sched = min(self.elapsed_h / CFG["deadline_hours"], 1.0)  # fraction of target we should have by now
        done = vol / target
        if done >= sched:
            mode, lim = 0, CFG["churn_max_spread"]
        elif done >= sched - CFG["late_margin"] and self.elapsed_h < CFG["deadline_hours"]:
            mode, lim = 1, CFG["churn_behind_spread"]
        else:
            mode, lim = 2, CFG["churn_late_spread"]
        if CFG["adapt_spread"] and len(self.mkt_min) >= CFG["adapt_min_markets"]:
            v = sorted(self.mkt_min.values())
            ref = v[0] if CFG["adapt_stat"] == "min" else v[len(v) // 2]
            lim = max(lim, ref + CFG["adapt_add"][mode])
        return lim

    def _churn(self, books, secs, cash, cap, vol, target):
        if secs < CFG["churn_secs_lo"]:
            return {"action": "HOLD"}
        spend = _floor6(min(cap, cash - CFG["churn_cash_floor"]))
        best = None
        for side in ("YES", "NO"):
            book = books.get(side, {})
            asks = _levels(book.get("asks", []), reverse=False)
            bids = _levels(book.get("bids", []), reverse=True)
            if not asks or not bids:
                continue
            a, b = asks[0][0], bids[0][0]
            if not (CFG["churn_min_ask"] <= a <= CFG["churn_max_ask"]) or a - b > self._spread_limit(vol, target) + 1e-9:
                continue
            shares, paid = _buy_fill(asks, spend)  # real L2 depth: walk the book both ways
            back = _sell_value(bids, shares)
            if shares < _min_size(book) or back is None or paid <= 0:
                continue
            cost = (paid - back) / paid  # round-trip cost per $ bought
            if best is None or cost < best[1]:
                best = (side, cost)
        if best is None:
            return {"action": "HOLD"}
        return {"action": "BUY", "outcome": best[0], "maxCashUsd": spend}


BOT = BoldBot()


def decide(obs):
    return BOT.decide(obs)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive: no new TCP/TLS handshake per call (200 ms budget)
    disable_nagle_algorithm = True  # headers + body are separate writes: avoid delayed-ACK stalls

    def _body(self) -> bytes:
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            out = b""
            while len(out) < 4_000_000:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass  # trailers
                    break
                out += self.rfile.read(size)
                self.rfile.readline()
            return out
        cl = self.headers.get("Content-Length")
        try:
            n = int(cl)
        except (TypeError, ValueError):
            n = -1
        if n < 0 or n > 4_000_000:
            self.close_connection = True  # unknown framing: do not reuse this connection
            return b""
        return self.rfile.read(n)

    def do_POST(self):  # noqa: N802
        try:
            body = json.loads(self._body() or b"{}")
            out = BOT.decide(body)  # BOT.decide serialises calls with a lock
        except Exception:  # noqa: BLE001
            out = {"action": "HOLD"}
        self._reply(out)

    def do_GET(self):  # noqa: N802 - health check
        self._reply({"ok": True, "bot": "bold-refuel"})

    def _reply(self, out):
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    BOT.enable_persistence(os.environ.get("BOT_STATE", "bold_state.json"))
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
