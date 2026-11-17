# COM5 Commodities Capstone: Trading Strategy Plan

## Context

We are building an automated trading bot for the Rotman COM5 Commodities Capstone competition. The case simulates crude oil and derivatives trading across 2 periods of 600 ticks (10 min each). There are 7 tradable securities (3 crude locations, 2 futures, 2 refined products) and 6 physical assets (3 storage, 2 pipelines, 1 refinery). Alpha comes primarily from EIA report reactions, with secondary P&L from arbitrage across locations, futures basis, and the crack spread. The system is designed to discover case structure dynamically from the API at runtime and tighten behavior from logged runs rather than a required scraper pipeline. Deployment is flexible (async, multi-thread allowed).

---

## Phase 0: Startup Discovery & Early-Run Validation

**Goal:** Discover the live case configuration from the API at startup, then refine execution and parsing from actual run logs.

### What must be discovered dynamically at startup
1. **Case structure** — `/case` for ticks per period and total periods
2. **Tradable instruments** — `/securities` for contract metadata, fees, and max order size
3. **Risk limits** — `/limits` for gross and per-ticker net constraints
4. **Asset economics** — `/assets` for lease costs, capacities, transfer times, and lease caps

### Early-run validation targets
- **News bodies** — confirm exact EIA/body phrasing from logged events so regex parsing stays grounded in the live feed
- **Observed liquidity** — CL and CL-1F spread/depth behavior for market-making and execution tuning
- **Order behavior** — round-trip latency, rate-limit behavior, and whether concurrent order submission is actually beneficial
- **Cross-market spreads** — basis, geographic arb, and crack-spread ranges seen in real runs

### Operating rule
The production workflow should rely on startup discovery, periodic refreshes, and logged run artifacts under `runs/`. Practice-heat scraping is optional research tooling, not a prerequisite for running the bot.

### Calibration Targets (from logged runs)
| Parameter | What to Measure | How It Informs Strategy |
|-----------|----------------|------------------------|
| EIA news format | Exact regex patterns for expected/actual values | Parser reliability |
| EIA price reaction curve | Price path in ticks 0-60 after each EIA | Entry/exit timing |
| Market efficiency ratio | Actual move vs calculated impact | Position hold duration |
| Spread distributions | CL vs CL-AK, CL vs CL-NYC, crack spread | Arb thresholds |
| Futures basis curve | CL-1F - CL over time within a period | Convergence trade timing |
| Order book depth | Typical bid/ask sizes and spread | Execution strategy |
| News headline patterns | Keyword frequency, impact magnitude | News classifier weights |
| Rate limit behavior | Max requests/sec before 429s | Polling frequency cap |
| Starting CL price | Typical range | Fair value model initialization |
| Market maker entry time | When MM bots activate near period end | Wind-down timing |
| Concurrent order acceptance | Whether API accepts parallel POSTs | Async vs sequential execution |

**Priority validation question:** What is the exact text format of EIA news bodies? The entire EIA parsing strategy depends on this.

---

## Phase 1: Strategy Architecture

### System Design (Single-process, async)

```
Main Event Loop (async, ~20 iterations/sec)
  |
  +-- State Manager (single source of truth)
  |     - positions, prices, cash, tick, limits, leases
  |     - reconciles with API every 30 ticks
  |
  +-- Signal Layer
  |     - EIA Parser (regex-based, highest priority)
  |     - News Classifier (keyword-weighted)
  |     - Spread Monitor (basis, geographic, crack)
  |     - Order Flow Analyzer (buy/sell imbalance from T&S)
  |     - Fair Value Tracker (cumulative EIA + news effects)
  |
  +-- Strategy Engines (priority-ordered)
  |     1. EIA Alpha (directional, ~60% of expected P&L)
  |     2. Futures Convergence (basis arb, ~15%)
  |     3. Geographic Arb (pipeline, ~10%)
  |     4. News Trading (event-driven, ~8%)
  |     5. Crack Spread (refinery, ~5%)
  |     6. Market Making (liquidity provision, ~2%)
  |
  +-- Execution Engine
  |     - Async HTTP (aiohttp + connection pooling)
  |     - Order splitting (30-contract cap)
  |     - Limit vs market decision logic
  |
  +-- Risk Manager
  |     - Gross/net limit enforcement
  |     - Storage lease management
  |     - End-of-period wind-down
  |     - Cash budget tracking
  |
  +-- Strategy Coordinator
        - Gross limit budget allocation across strategies
        - Conflict resolution between competing signals
```

### Key Technical Decisions
- **aiohttp** for async HTTP — fire multiple orders simultaneously if live API behavior supports it
- **`requests.Session()`** with connection pooling for all API calls (reuse TCP)
- **Pre-computed order payloads** for EIA reactions (bullish + bearish templates ready before each EIA tick)
- **Loop at 50ms** (20 cycles/sec) instead of template's 100ms
- **News polling at top of every loop iteration** — latency on news = latency on alpha

---

## Phase 2: EIA Alpha Engine (Primary Strategy, ~60% of P&L)

This is the single most important strategy. 8 EIA reports per case (4 per period at ticks 90, 240, 390, 540), each moving CL price by $1 per 10M barrel differential.

### How It Works

**Price model:** `PCL = PCL_start + sum(EIA_effects) + sum(news_effects)`

**EIA impact formula:** `impact = (expected_change - actual_change) / 10.0` dollars per barrel
- If expected build of +6M and actual draw of -16M: diff = 6 - (-16) = +22M, impact = +$2.20 (bullish)
- Positive impact = bullish (less supply than expected), negative = bearish

### Execution Sequence

**T-10 ticks (pre-EIA):**
- Cancel ALL resting orders (market making, arb orders, everything)
- Flatten any small market-making positions
- Pre-build order payloads for both bullish and bearish scenarios

**T+0 (EIA arrives):**
1. Poll `/news` — parse EIA body with regex
2. Calculate impact magnitude
3. **Immediately fire market orders on CL-1F** (shortable both directions, most liquid)
   - Use async to send multiple 30-contract orders simultaneously (if API supports concurrent POSTs; otherwise sequential rapid-fire)
   - Size: scale with magnitude (see below)
4. **Simultaneously** place limit orders on CL spot (if bullish — can't short spot)

**Position sizing:**
```
base_contracts = min(
    floor(abs(impact) / 0.50) * 15,    -- 15 contracts per $0.50 of impact
    100,                                 -- net limit
    (500 - current_gross) / 2            -- leave room for arb strategies
)
```

| Impact Magnitude | Contracts on CL-1F | Contracts on CL (if bullish) |
|-----------------|--------------------|-----------------------------|
| < $0.30 | 0 (below threshold) | 0 |
| $0.30 - $0.99 | 15-30 | 10-20 |
| $1.00 - $1.99 | 30-60 | 20-40 |
| $2.00+ | 60-100 | 30-50 |

**Exit schedule (post-EIA):**
- **T+10:** Exit 50% (capture overshoot, lock in fast profit)
- **T+30:** Exit 30% (mark-to-market boundary)
- **T+60:** Exit remaining 20% (unless fair value model shows continued mispricing)

### Cascade to Related Securities

After CL-1F trade is placed, check for dislocations in:
- **CL-2F** — should move by ~same amount as CL-1F
- **CL-AK / CL-NYC** — should move by ~same amount, adjusted for transport costs
- **HO / RB** — should move proportionally through crack spread relationship

If any of these lag the CL move by more than the transaction cost, trade the dislocation.

### Fair Value Tracker

Maintain a running fair value estimate across the entire period:
```
fair_value = start_price + cumulative_eia_impact + cumulative_news_impact
```
Every strategy module references this single number. If market price < fair_value - threshold: buy signal. If market price > fair_value + threshold: sell signal (on shortable instruments only).

### Bayesian Refinement (live observation)

Track how efficiently the market prices each EIA:
- `efficiency_ratio = actual_market_move / calculated_impact`
- If ratio < 1.0 consistently: market under-reacts → hold positions longer
- If ratio > 1.0: market over-reacts → fade the move after initial spike, exit faster

---

## Phase 3: Futures Basis / Convergence Trading (~15% of P&L)

### Theoretical Basis

With r=0, convenience yield=0:
```
F_theoretical = S + storage_cost_to_expiry
Storage cost per contract = $500/day * days_remaining / 10 contracts = $50/day * days_remaining
```

| Tick | Days Remaining | Theoretical Basis ($/barrel) |
|------|---------------|------------------------------|
| 0 | 20 | $1.00 |
| 150 | 15 | $0.75 |
| 300 | 10 | $0.50 |
| 450 | 5 | $0.25 |
| 540 | 2 | $0.10 |
| 570 | 1 | $0.05 |

### Cash-and-Carry Arbitrage

**When:** `F_market - S_market > theoretical_basis + $0.02 (fees)`

**Execution:**
1. Lease CL-STORAGE (MUST do first)
2. Buy 10 CL spot
3. Short 10 CL-1F
4. Hold until tick 600 — futures settle into CL, positions cancel

**Profit per 10-contract trade:** `(F_market - S_market - theoretical_basis) * 10,000 - $20 fees`

### Convergence Trade (Late Period)

After tick 540 (last EIA), the basis must collapse to near-zero by tick 600. This is the highest-conviction arb:

- At tick 570, if basis is still $0.30 but theoretical is $0.05 → $0.25/bbl mispricing
- Profit: $0.25 * 10,000 - $500 storage - $20 fees = **$1,980 per trade**
- Can repeat multiple times if basis persists

### CL-2F in Period 1

CL-2F settles at end of period 2. In period 1, theoretical basis includes carry for remaining period 1 + all of period 2. This creates larger basis opportunities but ties up capital across periods. **Trade CL-2F basis only if the mispricing is very large (>$0.50 above theoretical).**

---

## Phase 4: Geographic Arbitrage / Pipeline (~10% of P&L)

### Breakeven Thresholds

| Route | Pipeline Cost | Barrels | Breakeven Spread | With Buffer |
|-------|-------------|---------|------------------|-------------|
| Alaska → Cushing | $40,000 | 10,000 | $4.00/bbl | $4.50/bbl |
| Cushing → NYC | $20,000 | 10,000 | $2.00/bbl | $2.50/bbl |

Buffer accounts for price movement during 30-second transit and transaction costs.

### Execution (Alaska → Cushing example)

1. Lease AK-STORAGE (if not already held)
2. Buy 10 CL-AK at market
3. Lease AK-CS-PIPE → use immediately (10,000 bbl)
4. Wait 30 seconds — CL-AK converts to CL in Cushing
5. Lease CL-STORAGE (if needed)
6. Sell 10 CL at market
7. Release storage leases when empty

### Key Risks
- **Price movement during 30-sec transit** — the spread can narrow or reverse
- **Pipeline cost changes** — news can alter pipeline costs mid-case; must parse these news items and update thresholds immediately
- **Storage capacity** — need leases at both origin and destination

### When to Execute
- Only when spread exceeds breakeven + buffer
- Avoid executing within 15 ticks of an EIA (spread may shift dramatically)
- Monitor spread continuously; act when spread is at local maximum

---

## Phase 5: News/Geopolitical Event Trading (~8% of P&L)

### Classification Approach

Keyword-weighted classifier with magnitude estimation:

**Bullish keywords (supply disruption / demand increase):**
pipeline outage, shutdown, geopolitical tension, conflict, sanctions, embargo, hurricane, supply disruption, production cut, OPEC cut, export ban, refinery fire, demand increase

**Bearish keywords (supply increase / demand decrease):**
oversupply, surplus, demand decrease, recession, production increase, OPEC increase, ceasefire, peace, pipeline reopened, sanctions lifted

**Pipeline cost change keywords (separate category):**
pipeline cost, transportation cost → update arb thresholds, don't trade CL directionally

### Sizing (conservative — magnitude is uncertain)
- Single keyword match: 10-15 contracts
- Multiple keyword matches / clear direction: 15-30 contracts
- Explicit price impact in body: 30-50 contracts

### Exit
- Shorter hold than EIA (no formula to anchor to)
- Exit 70% at +15 ticks, remainder at +30 ticks

Refine the keyword list and magnitude calibration from logged live news bodies as runs accumulate.

---

## Phase 6: Crack Spread / Refinery (~5% of P&L)

### Profitability Check

3:2:1 crack: 3 CL → 2 RB + 1 HO

```
revenue = rb_price * 84,000 + ho_price * 42,000
cost = cl_price * 3,000
net_profit = revenue - cost - $300,000 - $7 (fees)
```

The $300K refinery cost makes this viable only during extreme product premiums. Typical crack spreads will NOT cover this cost. **Only execute when net_profit > $20,000 (meaningful margin).**

### Non-Blocking Execution

Do NOT use `time.sleep(45)` to wait for refinery. Instead:
- Track `refinery_start_tick` in state
- Every loop iteration, check if `current_tick - start_tick >= 46`
- If so, release refinery immediately (avoid double-charge at 60 ticks)
- Sell RB and HO at market to lock profit and free gross limit

### Gross Limit Impact
- Temporarily consumes 3 CL (input) + will produce 3 contracts output
- After conversion and selling: net 0 gross
- Plan around this — don't start a refinery run if you're near 500 gross

---

## Phase 7: Market Making (~2% of P&L, fills dead time)

### When Active
- Between EIA releases (ticks 95-230, 245-380, 395-530)
- Only when fair value confidence is high
- Cancel all MM orders at T-10 before each EIA

### How
- Post limit orders on both sides of fair value on CL-1F
- Half-spread: $0.05-$0.10 to start, then retune from observed order book depth
- Size: 5-10 contracts per side
- **Inventory management:** If net position drifts from MM fills, widen the quote on the losing side

### Risk Controls
- Max net exposure from MM: 20 contracts
- Cancel and re-quote every 5-10 ticks
- Never MM within 10 ticks of EIA

---

## Phase 8: Execution Engine

### Async HTTP Architecture

```python
# Use aiohttp for concurrent order submission when live API behavior supports it
# Fallback: sequential requests.Session() with connection pooling
# Pre-build Session at startup with connection pooling
# Fire multiple 30-contract orders simultaneously after EIA
# Single API call for /securities (all 7 tickers) vs 7 individual calls
```

### Limit vs Market Decision Matrix

| Scenario | Order Type | Rationale |
|----------|-----------|-----------|
| EIA reaction (first 5 ticks) | MARKET | Speed > price |
| Geopolitical news | MARKET | Speed > price |
| Storage/transport arb | LIMIT | Arb is patient |
| Convergence trade | LIMIT | Slow-moving |
| Market making | LIMIT | By definition |
| End-of-period flatten (tick 590+) | MARKET | Must fill, can't risk non-fill |

### Order Splitting for >30 Contracts
Fire all split orders simultaneously via async (if API supports it). If not, rapid sequential fire without waiting for fill confirmation between orders.

---

## Phase 9: Risk Management

### Gross Limit Budget (500 total)

| Strategy | Budget | Notes |
|----------|--------|-------|
| EIA directional | 200 | Highest alpha, needs both long/short room |
| Futures basis | 100 | Paired positions (long CL + short CL-1F) |
| Geographic arb | 60 | Temporary (30 sec transit then sell) |
| News trading | 60 | Opportunistic |
| Crack spread | 40 | Temporary (45 sec refinery then sell) |
| Market making | 40 | Small size, both sides |

Dynamic — coordinator reallocates unused budget.

### Storage Lease Protocol
1. **NEVER buy spot CL without leased storage** ($2,500 penalty per unit)
2. Lease just-in-time (not at tick 0 if you won't buy spot until tick 90)
3. One lease = 10,000 bbl = 10 contracts
4. Release immediately when emptied
5. Before futures settlement (tick 600): verify all spot CL has storage coverage

### End-of-Period Wind-Down

| Tick | Action |
|------|--------|
| 540 | Trade last EIA (normal) |
| 560 | Cancel all resting orders. Begin scaling out non-convergence positions |
| 570 | Aggressively trade any remaining futures basis (convergence alpha) |
| 580 | Flatten all directional positions via market orders |
| 590 | Emergency flatten — market order everything to flat |
| 595 | Release all unnecessary storage leases |
| 600 | Settlement — futures → CL, ensure hedged pairs cancel cleanly |

### Reconciliation
Every 30 ticks (mark-to-market boundary): pull `/securities` and `/leases`, compare to internal state, correct any drift. Never trust internal bookkeeping over the API.

---

## Phase 10: Temporal Playbook

### Period 1

| Phase | Ticks | Strategy |
|-------|-------|----------|
| Opening | 0-80 | Initialize fair value, check initial spreads, lease storage if basis arb available, light MM |
| EIA 1 | 80-150 | Parse, trade, cascade, exit |
| Between | 150-230 | Arb strategies, MM, process random news |
| EIA 2 | 230-300 | Parse, trade, cascade, exit |
| Between | 300-380 | Arb strategies, MM, process random news |
| EIA 3 | 380-450 | Parse, trade, cascade, exit |
| Between | 450-530 | Arb strategies, MM, process random news |
| EIA 4 | 530-560 | Parse, trade, cascade, exit |
| Wind-down | 560-600 | Convergence trade, flatten, release, settle |

### Period 2
Same structure, but:
- **CL-2F** now has same convergence dynamics as CL-1F did in period 1
- Be **more aggressive** — last chance to make money
- Fair value tracker resets (new starting price) but Bayesian efficiency estimate carries over

---

## Phase 11: Competitive Edge Summary

What separates a top-tier bot from average:

1. **Speed** — async HTTP, connection pooling, pre-built order payloads, news at top of every loop. 200ms faster on EIA = $0.10-$0.50 better fills across 100 contracts = $10K-$50K more profit
2. **Multi-strategy coordination** — EIA moves price, which widens geographic/crack spreads. A coordinated bot captures the cascade. An isolated-strategy bot misses secondary alpha.
3. **Adaptive calibration** — thresholds tightened from observed run data, not guesswork. EIA exit timing, arb buffers, and MM spread width all improve from real logs.
4. **Non-blocking refinery/pipeline** — bots that `sleep(45)` lose 45 seconds of trading on every refinery run. Non-blocking execution keeps all strategies active.
5. **Convergence alpha** — most bots won't trade the last 60 ticks well. The basis convergence trade is nearly risk-free and can generate $2K-$4K per execution in those ticks.
6. **Defensive programming** — storage penalty avoidance, limit checks before every order, API reconciliation, crash-proof error handling. One unhandled exception = 10 min of lost trading.

---

## Open Questions / Early-Run Validation

These questions should be answered through early live runs and the bot's own logs:

1. **What is the exact EIA news body text format?** (regex parser depends on this)
2. **How fast does the market react to EIA?** (determines exit timing)
3. **What are typical geographic spreads?** (determines if pipeline arb is ever viable)
4. **What is the typical crack spread range?** (determines if refinery is ever viable)
5. **What is the API rate limit?** (determines max polling frequency)
6. **Does the API accept concurrent POST /orders requests?** (async vs sequential execution)
7. **What is the round-trip latency for order placement?** (split-order timing)
8. **What are typical bid-ask spreads?** (determines MM profitability)
9. **Do random news events follow predictable keyword patterns?** (classifier accuracy)
10. **What is the starting CL price?** (fair value initialization)
11. **How do other competitors behave?** (opportunity in thin markets after EIA)
12. **When exactly do market makers enter?** (end-of-period execution window)

---

## Implementation Order

1. **Dynamic discovery + state manager + risk manager** — foundation, prevents catastrophic losses
2. **EIA parser + alpha engine** — highest P&L contributor
3. **Execution engine** — speed advantage, with concurrency enabled only if live behavior warrants it
4. **Futures convergence** — reliable arb
5. **News classifier** — incremental alpha
6. **Geographic arb** — conditional on spread data
7. **Crack spread** — conditional on profitability data
8. **Market making** — polish
9. **Bayesian refinement + parameter tuning** — final tightening from accumulated run logs

---

## Verification Plan

1. **Unit test** EIA parser against captured sample news bodies
2. **Paper trade** (run bot against practice heat with logging, no real orders) to verify state management and signal generation
3. **Dry run** with small position sizes (5 contracts) to verify execution, storage leasing, limit compliance
4. **Full run** with production sizes against practice heat, measure P&L
5. **A/B compare** with/without each strategy module to quantify marginal contribution
6. **Stress test** edge cases: what happens at tick 599 with open positions, what if API returns errors, what if EIA body format changes
