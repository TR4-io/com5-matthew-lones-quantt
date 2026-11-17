"""
COM5 Commodities Capstone — Competition Bot
============================================
Multi-strategy automated trading bot for Rotman COM5.

Server limits and asset metadata are validated at startup.
Signals retain COM5-specific assumptions; see START_HERE.md.

Strategies (priority order):
  1. EIA Alpha       — directional trading on EIA report releases
  2. Futures Basis   — cost-of-carry arb + end-of-period convergence
  3. News Trading    — keyword-classified geopolitical events
  4. Geographic Arb  — pipeline transport between crude locations
  5. Crack Spread    — refinery 3:2:1 conversion
  6. Market Making   — liquidity provision between events

Usage: python bot.py [--futures-only]; python bot.py --check
"""

import argparse
import math
from runtime import Client, TradingHalt, risk_allows, conversion_spec
import time
import re
import json
import os
import sys
import logging
from datetime import datetime
from collections import defaultdict, deque

from com5_template import API_KEY, BASE, HEADERS

CLIENT = Client(BASE, HEADERS)  # main() selects live mode unless explicitly checking.


def _get(endpoint, params=None):
    return CLIENT.request("GET", endpoint, params)


def _post(endpoint, params=None):
    return CLIENT.request("POST", endpoint, params)


def _delete(endpoint):
    return CLIENT.request("DELETE", endpoint)


def active_future(state):
    return "CL-1F" if state.period <= 1 else "CL-2F"


def filled(orders):
    return sum(int(o.get("quantity_filled", 0)) for o in orders)


# ═══════════════════════════════════════════════════════════════════════════════
#  CASE CONFIG — live metadata with explicit COM5 assumptions
# ═══════════════════════════════════════════════════════════════════════════════

class CaseConfig:
    """All competition parameters, discovered from API at runtime.

    Call discover() once at case startup. Call refresh() every 30 ticks
    to detect mid-case changes (asset costs, limit adjustments).
    """

    def __init__(self):
        # ── Case structure (from /case) ─────────────────────────────
        self.physical_enabled = True
        self.raw_assets = {}
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
                "shortable": bool(s.get("is_shortable", False)),
                "type": s.get("type", ""),
            }
            # Try to get max trade size and fees from API
            if s.get("max_trade_size"):
                self.max_order_size = s["max_trade_size"]
            if s.get("trading_fees") is not None:
                self.fee_per_contract = s["trading_fees"]

        # Shortability: only futures are shortable in COM5
        # Override from known COM5 rules if API doesn't expose it clearly
        for ticker in self.tickers:
            t = self.tickers[ticker]["type"].upper()
            self.tickers[ticker]["shortable"] = bool(next(r for r in securities if r["ticker"] == ticker).get("is_shortable", False))

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

        self.load_assets(_get("/assets"))
        if self.physical_enabled:
            for ticker, source, outputs in (
                ("AK-CS-PIPE", "CL-AK", {"CL": 10}),
                ("CS-NYC-PIPE", "CL", {"CL-NYC": 10}),
                ("CL-REFINERY", "CL", {"RB": 20, "HO": 10}),
            ):
                conversion_spec(self.raw_assets[ticker], source, outputs)

        self._derive()
        self._print_config()

    def load_assets(self, assets):
        self.raw_assets = {a["ticker"]: a for a in assets}
        self.assets = {}
        for a in assets:
            containment = a.get("containment") or {}
            source_size = self.get_contract_size(containment.get("ticker", "CL"))
            conversions = a.get("convert_from") or []
            capacity = float(containment.get("quantity", 0)) * source_size
            if conversions:
                capacity = float(conversions[0]["quantity"]) * self.get_contract_size(conversions[0]["ticker"])
            self.assets[a["ticker"]] = {
                "cost": float(a["lease_price"]), "capacity": capacity,
                "transfer_sec": int(a.get("ticks_per_conversion", 0)),
                "max_leases": int(a.get("total_quantity", 0)),
            }

    def refresh(self):
        self.load_assets(_get("/assets"))
        limits = _get("/limits")
        if not isinstance(limits, list) or not limits:
            raise TradingHalt("Missing live limits")
        self.gross_limit = min(float(r["gross_limit"]) for r in limits)
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
        return self.assets.get(asset_ticker, {}).get("cost", float("inf"))

    def get_asset_capacity(self, asset_ticker):
        """Get capacity for an asset."""
        return self.assets.get(asset_ticker, {}).get("capacity", 0)

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
        self.conversion_outputs = set()

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
        # Conversion tracking survives period boundaries.
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
        return sum(abs(p) * max((float(m["units"]) for m in self.securities.get(t, {}).get("limits", [])), default=1) for t, p in self.positions.items())

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
        self.state, self.config = state, config
        self.logger = None
        self.position_mismatch = None

    def refresh_positions(self):
        rows = _get("/securities")
        self.state.update_from_securities(rows)
        return rows

    def approved_quantity(self, ticker, action, desired):
        orders = _get("/orders", {"status": "OPEN"})
        rows = self.refresh_positions()
        if not any(r["ticker"] == ticker for r in rows):
            return 0
        limits = _get("/limits")
        for qty in range(int(desired), 0, -1):
            if risk_allows(rows, limits, orders, ticker, action, qty):
                return qty
        return 0

    def place_order(self, ticker, action, quantity, price=None, order_type="MARKET", reduce_only=False):
        if quantity <= 0:
            return None
        if order_type == "MARKET":
            # Remove other live orders before establishing the fill baseline.
            self.cancel_all_orders()
        orders = _get("/orders", {"status": "OPEN"})
        rows = self.refresh_positions()
        row = next((r for r in rows if r["ticker"] == ticker), None)
        if row is None or (ticker == "CL-1F" and self.state.period > 1):
            return None
        cap = int(row.get("max_trade_size", self.config.max_order_size))
        quantity = min(int(quantity), cap)
        if action == "SELL" and not row.get("is_shortable", False):
            quantity = min(quantity, int(row["position"]))
        if quantity <= 0:
            return None
        if reduce_only:
            quantity = min(quantity, max(0, int(row["position"]) if action == "SELL" else -int(row["position"])))
        if quantity <= 0:
            return None
        if not risk_allows(rows, _get("/limits"), orders, ticker, action, quantity):
            print(f"  [RISK] Blocked {action} {quantity} {ticker}")
            return None
        params = {"ticker": ticker, "type": order_type, "quantity": quantity, "action": action}
        if order_type == "LIMIT":
            if price is None or not math.isfinite(price) or price <= 0:
                raise TradingHalt("Invalid limit price")
            params["price"] = round(price, 2)
        result = _post("/orders", params)
        if not isinstance(result, dict) or "order_id" not in result:
            CLIENT.uncertain_write = True
            raise TradingHalt("Order acknowledgement has no ID; not resubmitting")
        if self.logger:
            self.logger.event("order_ack", self.state.period, self.state.tick, order=result)
        if order_type == "MARKET":
            result = self.finish_order(result)
        if self.logger:
            self.logger.event("order_result", self.state.period, self.state.tick, order=result)
        if order_type == "MARKET":
            self.confirm_inventory(result, float(row["position"]))
        return result

    def confirm_inventory(self, order, before):
        """A terminal fill report can precede the securities inventory update."""
        ticker = order["ticker"]
        delta = float(order["quantity_filled"]) * (1 if order["action"] == "BUY" else -1)
        expected = before + delta
        observed = None
        for attempt in range(30):
            self.refresh_positions()
            observed = float(self.state.get_position(ticker))
            if math.isclose(observed, expected, rel_tol=0, abs_tol=1e-8):
                if self.logger:
                    self.logger.event("inventory_confirmed", self.state.period, self.state.tick,
                                      order_id=order["order_id"], ticker=ticker,
                                      expected=expected, observed=observed, reads=attempt + 1)
                return
            if attempt < 29:
                time.sleep(.1)
        self.position_mismatch = {"order_id": order["order_id"], "ticker": ticker,
                                  "before": before, "filled": order["quantity_filled"],
                                  "expected": expected, "observed": observed}
        if self.logger:
            self.logger.event("inventory_mismatch", self.state.period, self.state.tick,
                              **self.position_mismatch)
        raise TradingHalt(f"Inventory reconciliation failed for {ticker}, order {order['order_id']}: "
                          f"expected {expected}, observed {observed}. No order resubmitted.")

    def finish_order(self, result):
        oid = result["order_id"]
        for attempt in range(12):
            if result.get("status") in ("TRANSACTED", "CANCELLED"):
                if not 0 <= float(result["quantity_filled"]) <= float(result["quantity"]):
                    raise TradingHalt("Invalid cumulative fill quantity")
                return result
            if attempt == 6:
                _delete(f"/orders/{oid}")
            time.sleep(.1)
            result = _get(f"/orders/{oid}")
        raise TradingHalt(f"Order {oid} remains unresolved; stopping entries")

    def place_split_orders(self, ticker, action, total_quantity, price=None, order_type="MARKET", reduce_only=False):
        orders, remaining = [], int(total_quantity)
        while remaining > 0:
            result = self.place_order(ticker, action, remaining, price, order_type, reduce_only=reduce_only)
            if not result:
                break
            orders.append(result)
            done = int(result.get("quantity_filled", 0)) if order_type == "MARKET" else int(result["quantity"])
            if done <= 0:
                break
            remaining -= done
            if order_type == "MARKET" and done < int(result["quantity"]):
                break
        return orders

    def cancel_all_orders(self):
        if not _get("/orders", {"status": "OPEN"}):
            self.refresh_positions()
            return
        _post("/commands/cancel", {"all": 1})
        for _ in range(10):
            if not _get("/orders", {"status": "OPEN"}):
                self.refresh_positions()
                return
            time.sleep(.1)
        raise TradingHalt("Cancellation not confirmed; stopping entries")


# ═══════════════════════════════════════════════════════════════════════════════
#  STORAGE MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class StorageManager:
    def __init__(self, state, config):
        self.state = state
        self.config = config
        self.failed_releases = {}

    def ensure_storage(self, location, contracts_needed):
        storage_ticker = {"CL": "CL-STORAGE", "AK": "AK-STORAGE",
                          "NYC": "NYC-STORAGE"}.get(location)
        if not storage_ticker:
            return False

        contract_size = self.config.get_contract_size("CL")
        self.state.update_from_securities(_get("/securities"))
        self.state.leases = {l["id"]: l for l in _get("/leases")}
        held_ticker = {"CL": "CL", "AK": "CL-AK", "NYC": "CL-NYC"}[location]
        barrels_needed = (contracts_needed + max(0, self.state.get_position(held_ticker))) * contract_size
        if self.config.get_asset_capacity(storage_ticker) <= 0:
            raise TradingHalt(f"Missing containment metadata for {storage_ticker}")
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
        leases = _get("/leases")
        self.state.leases = {l["id"]: l for l in leases}
        # Keep destination capacity while any conversion lease remains unresolved.
        if self.state.active_pipelines or self.state.refinery_lease_id is not None:
            return
        if any(l.get("ticker") in ("AK-CS-PIPE", "CS-NYC-PIPE", "CL-REFINERY") for l in leases):
            return
        for lease in leases:
            if lease.get("ticker") in ("CL-STORAGE", "AK-STORAGE", "NYC-STORAGE") and lease.get("containment_usage") == 0:
                if lease['id'] in self.failed_releases:
                    continue  # Do not blindly repeat an uncertain DELETE during shutdown.
                try:
                    _delete(f"/leases/{lease['id']}")
                except (Exception, TradingHalt) as exc:
                    self.failed_releases[lease['id']] = str(exc)
        if self.failed_releases:
            raise TradingHalt(f"Storage release failures (not retried): {self.failed_releases}")


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
        """Trade parsed EIA surprises within live risk constraints; alpha is not guaranteed.

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
        for fticker in (["CL-1F", "CL-2F"] if self.state.period <= 1 else ["CL-2F"]):
            net_limit = self.config.get_net_limit(fticker)
            if bullish:
                qty = self.state.available_net_buy(fticker)
            else:
                qty = self.state.available_net_sell(fticker)
            qty = min(qty, net_limit)
            if qty > 0:
                action = "BUY" if bullish else "SELL"
                print(f"  [EIA] {action} {qty} {fticker} (MAX)")
                qty = filled(self.execution.place_split_orders(fticker, action, qty,
                                                  order_type="MARKET"))
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
        if bullish and self.config.physical_enabled and not self.state.active_pipelines and self.state.refinery_lease_id is None:
            # CL spot — needs storage
            cl_qty = min(
                self.state.available_net_buy("CL"),
                self.config.get_net_limit("CL"))
            cl_qty = self.execution.approved_quantity("CL", "BUY", cl_qty)
            if cl_qty > 0 and self.storage.ensure_storage("CL", cl_qty):
                print(f"  [EIA] BUY {cl_qty} CL spot (MAX)")
                cl_qty = filled(self.execution.place_split_orders("CL", "BUY", cl_qty,
                                                  order_type="MARKET"))
                total_traded += cl_qty
                self.state.scheduled_exits.append(
                    (tick + self.config.eia_exit_fast_ticks, "CL",
                     "SELL", cl_qty, "eia"))

            # CL-AK — needs AK storage
            ak_qty = min(
                self.state.available_net_buy("CL-AK"),
                self.config.get_net_limit("CL-AK"))
            ak_qty = self.execution.approved_quantity("CL-AK", "BUY", ak_qty)
            if ak_qty > 0 and self.storage.ensure_storage("AK", ak_qty):
                print(f"  [EIA] BUY {ak_qty} CL-AK (MAX)")
                ak_qty = filled(self.execution.place_split_orders("CL-AK", "BUY", ak_qty,
                                                  order_type="MARKET"))
                total_traded += ak_qty
                self.state.scheduled_exits.append(
                    (tick + self.config.eia_exit_fast_ticks, "CL-AK",
                     "SELL", ak_qty, "eia"))

            # CL-NYC — needs NYC storage
            nyc_qty = min(
                self.state.available_net_buy("CL-NYC"),
                self.config.get_net_limit("CL-NYC"))
            nyc_qty = self.execution.approved_quantity("CL-NYC", "BUY", nyc_qty)
            if nyc_qty > 0 and self.storage.ensure_storage("NYC", nyc_qty):
                print(f"  [EIA] BUY {nyc_qty} CL-NYC (MAX)")
                nyc_qty = filled(self.execution.place_split_orders("CL-NYC", "BUY", nyc_qty,
                                                  order_type="MARKET"))
                total_traded += nyc_qty
                self.state.scheduled_exits.append(
                    (tick + self.config.eia_exit_fast_ticks, "CL-NYC",
                     "SELL", nyc_qty, "eia"))

            # HO and RB — products cascade, no storage needed
            for pticker in ["HO", "RB"]:
                p_qty = min(
                    self.state.available_net_buy(pticker),
                    self.config.get_net_limit(pticker))
                p_qty = self.execution.approved_quantity(pticker, "BUY", p_qty)
                if p_qty > 0:
                    print(f"  [EIA] BUY {p_qty} {pticker} (MAX)")
                    p_qty = filled(self.execution.place_split_orders(pticker, "BUY", p_qty,
                                                      order_type="MARKET"))
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
            contracts = min(contracts, self.state.available_net_buy(active_future(self.state)),
                           self.state.available_gross("news"))
        else:
            contracts = min(contracts, self.state.available_net_sell(active_future(self.state)),
                           self.state.available_gross("news"))
        if contracts <= 0:
            return

        print(f"  [NEWS] score={score:+.1f} ({matches} matches) -> "
              f"{action} {contracts} {active_future(self.state)}")
        contracts = filled(self.execution.place_split_orders(active_future(self.state), action, contracts,
                                          order_type="MARKET"))
        self.state.strategy_gross["news"] += contracts

        tick = self.state.tick
        exit_action = "SELL" if action == "BUY" else "BUY"
        t1 = int(contracts * 0.70)
        t2 = contracts - t1
        self.state.scheduled_exits.append(
            (tick + self.config.news_exit_fast_ticks, active_future(self.state), exit_action, t1, "news"))
        self.state.scheduled_exits.append(
            (tick + self.config.news_exit_slow_ticks, active_future(self.state), exit_action, t2, "news"))

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
        f1_bid, f1_ask, f1_last = self.state.get_price(active_future(self.state))
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
            profit = (f1_bid - cl_ask) * 10000 - storage_cost - fee if f1_bid and cl_ask else float("-inf")
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
                    self.state.available_net_sell(active_future(self.state)) >= contracts):
                if self.storage.ensure_storage("CL", contracts):
                    print(f"  [BASIS] Convergence: buy CL, short {active_future(self.state)} "
                          f"(basis={market_basis:.3f}, theo={theo_basis:.3f})")
                    self._trade_pair("CL", "BUY", active_future(self.state), "SELL", contracts)

        elif market_basis < theo_basis - self.config.convergence_min_basis:
            held_cl = self.state.get_position("CL")
            sell_qty = min(contracts, held_cl)
            if sell_qty > 0 and self.state.available_net_buy(active_future(self.state)) >= sell_qty:
                print(f"  [BASIS] Convergence: sell CL, buy {active_future(self.state)} "
                      f"(basis={market_basis:.3f}, theo={theo_basis:.3f})")
                self._trade_pair("CL", "SELL", active_future(self.state), "BUY", sell_qty)

    def _execute_cash_carry(self, cl_ask, f1_bid, estimated_profit):
        contracts = 10
        if (self.state.available_net_buy("CL") < contracts or
                self.state.available_net_sell(active_future(self.state)) < contracts or
                self.state.available_gross("basis") < contracts * 2):
            return
        if not self.storage.ensure_storage("CL", contracts):
            return
        print(f"  [BASIS] Cash-carry: buy 10 CL, short 10 {active_future(self.state)} "
              f"(est profit=${estimated_profit:.0f})")
        self._trade_pair("CL", "BUY", active_future(self.state), "SELL", contracts)

    def _trade_pair(self, first, first_action, second, second_action, quantity):
        self.execution.cancel_all_orders()
        entered = filled(self.execution.place_split_orders(first, first_action, quantity))
        if not entered:
            return
        hedged = filled(self.execution.place_split_orders(second, second_action, entered))
        if hedged < entered:
            undo = "SELL" if first_action == "BUY" else "BUY"
            closed = filled(self.execution.place_split_orders(first, undo, entered - hedged))
            if closed != entered - hedged:
                raise TradingHalt("Unmatched basis leg could not be unwound")
        self.state.strategy_gross["basis"] += hedged * 2


# ═══════════════════════════════════════════════════════════════════════════════
#  GEOGRAPHIC ARB ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class PhysicalEngine:
    def __init__(self, state, execution, storage, config):
        self.state, self.execution, self.storage, self.config = state, execution, storage, config
        self.jobs = []

    def now(self):
        return (self.state.period - 1) * self.config.ticks_per_period + self.state.tick

    def confirm_lease(self, lease, ticker):
        """Wait for the acknowledged lease to appear; never create a replacement."""
        for attempt in range(30):
            leases = _get("/leases")
            match = next((row for row in leases if row.get("id") == lease["id"]), None)
            if match is not None:
                if match.get("ticker") != ticker:
                    raise TradingHalt(f"Lease {lease['id']} ticker mismatch for {ticker}: {match}")
                return match
            if attempt < 29:
                time.sleep(.1)
        raise TradingHalt(f"Lease {lease['id']} for {ticker} not visible after creation; "
                          f"no conversion submitted. Creation response: {lease}; active leases: {leases}")

    def start_conversion(self, ticker, source, outputs):
        asset = next(a for a in _get("/assets") if a["ticker"] == ticker)
        params = conversion_spec(asset, source, outputs)
        qty = params["quantity1"]
        self.execution.cancel_all_orders()
        if any(self.state.get_position(t) != 0 for t in outputs):
            return False  # Output accounting must not mix with existing inventory.
        source_location = {"CL": "CL", "CL-AK": "AK"}[source]
        missing = max(0, qty - self.state.get_position(source))
        if missing and self.execution.approved_quantity(source, "BUY", missing) < missing:
            return False
        if not self.storage.ensure_storage(source_location, missing):
            return False
        for destination, quantity in outputs.items():
            if destination in ("CL", "CL-NYC"):
                location = "CL" if destination == "CL" else "NYC"
                if not self.storage.ensure_storage(location, quantity):
                    return False
        if missing and filled(self.execution.place_split_orders(source, "BUY", missing)) != missing:
            raise TradingHalt("Incomplete conversion input purchase; stopping before leasing")
        rows = self.execution.refresh_positions()
        if self.state.get_position(source) < qty:
            raise TradingHalt(f"Conversion input not confirmed for {source}: need {qty}, observed {self.state.get_position(source)}")
        # Check the final physical inventory against the same live weighted limits.
        projected = [dict(r) for r in rows]
        next(r for r in projected if r["ticker"] == source)["position"] -= qty
        limits = _get("/limits")
        for destination, quantity in outputs.items():
            if not risk_allows(projected, limits, [], destination, "BUY", quantity):
                raise TradingHalt("Conversion output would breach portfolio limits")
            next(r for r in projected if r["ticker"] == destination)["position"] += quantity
        lease = _post("/leases", {"ticker": ticker})
        if not isinstance(lease, dict) or "id" not in lease:
            CLIENT.uncertain_write = True
            raise TradingHalt("Unresolved lease creation")
        job = {"id": lease["id"], "ticker": ticker, "outputs": dict(outputs),
               "due": self.now() + int(asset["ticks_per_conversion"]) + 1,
               "confirmed": False, "released": False}
        self.jobs.append(job)
        self.state.conversion_outputs.update(outputs)
        if ticker == "CL-REFINERY":
            self.state.refinery_lease_id = lease["id"]
        else:
            self.state.active_pipelines.append(lease["id"])
        self.confirm_lease(lease, ticker)
        _post(f"/leases/{lease['id']}", params)
        job["confirmed"] = True
        self.execution.refresh_positions()
        print(f"  [PHYSICAL] {ticker}: {qty} {source}; expected outputs {outputs}")
        return True

    def complete(self, release_storage=True):
        for job in list(self.jobs):
            if not job["confirmed"] or self.now() < job["due"]:
                continue
            self.execution.refresh_positions()
            if not job["released"]:
                if any(self.state.get_position(t) < q for t, q in job["outputs"].items()):
                    continue  # Elapsed time alone does not establish output arrival.
                _delete(f"/leases/{job['id']}")
                job["released"] = True
            for ticker, remaining in list(job["outputs"].items()):
                done = filled(self.execution.place_split_orders(ticker, "SELL", remaining, reduce_only=True))
                job["outputs"][ticker] -= done
            if any(job["outputs"].values()):
                continue
            self.jobs.remove(job)
            self.state.conversion_outputs.difference_update(job["outputs"])
            if job["ticker"] == "CL-REFINERY":
                self.state.refinery_lease_id = None
            else:
                self.state.active_pipelines.remove(job["id"])
        if release_storage:
            self.storage.release_empty_storage()

    def busy(self):
        return bool(self.state.active_pipelines or self.state.refinery_lease_id is not None)


class TransportEngine(PhysicalEngine):
    def evaluate(self):
        if self.busy():
            return
        for ticker, source, destination, tanks, buffer in (
            ("AK-CS-PIPE", "CL-AK", "CL", ("AK-STORAGE", "CL-STORAGE"), self.config.ak_cs_buffer),
            ("CS-NYC-PIPE", "CL", "CL-NYC", ("CL-STORAGE", "NYC-STORAGE"), self.config.cs_nyc_buffer),
        ):
            asset = self.config.raw_assets.get(ticker, {})
            if not asset.get("is_available", False) or asset.get("available_quantity", 0) < 1:
                continue
            if self.state.tick + self.config.get_transfer_sec(ticker) + 15 >= self.config.wind_down_start:
                continue
            _, ask, _ = self.state.get_price(source)
            bid, _, _ = self.state.get_price(destination)
            if not ask or not bid:
                continue
            # Include both tanks for two billing intervals, fees and price buffer.
            costs = self.config.get_asset_cost(ticker) + 2 * sum(self.config.get_asset_cost(t) for t in tanks)
            net = (bid - ask - buffer) * 10000 - costs - 20 * self.config.fee_per_contract
            if net > 0 and self.start_conversion(ticker, source, {destination: 10}):
                break

    def check_pipeline_completions(self):
        self.complete()


class CrackEngine(PhysicalEngine):
    def evaluate(self):
        if self.busy():
            return
        asset = self.config.raw_assets.get("CL-REFINERY", {})
        if not asset.get("is_available", False) or asset.get("available_quantity", 0) < 1:
            return
        if self.state.tick + self.config.get_transfer_sec("CL-REFINERY") + 15 >= self.config.wind_down_start:
            return
        _, cl_ask, _ = self.state.get_price("CL")
        rb_bid, _, _ = self.state.get_price("RB")
        ho_bid, _, _ = self.state.get_price("HO")
        if not all((cl_ask, rb_bid, ho_bid)):
            return
        revenue = rb_bid * 20 * self.config.get_contract_size("RB") + ho_bid * 10 * self.config.get_contract_size("HO")
        costs = cl_ask * 30 * self.config.get_contract_size("CL")
        costs += self.config.get_asset_cost("CL-REFINERY") + 6 * self.config.get_asset_cost("CL-STORAGE")
        costs += 60 * self.config.fee_per_contract
        if revenue - costs > self.config.crack_min_profit:
            self.start_conversion("CL-REFINERY", "CL", {"RB": 20, "HO": 10})

    def check_refinery_completion(self):
        self.complete()


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
        net_pos = self.state.get_position(active_future(self.state))
        eia_active = self.state.strategy_gross.get("eia", 0) > 10
        if eia_active:
            return
        if self.state.available_gross("mm") < self.config.mm_size * 2:
            return

        # Use market mid as anchor, blended with fair value model
        market_mid = self.state.get_mid(active_future(self.state))
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

        self.execution.cancel_all_orders()
        print(f"  [MM] Quoting {active_future(self.state)}: bid=${bid_price:.2f} ask=${ask_price:.2f} "
              f"(anchor=${anchor:.2f}, net={net_pos})")
        self.execution.place_order(active_future(self.state), "BUY", self.config.mm_size,
                                   price=bid_price, order_type="LIMIT")
        self.execution.place_order(active_future(self.state), "SELL", self.config.mm_size,
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
        remaining = []
        if any(self.state.tick >= e[0] for e in self.state.scheduled_exits):
            self.execution.cancel_all_orders()
        self.execution.refresh_positions()
        for exit_tick, ticker, action, quantity, strategy in self.state.scheduled_exits:
            if self.state.tick < exit_tick or ticker in self.state.conversion_outputs:
                remaining.append((exit_tick, ticker, action, quantity, strategy))
                continue
            pos = self.state.get_position(ticker)
            reducible = max(0, pos if action == "SELL" else -pos)
            quantity = min(quantity, reducible)
            done = filled(self.execution.place_split_orders(ticker, action, quantity, reduce_only=True)) if quantity else 0
            self.state.strategy_gross[strategy] = max(0, self.state.strategy_gross[strategy] - done)
            if done < quantity:
                remaining.append((self.state.tick + 1, ticker, action, quantity - done, strategy))
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
        self._emergency_flatten()

    def _emergency_flatten(self):
        self.execution.cancel_all_orders()
        self.execution.refresh_positions()
        for ticker in self.config.tickers:
            if ticker in self.state.conversion_outputs:
                continue
            pos = self.state.get_position(ticker)
            if pos > 0:
                self.execution.place_split_orders(ticker, "SELL", pos,
                                                  order_type="MARKET", reduce_only=True)
            elif pos < 0 and self.config.is_shortable(ticker):
                self.execution.place_split_orders(ticker, "BUY", abs(pos),
                                                  order_type="MARKET", reduce_only=True)
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
        spreads = self.observed_spreads.get(active_future(self.state), [])
        if len(spreads) < 5:
            return

        # Use recent observations (last 30)
        recent = spreads[-30:]
        avg_spread = sum(recent) / len(recent)

        depths = self.observed_depths.get(active_future(self.state), [])
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
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
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
        for handler in list(self.logger.handlers):
            handler.close()
            self.logger.removeHandler(handler)


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════════════

def shutdown(state, execution, storage, transport, crack, seconds=60):
    """Best effort only: report residual exposure instead of claiming flatness."""
    deadline = time.monotonic() + seconds
    failures = []
    try:
        execution.cancel_all_orders()
        while time.monotonic() < deadline:
            case = _get("/case")
            state.update_from_case(case)
            execution.refresh_positions()
            if case.get("status") != "ACTIVE":
                break
            transport.complete(release_storage=False)
            crack.complete(release_storage=False)
            # Do not consume outputs still belonging to a pending conversion job.
            reserved = {t for engine in (transport, crack) for job in engine.jobs for t in job["outputs"]}
            for ticker, position in list(state.positions.items()):
                if position and ticker not in reserved:
                    execution.place_split_orders(ticker, "SELL" if position > 0 else "BUY", abs(position), reduce_only=True)
            execution.refresh_positions()
            storage.release_empty_storage()
            if not any(state.positions.values()) and not transport.jobs and not crack.jobs:
                break
            time.sleep(.2)
    except (Exception, TradingHalt) as exc:
        failures.append(str(exc))
    except KeyboardInterrupt:
        failures.append("Cleanup interrupted a second time")
    # These reads never imply that a failed cancellation succeeded.
    report = {"positions": dict(state.positions), "open_orders": None, "leases": None,
              "errors": failures, "flat": False, "uncertain_write": bool(getattr(CLIENT, "uncertain_write", False)),
              "position_mismatch": execution.position_mismatch}
    try:
        execution.refresh_positions()
        report["positions"] = dict(state.positions)
        report["open_orders"] = _get("/orders", {"status": "OPEN"})
        report["leases"] = _get("/leases")
        state.nlv = _get("/trader").get("nlv", state.nlv)
        report["flat"] = not report["position_mismatch"] and not report["uncertain_write"] and not any(state.positions.values()) and not report["open_orders"] and not report["leases"]
    except (Exception, TradingHalt, KeyboardInterrupt) as exc:
        report["errors"].append(f"Final reconciliation failed: {exc}")
    print("SHUTDOWN: " + json.dumps(report, default=str))
    return report


def ensure_clean_account():
    positions = _get("/securities")
    if any(r["position"] for r in positions) or _get("/orders", {"status": "OPEN"}) or _get("/leases"):
        raise TradingHalt("Start with no positions, open orders or leases. Existing account left untouched.")


def run_live(config):
    log = None
    state = None
    owned = False
    exit_code = 0
    try:
        ensure_clean_account()
        print("Waiting for ACTIVE case. Ctrl+C exits without trading.")
        while _get("/case").get("status") != "ACTIVE":
            time.sleep(.5)
        config.discover()
        ensure_clean_account()
        state = StateManager(config)
        state.update_from_case(_get("/case"))
        state.update_from_securities(_get("/securities"))
        state.nlv = _get("/trader").get("nlv", 0)
        old_news = _get("/news", {"since": 0})
        state.last_news_id = max((n["news_id"] for n in old_news), default=0)
        execution = ExecutionEngine(state, config)
        storage = StorageManager(state, config)
        transport = TransportEngine(state, execution, storage, config)
        crack = CrackEngine(state, execution, storage, config)
        eia = EIAEngine(state, execution, storage, config)
        news_engine = NewsEngine(state, execution, config)
        basis = BasisEngine(state, execution, storage, config)
        mm = MarketMaker(state, execution, config)
        risk = RiskManager(state, execution, storage, config)
        orchestrator = Orchestrator(state, config)
        log = RunLogger()
        execution.logger = log
        log.save_config(config)
        owned = True
        last_time = None
        last_period = state.period
        prev_price = None
        while True:
            case = _get("/case")
            if case.get("status") != "ACTIVE":
                if state.period >= config.total_periods:
                    break
                time.sleep(.5)
                continue
            state.update_from_case(case)
            tick, period = state.tick, state.period
            if last_time == (period, tick):
                time.sleep(.05)
                continue
            last_time = (period, tick)
            if period != last_period:
                risk.on_period_end()
                eia.processed_news_ids.clear()
                last_period = period
            # No signal can use the previous tick's inventory or prices.
            execution.refresh_positions()
            state.nlv = _get("/trader").get("nlv", state.nlv)
            transport.complete()
            crack.complete()
            risk.process_scheduled_exits()
            risk.pre_eia_cleanup(tick)
            if tick >= config.wind_down_start:
                risk.check_wind_down()
                # Keep trying exits during wind-down, with no new news entries.
                risk._emergency_flatten()
            else:
                state.drift_fair_value()
                price = state.get_mid("CL")
                orchestrator.observe_price(prev_price, price)
                prev_price = price
                for item in _get("/news", {"since": state.last_news_id}):
                    log.event("news", period, tick, news=item)
                    if eia.is_eia_report(item):
                        before = len(state.eia_calculated_impacts)
                        pre_price = state.get_mid("CL")
                        eia.process(item)
                        if len(state.eia_calculated_impacts) > before and pre_price:
                            orchestrator.observe_eia_pre(tick, pre_price, state.eia_calculated_impacts[-1])
                    else:
                        news_engine.process(item)
                    state.last_news_id = max(state.last_news_id, item["news_id"])
                near_eia = any(abs(tick - t) <= config.eia_pre_cancel_buffer for t in config.eia_ticks)
                if not near_eia:
                    if config.physical_enabled and not transport.busy():
                        basis.evaluate()
                        transport.evaluate()
                        crack.evaluate()
                    mm.update()
                for event_tick in list(orchestrator.eia_pre_prices):
                    if tick - event_tick >= 10 and price:
                        orchestrator.observe_eia_post(event_tick, price)
                if tick % config.reconcile_interval == 0:
                    orchestrator.observe_book(active_future(state), _get("/securities/book", {"ticker": active_future(state)}))
                orchestrator.tune(tick)
            if tick % config.reconcile_interval == 0:
                config.refresh()
                storage.release_empty_storage()
            log.snapshot(period, tick, state.nlv, state.total_gross(), state.fair_value, state.get_mid("CL"))
            time.sleep(config.loop_sleep)
    except KeyboardInterrupt:
        print("Stop requested. Attempting account cleanup; Ctrl+C again aborts cleanup.")
    except (Exception, TradingHalt) as exc:
        exit_code = 1
        if log:
            log.error(f"STOP: {exc}")
        else:
            print(f"STOP: {exc}")
    finally:
        if owned:
            report = shutdown(state, execution, storage, transport, crack)
            if not report["flat"]:
                exit_code = 1
            log.event("shutdown", state.period, state.tick, **report)
            log.save_summary(state, config)
        if log:
            log.close()
    return exit_code


def main(argv=None):
    global CLIENT
    parser = argparse.ArgumentParser(description="Matthew Lones COM5 bot with corrected execution and shutdown")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="Enable orders and asset leases (default)")
    mode.add_argument("--check", action="store_true", help="Read configuration once; no trading")
    mode.add_argument("--dry-run", action="store_true", help="Alias for read-only --check; does not simulate fills")
    parser.add_argument("--futures-only", action="store_true", help="Run EIA/news/MM futures; disable spot, basis and physical assets")
    args = parser.parse_args(argv)
    if not API_KEY:
        parser.error("Set RIT_API_KEY or API_KEY in com5_template.py to your own RIT Client key")
    live = not (args.check or args.dry_run)
    CLIENT = Client(BASE, HEADERS, live=live)
    config = CaseConfig()
    config.physical_enabled = not args.futures_only
    if not live:
        config.discover()
        case = _get("/case")
        print(f"READ-ONLY CHECK: status={case.get('status')} period={case.get('period')} tick={case.get('tick')}")
        print("No orders or leases sent. A read-only check does not verify API order permissions.")
        return
    lock = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bot.lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        parser.error(".bot.lock exists. Confirm no other bot is running before removing a stale lock.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(str(os.getpid()))
        return run_live(config)
    finally:
        os.unlink(lock)


if __name__ == "__main__":
    raise SystemExit(main())
