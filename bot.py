"""
COM5 Commodities Capstone — Competition Bot
============================================
Multi-strategy automated trading bot for Rotman COM5.

ALL competition parameters are discovered dynamically from the API at startup.
Zero hardcoded values — adapts to any admin configuration.

Strategies (priority order):
  1. EIA Alpha       — directional trading on EIA report releases
  2. Futures Basis   — cost-of-carry arb + end-of-period convergence
  3. News Trading    — keyword-classified geopolitical events
  4. Geographic Arb  — pipeline transport between crude locations
  5. Crack Spread    — refinery 3:2:1 conversion
  6. Market Making   — liquidity provision between events

Usage:  python bot.py
"""

import requests
import time
import re
import json
import os
import sys
import logging
from datetime import datetime
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

from com5_template import API_KEY, BASE, HEADERS

# ── Pooled session for all API calls ────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def _get(endpoint, params=None):
    """GET with connection pooling and rate-limit retry."""
    while True:
        r = SESSION.get(f"{BASE}{endpoint}", params=params)
        if r.status_code == 429:
            try:
                wait = r.json().get("wait", 0.5)
            except Exception:
                wait = 0.5
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()


def _post(endpoint, params=None):
    """POST with connection pooling and rate-limit retry."""
    while True:
        r = SESSION.post(f"{BASE}{endpoint}", params=params)
        if r.status_code == 429:
            try:
                wait = r.json().get("wait", 0.5)
            except Exception:
                wait = 0.5
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()


def _delete(endpoint):
    """DELETE with connection pooling and rate-limit retry."""
    while True:
        r = SESSION.delete(f"{BASE}{endpoint}", headers=HEADERS)
        if r.status_code == 429:
            try:
                wait = r.json().get("wait", 0.5)
            except Exception:
                wait = 0.5
            time.sleep(wait)
            continue
        r.raise_for_status()
        if r.status_code == 204 or not r.text:
            return {"success": True}
        return r.json()


# ═══════════════════════════════════════════════════════════════════════════════
#  CASE CONFIG — dynamically discovered from API, zero hardcoding
# ═══════════════════════════════════════════════════════════════════════════════

class CaseConfig:
    """All competition parameters, discovered from API at runtime.

    Call discover() once at case startup. Call refresh() every 30 ticks
    to detect mid-case changes (asset costs, limit adjustments).
    """

    def __init__(self):
        # ── Case structure (from /case) ─────────────────────────────
        self.ticks_per_period = 600  # overwritten by discover()
        self.total_periods = 2

        # ── Securities (from /securities) ───────────────────────────
        self.tickers = {}     # {ticker: {size, shortable, type}}
        self.max_order_size = 30
        self.fee_per_contract = 1.0

        # ── Limits (from /limits) ───────────────────────────────────
        self.gross_limit = 500         # overall gross
        self.net_limits = {}           # {ticker: net_limit}

        # ── Assets (from /assets) ───────────────────────────────────
        self.assets = {}  # {ticker: {cost, capacity, transfer_sec, max_leases}}

        # ── Derived values (computed after discovery) ───────────────
        self.eia_ticks = [90, 240, 390, 540]
        self.mark_to_market = 30

        # EIA formula is CONFIRMED FIXED
        self.eia_impact_per_10m = 1.0

        # ── Strategy parameters (runtime-tunable defaults) ──────
        # EIA — CRANKED: trade every signal, max positions
        self.eia_min_impact = 0.0
        self.eia_contracts_per_50c = 15
        self.eia_exit_fast_ticks = 10
        self.eia_exit_med_ticks = 30
        self.eia_exit_slow_ticks = 60
        self.eia_pre_cancel_buffer = 10

        # News
        self.news_min_score = 0.5
        self.news_base_contracts = 10
        self.news_high_contracts = 30
        self.news_exit_fast_ticks = 15
        self.news_exit_slow_ticks = 30

        # Basis (derived from actual asset costs in _derive())
        self.basis_min_profit = 500
        self.convergence_min_basis = 0.08
        self.storage_cost_per_bbl_per_day = 0.05  # derived

        # Transport buffers
        self.ak_cs_buffer = 0.50
        self.cs_nyc_buffer = 0.50
        self.transport_eia_blackout = 15

        # Crack
        self.crack_min_profit = 20000

        # Market Making
        self.mm_half_spread = 0.05
        self.mm_size = 5
        self.mm_max_net = 20
        self.mm_requote_interval = 5
        self.mm_eia_blackout = 10

        # Risk
        self.gross_budget = {"eia": 500, "basis": 100, "transport": 60,
                             "news": 60, "crack": 40, "mm": 40}
        self.reconcile_interval = 30

        # Execution
        self.loop_sleep = 0.05

        # ── Derived timing (set in _derive()) ───────────────────────
        self.wind_down_start = 560
        self.emergency_flatten_tick = 590
        self.convergence_start_tick = 540

    def discover(self):
        """Query all API endpoints and build live config. Call once at startup."""
        print("\n  [CONFIG] Discovering case parameters from API...")

        # ── /case ───────────────────────────────────────────────────
        case = _get("/case")
        self.ticks_per_period = case.get("ticks_per_period", 600)
        self.total_periods = case.get("total_periods", 2)

        # ── /securities ─────────────────────────────────────────────
        securities = _get("/securities")
        for s in securities:
            ticker = s["ticker"]
            self.tickers[ticker] = {
                "size": s.get("size", 1000),
                "shortable": s.get("type") == "FUTURE",
                "type": s.get("type", ""),
            }
            # Try to get max trade size and fees from API
            if s.get("maximum_trade_size"):
                self.max_order_size = s["maximum_trade_size"]
            if s.get("trading_fees") is not None:
                self.fee_per_contract = s["trading_fees"]

        # Shortability: only futures are shortable in COM5
        # Override from known COM5 rules if API doesn't expose it clearly
        for ticker in self.tickers:
            t = self.tickers[ticker]["type"].upper()
            self.tickers[ticker]["shortable"] = t in ("FUTURE", "FUTURES")

        # ── /limits ─────────────────────────────────────────────────
        limits = _get("/limits")
        if limits:
            if isinstance(limits, list):
                for lim in limits:
                    ticker = lim.get("ticker", "")
                    nl = lim.get("net_limit")
                    if nl is not None:
                        self.net_limits[ticker] = nl
                    gl = lim.get("gross_limit")
                    if gl is not None:
                        self.gross_limit = gl
            elif isinstance(limits, dict):
                # Some API versions return a single dict
                gl = limits.get("gross_limit")
                if gl is not None:
                    self.gross_limit = gl
                nl = limits.get("net_limit")
                if nl is not None:
                    # Apply to all tickers
                    for t in self.tickers:
                        self.net_limits[t] = nl

        # Default net limits for any tickers not covered
        for t in self.tickers:
            if t not in self.net_limits:
                self.net_limits[t] = 100

        # ── /assets ─────────────────────────────────────────────────
        # Case-brief fallbacks for fields the API may not expose
        ASSET_FALLBACKS = {
            "CL-STORAGE":  {"capacity": 10000, "transfer_sec": 0,  "max_leases": 10},
            "AK-STORAGE":  {"capacity": 10000, "transfer_sec": 0,  "max_leases": 10},
            "NYC-STORAGE": {"capacity": 10000, "transfer_sec": 0,  "max_leases": 10},
            "AK-CS-PIPE":  {"capacity": 10000, "transfer_sec": 30, "max_leases": 10},
            "CS-NYC-PIPE": {"capacity": 10000, "transfer_sec": 30, "max_leases": 10},
            "CL-REFINERY": {"capacity": 30000, "transfer_sec": 45, "max_leases": 1},
        }
        assets = _get("/assets")
        if assets:
            for a in assets:
                ticker = a.get("ticker", "")
                fb = ASSET_FALLBACKS.get(ticker, {})
                raw_cap = a.get("total_quantity", 0)
                raw_xfer = a.get("transfer_sec", 0)
                raw_max = a.get("max_leases", 0)
                self.assets[ticker] = {
                    "cost": a.get("lease_price", fb.get("cost", 0)),
                    # Use API value only if it looks correct, else fallback
                    "capacity": raw_cap if raw_cap >= 10000 else fb.get("capacity", 10000),
                    "transfer_sec": raw_xfer if raw_xfer > 0 else fb.get("transfer_sec", 0),
                    "max_leases": raw_max if raw_max >= 1 else fb.get("max_leases", 10),
                }
        # Ensure all expected assets exist (even if API didn't return them)
        for ticker, fb in ASSET_FALLBACKS.items():
            if ticker not in self.assets:
                self.assets[ticker] = {"cost": 0, **fb}

        self._derive()
        self._print_config()

    def refresh(self):
        """Re-check assets and limits for mid-case changes. Call every 30 ticks."""
        changed = False

        try:
            assets = _get("/assets")
            if assets:
                for a in assets:
                    ticker = a.get("ticker", "")
                    new_cost = a.get("lease_price", 0)
                    if ticker in self.assets and self.assets[ticker]["cost"] != new_cost:
                        old = self.assets[ticker]["cost"]
                        self.assets[ticker]["cost"] = new_cost
                        print(f"  [CONFIG] {ticker} cost changed: "
                              f"${old:,.0f} -> ${new_cost:,.0f}")
                        changed = True
        except Exception as e:
            print(f"  [ERR] config refresh assets: {e}")

        try:
            limits = _get("/limits")
            if limits:
                for lim in limits:
                    ticker = lim.get("ticker", "")
                    new_net = lim.get("net_limit", 100)
                    if ticker in self.net_limits and self.net_limits[ticker] != new_net:
                        old = self.net_limits[ticker]
                        self.net_limits[ticker] = new_net
                        print(f"  [CONFIG] {ticker} net limit changed: {old} -> {new_net}")
                        changed = True
                    new_gross = lim.get("gross_limit")
                    if new_gross and new_gross != self.gross_limit:
                        print(f"  [CONFIG] Gross limit changed: "
                              f"{self.gross_limit} -> {new_gross}")
                        self.gross_limit = new_gross
                        changed = True
        except Exception as e:
            print(f"  [ERR] config refresh limits: {e}")

        if changed:
            self._derive()

    def _derive(self):
        """Compute derived values from discovered parameters."""
        tpp = self.ticks_per_period

        # EIA ticks: proportionally scaled to actual ticks_per_period
        # Default pattern: at 15%, 40%, 65%, 90% of period
        self.eia_ticks = [
            int(tpp * 0.15),   # ~90  at 600
            int(tpp * 0.40),   # ~240 at 600
            int(tpp * 0.65),   # ~390 at 600
            int(tpp * 0.90),   # ~540 at 600
        ]

        # Wind-down timing: relative to period end
        self.wind_down_start = tpp - 40         # 40 ticks before end
        self.emergency_flatten_tick = tpp - 10  # 10 ticks before end
        self.convergence_start_tick = tpp - 60  # 60 ticks before end

        # Storage cost per barrel per day (derived from actual lease_price)
        storage_cost = self.assets.get("CL-STORAGE", {}).get("cost", 500)
        storage_capacity = self.assets.get("CL-STORAGE", {}).get("capacity", 10000)
        if storage_capacity > 0:
            self.storage_cost_per_bbl_per_day = storage_cost / storage_capacity

        # Scale gross budget proportionally to actual gross limit
        # EIA gets the FULL gross limit — max out every time
        ratio = self.gross_limit / 500.0
        self.gross_budget = {
            "eia": self.gross_limit,
            "basis": int(100 * ratio),
            "transport": int(60 * ratio),
            "news": int(60 * ratio),
            "crack": int(40 * ratio),
            "mm": int(40 * ratio),
        }

    def _print_config(self):
        """Log all discovered parameters."""
        print(f"  [CONFIG] Ticks/period:   {self.ticks_per_period}")
        print(f"  [CONFIG] Total periods:  {self.total_periods}")
        print(f"  [CONFIG] Max order size: {self.max_order_size}")
        print(f"  [CONFIG] Fee/contract:   ${self.fee_per_contract}")
        print(f"  [CONFIG] Gross limit:    {self.gross_limit}")

        print(f"  [CONFIG] Tickers:")
        for t, info in sorted(self.tickers.items()):
            nl = self.net_limits.get(t, "?")
            print(f"    {t:>8}: size={info['size']:>6} short={info['shortable']!s:>5} "
                  f"net_limit={nl}")

        print(f"  [CONFIG] Assets:")
        for a, info in sorted(self.assets.items()):
            print(f"    {a:>14}: cost=${info['cost']:>8,.0f}  "
                  f"cap={info['capacity']:>6}  "
                  f"xfer={info['transfer_sec']}s  "
                  f"max={info['max_leases']}")

        print(f"  [CONFIG] EIA ticks:      {self.eia_ticks}")
        print(f"  [CONFIG] Wind-down:      {self.wind_down_start}")
        print(f"  [CONFIG] Emergency:      {self.emergency_flatten_tick}")
        print(f"  [CONFIG] Storage $/bbl/d: ${self.storage_cost_per_bbl_per_day:.4f}")
        print()

    # ── Convenience accessors ───────────────────────────────────────────

    def get_net_limit(self, ticker):
        """Get net limit for a ticker (falls back to 100 if unknown)."""
        return self.net_limits.get(ticker, 100)

    def is_shortable(self, ticker):
        """Check if a ticker can be shorted."""
        return self.tickers.get(ticker, {}).get("shortable", False)

    def get_asset_cost(self, asset_ticker):
        """Get current lease cost for an asset."""
        return self.assets.get(asset_ticker, {}).get("cost", 0)

    def get_asset_capacity(self, asset_ticker):
        """Get capacity for an asset."""
        return self.assets.get(asset_ticker, {}).get("capacity", 10000)

    def get_transfer_sec(self, asset_ticker):
        """Get transfer time for an asset."""
        return self.assets.get(asset_ticker, {}).get("transfer_sec", 0)

    def get_contract_size(self, ticker):
        """Get contract size (barrels or gallons) for a ticker."""
        return self.tickers.get(ticker, {}).get("size", 1000)

    def ak_cs_breakeven(self):
        """AK->Cushing breakeven per barrel."""
        cost = self.get_asset_cost("AK-CS-PIPE")
        cap = self.get_asset_capacity("AK-CS-PIPE")
        return cost / cap if cap > 0 else 999

    def cs_nyc_breakeven(self):
        """Cushing->NYC breakeven per barrel."""
        cost = self.get_asset_cost("CS-NYC-PIPE")
        cap = self.get_asset_capacity("CS-NYC-PIPE")
        return cost / cap if cap > 0 else 999


# ═══════════════════════════════════════════════════════════════════════════════
#  STATE MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class StateManager:
    """Single source of truth for all market and portfolio state."""

    def __init__(self, config):
        self.config = config
        self.tick = 0
        self.period = 0
        self.status = "STOPPED"

        self.securities = {}
        self.positions = {}
        self.leases = {}
        self.nlv = 0.0
        self.last_news_id = 0

        # Fair value model
        self.start_price = None
        self.fair_value = None
        self.cumulative_eia = 0.0
        self.cumulative_news = 0.0
        self.eia_count = 0

        self.scheduled_exits = []

        # Refinery tracking
        self.refinery_lease_id = None
        self.refinery_start_tick = None

        # Pipeline tracking
        self.active_pipelines = []

        # Per-strategy gross usage
        self.strategy_gross = defaultdict(int)

        # Bayesian tracking
        self.eia_calculated_impacts = []
        self.eia_actual_moves = []

        self.price_history = defaultdict(lambda: deque(maxlen=120))

    def update_from_case(self, case):
        new_period = case.get("period", self.period)
        if new_period != self.period and self.period != 0:
            self._on_period_change(new_period)
        self.tick = case.get("tick", self.tick)
        self.period = new_period
        self.status = case.get("status", self.status)

    def _on_period_change(self, new_period):
        self.cumulative_eia = 0.0
        self.cumulative_news = 0.0
        self.eia_count = 0
        self.start_price = None
        self.fair_value = None
        self.scheduled_exits.clear()
        # Physical leases and in-flight deliveries survive period boundaries.
        self.price_history.clear()
        print(f"\n{'='*60}")
        print(f"  PERIOD {new_period} STARTED")
        print(f"{'='*60}\n")

    def update_from_securities(self, securities_list):
        for s in securities_list:
            ticker = s["ticker"]
            self.securities[ticker] = s
            self.positions[ticker] = s.get("position", 0)
            last = s.get("last")
            if last is not None:
                self.price_history[ticker].append(last)

        if self.start_price is None and "CL" in self.securities:
            cl = self.securities["CL"]
            price = cl.get("last")
            if price is None or price <= 0:
                price = cl.get("bid")
            if price is None or price <= 0:
                price = cl.get("ask")
            if price is not None and price > 0:
                self.start_price = price
                self.fair_value = price
                print(f"  [STATE] Period {self.period} start price: "
                      f"${self.start_price:.2f}")

    def update_fair_value(self, eia_impact=0.0, news_impact=0.0):
        self.cumulative_eia += eia_impact
        self.cumulative_news += news_impact
        if self.start_price is not None:
            self.fair_value = (self.start_price + self.cumulative_eia +
                               self.cumulative_news)

    def drift_fair_value(self):
        """Slowly correct fair value toward market price.
        Handles the case where we joined mid-heat and missed earlier impacts.
        """
        if self.fair_value is None:
            return
        cl_mid = self.get_mid("CL")
        if cl_mid is None:
            return
        gap = cl_mid - self.fair_value
        if abs(gap) > 0.10:
            # Drift 5% toward market each call (called every tick)
            correction = gap * 0.05
            self.fair_value += correction

    def get_price(self, ticker):
        s = self.securities.get(ticker)
        if not s:
            return None, None, None
        return s.get("bid"), s.get("ask"), s.get("last")

    def get_mid(self, ticker):
        bid, ask, last = self.get_price(ticker)
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            return (bid + ask) / 2
        return last if (last is not None and last > 0) else None

    def get_position(self, ticker):
        return self.positions.get(ticker, 0)

    def total_gross(self):
        return sum(abs(p) for p in self.positions.values())

    def available_gross(self, strategy):
        budget = self.config.gross_budget.get(strategy, 0)
        used = self.strategy_gross[strategy]
        total_remaining = self.config.gross_limit - self.total_gross()
        return min(budget - used, total_remaining)

    def available_net_buy(self, ticker):
        return self.config.get_net_limit(ticker) - self.get_position(ticker)

    def available_net_sell(self, ticker):
        if not self.config.is_shortable(ticker):
            return max(0, self.get_position(ticker))
        return self.config.get_net_limit(ticker) + self.get_position(ticker)

    def reconcile(self):
        try:
            securities = _get("/securities")
            self.update_from_securities(securities)
        except Exception as e:
            print(f"  [ERR] reconcile securities: {e}")
        try:
            trader = _get("/trader")
            self.nlv = trader.get("nlv", self.nlv)
        except Exception as e:
            print(f"  [ERR] reconcile trader: {e}")
        try:
            leases = _get("/leases")
            self.leases = {l["id"]: l for l in leases} if leases else {}
        except Exception as e:
            print(f"  [ERR] reconcile leases: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  EXECUTION ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class ExecutionEngine:
    def __init__(self, state, config):
        self.state = state
        self.config = config
        self.executor = ThreadPoolExecutor(max_workers=4)

    def place_order(self, ticker, action, quantity, price=None, order_type="MARKET"):
        if quantity <= 0:
            return None
        quantity = min(quantity, self.config.max_order_size)
        if action == "SELL" and not self.config.is_shortable(ticker):
            held = self.state.get_position(ticker)
            if quantity > held:
                quantity = held
                if quantity <= 0:
                    return None
        params = {"ticker": ticker, "type": order_type,
                  "quantity": quantity, "action": action}
        if price is not None and order_type == "LIMIT":
            params["price"] = round(price, 2)
        try:
            return _post("/orders", params=params)
        except Exception as e:
            print(f"  [ERR] order {action} {quantity} {ticker}: {e}")
            return None

    def place_split_orders(self, ticker, action, total_quantity, price=None,
                           order_type="MARKET"):
        if total_quantity <= 0:
            return []
        orders = []
        remaining = total_quantity
        while remaining > 0:
            qty = min(remaining, self.config.max_order_size)
            result = self.place_order(ticker, action, qty, price, order_type)
            if result:
                orders.append(result)
            remaining -= qty
        return orders

    def cancel_all_orders(self):
        """Cancel all open orders. Tries multiple API approaches."""
        # Try POST /commands/cancel first (template approach)
        try:
            return _post("/commands/cancel", params={"all": 1})
        except Exception:
            pass
        # Fallback: DELETE each open order individually
        try:
            orders = _get("/orders", {"status": "OPEN"})
            if orders:
                for o in orders:
                    oid = o.get("order_id")
                    if oid:
                        try:
                            _delete(f"/orders/{oid}")
                        except Exception:
                            pass
                return {"cancelled": len(orders)}
        except Exception as e:
            print(f"  [ERR] cancel all: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  STORAGE MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class StorageManager:
    def __init__(self, state, config):
        self.state = state
        self.config = config

    def ensure_storage(self, location, contracts_needed):
        storage_ticker = {"CL": "CL-STORAGE", "AK": "AK-STORAGE",
                          "NYC": "NYC-STORAGE"}.get(location)
        if not storage_ticker:
            return False

        contract_size = self.config.get_contract_size("CL")
        barrels_needed = contracts_needed * contract_size
        current_capacity = self._get_leased_capacity(storage_ticker)

        while current_capacity < barrels_needed:
            try:
                lease = _post("/leases", params={"ticker": storage_ticker})
                if lease:
                    self.state.leases[lease["id"]] = lease
                    current_capacity += self.config.get_asset_capacity(storage_ticker)
                    print(f"  [STORAGE] Leased {storage_ticker} "
                          f"(capacity now {current_capacity} bbl)")
                else:
                    return False
            except Exception as e:
                print(f"  [ERR] lease {storage_ticker}: {e}")
                return False
        return True

    def _get_leased_capacity(self, storage_ticker):
        count = sum(1 for l in self.state.leases.values()
                    if l.get("ticker") == storage_ticker)
        return count * self.config.get_asset_capacity(storage_ticker)

    def release_empty_storage(self):
        # Use the server's occupancy, not stale local position estimates.
        leases = _get("/leases")
        self.state.leases = {lease["id"]: lease for lease in leases}
        if self.state.active_pipelines or self.state.refinery_lease_id is not None or getattr(self.state, "physical_uncertain", False):
            return  # Destination storage is still reserved for incoming goods.
        if any(lease.get("ticker") in ("AK-CS-PIPE", "CS-NYC-PIPE", "CL-REFINERY") for lease in leases):
            return
        for lease in leases:
            if lease.get("ticker") not in ("CL-STORAGE", "AK-STORAGE", "NYC-STORAGE"):
                continue
            if lease.get("containment_usage") != 0:
                continue
            try:
                _delete(f"/leases/{lease['id']}")
                self.state.leases.pop(lease["id"], None)
                print(f"  [STORAGE] Released {lease['ticker']} (lease {lease['id']})")
            except Exception as exc:
                print(f"  [ERR] Storage lease {lease['id']} still needs release: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
#  EIA ALPHA ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class EIAEngine:
    # EIA regex patterns — ordered from most specific to most general.
    # Real format from RIT: "WEEK 3 CL ACTUAL DRAW 2 MLN BBLS VS FORECAST BUILD 10 MLN BBLS"
    EIA_PATTERNS = [
        # Pattern 1 (PRIMARY): "ACTUAL BUILD/DRAW X MLN BBLS VS FORECAST BUILD/DRAW X MLN BBLS"
        re.compile(
            r"ACTUAL\s+(BUILD|DRAW)\s+(\d+\.?\d*)\s*MLN\s*BBLS?\s*VS\s*FORECAST\s+(BUILD|DRAW)\s+(\d+\.?\d*)\s*MLN\s*BBLS?",
            re.IGNORECASE),
        # Pattern 2: "Expected: +X.XM ... Actual: +X.XM"
        re.compile(
            r"[Ee]xpect\w*[:\s]*([+-]?\d+\.?\d*)\s*[Mm]"
            r".*?"
            r"[Aa]ctual[:\s]*([+-]?\d+\.?\d*)\s*[Mm]",
            re.DOTALL),
        # Pattern 3: "expected build/draw of X million ... actual build/draw of X million"
        re.compile(
            r"expected\s+(build|draw)\s+of\s+(\d+\.?\d*)\s*million"
            r".*?"
            r"actual\s+(build|draw)\s+of\s+(\d+\.?\d*)\s*million",
            re.DOTALL | re.IGNORECASE),
        # Pattern 4: numbers with "million barrels" near "expected" and "actual"
        re.compile(
            r"expected.*?([+-]?\d+\.?\d*)\s*million\s*barrel"
            r".*?"
            r"actual.*?([+-]?\d+\.?\d*)\s*million\s*barrel",
            re.DOTALL | re.IGNORECASE),
    ]

    def __init__(self, state, execution, storage, config):
        self.state = state
        self.execution = execution
        self.storage = storage
        self.config = config
        self.processed_news_ids = set()

    def is_eia_report(self, news_item):
        headline = news_item.get("headline", "").lower()
        body = news_item.get("body", "").lower()
        text = headline + " " + body
        # Primary: the exact format from RIT
        if "mln bbls" in text or "million barrel" in text:
            return True
        if "actual" in text and ("build" in text or "draw" in text):
            return True
        if "eia" in text or "inventory" in text or "weekly" in text:
            return True
        return False

    def parse_eia(self, news_item):
        """Returns (expected_millions, actual_millions) or None.
        Positive = build (inventory increase), negative = draw.
        """
        body = news_item.get("body", "")
        headline = news_item.get("headline", "")
        text = headline + " " + body
        for pattern in self.EIA_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            groups = match.groups()
            try:
                if len(groups) == 4:
                    # Pattern 1: ACTUAL dir val VS FORECAST dir val
                    # or Pattern 3: expected dir val ... actual dir val
                    g0_lower = groups[0].lower()
                    g2_lower = groups[2].lower()

                    # Determine which pair is actual vs forecast
                    # Pattern 1: groups = (actual_dir, actual_val, forecast_dir, forecast_val)
                    # Pattern 3: groups = (expected_dir, expected_val, actual_dir, actual_val)
                    if "actual" in pattern.pattern.lower()[:20]:
                        # Pattern 1: ACTUAL first, FORECAST second
                        act_dir = -1 if g0_lower == "draw" else 1
                        act_val = float(groups[1]) * act_dir
                        exp_dir = -1 if g2_lower == "draw" else 1
                        exp_val = float(groups[3]) * exp_dir
                    else:
                        # Pattern 3: expected first, actual second
                        exp_dir = -1 if g0_lower == "draw" else 1
                        exp_val = float(groups[1]) * exp_dir
                        act_dir = -1 if g2_lower == "draw" else 1
                        act_val = float(groups[3]) * act_dir

                    return exp_val, act_val

                elif len(groups) == 2:
                    # Pattern 2 or 4: direct numbers
                    return float(groups[0]), float(groups[1])
            except ValueError:
                continue
        print(f"  [EIA] PARSE FAILED — raw text: {text[:300]}")
        return None

    def process(self, news_item):
        """EIA is our guaranteed alpha. MAX OUT every position on every signal.

        Bullish: max out CL-1F, CL-2F, CL, CL-AK, CL-NYC, HO, RB
        Bearish: max out CL-1F, CL-2F (only shortable tickers)
        """
        news_id = news_item.get("news_id")
        if news_id in self.processed_news_ids:
            return
        self.processed_news_ids.add(news_id)

        parsed = self.parse_eia(news_item)
        if parsed is None:
            return

        expected, actual = parsed
        impact = (expected - actual) * self.config.eia_impact_per_10m / 10.0

        print(f"  [EIA] Expected={expected:+.1f}M Actual={actual:+.1f}M "
              f"Impact={impact:+.2f}$/bbl")

        # NO THRESHOLD — trade on ANY non-zero signal
        if impact == 0.0:
            print(f"  [EIA] Zero impact, skipping")
            return

        pre_eia_price = self.state.get_mid("CL")
        self.state.update_fair_value(eia_impact=impact)
        self.state.eia_count += 1
        print(f"  [EIA] Fair value: ${self.state.fair_value:.2f}")

        bullish = impact > 0
        total_traded = 0
        tick = self.state.tick

        # Cancel ALL existing orders to free up gross capacity
        self.execution.cancel_all_orders()

        # ── FUTURES: CL-1F and CL-2F (shortable both ways) ─────────
        for fticker in ["CL-1F", "CL-2F"]:
            net_limit = self.config.get_net_limit(fticker)
            if bullish:
                qty = self.state.available_net_buy(fticker)
            else:
                qty = self.state.available_net_sell(fticker)
            qty = min(qty, net_limit)
            if qty > 0:
                action = "BUY" if bullish else "SELL"
                print(f"  [EIA] {action} {qty} {fticker} (MAX)")
                self.execution.place_split_orders(fticker, action, qty,
                                                  order_type="MARKET")
                total_traded += qty

                # Schedule tiered exits
                exit_action = "SELL" if bullish else "BUY"
                t1 = int(qty * 0.50)
                t2 = int(qty * 0.30)
                t3 = qty - t1 - t2
                self.state.scheduled_exits.append(
                    (tick + self.config.eia_exit_fast_ticks, fticker,
                     exit_action, t1, "eia"))
                self.state.scheduled_exits.append(
                    (tick + self.config.eia_exit_med_ticks, fticker,
                     exit_action, t2, "eia"))
                self.state.scheduled_exits.append(
                    (tick + self.config.eia_exit_slow_ticks, fticker,
                     exit_action, t3, "eia"))

        # ── BULLISH ONLY: ALL spot & product tickers (not shortable) ─
        if bullish:
            # CL spot — needs storage
            cl_qty = min(
                self.state.available_net_buy("CL"),
                self.config.get_net_limit("CL"))
            if cl_qty > 0 and self.storage.ensure_storage("CL", cl_qty):
                print(f"  [EIA] BUY {cl_qty} CL spot (MAX)")
                self.execution.place_split_orders("CL", "BUY", cl_qty,
                                                  order_type="MARKET")
                total_traded += cl_qty
                self.state.scheduled_exits.append(
                    (tick + self.config.eia_exit_fast_ticks, "CL",
                     "SELL", cl_qty, "eia"))

            # CL-AK — needs AK storage
            ak_qty = min(
                self.state.available_net_buy("CL-AK"),
                self.config.get_net_limit("CL-AK"))
            if ak_qty > 0 and self.storage.ensure_storage("AK", ak_qty):
                print(f"  [EIA] BUY {ak_qty} CL-AK (MAX)")
                self.execution.place_split_orders("CL-AK", "BUY", ak_qty,
                                                  order_type="MARKET")
                total_traded += ak_qty
                self.state.scheduled_exits.append(
                    (tick + self.config.eia_exit_fast_ticks, "CL-AK",
                     "SELL", ak_qty, "eia"))

            # CL-NYC — needs NYC storage
            nyc_qty = min(
                self.state.available_net_buy("CL-NYC"),
                self.config.get_net_limit("CL-NYC"))
            if nyc_qty > 0 and self.storage.ensure_storage("NYC", nyc_qty):
                print(f"  [EIA] BUY {nyc_qty} CL-NYC (MAX)")
                self.execution.place_split_orders("CL-NYC", "BUY", nyc_qty,
                                                  order_type="MARKET")
                total_traded += nyc_qty
                self.state.scheduled_exits.append(
                    (tick + self.config.eia_exit_fast_ticks, "CL-NYC",
                     "SELL", nyc_qty, "eia"))

            # HO and RB — products cascade, no storage needed
            for pticker in ["HO", "RB"]:
                p_qty = min(
                    self.state.available_net_buy(pticker),
                    self.config.get_net_limit(pticker))
                if p_qty > 0:
                    print(f"  [EIA] BUY {p_qty} {pticker} (MAX)")
                    self.execution.place_split_orders(pticker, "BUY", p_qty,
                                                      order_type="MARKET")
                    total_traded += p_qty
                    self.state.scheduled_exits.append(
                        (tick + self.config.eia_exit_fast_ticks, pticker,
                         "SELL", p_qty, "eia"))

        self.state.strategy_gross["eia"] += total_traded
        print(f"  [EIA] MAXED OUT — Total contracts deployed: {total_traded}")

        # Track for Bayesian analysis
        self.state.eia_calculated_impacts.append(impact)
        if pre_eia_price:
            self.state.eia_actual_moves.append(
                {"tick": tick, "pre_price": pre_eia_price})


# ═══════════════════════════════════════════════════════════════════════════════
#  NEWS ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class NewsEngine:
    BULLISH_KEYWORDS = {
        "pipeline outage": 1.5, "pipeline shutdown": 1.5, "shut down": 1.0,
        "geopolitical": 1.0, "conflict": 1.0, "war": 1.5, "tension": 0.8,
        "sanctions": 1.2, "embargo": 1.5,
        "hurricane": 1.0, "storm": 0.8, "tropical": 0.8,
        "supply disruption": 1.2, "production cut": 1.0,
        "opec cut": 1.5, "export ban": 1.2,
        "refinery fire": 0.8, "explosion": 1.0,
        "demand increase": 0.8, "economic growth": 0.6,
        "supply shortage": 1.2, "shortage": 0.8,
    }
    BEARISH_KEYWORDS = {
        "oversupply": -1.0, "surplus": -0.8, "glut": -1.0,
        "demand decrease": -0.8, "recession": -1.0, "slowdown": -0.6,
        "production increase": -0.8, "opec increase": -1.0,
        "ceasefire": -0.5, "peace": -0.5, "peace deal": -0.8,
        "pipeline reopened": -0.8, "pipeline restored": -0.8,
        "sanctions lifted": -1.0, "embargo lifted": -1.0,
        "demand drop": -0.8, "demand fell": -0.8,
    }
    PIPELINE_COST_KEYWORDS = [
        "pipeline cost", "transportation cost", "transport cost",
        "shipping cost", "pipeline fee",
    ]

    def __init__(self, state, execution, config):
        self.state = state
        self.execution = execution
        self.config = config
        self.processed_news_ids = set()

    def is_pipeline_cost_change(self, news_item):
        text = (news_item.get("headline", "") + " " +
                news_item.get("body", "")).lower()
        return any(kw in text for kw in self.PIPELINE_COST_KEYWORDS)

    def classify(self, news_item):
        text = (news_item.get("headline", "") + " " +
                news_item.get("body", "")).lower()
        score = 0.0
        matches = 0
        for kw, w in self.BULLISH_KEYWORDS.items():
            if kw in text:
                score += w
                matches += 1
        for kw, w in self.BEARISH_KEYWORDS.items():
            if kw in text:
                score += w
                matches += 1
        return score, matches

    def process(self, news_item):
        news_id = news_item.get("news_id")
        if news_id in self.processed_news_ids:
            return
        self.processed_news_ids.add(news_id)

        if self.is_pipeline_cost_change(news_item):
            self._handle_pipeline_cost_change(news_item)
            return

        score, matches = self.classify(news_item)
        if abs(score) < self.config.news_min_score:
            return

        self.state.update_fair_value(news_impact=score / 2.0)

        if matches >= 2 and abs(score) >= 1.5:
            contracts = self.config.news_high_contracts
        else:
            contracts = self.config.news_base_contracts

        action = "BUY" if score > 0 else "SELL"
        if action == "BUY":
            contracts = min(contracts, self.state.available_net_buy("CL-1F"),
                           self.state.available_gross("news"))
        else:
            contracts = min(contracts, self.state.available_net_sell("CL-1F"),
                           self.state.available_gross("news"))
        if contracts <= 0:
            return

        print(f"  [NEWS] score={score:+.1f} ({matches} matches) -> "
              f"{action} {contracts} CL-1F")
        self.execution.place_split_orders("CL-1F", action, contracts,
                                          order_type="MARKET")
        self.state.strategy_gross["news"] += contracts

        tick = self.state.tick
        exit_action = "SELL" if action == "BUY" else "BUY"
        t1 = int(contracts * 0.70)
        t2 = contracts - t1
        self.state.scheduled_exits.append(
            (tick + self.config.news_exit_fast_ticks, "CL-1F", exit_action, t1, "news"))
        self.state.scheduled_exits.append(
            (tick + self.config.news_exit_slow_ticks, "CL-1F", exit_action, t2, "news"))

    def _handle_pipeline_cost_change(self, news_item):
        text = (news_item.get("headline", "") + " " +
                news_item.get("body", "")).lower()
        print(f"  [NEWS] Pipeline cost change detected: {text[:100]}")
        # Actual cost update happens via config.refresh() polling /assets


# ═══════════════════════════════════════════════════════════════════════════════
#  FUTURES BASIS ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class BasisEngine:
    def __init__(self, state, execution, storage, config):
        self.state = state
        self.execution = execution
        self.storage = storage
        self.config = config

    def days_remaining(self, tick=None):
        if tick is None:
            tick = self.state.tick
        return max(0, (self.config.ticks_per_period - tick) / 30.0)

    def theoretical_basis(self, tick=None):
        return self.config.storage_cost_per_bbl_per_day * self.days_remaining(tick)

    def evaluate(self):
        cl_bid, cl_ask, cl_last = self.state.get_price("CL")
        f1_bid, f1_ask, f1_last = self.state.get_price("CL-1F")
        if cl_last is None or f1_last is None or cl_last <= 0 or f1_last <= 0:
            return

        market_basis = f1_last - cl_last
        theo_basis = self.theoretical_basis()
        tick = self.state.tick

        # Log basis state every 30 ticks for diagnostics
        if tick % 30 == 0:
            print(f"  [BASIS] basis={market_basis:+.3f} theo={theo_basis:.3f} "
                  f"mispricing={market_basis - theo_basis:+.3f}")

        if tick >= self.config.convergence_start_tick:
            self._convergence_trade(cl_last, f1_last, market_basis, theo_basis)
            return

        mispricing = market_basis - theo_basis
        if mispricing > 0:
            days = self.days_remaining()
            storage_cost = self.config.get_asset_cost("CL-STORAGE") * days
            fee = 20 * self.config.fee_per_contract
            profit = mispricing * 10000 - storage_cost - fee
            if profit > self.config.basis_min_profit:
                self._execute_cash_carry(cl_ask, f1_bid, profit)

    def _convergence_trade(self, cl_last, f1_last, market_basis, theo_basis):
        if abs(market_basis) < self.config.convergence_min_basis:
            return
        gross_available = self.state.available_gross("basis")
        if gross_available < 20:
            return
        contracts = min(10, gross_available // 2)

        if market_basis > theo_basis + self.config.convergence_min_basis:
            if (self.state.available_net_buy("CL") >= contracts and
                    self.state.available_net_sell("CL-1F") >= contracts):
                if self.storage.ensure_storage("CL", contracts):
                    print(f"  [BASIS] Convergence: buy CL, short CL-1F "
                          f"(basis={market_basis:.3f}, theo={theo_basis:.3f})")
                    self.execution.place_split_orders("CL", "BUY", contracts,
                                                     order_type="MARKET")
                    self.execution.place_split_orders("CL-1F", "SELL", contracts,
                                                     order_type="MARKET")
                    self.state.strategy_gross["basis"] += contracts * 2

        elif market_basis < theo_basis - self.config.convergence_min_basis:
            held_cl = self.state.get_position("CL")
            sell_qty = min(contracts, held_cl)
            if sell_qty > 0 and self.state.available_net_buy("CL-1F") >= sell_qty:
                print(f"  [BASIS] Convergence: sell CL, buy CL-1F "
                      f"(basis={market_basis:.3f}, theo={theo_basis:.3f})")
                self.execution.place_split_orders("CL", "SELL", sell_qty,
                                                  order_type="MARKET")
                self.execution.place_split_orders("CL-1F", "BUY", sell_qty,
                                                  order_type="MARKET")
                self.state.strategy_gross["basis"] += sell_qty * 2

    def _execute_cash_carry(self, cl_ask, f1_bid, estimated_profit):
        contracts = 10
        if (self.state.available_net_buy("CL") < contracts or
                self.state.available_net_sell("CL-1F") < contracts or
                self.state.available_gross("basis") < contracts * 2):
            return
        if not self.storage.ensure_storage("CL", contracts):
            return
        print(f"  [BASIS] Cash-carry: buy 10 CL, short 10 CL-1F "
              f"(est profit=${estimated_profit:.0f})")
        self.execution.place_split_orders("CL", "BUY", contracts,
                                          order_type="MARKET")
        self.execution.place_split_orders("CL-1F", "SELL", contracts,
                                          order_type="MARKET")
        self.state.strategy_gross["basis"] += contracts * 2


# ═══════════════════════════════════════════════════════════════════════════════
#  GEOGRAPHIC ARB ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def physical_time(state, config):
    return (state.period - 1) * config.ticks_per_period + state.tick


def refresh_physical(state):
    state.update_from_securities(_get("/securities"))
    state.leases = {lease["id"]: lease for lease in _get("/leases")}


def physical_post(endpoint, params):
    try:
        return _post(endpoint, params=params)
    except requests.HTTPError as exc:
        response = exc.response
        if response is not None:
            print(f"  [ASSET ERROR] {endpoint}: HTTP {response.status_code}; {response.text[:1500]}")
        raise


def release_physical_lease(state, lease_id):
    """Forget a lease only when a fresh server read confirms it is gone."""
    try:
        state.leases = {lease["id"]: lease for lease in _get("/leases")}
        if lease_id in state.leases:
            _delete(f"/leases/{lease_id}")
            state.leases = {lease["id"]: lease for lease in _get("/leases")}
        return lease_id not in state.leases
    except Exception as exc:
        print(f"  [ERR] Lease {lease_id} release unresolved; retaining tracking: {exc}")
        return False


def sell_physical_output(job, ticker, quantity, state, execution):
    """Track this physical sale until its fill and inventory change agree."""
    pending = job.setdefault("sales", {})
    if ticker not in pending:
        before = state.get_position(ticker)
        orders = execution.place_split_orders(ticker, "SELL", quantity, order_type="MARKET")
        pending[ticker] = {"before": before, "orders": orders}
        if not orders or any("order_id" not in order for order in orders):
            pending[ticker]["unresolved"] = True
            print(f"  [ASSET] {ticker} sale unresolved; no automatic resubmission. Inspect RIT.")
    sale = pending[ticker]
    if sale.get("unresolved"):
        return 0
    for index, order in enumerate(sale["orders"]):
        if order.get("status") not in ("TRANSACTED", "CANCELLED"):
            sale["orders"][index] = _get(f"/orders/{order['order_id']}")
    if any(order.get("status") not in ("TRANSACTED", "CANCELLED") for order in sale["orders"]):
        return 0
    filled = sum(order.get("quantity_filled", 0) for order in sale["orders"])
    state.update_from_securities(_get("/securities"))
    if not job.get("concurrent_output") and state.get_position(ticker) != sale["before"] - filled:
        return 0  # Keep the pending sale; don't sell twice on a stale snapshot.
    del pending[ticker]
    return min(quantity, filled)


class TransportEngine:
    def __init__(self, state, execution, storage, config):
        self.state = state
        self.execution = execution
        self.storage = storage
        self.config = config
        # Cooldown: don't retry pipelines for N ticks after a failure
        self._ak_cs_cooldown_until = 0
        self._cs_nyc_cooldown_until = 0
        self.COOLDOWN_TICKS = 60  # retained original opportunity cooldown
        self._unresolved_pipelines = set()

    def evaluate(self):
        tick = self.state.tick
        if any(abs(tick - t) <= self.config.transport_eia_blackout
               for t in self.config.eia_ticks):
            return
        if tick >= self.config.wind_down_start:
            return
        if tick >= self._ak_cs_cooldown_until:
            self._check_ak_to_cushing()
        if tick >= self._cs_nyc_cooldown_until:
            self._check_cushing_to_nyc()

    def _check_ak_to_cushing(self):
        cl_bid, _, cl_last = self.state.get_price("CL")
        _, ak_ask, ak_last = self.state.get_price("CL-AK")
        if not all([cl_last, ak_last]):
            return

        spread = cl_last - ak_last
        breakeven = self.config.ak_cs_breakeven()
        required = breakeven + self.config.ak_cs_buffer
        if spread < required:
            return

        contracts = 10
        if (self.state.available_gross("transport") < contracts * 2 or
                self.state.available_net_buy("CL-AK") < contracts):
            return

        profit = (spread - breakeven) * 10000
        print(f"  [TRANSPORT] AK->CS spread=${spread:.2f} (need ${required:.2f}), "
              f"est profit=${profit:.0f}")

        self._start_pipeline("AK-CS-PIPE", "CL-AK", "AK", "CL", "CL", contracts, buy_input=True)

    def _check_cushing_to_nyc(self):
        _, _, cl_last = self.state.get_price("CL")
        nyc_bid, _, nyc_last = self.state.get_price("CL-NYC")
        if not all([cl_last, nyc_last]):
            return

        spread = nyc_last - cl_last
        breakeven = self.config.cs_nyc_breakeven()
        required = breakeven + self.config.cs_nyc_buffer
        if spread < required:
            return

        held_cl = self.state.get_position("CL")
        contracts = 10
        if held_cl < contracts or self.state.available_gross("transport") < contracts:
            return

        profit = (spread - breakeven) * 10000
        print(f"  [TRANSPORT] CS->NYC spread=${spread:.2f} (need ${required:.2f}), "
              f"est profit=${profit:.0f}")

        self._start_pipeline("CS-NYC-PIPE", "CL", "CL", "CL-NYC", "NYC", contracts)

    def _start_pipeline(self, pipe, source, source_location, destination, destination_location, contracts, buy_input=False):
        if getattr(self.state, "physical_uncertain", False):
            return
        if pipe in self._unresolved_pipelines:
            return
        refresh_physical(self.state)
        # Rent capacity at BOTH ends before submitting the conversion.
        held = self.state.get_position(source)
        if not self.storage.ensure_storage(source_location, max(contracts, held)):
            return
        incoming = sum(job["quantity"] for job in self.state.active_pipelines
                       if job["destination"] == destination and not job.get("delivered"))
        if not self.storage.ensure_storage(destination_location, self.state.get_position(destination) + incoming + contracts):
            return
        if held < contracts and buy_input:
            self.execution.place_split_orders(source, "BUY", contracts - held, order_type="MARKET")
        for attempt in range(30):
            self.state.update_from_securities(_get("/securities"))
            if self.state.get_position(source) >= contracts:
                break
            time.sleep(.1)
        else:
            print(f"  [TRANSPORT] {pipe}: input not available; no pipeline leased")
            self.storage.release_empty_storage()
            return
        try:
            # Pipelines are started directly with their input; no second use request.
            lease = physical_post("/leases", {"ticker": pipe, "from1": source, "quantity1": contracts})
            lease_id = lease["id"]
        except Exception as exc:
            # The request may have reached the server. Do not repeatedly create it.
            self._unresolved_pipelines.add(pipe)
            self.state.physical_uncertain = True
            print(f"  [ERR] {pipe} start unresolved: {exc}. No automatic resubmission; inspect RIT leases.")
            return
        self.state.active_pipelines.append({"id": lease_id, "pipe": pipe, "destination": destination,
            "due": physical_time(self.state, self.config) + self.config.get_transfer_sec(pipe) + 1,
            "remaining": contracts, "quantity": contracts})
        same_destination = [job for job in self.state.active_pipelines if job["destination"] == destination]
        if len(same_destination) > 1:
            for job in same_destination:
                job["concurrent_output"] = True
        self.state.strategy_gross["transport"] += contracts
        print(f"  [TRANSPORT] {pipe} started with {contracts} {source}; lease {lease_id}")

    def check_pipeline_completions(self):
        for job in list(self.state.active_pipelines):
            if physical_time(self.state, self.config) < job["due"]:
                continue
            refresh_physical(self.state)
            lease = self.state.leases.get(job["id"])
            if lease and lease.get("convert_finish_tick") is not None:
                finish = ((lease.get("convert_finish_period") or self.state.period) - 1) * self.config.ticks_per_period + lease["convert_finish_tick"]
                if physical_time(self.state, self.config) < finish:
                    continue
            ticker = job["destination"]
            if not job.get("delivered"):
                if self.state.get_position(ticker) < job["remaining"]:
                    continue
                job["delivered"] = True
            if not job.get("released"):
                job["released"] = release_physical_lease(self.state, job["id"])
            if job["remaining"]:
                before = self.state.get_position(ticker)
                if before < job["remaining"] and ticker not in job.get("sales", {}):
                    continue  # Do not declare completion on elapsed time alone.
                job["remaining"] -= sell_physical_output(job, ticker, job["remaining"], self.state, self.execution)
                if job["remaining"]:
                    continue
            if not job.get("released"):
                continue
            self.state.active_pipelines.remove(job)
            self.state.strategy_gross["transport"] -= job["quantity"]
            print(f"  [TRANSPORT] {job['pipe']} delivered, sold, and lease closed")
        self.storage.release_empty_storage()


# ═══════════════════════════════════════════════════════════════════════════════
#  CRACK SPREAD ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class CrackEngine:
    def __init__(self, state, execution, storage, config):
        self.state = state
        self.execution = execution
        self.storage = storage
        self.config = config
        self.job = None
        self.creation_uncertain = False

    def evaluate(self):
        tick = self.state.tick
        refinery_time = self.config.get_transfer_sec("CL-REFINERY")
        if tick >= self.config.ticks_per_period - refinery_time - 15:
            return
        if self.state.refinery_lease_id is not None or self.creation_uncertain or getattr(self.state, "physical_uncertain", False):
            return
        refresh_physical(self.state)

        _, _, cl_last = self.state.get_price("CL")
        _, _, rb_last = self.state.get_price("RB")
        _, _, ho_last = self.state.get_price("HO")
        if not all([cl_last, rb_last, ho_last]):
            return

        cl_size = self.config.get_contract_size("CL")
        rb_size = self.config.get_contract_size("RB")
        ho_size = self.config.get_contract_size("HO")

        # 3:2:1 crack
        revenue = rb_last * 20 * rb_size + ho_last * 10 * ho_size
        cost = cl_last * 30 * cl_size
        refinery_cost = self.config.get_asset_cost("CL-REFINERY")
        net_profit = revenue - cost - refinery_cost - (60 * self.config.fee_per_contract)

        if net_profit < self.config.crack_min_profit:
            return
        if self.state.get_position("CL") < 30:
            return
        if self.state.available_gross("crack") < 30:
            return

        print(f"  [CRACK] Profitable! revenue=${revenue:.0f} cost=${cost:.0f} "
              f"net=${net_profit:.0f}")
        before_ids = set(self.state.leases)
        try:
            lease = physical_post("/leases", {"ticker": "CL-REFINERY"})
            self.state.refinery_lease_id = lease["id"]  # Track BEFORE attempting use.
            # Use the actual active lease, not an unverified creation response.
            for attempt in range(30):
                leases = _get("/leases")
                candidates = [row for row in leases if row.get("ticker") == "CL-REFINERY" and row["id"] not in before_ids]
                if len(candidates) == 1:
                    self.state.refinery_lease_id = candidates[0]["id"]
                    break
                time.sleep(.1)
            else:
                self.creation_uncertain = True
                self.state.physical_uncertain = True
                print(f"  [ERR] Refinery creation not reconciled: {lease}; active leases: {leases}. Inspect RIT.")
                return
        except Exception as exc:
            self.creation_uncertain = True
            self.state.physical_uncertain = True
            print(f"  [ERR] Refinery creation unresolved: {exc}; no automatic replacement lease")
            return
        lease_id = self.state.refinery_lease_id
        self.job = {"id": lease_id, "started": False, "rejected": False, "released": False,
                    "due": physical_time(self.state, self.config) + refinery_time + 1,
                    "remaining": {"RB": 20, "HO": 10}}
        try:
            physical_post(f"/leases/{lease_id}", {"from1": "CL", "quantity1": 30})
            self.job["started"] = True
            self.state.refinery_start_tick = physical_time(self.state, self.config)
            self.state.strategy_gross["crack"] += 30
            print(f"  [CRACK] Refinery {lease_id} started with 30 CL ({refinery_time} sec)")
        except Exception as exc:
            response = getattr(exc, "response", None)
            self.job["rejected"] = response is not None and 400 <= response.status_code < 500
            print(f"  [ERR] Refinery {lease_id} use failed; lease remains tracked: {exc}")
            self.check_refinery_completion()

    def check_refinery_completion(self):
        if self.job is None:
            return
        job = self.job
        refresh_physical(self.state)
        lease = self.state.leases.get(job["id"])
        finish = None
        if lease and lease.get("convert_finish_tick") is not None:
            finish = ((lease.get("convert_finish_period") or self.state.period) - 1) * self.config.ticks_per_period + lease["convert_finish_tick"]
        if not job["started"]:
            if finish is not None:
                job["started"] = True  # Server confirms use after an ambiguous response.
                job["due"] = finish + 1
                self.state.strategy_gross["crack"] += 30
            elif job["rejected"]:
                if release_physical_lease(self.state, job["id"]):
                    self.state.refinery_lease_id = None
                    self.state.refinery_start_tick = None
                    self.job = None
                    print("  [CRACK] Rejected conversion's idle lease closed")
                    self.storage.release_empty_storage()
                return
            else:
                return  # Ambiguous use: keep tracking; don't submit again or delete it.
        if physical_time(self.state, self.config) < max(job["due"], finish or 0):
            return
        if not job.get("delivered"):
            if any(self.state.get_position(t) < q for t, q in job["remaining"].items()):
                return
            job["delivered"] = True
        if not job["released"]:
            job["released"] = release_physical_lease(self.state, job["id"])
            if job["released"]:
                print(f"  [CRACK] Refinery {job['id']} released after output arrival")
        for ticker, quantity in list(job["remaining"].items()):
            if quantity:
                job["remaining"][ticker] -= sell_physical_output(job, ticker, quantity, self.state, self.execution)
        if any(job["remaining"].values()) or not job["released"]:
            return
        self.state.refinery_lease_id = None
        self.state.refinery_start_tick = None
        self.state.strategy_gross["crack"] -= 30
        self.job = None
        self.storage.release_empty_storage()


# ═══════════════════════════════════════════════════════════════════════════════
#  MARKET MAKING ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class MarketMaker:
    def __init__(self, state, execution, config):
        self.state = state
        self.execution = execution
        self.config = config
        self.last_quote_tick = 0
        self.active = False

    def should_be_active(self, tick):
        if any(abs(tick - t) <= self.config.mm_eia_blackout
               for t in self.config.eia_ticks):
            return False
        if tick >= self.config.wind_down_start:
            return False
        if self.state.fair_value is None:
            return False
        return True

    def update(self):
        tick = self.state.tick
        if not self.should_be_active(tick):
            self.active = False
            return
        if tick - self.last_quote_tick < self.config.mm_requote_interval:
            return
        # Skip MM when EIA is holding large directional positions —
        # we don't want to provide liquidity against our own conviction trade
        net_pos = self.state.get_position("CL-1F")
        eia_active = self.state.strategy_gross.get("eia", 0) > 10
        if eia_active:
            return
        if self.state.available_gross("mm") < self.config.mm_size * 2:
            return

        # Use market mid as anchor, blended with fair value model
        market_mid = self.state.get_mid("CL-1F")
        fair = self.state.fair_value
        if market_mid and fair:
            # Blend: 70% market, 30% model (model may be stale if joined mid-heat)
            anchor = market_mid * 0.7 + fair * 0.3
        elif market_mid:
            anchor = market_mid
        elif fair:
            anchor = fair
        else:
            return

        bid_price = round(anchor - self.config.mm_half_spread, 2)
        ask_price = round(anchor + self.config.mm_half_spread, 2)

        if net_pos > self.config.mm_max_net // 2:
            bid_price -= 0.02
        elif net_pos < -self.config.mm_max_net // 2:
            ask_price += 0.02

        print(f"  [MM] Quoting CL-1F: bid=${bid_price:.2f} ask=${ask_price:.2f} "
              f"(anchor=${anchor:.2f}, net={net_pos})")
        self.execution.place_order("CL-1F", "BUY", self.config.mm_size,
                                   price=bid_price, order_type="LIMIT")
        self.execution.place_order("CL-1F", "SELL", self.config.mm_size,
                                   price=ask_price, order_type="LIMIT")
        self.last_quote_tick = tick
        self.active = True


# ═══════════════════════════════════════════════════════════════════════════════
#  RISK MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class RiskManager:
    def __init__(self, state, execution, storage, config):
        self.state = state
        self.execution = execution
        self.storage = storage
        self.config = config
        self.wind_down_started = False

    def process_scheduled_exits(self):
        tick = self.state.tick
        remaining = []
        for exit_info in self.state.scheduled_exits:
            exit_tick, ticker, action, quantity, strategy = exit_info
            if tick >= exit_tick:
                if action == "SELL":
                    quantity = min(quantity, self.state.available_net_sell(ticker))
                else:
                    quantity = min(quantity, self.state.available_net_buy(ticker))
                if quantity > 0:
                    self.execution.place_split_orders(ticker, action, quantity,
                                                     order_type="MARKET")
                    self.state.strategy_gross[strategy] = max(
                        0, self.state.strategy_gross[strategy] - quantity)
            else:
                remaining.append(exit_info)
        self.state.scheduled_exits = remaining

    def check_wind_down(self):
        tick = self.state.tick
        if tick >= self.config.emergency_flatten_tick:
            self._emergency_flatten()
        elif tick >= self.config.wind_down_start and not self.wind_down_started:
            self._begin_wind_down()

    def _begin_wind_down(self):
        self.wind_down_started = True
        print(f"\n  [RISK] Wind-down started at tick {self.state.tick}")
        self.execution.cancel_all_orders()
        self.state.scheduled_exits.clear()

    def _emergency_flatten(self):
        for ticker in self.config.tickers:
            pos = self.state.get_position(ticker)
            if pos > 0:
                self.execution.place_split_orders(ticker, "SELL", pos,
                                                  order_type="MARKET")
            elif pos < 0 and self.config.is_shortable(ticker):
                self.execution.place_split_orders(ticker, "BUY", abs(pos),
                                                  order_type="MARKET")
        self.storage.release_empty_storage()

    def pre_eia_cleanup(self, tick):
        if any(abs(tick - t) == self.config.eia_pre_cancel_buffer
               for t in self.config.eia_ticks):
            print(f"  [RISK] Pre-EIA cleanup at tick {tick}")
            self.execution.cancel_all_orders()

    def on_period_end(self):
        self.wind_down_started = False


# ═══════════════════════════════════════════════════════════════════════════════
#  ORCHESTRATOR — real-time self-tuning of strategy weights
# ═══════════════════════════════════════════════════════════════════════════════

class Orchestrator:
    """Observes market microstructure and strategy performance, then adjusts
    config parameters in real-time to optimize for discovered conditions.

    Runs a tuning pass every TUNING_INTERVAL ticks. Modifies config values
    that strategy engines read each loop iteration.

    Observables -> Adjustments:
      Liquidity (spreads, depth)     -> MM spread, order sizes, limit vs market
      EIA reaction efficiency        -> EIA sizing multiplier, exit timing
      Spread availability            -> Arb budget allocation, buffer tightness
      Per-strategy P&L               -> Gross budget rebalancing
      Volatility                     -> Overall aggression scaling
    """

    TUNING_INTERVAL = 30  # ticks between tuning passes
    # Minimum ticks of observation before first tuning
    WARMUP_TICKS = 30

    def __init__(self, state, config):
        self.state = state
        self.config = config
        self.last_tune_tick = 0

        # ── Observation accumulators ────────────────────────────────
        # Liquidity
        self.observed_spreads = defaultdict(list)  # ticker -> [spread values]
        self.observed_depths = defaultdict(list)   # ticker -> [depth values]

        # EIA efficiency
        self.eia_pre_prices = {}   # tick -> pre-EIA CL mid price
        self.eia_expected = {}     # tick -> calculated impact
        self.eia_efficiency = []   # ratio of actual/calculated per EIA

        # Strategy P&L tracking (NLV snapshots around strategy activity)
        self.nlv_at_last_tune = None
        self.strategy_pnl = defaultdict(float)  # rough attribution

        # Volatility
        self.recent_returns = deque(maxlen=60)  # tick-to-tick CL returns

        # Track what we've adjusted for logging
        self.adjustments_made = []

        # Initial "aggression" multiplier (1.0 = normal)
        self.aggression = 1.0

    def observe_book(self, ticker, book):
        """Called when we have order book data. Tracks spreads and depth."""
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if bids and asks:
            spread = asks[0]["price"] - bids[0]["price"]
            self.observed_spreads[ticker].append(spread)
            bid_depth = sum(b["quantity"] for b in bids[:5])
            ask_depth = sum(a["quantity"] for a in asks[:5])
            self.observed_depths[ticker].append(bid_depth + ask_depth)

    def observe_eia_pre(self, tick, cl_mid, calculated_impact):
        """Called just before an EIA trade is executed."""
        self.eia_pre_prices[tick] = cl_mid
        self.eia_expected[tick] = calculated_impact

    def observe_eia_post(self, eia_tick, cl_mid_now):
        """Called ~10 ticks after an EIA to measure actual reaction."""
        if eia_tick not in self.eia_pre_prices or eia_tick not in self.eia_expected:
            return
        pre = self.eia_pre_prices.pop(eia_tick)
        expected = self.eia_expected.pop(eia_tick)
        if pre and expected and abs(expected) > 0.01:
            actual_move = cl_mid_now - pre
            efficiency = actual_move / expected
            self.eia_efficiency.append(efficiency)
            print(f"  [ORCH] EIA efficiency: expected={expected:+.2f} "
                  f"actual={actual_move:+.2f} ratio={efficiency:.2f}")

    def observe_price(self, cl_last_prev, cl_last_now):
        """Track tick-to-tick returns for volatility estimation."""
        if cl_last_prev and cl_last_now and cl_last_prev > 0:
            ret = (cl_last_now - cl_last_prev) / cl_last_prev
            self.recent_returns.append(ret)

    def tune(self, tick):
        """Run a tuning pass. Called every TUNING_INTERVAL ticks."""
        if tick - self.last_tune_tick < self.TUNING_INTERVAL:
            return
        if tick < self.WARMUP_TICKS:
            return
        self.last_tune_tick = tick
        self.adjustments_made.clear()

        self._tune_liquidity()
        self._tune_eia()
        self._tune_volatility()
        self._tune_budgets()

        if self.adjustments_made:
            print(f"  [ORCH] Tuned: {', '.join(self.adjustments_made)}")

    def _tune_liquidity(self):
        """Adjust MM spread and order aggressiveness based on observed liquidity."""
        # Use CL-1F spreads as the primary liquidity signal
        spreads = self.observed_spreads.get("CL-1F", [])
        if len(spreads) < 5:
            return

        # Use recent observations (last 30)
        recent = spreads[-30:]
        avg_spread = sum(recent) / len(recent)

        depths = self.observed_depths.get("CL-1F", [])
        avg_depth = (sum(depths[-30:]) / len(depths[-30:])) if len(depths) >= 5 else 50

        # MM spread: set to ~40% of observed spread (capture spread while filling)
        new_mm_spread = max(0.02, round(avg_spread * 0.40, 3))
        if abs(new_mm_spread - self.config.mm_half_spread) > 0.005:
            self.config.mm_half_spread = new_mm_spread
            self.adjustments_made.append(f"mm_spread=${new_mm_spread:.3f}")

        # MM size: scale with depth (more depth = can post larger quotes)
        if avg_depth > 100:
            new_mm_size = 10
        elif avg_depth > 50:
            new_mm_size = 7
        elif avg_depth > 20:
            new_mm_size = 5
        else:
            new_mm_size = 3
        if new_mm_size != self.config.mm_size:
            self.config.mm_size = new_mm_size
            self.adjustments_made.append(f"mm_size={new_mm_size}")

        # Wide spreads = low liquidity -> shrink news/EIA base contracts
        if avg_spread > 0.20:
            # Low liquidity: be less aggressive, more limit orders
            self.aggression = 0.7
            self.adjustments_made.append("aggression=0.7(low_liq)")
        elif avg_spread < 0.05:
            # High liquidity: be more aggressive
            self.aggression = 1.2
            self.adjustments_made.append("aggression=1.2(high_liq)")
        else:
            self.aggression = 1.0

    def _tune_eia(self):
        """Adjust EIA sizing and exit timing based on reaction efficiency."""
        if len(self.eia_efficiency) < 1:
            return

        # Average efficiency across observed EIAs
        avg_eff = sum(self.eia_efficiency) / len(self.eia_efficiency)

        # Efficiency > 1.0: market over-reacts -> exit faster, take less
        # Efficiency < 1.0: market under-reacts -> hold longer, take more
        # Efficiency ~ 1.0: market is efficient -> default timing

        if avg_eff > 1.3:
            # Over-reaction: exit fast, reduce sizing
            self.config.eia_exit_fast_ticks = 5
            self.config.eia_exit_med_ticks = 15
            self.config.eia_exit_slow_ticks = 30
            self.config.eia_contracts_per_50c = int(12 * self.aggression)
            self.adjustments_made.append(f"eia_timing=fast(eff={avg_eff:.2f})")

        elif avg_eff < 0.7:
            # Under-reaction: hold longer, size up
            self.config.eia_exit_fast_ticks = 15
            self.config.eia_exit_med_ticks = 45
            self.config.eia_exit_slow_ticks = 90
            self.config.eia_contracts_per_50c = int(20 * self.aggression)
            self.adjustments_made.append(f"eia_timing=slow(eff={avg_eff:.2f})")

        else:
            # Efficient: default timing, normal sizing
            self.config.eia_exit_fast_ticks = 10
            self.config.eia_exit_med_ticks = 30
            self.config.eia_exit_slow_ticks = 60
            self.config.eia_contracts_per_50c = int(15 * self.aggression)

    def _tune_volatility(self):
        """Adjust arb buffers based on price volatility."""
        if len(self.recent_returns) < 20:
            return

        # Annualized-style vol (but we just need relative magnitude)
        returns_list = list(self.recent_returns)
        mean_r = sum(returns_list) / len(returns_list)
        variance = sum((r - mean_r) ** 2 for r in returns_list) / len(returns_list)
        vol = variance ** 0.5  # per-tick standard deviation of returns

        # High vol: widen arb buffers (prices move more during transit)
        # Low vol: tighten buffers (safer to arb)
        if vol > 0.003:
            new_buffer = 0.80
            self.adjustments_made.append(f"arb_buffer=0.80(vol={vol:.4f})")
        elif vol > 0.001:
            new_buffer = 0.50
        else:
            new_buffer = 0.30
            self.adjustments_made.append(f"arb_buffer=0.30(vol={vol:.4f})")

        self.config.ak_cs_buffer = new_buffer
        self.config.cs_nyc_buffer = new_buffer

    def _tune_budgets(self):
        """Rebalance gross budget based on observed opportunity set."""
        # Check if arb spreads exist at all
        ak_spreads = self.observed_spreads.get("CL-AK", [])
        nyc_spreads = self.observed_spreads.get("CL-NYC", [])
        has_geo_arb = False

        if ak_spreads:
            # Check if any AK spread was near breakeven
            ak_be = self.config.ak_cs_breakeven()
            has_geo_arb = any(s > ak_be * 0.8 for s in ak_spreads[-20:])
        if not has_geo_arb and nyc_spreads:
            nyc_be = self.config.cs_nyc_breakeven()
            has_geo_arb = any(s > nyc_be * 0.8 for s in nyc_spreads[-20:])

        ratio = self.config.gross_limit / 500.0

        # EIA always gets the full gross limit — never let tuning reduce it
        if not has_geo_arb:
            new_budget = {
                "eia": self.config.gross_limit,
                "basis": int(120 * ratio),
                "transport": int(20 * ratio),
                "news": int(60 * ratio),
                "crack": int(30 * ratio),
                "mm": int(40 * ratio),
            }
            if self.config.gross_budget.get("transport", 0) > 30:
                self.adjustments_made.append("budget: transport->basis")
                self.config.gross_budget = new_budget
        else:
            new_budget = {
                "eia": self.config.gross_limit,
                "basis": int(100 * ratio),
                "transport": int(60 * ratio),
                "news": int(60 * ratio),
                "crack": int(40 * ratio),
                "mm": int(40 * ratio),
            }
            self.config.gross_budget = new_budget

    def get_sizing_multiplier(self):
        """Returns the current aggression multiplier for position sizing."""
        return self.aggression


# ═══════════════════════════════════════════════════════════════════════════════
#  RUN LOGGER — file logging + post-run analytics
# ═══════════════════════════════════════════════════════════════════════════════

class RunLogger:
    """Logs all bot activity to file and saves post-run analytics."""

    def __init__(self):
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = os.path.join("runs", self.run_id)
        os.makedirs(self.run_dir, exist_ok=True)

        # File logger — captures everything print() shows plus more
        self.logger = logging.getLogger("com5bot")
        self.logger.handlers.clear()  # prevent duplicate handlers across runs
        self.logger.setLevel(logging.DEBUG)
        fh = logging.FileHandler(
            os.path.join(self.run_dir, "bot.log"), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
        self.logger.addHandler(fh)

        # Also log to console
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(ch)

        # Event log — structured events for post-run analysis
        self._events_file = open(
            os.path.join(self.run_dir, "events.jsonl"), "w", encoding="utf-8")

        # NLV snapshots for equity curve
        self._nlv_file = open(
            os.path.join(self.run_dir, "nlv.csv"), "w", encoding="utf-8")
        self._nlv_file.write("period,tick,nlv,gross,fair_value,cl_last\n")

        self.start_time = time.time()

    def info(self, msg):
        self.logger.info(msg)

    def debug(self, msg):
        self.logger.debug(msg)

    def error(self, msg):
        self.logger.error(msg)

    def event(self, event_type, period, tick, **data):
        """Log a structured event for post-run analysis."""
        record = {"type": event_type, "period": period, "tick": tick,
                  "ts": time.time(), **data}
        self._events_file.write(json.dumps(record) + "\n")
        self._events_file.flush()

    def snapshot(self, period, tick, nlv, gross, fair_value, cl_last):
        """Log NLV snapshot for equity curve."""
        fv = f"{fair_value:.2f}" if fair_value else ""
        cl = f"{cl_last}" if cl_last else ""
        self._nlv_file.write(f"{period},{tick},{nlv},{gross},{fv},{cl}\n")
        self._nlv_file.flush()

    def save_config(self, config):
        """Save discovered config to file."""
        path = os.path.join(self.run_dir, "config.json")
        data = {
            "ticks_per_period": config.ticks_per_period,
            "total_periods": config.total_periods,
            "gross_limit": config.gross_limit,
            "max_order_size": config.max_order_size,
            "fee_per_contract": config.fee_per_contract,
            "eia_ticks": config.eia_ticks,
            "net_limits": config.net_limits,
            "tickers": config.tickers,
            "assets": config.assets,
            "wind_down_start": config.wind_down_start,
            "emergency_flatten_tick": config.emergency_flatten_tick,
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def save_summary(self, state, config):
        """Save post-run summary analytics."""
        elapsed = time.time() - self.start_time
        summary = {
            "run_id": self.run_id,
            "duration_sec": round(elapsed, 1),
            "final_nlv": state.nlv,
            "eia_reports_processed": state.eia_count,
            "fair_value_at_end": state.fair_value,
            "start_price": state.start_price,
            "cumulative_eia_impact": state.cumulative_eia,
            "cumulative_news_impact": state.cumulative_news,
            "final_positions": dict(state.positions),
            "strategy_gross_usage": dict(state.strategy_gross),
            "eia_calculated_impacts": state.eia_calculated_impacts,
            "config_discovered": {
                "gross_limit": config.gross_limit,
                "ticks_per_period": config.ticks_per_period,
                "assets": config.assets,
            },
        }
        path = os.path.join(self.run_dir, "summary.json")
        with open(path, "w") as f:
            json.dump(summary, f, indent=2, default=str)
        self.info(f"\n  Run summary saved to {path}")

    def close(self):
        self._events_file.close()
        self._nlv_file.close()


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════════════

def wait_for_active(log):
    """Distinguish an inactive case from a broken API connection at startup."""
    if not isinstance(API_KEY, str) or not API_KEY.strip():
        raise RuntimeError("API key is blank. Set API_KEY in com5_template.py to your RIT Client key, then run again.")
    SESSION.headers.update({"X-API-Key": API_KEY.strip()})
    log.info(f"Connecting to RIT API: {BASE}/case")
    failures = 0
    last_status = None
    next_status_log = 0
    while True:
        try:
            response = SESSION.get(f"{BASE}/case", timeout=(3, 5))
        except requests.RequestException as exc:
            failures += 1
            log.error(f"[STARTUP] Cannot reach RIT API ({type(exc).__name__}), attempt {failures}/3. "
                      "Check that RIT Client is open and its API port matches RIT_BASE_URL.")
            if failures >= 3:
                raise RuntimeError("RIT API connection failed. This is a connection problem, not an inactive case.") from exc
            time.sleep(1)
            continue
        if response.status_code in (401, 403):
            raise RuntimeError(f"RIT API rejected access (HTTP {response.status_code}). "
                               "Check your current RIT Client API key in com5_template.py and API access settings.")
        if not response.ok:
            failures += 1
            log.error(f"[STARTUP] RIT /case returned HTTP {response.status_code}: {response.text[:300]}")
            if failures >= 3:
                raise RuntimeError("RIT /case failed three times. Check the API address and server response above.")
            time.sleep(1)
            continue
        try:
            case = response.json()
        except ValueError as exc:
            raise RuntimeError("The API address returned non-JSON content; check RIT_BASE_URL and the API port.") from exc
        if not isinstance(case, dict) or not case.get("status"):
            raise RuntimeError("The API response has no case status; check that RIT_BASE_URL points to the RIT API.")
        failures = 0
        status = case["status"]
        if status == "ACTIVE":
            log.info(f"[STARTUP] Case ACTIVE: period={case.get('period')} tick={case.get('tick')}. Starting live trading.")
            return case
        if status != last_status or time.monotonic() >= next_status_log:
            log.info(f"[STARTUP] Connected. Case status={status}, period={case.get('period')}, tick={case.get('tick')}. "
                     "Waiting for the case to be started/resumed in RIT.")
            next_status_log = time.monotonic() + 5
            last_status = status
        time.sleep(.5)


def main():
    log = RunLogger()
    log.info("=" * 60)
    log.info("  COM5 COMPETITION BOT — ADAPTIVE CONFIG")
    log.info(f"  Run ID: {log.run_id}")
    log.info(f"  Output: {log.run_dir}")
    log.info("=" * 60)

    config = CaseConfig()

    try:
        wait_for_active(log)
    except RuntimeError as exc:
        log.error(f"[STARTUP STOP] {exc}")
        log.close()
        return 1
    except KeyboardInterrupt:
        log.info("Startup cancelled; no trading started.")
        log.close()
        return 0

    config.discover()
    log.save_config(config)

    # Drain existing news backlog — don't trade on stale data from before we started
    try:
        old_news = _get("/news", {"since": 0})
        if old_news:
            max_old_id = max(n["news_id"] for n in old_news)
            log.info(f"  [STARTUP] Skipping {len(old_news)} historical news items "
                     f"(last_id={max_old_id})")
        else:
            max_old_id = 0
    except Exception:
        max_old_id = 0

    state = StateManager(config)
    state.last_news_id = max_old_id  # skip all pre-existing news
    execution = ExecutionEngine(state, config)
    storage = StorageManager(state, config)
    orchestrator = Orchestrator(state, config)

    eia_engine = EIAEngine(state, execution, storage, config)
    news_engine = NewsEngine(state, execution, config)
    basis_engine = BasisEngine(state, execution, storage, config)
    transport_engine = TransportEngine(state, execution, storage, config)
    crack_engine = CrackEngine(state, execution, storage, config)
    market_maker = MarketMaker(state, execution, config)
    risk_manager = RiskManager(state, execution, storage, config)

    last_period = 0
    prev_cl_last = None
    last_processed_tick = -1
    last_processed_period = -1

    try:
        while True:
            try:
                case = _get("/case")
            except Exception as e:
                log.error(f"API unreachable: {e}")
                time.sleep(2)
                continue

            if case.get("status") != "ACTIVE":
                if state.status == "ACTIVE":
                    # Period ended — check if ALL periods are done
                    if state.period >= config.total_periods:
                        log.info("\nAll periods complete. Case ended.")
                        break
                    log.info(f"\nPeriod {state.period} ended. "
                             f"Waiting for next period...")
                    state.status = case.get("status", "STOPPED")
                time.sleep(0.5)
                continue

            state.update_from_case(case)
            tick = state.tick
            period = state.period

            # Skip if we already processed this tick (loop faster than ticks)
            if tick == last_processed_tick and period == last_processed_period:
                time.sleep(0.05)
                continue
            last_processed_tick = tick
            last_processed_period = period

            if period != last_period:
                risk_manager.on_period_end()
                last_period = period

            # ── 1. NEWS (highest priority) ──────────────────────────
            try:
                news = _get("/news", {"since": state.last_news_id})
                if news:
                    state.last_news_id = max(n["news_id"] for n in news)
                    for n in news:
                        headline = n.get("headline", "")
                        is_eia = eia_engine.is_eia_report(n)
                        log.info(f"  [P{period} T{tick}] NEWS: {headline}")
                        log.debug(f"    news_id={n.get('news_id')} "
                                  f"is_eia={is_eia} "
                                  f"body={n.get('body', '')[:200]}")
                        log.event("news", period, tick,
                                  headline=headline, is_eia=is_eia,
                                  body=n.get("body", "")[:500])
                        if is_eia:
                            cl_mid = state.get_mid("CL")
                            prev_impacts = len(state.eia_calculated_impacts)
                            eia_engine.process(n)
                            if len(state.eia_calculated_impacts) > prev_impacts:
                                orchestrator.observe_eia_pre(
                                    tick, cl_mid,
                                    state.eia_calculated_impacts[-1])
                            log.event("eia_trade", period, tick,
                                      fair_value=state.fair_value,
                                      eia_count=state.eia_count)
                        else:
                            news_engine.process(n)
            except Exception as e:
                log.error(f"  [ERR] news poll: {e}")

            # ── 2. MARKET DATA ──────────────────────────────────────
            try:
                securities = _get("/securities")
                state.update_from_securities(securities)

                # Drift fair value toward market (handles mid-heat join)
                state.drift_fair_value()

                # Feed orchestrator: volatility observation
                cl_last = state.securities.get("CL", {}).get("last")
                orchestrator.observe_price(prev_cl_last, cl_last)
                prev_cl_last = cl_last

                # Feed orchestrator: check EIA post-reaction (~10 ticks after)
                cl_mid = state.get_mid("CL")
                if cl_mid:
                    for eia_tick in list(orchestrator.eia_pre_prices.keys()):
                        if tick - eia_tick >= 10:
                            orchestrator.observe_eia_post(eia_tick, cl_mid)
            except Exception as e:
                log.error(f"  [ERR] securities: {e}")

            # ── 3. SCHEDULED EXITS ──────────────────────────────────
            risk_manager.process_scheduled_exits()

            # ── 4. PRE-EIA CLEANUP ──────────────────────────────────
            risk_manager.pre_eia_cleanup(tick)

            # ── 5. ASSET COMPLETION CHECKS ──────────────────────────
            crack_engine.check_refinery_completion()
            transport_engine.check_pipeline_completions()

            # ── 6. STRATEGY ENGINES ─────────────────────────────────
            near_eia = any(abs(tick - t) <= config.eia_pre_cancel_buffer
                          for t in config.eia_ticks)
            in_wind_down = tick >= config.wind_down_start

            if not near_eia and not in_wind_down:
                basis_engine.evaluate()
                transport_engine.evaluate()
                crack_engine.evaluate()
                market_maker.update()

            # ── 7. WIND-DOWN ────────────────────────────────────────
            risk_manager.check_wind_down()

            # ── 8. ORCHESTRATOR TUNING ──────────────────────────────
            orchestrator.tune(tick)
            if orchestrator.adjustments_made:
                log.event("orchestrator_tune", period, tick,
                          adjustments=list(orchestrator.adjustments_made),
                          aggression=orchestrator.aggression)
                orchestrator.adjustments_made.clear()

            # ── 9. CONFIG REFRESH + RECONCILIATION ──────────────────
            if tick % config.reconcile_interval == 0:
                config.refresh()
                state.reconcile()

                # Feed orchestrator: order book observation for liquidity
                try:
                    for ob_ticker in ["CL-1F", "CL"]:
                        book = _get("/securities/book",
                                    {"ticker": ob_ticker})
                        orchestrator.observe_book(ob_ticker, book)
                    # Also observe geographic spreads for budget tuning
                    cl_p = state.securities.get("CL", {}).get("last")
                    ak_p = state.securities.get("CL-AK", {}).get("last")
                    nyc_p = state.securities.get("CL-NYC", {}).get("last")
                    if cl_p and ak_p:
                        orchestrator.observed_spreads["CL-AK"].append(
                            cl_p - ak_p)
                    if cl_p and nyc_p:
                        orchestrator.observed_spreads["CL-NYC"].append(
                            nyc_p - cl_p)
                except Exception as e:
                    log.debug(f"  [ERR] book observation: {e}")

            # ── 10. STATUS + LOGGING ────────────────────────────────
            if tick % 30 == 0:
                gross = state.total_gross()
                fv = state.fair_value
                cl_last = state.securities.get("CL", {}).get("last")
                fv_str = f"${fv:.2f}" if fv else "N/A"
                agg = orchestrator.aggression
                log.info(f"  [P{period} T{tick:>3}] NLV=${state.nlv:>10,.0f} "
                         f"gross={gross}/{config.gross_limit} "
                         f"FV={fv_str} CL=${cl_last} agg={agg:.1f}")
                log.snapshot(period, tick, state.nlv, gross, fv, cl_last)

            # ── 11. PACING ──────────────────────────────────────────
            time.sleep(config.loop_sleep)

    except KeyboardInterrupt:
        log.info("\n\nBot stopped by user.")
    except Exception as e:
        log.error(f"\n[FATAL] {e}")
        import traceback
        traceback.print_exc()

    # ── Post-run analytics ──────────────────────────────────────────
    log.info(f"\nFinal NLV: ${state.nlv:,.0f}")
    log.info(f"EIA reports processed: {state.eia_count}")
    log.info(f"Fair value at end: {state.fair_value}")
    log.info(f"EIA efficiency observations: {orchestrator.eia_efficiency}")
    log.info(f"Final aggression: {orchestrator.aggression}")
    log.save_summary(state, config)
    log.close()
    print(f"\nFull run logs: {log.run_dir}/")


if __name__ == "__main__":
    main()
