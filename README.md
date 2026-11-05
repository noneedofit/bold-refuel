# Bold Refuel — volume bot for BTC 5-minute up/down markets

`POST /decide` server, Python 3 standard library only.

```bash
python3 bot.py                       # listens on 0.0.0.0:8080 (PORT env to change)
python3 tests/test_bot.py            # fuzz + scenario tests
```

## Strategy

Taker fees plus the 1% overlay cost at least ~$11–14 per $1,000 of volume, so $10 of starting cash
cannot fund $1,000 of volume at fair prices. BUYs are capped at $5, but payouts are not.

1. **Refuel:** stake up to $5 on an underdog. The price is chosen so that a win lifts cash to about $35.
   - Hold it to settlement.
   - Sell a clear winner (bid ≥ 0.98) in the final seconds; that counts as eligible volume.
   - If the bet loses, repeat boldly with the remaining cash.
2. **Churn:** buy the favourite with the tightest book (ask 0.95–0.995) and sell it straight back. Repeat until $1,000.
   - The allowed spread widens only when the bot falls behind a 20-hour schedule.
   - It measures its own churn cost. If the remaining cash will not reach the target, it refuels again.
3. **After $1,000:** stays flat (HOLD).

The bot uses only the order books, never the BTC reference. It keeps a small state file (`BOT_STATE`, default
`bold_state.json`) so a process restart mid-run resumes correctly.
- **New runs:** it detects a new run (volume or cycles drop, or a time gap over 3 h) and starts fresh, so a leftover state
  file or a reused process cannot leak into a new run. Still, use one process per concurrent run.
- **Order book:** sizing walks the L2 book and respects each book's `minOrderSize`.
- **Churn limits:** it is patient for rare ultra-tight books on pace, but when far behind schedule it accepts spreads up to 0.03, so wide-book regimes still finish.
- **Leftover cash:** small leftover cash climbs in steps rather than sitting idle.
- **Positions:** classified by average buy price (refuel bets < 0.9, churn ≥ 0.95), so differing position/market id formats, rejected orders and restarts are handled.
- **HTTP:** keep-alive, chunked bodies, TCP_NODELAY, `GET` health check.
- **Replies:** every request gets one; malformed input gets HOLD. Replies take about 1–4 ms.

Tests (fuzzing plus failure scenarios): `python3 tests/test_bot.py`.

## Results on a replay simulator

These come from archived BTC 5-minute market books, Sep 14 – Oct 29 2026, with 48 day-long windows per condition. Development results only.

| condition | qualified | median cash at $1,000 |
|---|---|---|
| quoted fills | 15/48 (31%); 49/200 (24% ± 6%) on a 200-window sample | $22.15 |
| 1¢ worse fills on every trade | 14/48 (29%) | $10.43 |
| 2¢ worse fills | 6/48 | $12.59 |
| quoted spreads 0.002 wider | 15/48 | $18.79 |
| quoted spreads 0.006 wider | 15/48 | $13.87 |
| only 10 shares at the best price (L2 walk) | 15/48 | $20.22 |
| 1 s fill latency | 15/48 | $19.71 |
| fees ×1.5 | 15/48 | $14.77 |
| half of the decision calls missing | 15/48 | $21.47 |
| called every 2 s | 14/48 | $20.93 |
| half of BUYs fill only 50% | 11/48 | $20.96 |
| process crash + restart every 4 h, state file lost | 15/48 | $20.99 |
| books 1 s stale | 15/48 | $22.12 |

A `minOrderSize` of 10 makes $5 favourite churn impossible for any strategy; typical books use 5.
