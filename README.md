# COM5 Commodities Capstone: Algorithm Documentation

This bot placed 3rd at the QUANTT End Of Year Conference Case Competition. The event consisted of 4 rounds, each with a 20-minute trading period, while the underlying COM5 case model described below still ran as a 2-period, 600-tick simulation.

## Overview

This document describes the automated trading system developed for the Rotman Interactive Trading (RIT) COM5 Commodities Capstone competition. The system implements a multi-strategy approach to crude oil and derivatives trading, executing across spot, futures, refined products, and physical asset markets within a simulated two-period trading environment.

The system is designed with two core architectural principles: (1) **adaptive parameter discovery**, where all competition parameters are queried from the API at runtime rather than hardcoded, enabling robustness across varying administrator configurations; and (2) **modular strategy composition**, where six independent strategy engines share a common state manager and risk framework, each contributing to overall P&L without conflicting with one another.

---

## System Architecture

```
                          ┌──────────────┐
                          │  RIT REST API │
                          └──────┬───────┘
                                 │
                    ┌────────────┴────────────┐
                    │     CaseConfig           │
                    │  (dynamic discovery)     │
                    └────────────┬────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              │                  │                   │
        ┌─────┴─────┐    ┌──────┴──────┐    ┌──────┴──────┐
        │  State     │    │  Execution  │    │  Run        │
        │  Manager   │    │  Engine     │    │  Logger     │
        └─────┬─────┘    └──────┬──────┘    └─────────────┘
              │                  │
    ┌─────────┼─────────┬───────┼───────┬─────────┐
    │         │         │       │       │         │
┌───┴──┐ ┌───┴──┐ ┌────┴──┐ ┌─┴────┐ ┌┴─────┐ ┌─┴────┐
│ EIA  │ │Basis │ │ News  │ │Trans-│ │Crack │ │Market│
│Alpha │ │Engine│ │Engine │ │port  │ │Engine│ │Maker │
└──────┘ └──────┘ └───────┘ └──────┘ └──────┘ └──────┘
              │
        ┌─────┴─────┐
        │  Storage   │
        │  Manager   │
        └─────┬─────┘
              │
        ┌─────┴─────┐
        │   Risk     │
        │  Manager   │
        └───────────┘
```

### Component Summary

| Component | Responsibility |
|-----------|---------------|
| **CaseConfig** | Queries `/case`, `/securities`, `/limits`, `/assets` at startup to discover all competition parameters. Refreshes every 30 ticks to detect mid-case changes. |
| **Orchestrator** | Real-time self-tuning layer. Observes liquidity, EIA reaction efficiency, volatility, and spread availability, then adjusts strategy weights, position sizing, exit timing, and gross budget allocation. |
| **StateManager** | Single source of truth for positions, prices, fair value model, lease inventory, and scheduled exit orders. Reconciles with API periodically. |
| **ExecutionEngine** | Places orders with safety checks (max size enforcement, shortability validation). Handles order splitting for quantities exceeding the per-order maximum. |
| **StorageManager** | Manages storage leases to prevent the penalty for holding spot crude without a lease. Leases just-in-time and releases when empty. |
| **RunLogger** | Dual logging to console and file. Records structured events (JSONL), NLV equity curve (CSV), discovered config, and post-run analytics summary (JSON). |
| **RiskManager** | Enforces gross/net position limits, manages scheduled exit orders, orchestrates end-of-period wind-down sequence, and handles pre-EIA order cancellation. |

---

## Self-Tuning Orchestrator

The Orchestrator observes market conditions in real-time and modifies strategy parameters on the `CaseConfig` object, which all strategy engines read each loop iteration. This creates a feedback loop where the bot adapts to the specific market regime it encounters.

### Observation Inputs

| Observable | How Measured | Update Frequency |
|-----------|-------------|-----------------|
| **Liquidity** | CL-1F bid-ask spread and top-5 depth from order book | Every 30 ticks |
| **EIA reaction efficiency** | Ratio of actual CL price move to calculated impact, measured 10 ticks post-EIA | After each EIA |
| **Price volatility** | Standard deviation of tick-to-tick CL returns over a 60-tick window | Every tick |
| **Spread availability** | CL vs CL-AK and CL vs CL-NYC price differentials relative to pipeline breakevens | Every 30 ticks |

### Tunable Outputs

| Parameter | What Drives It | Range |
|-----------|---------------|-------|
| **Aggression multiplier** | Liquidity regime (spread width) | 0.7 (low liq) to 1.2 (high liq) |
| **EIA contracts/50c** | Aggression * base rate | 8-24 contracts |
| **EIA exit timing** | Reaction efficiency ratio | Fast (5/15/30) to slow (15/45/90) ticks |
| **MM half-spread** | 40% of observed bid-ask spread | $0.02 - $0.20+ |
| **MM size** | Order book depth | 3-10 contracts |
| **Arb buffers** | Price volatility (tick return stdev) | $0.30 (low vol) to $0.80 (high vol) |
| **Gross budget allocation** | Whether geographic arb spreads exist | Shifts transport budget to EIA/basis when arb is absent |

### Tuning Logic

**Liquidity regime detection:** Wide spreads (>$0.20) indicate low liquidity from reduced market-maker activity. The orchestrator responds by reducing the aggression multiplier to 0.7, which scales down position sizes across all strategies and shifts execution toward limit orders. Tight spreads (<$0.05) trigger increased aggression (1.2x).

**EIA calibration:** After each EIA report, the orchestrator measures the ratio of the actual CL price move to the calculated impact. An efficiency ratio above 1.3 (market over-reacts) triggers faster exit timing and reduced sizing. A ratio below 0.7 (market under-reacts) triggers slower exits and increased sizing. This adapts within the heat as more EIA observations accumulate.

**Volatility-responsive arb buffers:** High tick-to-tick price volatility (>0.3% per tick) widens pipeline arbitrage safety buffers to $0.80/barrel, accounting for price movement during the 30-second transit period. Low volatility tightens buffers to $0.30/barrel, enabling the bot to capture smaller spreads safely.

**Dynamic budget reallocation:** If geographic spreads never approach pipeline breakeven costs, the orchestrator reallocates the transport strategy's gross budget to EIA and basis strategies where capital is more productive. This prevents gross limit capacity from sitting idle.

### Warmup Period

The orchestrator waits 30 ticks (one mark-to-market cycle) before its first tuning pass, ensuring sufficient observation data. Tuning runs every 30 ticks thereafter. All strategy parameters begin at sensible defaults and are refined as data accumulates.

---

## Strategy Engines

### 1. EIA Alpha Engine (Primary)

**Signal source:** Weekly EIA petroleum inventory reports released at known tick intervals.

**Mechanism:** Each EIA report contains an expected and actual inventory change in millions of barrels. The price impact follows the confirmed formula:

```
impact = (expected - actual) / 10.0 (dollars per barrel)
```

A positive impact (actual inventory below expectations) is bullish; a negative impact is bearish.

**Execution:**
- Reports are parsed via a series of regex patterns applied to the raw news body, with fallback patterns tried in sequence.
- Market orders are placed immediately on CL-1F (the most liquid shortable instrument) upon signal confirmation.
- Position size scales linearly with impact magnitude at a configurable rate per $0.50 of impact, clamped by net and gross limits.
- Simultaneous long positions are taken on CL spot for bullish signals (subject to storage lease pre-condition).

**Exit schedule (configurable):**
- 50% of position at +10 ticks (capture initial overshoot)
- 30% at +30 ticks (mark-to-market boundary)
- 20% at +60 ticks (held only if continued mispricing)

**Cascade logic:** After the primary CL-1F trade, the engine checks for dislocations in CL-2F, CL-AK, CL-NYC, HO, and RB relative to the updated fair value.

**Fair value model:** A running estimate maintained across the period:
```
fair_value = start_price + cumulative_eia_impact + cumulative_news_impact
```
All strategy modules reference this single number for directional bias.

### 2. Futures Basis Engine

**Signal source:** Mispricing between CL spot and CL-1F futures relative to theoretical cost-of-carry.

**Theoretical basis:**
```
F_theoretical = S + storage_cost_per_barrel_per_day * days_remaining
```
With the risk-free rate and convenience yield both at zero, storage cost is the sole carry component. The storage cost per barrel per day is derived dynamically from the actual lease price and capacity discovered via the API.

**Cash-and-carry arbitrage:** When futures trade above spot by more than the theoretical basis plus transaction costs, the engine buys spot CL (after leasing storage) and shorts CL-1F. The paired position converges at settlement (tick 600) when futures settle into spot.

**Convergence trading:** In the final 60 ticks of each period, the basis must collapse to near-zero. Any remaining mispricing above a configurable threshold triggers convergence trades with high conviction, as the settlement mechanism guarantees convergence.

### 3. News Trading Engine

**Signal source:** Non-EIA news headlines containing geopolitical events, supply disruptions, or demand shifts.

**Classification:** A keyword-weighted scoring system assigns bullish or bearish scores based on matched phrases. Keywords include supply disruption indicators (pipeline outage, sanctions, hurricane) and demand/supply balance indicators (oversupply, recession, production increase). A separate keyword set detects pipeline cost change announcements, which are routed to the config refresh pathway rather than generating directional trades.

**Sizing:** Conservative position sizing due to uncertain magnitude. Low-confidence signals (single keyword match) use a smaller base size; high-confidence signals (multiple matches, score above threshold) use a larger allocation.

**Exit:** Faster than EIA exits (70% at +15 ticks, 30% at +30 ticks) reflecting lower conviction in the magnitude estimate.

### 4. Geographic Arbitrage Engine

**Signal source:** Price differentials between crude oil at different physical locations (Alaska, Cushing, New York City) exceeding pipeline transportation costs.

**Breakeven calculation:** Derived dynamically from actual pipeline lease costs and capacities:
```
breakeven_per_barrel = pipeline_lease_cost / pipeline_capacity
```

**Execution:** When the spread exceeds breakeven plus a safety buffer:
1. Storage is leased at the origin location
2. Crude is purchased at the cheaper location
3. Pipeline is leased and used immediately
4. After the transfer period (discovered from API), crude is sold at the destination

**Risk mitigation:** Trades are not initiated within 15 ticks of an EIA release (spread may shift dramatically) or during end-of-period wind-down. Pipeline costs are monitored for mid-case changes via the periodic config refresh.

### 5. Crack Spread Engine

**Signal source:** The 3:2:1 refinery crack spread (3 CL contracts input, 2 RB + 1 HO contracts output) exceeding the refinery lease cost by a configurable profit threshold.

**Profitability check:**
```
net_profit = (rb_price * 2 * rb_contract_size + ho_price * ho_contract_size)
           - (cl_price * 3 * cl_contract_size)
           - refinery_lease_cost - transaction_fees
```

Contract sizes and refinery costs are discovered from the API. Due to the high fixed cost of the refinery lease, this strategy activates only during extreme product premiums.

**Non-blocking execution:** The refinery conversion process runs asynchronously. Rather than blocking the main loop, the engine tracks the start tick and checks for completion each iteration, releasing the lease immediately upon completion to avoid double-charging.

### 6. Market Making Engine

**Signal source:** Fair value estimate from the cumulative EIA/news model.

**Mechanism:** Posts limit orders on both sides of fair value on CL-1F during quiet periods (between EIA releases, outside wind-down). Earns the bid-ask spread when filled.

**Inventory management:** Quotes are skewed based on net position — widening the side where inventory is building to reduce accumulation risk.

**Safety:** All market-making orders are cancelled at a configurable number of ticks before each EIA release. Maximum net exposure from market-making fills is capped independently.

---

## Adaptive Parameter Discovery

The system treats all competition parameters as potentially variable across heats. At case startup, the `CaseConfig` class queries four API endpoints and constructs the live configuration:

| Endpoint | Parameters Discovered |
|----------|----------------------|
| `GET /case` | `ticks_per_period`, `total_periods` |
| `GET /securities` | Contract sizes, maximum trade size, trading fees per ticker |
| `GET /limits` | Gross limit, per-ticker net limits |
| `GET /assets` | Lease prices, capacities, transfer times, max concurrent leases |

**Derived values** are recomputed from discovered parameters:
- EIA release ticks scaled proportionally to actual period length
- Wind-down and emergency flatten timing relative to period end
- Storage cost per barrel per day from actual lease economics
- Pipeline arbitrage breakevens from actual lease costs
- Gross limit budget allocation scaled to actual gross limit

**Mid-case detection:** Every 30 ticks, the system re-fetches `/assets` and `/limits` to detect administrator changes or news-driven cost updates. Changed values are logged, and dependent thresholds are recalculated automatically.

---

## Risk Management

### Position Limits
- **Gross limit:** Enforced globally across all tickers. Budget is allocated per-strategy to prevent any single strategy from consuming all capacity.
- **Net limit:** Enforced per-ticker using values discovered from the API. All order placement functions check available net capacity before submission.
- **Order size:** Enforced per-order. Orders exceeding the maximum are automatically split into compliant chunks.

### Storage Penalty Avoidance
The storage manager ensures that spot crude is never purchased without a corresponding storage lease. Storage is leased just-in-time (not pre-emptively) and released when positions are flattened.

### End-of-Period Wind-Down
A two-phase wind-down prevents positions from carrying through settlement unpredictably:
1. **Orderly wind-down** (40 ticks before period end): All resting orders cancelled, scheduled exits cleared, directional positions begin scaling out.
2. **Emergency flatten** (10 ticks before period end): Market orders sent to close all remaining positions. Storage leases released.

### Pre-EIA Cleanup
At a configurable number of ticks before each EIA release, all resting limit orders are cancelled to prevent adverse fills during the volatile post-announcement period.

---

## Observability

### Runtime Logging
All bot output is written to both console (INFO level) and a timestamped log file (DEBUG level) in `runs/<run_id>/bot.log`.

### Structured Event Log
Every news event, EIA trade, and significant action is recorded as a JSON line in `runs/<run_id>/events.jsonl`, enabling programmatic post-run analysis.

### Equity Curve
NLV, gross exposure, fair value, and CL price are sampled every 30 ticks and written to `runs/<run_id>/nlv.csv` for P&L visualization.

### Post-Run Summary
At completion, a `runs/<run_id>/summary.json` file captures final NLV, EIA report count, cumulative impacts, final positions, per-strategy gross usage, and the discovered configuration snapshot.

### Discovered Config Snapshot
The full set of API-discovered parameters is saved to `runs/<run_id>/config.json` at startup, providing an audit trail of what the bot believed the competition parameters to be.

---

## Runtime Adaptation

The supported workflow is to run `bot.py`, let the bot discover case structure directly from the live API, and review the resulting artifacts under `runs/<run_id>/`. Parameter discovery, limit refreshes, and in-heat adaptation happen inside the runtime bot rather than through a required pre-scrape step.

## Legacy Research Tooling

`data_scraper.py` and `analyze_data.py` remain in the repo as optional research utilities for archived datasets and exploratory calibration work. They are not part of the primary competition-day operating path.

---

## File Inventory

| File | Purpose |
|------|---------|
| `bot.py` | Main trading bot — all strategies, adaptive config, risk management, logging |
| `com5_template.py` | Reference API wrapper and scaffold — helper endpoints plus the original starter loop |
| `data_scraper.py` | Legacy research utility for optional practice-heat collection and API experiments |
| `analyze_data.py` | Legacy analysis utility for archived scraper datasets |
| `STRATEGY.md` | Detailed strategy plan and implementation roadmap |
| `.gitignore` | Excludes `data/`, `runs/`, `__pycache__/`, `.claude/` |

---

## Execution

```bash
# Run the competition bot
python bot.py

# Review run results
ls runs/<run_id>/
# -> bot.log, events.jsonl, nlv.csv, config.json, summary.json
```
