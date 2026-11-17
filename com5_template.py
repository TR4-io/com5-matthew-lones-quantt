"""
COM5 Commodities Capstone — Python REST API Template
=====================================================
All API endpoints are wrapped below. COM5 rules and limits are enforced.
This file is a reference scaffold, not the active production bot.
Use `bot.py` for the maintained competition runtime.
Fill in your strategy in the main loop at the bottom if you want a starter template.

Setup:  pip install requests
Usage:  1. Connect to RIT Client (rit.ethansolnik.com:443)
        2. Copy your API key from the footer icon
        3. Paste it below and run this script
"""

import os
import requests
import time

# ── Config ───────────────────────────────────────────────────────────────────
API_KEY = os.environ.get("RIT_API_KEY", "")  # Set your own key here or in the environment.
BASE    = os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1")
HEADERS = {"X-API-Key": API_KEY} #<-- DO NOT CHANGE THIS

# ── COM5 Case Rules ─────────────────────────────────────────────────────────
GROSS_LIMIT      = 500        # max total contracts across all tickers
NET_LIMIT        = 100        # max net position per ticker
MAX_ORDER_SIZE   = 30         # max contracts per single order
FEE_PER_CONTRACT = 1.00       # $1 per contract traded
PERIODS          = 2          # 2 periods per case
TICKS_PER_PERIOD = 600        # 10 minutes at 1 tick/sec
MARK_TO_MARKET   = 30         # every 30 ticks

# EIA report ticks each period
EIA_TICKS = [90, 240, 390, 540]

# $1 price move per 10M barrel differential
EIA_IMPACT_PER_10M = 1.0

# Securities
TICKERS = {
    "CL":     {"size": 1000,  "unit": "barrels", "shortable": False, "type": "spot"},
    "CL-AK":  {"size": 1000,  "unit": "barrels", "shortable": False, "type": "spot"},
    "CL-NYC": {"size": 1000,  "unit": "barrels", "shortable": False, "type": "spot"},
    "CL-1F":  {"size": 1000,  "unit": "barrels", "shortable": True,  "type": "futures"},
    "CL-2F":  {"size": 1000,  "unit": "barrels", "shortable": True,  "type": "futures"},
    "HO":     {"size": 42000, "unit": "gallons", "shortable": False, "type": "product"},
    "RB":     {"size": 42000, "unit": "gallons", "shortable": False, "type": "product"},
}

# Assets
ASSETS = {
    "CL-STORAGE":  {"cost": 500,     "unit": "per day",  "capacity": 10000, "max_leases": 10, "transfer_sec": 0},
    "AK-STORAGE":  {"cost": 500,     "unit": "per day",  "capacity": 10000, "max_leases": 10, "transfer_sec": 0},
    "NYC-STORAGE": {"cost": 500,     "unit": "per day",  "capacity": 10000, "max_leases": 10, "transfer_sec": 0},
    "AK-CS-PIPE":  {"cost": 40000,   "unit": "per use",  "capacity": 10000, "max_leases": 10, "transfer_sec": 30},
    "CS-NYC-PIPE": {"cost": 20000,   "unit": "per use",  "capacity": 10000, "max_leases": 10, "transfer_sec": 30},
    "CL-REFINERY": {"cost": 300000,  "unit": "per 2 days", "capacity": 30000, "max_leases": 1, "transfer_sec": 45},
}

# Refinery 3:2:1 crack spread
# Input:  30 CL contracts (30,000 barrels)
# Output: 20 RB contracts (840,000 gallons) + 10 HO contracts (420,000 gallons)
REFINERY_CL_IN   = 30
REFINERY_RB_OUT  = 840000   # gallons
REFINERY_HO_OUT  = 420000   # gallons
REFINERY_COST    = 300000

# Storage penalty if you hold spot CL without a lease
STORAGE_AUTO_PENALTY = 2500  # extra per unit


# ── API Helpers ──────────────────────────────────────────────────────────────
def _get(endpoint, params=None):
    """GET with automatic rate-limit retry."""
    while True:
        r = requests.get(f"{BASE}{endpoint}", headers=HEADERS, params=params)
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
    """POST with automatic rate-limit retry."""
    while True:
        r = requests.post(f"{BASE}{endpoint}", headers=HEADERS, params=params)
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
    """DELETE with automatic rate-limit retry."""
    while True:
        r = requests.delete(f"{BASE}{endpoint}", headers=HEADERS)
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


# ── Case & Trader ────────────────────────────────────────────────────────────
def get_case():
    """Returns: {name, period, tick, status, ticks_per_period, total_periods}"""
    return _get("/case")

def get_trader():
    """Returns: {trader_id, first_name, last_name, nlv}"""
    return _get("/trader")

def get_limits():
    """Returns: [{ticker, gross, net, gross_limit, net_limit, gross_used, net_used}]"""
    return _get("/limits")


# ── News ─────────────────────────────────────────────────────────────────────
def get_news(since=0, limit=None):
    """Returns: [{news_id, period, tick, trader_id, headline, body}]"""
    params = {"since": since}
    if limit:
        params["limit"] = limit
    return _get("/news", params)


# ── Securities ───────────────────────────────────────────────────────────────
def get_securities(ticker=None):
    """Returns: [{ticker, type, size, position, vwap, nlv, last, bid, ask, bid_size, ask_size, volume, unrealized_pl}]"""
    params = {"ticker": ticker} if ticker else None
    return _get("/securities", params)

def get_order_book(ticker):
    """Returns: {bids: [{price, quantity, trader_id}], asks: [...]}"""
    return _get("/securities/book", {"ticker": ticker})

def get_history(ticker, period=None, limit=None):
    """Returns: [{period, tick, open, high, low, close, volume}]"""
    params = {"ticker": ticker}
    if period: params["period"] = period
    if limit:  params["limit"] = limit
    return _get("/securities/history", params)

def get_tas(ticker):
    """Time & sales. Returns: [{id, period, tick, price, quantity}]"""
    return _get("/securities/tas", {"ticker": ticker})


# ── Orders ───────────────────────────────────────────────────────────────────
def get_orders(status=None):
    """Returns: [{order_id, period, tick, ticker, type, quantity, action, price, quantity_filled, vwap, status}]"""
    params = {"status": status} if status else None
    return _get("/orders", params)

def get_order(order_id):
    """Get a single order by ID."""
    return _get(f"/orders/{order_id}")

def place_order(ticker, action, quantity, price=None, order_type="LIMIT"):
    """
    Place an order with built-in safety checks.
    action: 'BUY' or 'SELL'
    order_type: 'LIMIT' or 'MARKET'
    """
    # Enforce max order size
    if quantity > MAX_ORDER_SIZE:
        raise ValueError(f"Order size {quantity} exceeds max {MAX_ORDER_SIZE}")

    # Block short-selling non-shortable tickers
    if action == "SELL" and ticker in TICKERS and not TICKERS[ticker]["shortable"]:
        sec = get_securities(ticker)
        if sec:
            position = sec[0]["position"]
            if quantity > position:
                raise ValueError(f"Cannot short {ticker} (not shortable). Position={position}, trying to sell {quantity}")

    params = {"ticker": ticker, "type": order_type, "quantity": quantity, "action": action}
    if price is not None and order_type == "LIMIT":
        params["price"] = price
    return _post("/orders", params=params)

def cancel_order(order_id):
    """Cancel a specific order."""
    return _delete(f"/orders/{order_id}")

def cancel_all():
    """Cancel all open orders. Returns: {cancelled: N}"""
    return _post("/commands/cancel", params={"all": 1})


# ── Tenders ──────────────────────────────────────────────────────────────────
def get_tenders():
    """Returns: [{tender_id, period, tick, expires, caption, quantity, action, is_fixed_bid, price}]"""
    return _get("/tenders")

def accept_tender(tender_id, price=None):
    """Accept a tender offer."""
    params = {"price": price} if price is not None else None
    return _post(f"/tenders/{tender_id}", params=params)

def decline_tender(tender_id):
    """Decline a tender offer."""
    return _delete(f"/tenders/{tender_id}")


# ── Assets & Leases ─────────────────────────────────────────────────────────
def get_assets():
    """Returns: [{ticker, type, description, lease_price, convert_from, convert_to, total_quantity, available_quantity, lease_size}]"""
    return _get("/assets")

def get_assets_history():
    """Returns activity log for all asset events."""
    return _get("/assets/history")

def get_leases():
    """Returns: [{id, ticker, type, start_lease_period, start_lease_tick, next_lease_period, next_lease_tick, convert_from, convert_to, containment_usage}]"""
    return _get("/leases")

def get_lease(lease_id):
    """Get a single lease by ID."""
    return _get(f"/leases/{lease_id}")

def lease_asset(ticker, quantity=1):
    """Lease an asset (storage, pipeline, or refinery)."""
    return _post("/leases", params={"ticker": ticker, "quantity": quantity})

def use_lease(lease_id, quantity, source):
    """Use a leased asset (e.g. transport through pipeline, run refinery)."""
    return _post(f"/leases/{lease_id}", params={"from1": source, "quantity1": quantity})

def release_lease(lease_id):
    """Release a leased asset. CRITICAL for refinery — release ASAP after conversion."""
    return _delete(f"/leases/{lease_id}")


# ── COM5 Utility Functions ──────────────────────────────────────────────────
def get_positions():
    """Get a {ticker: position} dict for all securities."""
    return {s["ticker"]: s["position"] for s in get_securities()}

def get_prices():
    """Get a {ticker: {bid, ask, last}} dict for all securities."""
    return {s["ticker"]: {"bid": s["bid"], "ask": s["ask"], "last": s["last"]} for s in get_securities()}

def calc_crack_spread(cl_price, rb_price, ho_price):
    """
    Calculate the 3:2:1 crack spread profit (before refinery cost).
    Returns profit in dollars. Profitable to refine if > REFINERY_COST ($300,000).
    """
    revenue = (rb_price * REFINERY_RB_OUT) + (ho_price * REFINERY_HO_OUT)
    cost    = cl_price * (REFINERY_CL_IN * TICKERS["CL"]["size"])
    return revenue - cost

def calc_transport_profit_ak(cl_price, cl_ak_price, contracts=10):
    """Profit from AK→Cushing pipeline arbitrage (per 10 contracts)."""
    spread = (cl_price - cl_ak_price) * contracts * TICKERS["CL"]["size"]
    return spread - ASSETS["AK-CS-PIPE"]["cost"]

def calc_transport_profit_nyc(cl_nyc_price, cl_price, contracts=10):
    """Profit from Cushing→NYC pipeline arbitrage (per 10 contracts)."""
    spread = (cl_nyc_price - cl_price) * contracts * TICKERS["CL"]["size"]
    return spread - ASSETS["CS-NYC-PIPE"]["cost"]

def calc_storage_arb(futures_price, spot_price, days_to_expiry):
    """
    Profit from cost-of-carry storage arbitrage per storage unit (10 contracts).
    One storage lease = 10K barrels = 10 contracts, costs $500/day.
    """
    contracts_per_unit = ASSETS["CL-STORAGE"]["capacity"] // TICKERS["CL"]["size"]  # 10
    storage_cost = ASSETS["CL-STORAGE"]["cost"] * days_to_expiry
    revenue = (futures_price - spot_price) * contracts_per_unit * TICKERS["CL"]["size"]
    return revenue - storage_cost

def parse_eia_impact(expected_change_m, actual_change_m):
    """
    Calculate CL price impact from an EIA report.
    Both values in millions of barrels (positive = inventory build).
    Returns expected dollar move in CL price.
    """
    diff = expected_change_m - actual_change_m  # positive = bullish (less supply than expected)
    return diff * EIA_IMPACT_PER_10M / 10

def is_near_eia(tick, buffer=5):
    """Check if current tick is near an EIA release."""
    return any(abs(tick - t) <= buffer for t in EIA_TICKS)

def check_limits():
    """Return current gross/net usage from /limits."""
    limits = get_limits()
    return {l["ticker"]: {"gross": l["gross"], "net": l["net"],
                          "gross_limit": l["gross_limit"], "net_limit": l["net_limit"]}
            for l in limits}


# ── Pipeline Helper ──────────────────────────────────────────────────────────
def run_pipeline(pipe_ticker, quantity=10):
    """
    Lease a pipeline, use it, return lease_id.
    Pipelines auto-release after transport (30 sec).
    """
    lease = lease_asset(pipe_ticker)
    lease_id = lease["id"]
    use_lease(lease_id, quantity, {"AK-CS-PIPE": "CL-AK", "CS-NYC-PIPE": "CL"}[pipe_ticker])
    return lease_id


# ── Refinery Helper ──────────────────────────────────────────────────────────
def run_refinery(cl_contracts=30, wait=True):
    """
    Lease refinery, convert CL→RB+HO, release.
    Set wait=True to block until conversion completes (45 sec).
    IMPORTANT: You must hold 30 CL contracts in your position before calling this.
    Returns lease_id.
    """
    lease = lease_asset("CL-REFINERY")
    lease_id = lease["id"]
    use_lease(lease_id, cl_contracts, "CL")
    if wait:
        time.sleep(ASSETS["CL-REFINERY"]["transfer_sec"] + 1)
        release_lease(lease_id)
    return lease_id


# ═══════════════════════════════════════════════════════════════════════════════
#  STRATEGY — FILL IN BELOW
# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    last_news_id = 0

    while True:
        case = get_case()
        if case["status"] != "ACTIVE":
            time.sleep(0.5)
            continue

        tick   = case["tick"]
        period = case["period"]

        # ── Market Data ──────────────────────────────────────────────────
        prices    = get_prices()
        positions = get_positions()

        # ── News Polling ─────────────────────────────────────────────────
        news = get_news(since=last_news_id)
        if news:
            last_news_id = max(n["news_id"] for n in news)
            for n in news:
                print(f"[P{period} T{tick}] {n['headline']}")
                # TODO: Parse EIA reports and geopolitical events
                # Use parse_eia_impact() for EIA data

        # ── Your Strategy Here ───────────────────────────────────────────
        #
        # Ideas to implement:
        #   1. EIA trading:     parse news → calc fair value → trade CL/CL-1F/CL-2F
        #   2. Storage arb:     calc_storage_arb() → lease storage → buy spot, short futures
        #   3. Transport arb:   calc_transport_profit_ak/nyc() → run_pipeline()
        #   4. Crack spread:    calc_crack_spread() → run_refinery()
        #
        # Remember:
        #   - Lease storage BEFORE buying spot CL (avoid $2,500 penalty)
        #   - Release refinery ASAP after 45 sec (avoid double charge)
        #   - Only CL-1F and CL-2F are shortable
        #   - Max 30 contracts per order
        #   - Gross limit 500, net limit 100
        #   - Futures settle into CL at tick 600
        #

        # ─────────────────────────────────────────────────────────────────

        time.sleep(0.1)  # ~10 loops/sec, adjust as needed
