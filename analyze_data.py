"""
COM5 Data Analysis & Parameter Calibration
===========================================
Reads scraped practice heat data and outputs calibration values for bot.py.

Usage:
  python analyze_data.py data/20260327_220000/   # analyze a specific heat
  python analyze_data.py data/                    # analyze all heats

Outputs:
  - EIA news body format analysis (regex patterns)
  - EIA price reaction curves
  - Spread distributions (geographic, basis, crack)
  - Order book depth statistics
  - Recommended parameter values for bot.py CALIBRATE fields
"""

import os
import sys
import json
import csv
import re
from collections import defaultdict
from statistics import mean, median, stdev


def load_csv(path):
    """Load a CSV file as list of dicts."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_jsonl(path):
    """Load a JSONL file as list of dicts."""
    if not os.path.exists(path):
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


class HeatAnalyzer:
    """Analyze a single heat's scraped data."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.securities = load_csv(os.path.join(data_dir, "securities.csv"))
        self.news = load_jsonl(os.path.join(data_dir, "news.jsonl"))
        self.orderbook = load_csv(os.path.join(data_dir, "orderbook.csv"))
        self.tas = load_csv(os.path.join(data_dir, "tas.csv"))
        self.trader = load_csv(os.path.join(data_dir, "trader.csv"))
        self.news_summary = load_csv(os.path.join(data_dir, "news_summary.csv"))

    def analyze_all(self):
        """Run all analyses and return results dict."""
        results = {}
        results["heat_dir"] = self.data_dir
        results["data_coverage"] = self._data_coverage()
        results["eia_analysis"] = self._analyze_eia()
        results["spread_analysis"] = self._analyze_spreads()
        results["orderbook_analysis"] = self._analyze_orderbook()
        results["price_analysis"] = self._analyze_prices()
        results["news_analysis"] = self._analyze_news()
        return results

    def _data_coverage(self):
        """Report what data was collected."""
        return {
            "securities_rows": len(self.securities),
            "news_items": len(self.news),
            "orderbook_rows": len(self.orderbook),
            "tas_trades": len(self.tas),
            "trader_rows": len(self.trader),
        }

    # ── EIA Analysis ────────────────────────────────────────────────────────

    def _analyze_eia(self):
        """Analyze EIA report format and price reactions."""
        eia_reports = []
        non_eia_news = []

        for n in self.news:
            headline = n.get("headline", "").lower()
            body = n.get("body", "").lower()
            if ("eia" in headline or "eia" in body
                    or "inventory" in headline or "storage" in headline
                    or "weekly" in headline):
                eia_reports.append(n)
            else:
                non_eia_news.append(n)

        result = {
            "total_eia_reports": len(eia_reports),
            "total_non_eia_news": len(non_eia_news),
            "eia_bodies": [],
            "eia_price_reactions": [],
        }

        # Dump raw EIA bodies for regex development
        for r in eia_reports:
            result["eia_bodies"].append({
                "news_id": r.get("news_id"),
                "period": r.get("period"),
                "tick": r.get("tick"),
                "headline": r.get("headline", ""),
                "body": r.get("body", ""),
            })

        # Price reactions: CL price before vs after each EIA
        if self.securities and eia_reports:
            # Build tick -> CL price lookup
            cl_prices = {}
            for s in self.securities:
                if s.get("ticker") == "CL":
                    try:
                        tick = int(s["tick"])
                        period = int(s["period"])
                        last = float(s["last"]) if s.get("last") else None
                        if last:
                            cl_prices[(period, tick)] = last
                    except (ValueError, KeyError):
                        pass

            for r in eia_reports:
                try:
                    eia_tick = int(r["tick"])
                    eia_period = int(r["period"])
                except (ValueError, KeyError):
                    continue

                pre_price = cl_prices.get((eia_period, eia_tick - 1))
                if pre_price is None:
                    pre_price = cl_prices.get((eia_period, eia_tick))

                reactions = {}
                for offset in [1, 5, 10, 30, 60]:
                    post = cl_prices.get((eia_period, eia_tick + offset))
                    if post and pre_price:
                        reactions[f"+{offset}"] = round(post - pre_price, 4)

                if reactions:
                    result["eia_price_reactions"].append({
                        "period": eia_period,
                        "tick": eia_tick,
                        "pre_price": pre_price,
                        "reactions": reactions,
                    })

        return result

    # ── Spread Analysis ─────────────────────────────────────────────────────

    def _analyze_spreads(self):
        """Analyze geographic, basis, and crack spreads over time."""
        # Build per-tick price lookup
        tick_prices = defaultdict(dict)  # (period, tick) -> {ticker: last}
        for s in self.securities:
            try:
                key = (int(s["period"]), int(s["tick"]))
                ticker = s["ticker"]
                last = float(s["last"]) if s.get("last") else None
                if last:
                    tick_prices[key][ticker] = last
            except (ValueError, KeyError):
                pass

        ak_cs_spreads = []
        cs_nyc_spreads = []
        basis_1f = []
        basis_2f = []
        crack_spreads = []

        for key, prices in sorted(tick_prices.items()):
            cl = prices.get("CL")
            cl_ak = prices.get("CL-AK")
            cl_nyc = prices.get("CL-NYC")
            cl_1f = prices.get("CL-1F")
            cl_2f = prices.get("CL-2F")
            rb = prices.get("RB")
            ho = prices.get("HO")

            if cl and cl_ak:
                ak_cs_spreads.append(cl - cl_ak)
            if cl and cl_nyc:
                cs_nyc_spreads.append(cl_nyc - cl)
            if cl and cl_1f:
                basis_1f.append(cl_1f - cl)
            if cl and cl_2f:
                basis_2f.append(cl_2f - cl)
            if cl and rb and ho:
                revenue = rb * 84000 + ho * 42000
                cost = cl * 3000
                crack_spreads.append(revenue - cost)

        def stats(data, name):
            if not data:
                return {"name": name, "count": 0}
            return {
                "name": name,
                "count": len(data),
                "min": round(min(data), 4),
                "max": round(max(data), 4),
                "mean": round(mean(data), 4),
                "median": round(median(data), 4),
                "stdev": round(stdev(data), 4) if len(data) > 1 else 0,
            }

        return {
            "ak_cushing_spread": stats(ak_cs_spreads, "CL - CL_AK ($/bbl)"),
            "cushing_nyc_spread": stats(cs_nyc_spreads, "CL_NYC - CL ($/bbl)"),
            "futures_1f_basis": stats(basis_1f, "CL_1F - CL ($/bbl)"),
            "futures_2f_basis": stats(basis_2f, "CL_2F - CL ($/bbl)"),
            "crack_spread_gross": stats(crack_spreads, "3:2:1 crack revenue - cost ($)"),
        }

    # ── Order Book Analysis ─────────────────────────────────────────────────

    def _analyze_orderbook(self):
        """Analyze typical order book depth and spreads."""
        spreads = defaultdict(list)  # ticker -> [spread values]
        depths = defaultdict(lambda: {"bid": [], "ask": []})

        # Group by (period, tick, ticker) to compute per-snapshot stats
        snapshots = defaultdict(lambda: {"bids": [], "asks": []})
        for row in self.orderbook:
            try:
                key = (row["period"], row["tick"], row["ticker"])
                price = float(row["price"])
                qty = int(row["quantity"])
                side = row["side"]
                snapshots[key][f"{side}s"].append((price, qty))
            except (ValueError, KeyError):
                pass

        for key, book in snapshots.items():
            ticker = key[2]
            bids = sorted(book["bids"], key=lambda x: -x[0])
            asks = sorted(book["asks"], key=lambda x: x[0])

            if bids and asks:
                spread = asks[0][0] - bids[0][0]
                spreads[ticker].append(spread)

            bid_depth = sum(q for _, q in bids[:5])
            ask_depth = sum(q for _, q in asks[:5])
            depths[ticker]["bid"].append(bid_depth)
            depths[ticker]["ask"].append(ask_depth)

        result = {}
        for ticker in spreads:
            s = spreads[ticker]
            d = depths[ticker]
            result[ticker] = {
                "spread_mean": round(mean(s), 4) if s else None,
                "spread_median": round(median(s), 4) if s else None,
                "bid_depth_mean": round(mean(d["bid"]), 1) if d["bid"] else None,
                "ask_depth_mean": round(mean(d["ask"]), 1) if d["ask"] else None,
                "snapshots": len(s),
            }
        return result

    # ── Price Analysis ──────────────────────────────────────────────────────

    def _analyze_prices(self):
        """Starting prices and overall price ranges."""
        ticker_prices = defaultdict(list)
        for s in self.securities:
            try:
                last = float(s["last"]) if s.get("last") else None
                if last:
                    ticker_prices[s["ticker"]].append(last)
            except (ValueError, KeyError):
                pass

        result = {}
        for ticker, prices in ticker_prices.items():
            result[ticker] = {
                "start": prices[0] if prices else None,
                "end": prices[-1] if prices else None,
                "min": round(min(prices), 2) if prices else None,
                "max": round(max(prices), 2) if prices else None,
                "range": round(max(prices) - min(prices), 2) if prices else None,
            }
        return result

    # ── Non-EIA News Analysis ───────────────────────────────────────────────

    def _analyze_news(self):
        """Catalog all non-EIA news for keyword development."""
        all_news = []
        for n in self.news:
            headline = n.get("headline", "").lower()
            body = n.get("body", "").lower()
            is_eia = ("eia" in headline or "eia" in body
                      or "inventory" in headline or "storage" in headline
                      or "weekly" in headline)
            all_news.append({
                "news_id": n.get("news_id"),
                "period": n.get("period"),
                "tick": n.get("tick"),
                "headline": n.get("headline", ""),
                "body_preview": n.get("body", "")[:200],
                "is_eia": is_eia,
            })
        return all_news


# ═══════════════════════════════════════════════════════════════════════════════
#  PARAMETER RECOMMENDER
# ═══════════════════════════════════════════════════════════════════════════════

def recommend_parameters(results_list):
    """Given analysis results from one or more heats, recommend bot.py parameters."""
    print("\n" + "=" * 60)
    print("  PARAMETER RECOMMENDATIONS")
    print("=" * 60)

    # Collect across heats
    all_eia_reactions = []
    all_basis_1f = []
    all_ak_spreads = []
    all_nyc_spreads = []
    all_crack_spreads = []
    all_book_spreads = {}

    for r in results_list:
        eia = r.get("eia_analysis", {})
        for reaction in eia.get("eia_price_reactions", []):
            all_eia_reactions.append(reaction)

        spreads = r.get("spread_analysis", {})
        s = spreads.get("futures_1f_basis", {})
        if s.get("count", 0) > 0:
            all_basis_1f.append(s)
        s = spreads.get("ak_cushing_spread", {})
        if s.get("count", 0) > 0:
            all_ak_spreads.append(s)
        s = spreads.get("cushing_nyc_spread", {})
        if s.get("count", 0) > 0:
            all_nyc_spreads.append(s)
        s = spreads.get("crack_spread_gross", {})
        if s.get("count", 0) > 0:
            all_crack_spreads.append(s)

        for ticker, book in r.get("orderbook_analysis", {}).items():
            if ticker not in all_book_spreads:
                all_book_spreads[ticker] = []
            if book.get("spread_mean"):
                all_book_spreads[ticker].append(book["spread_mean"])

    print("\n-- EIA Alpha Engine --")
    if all_eia_reactions:
        moves_10 = [r["reactions"].get("+10", 0) for r in all_eia_reactions
                     if "+10" in r.get("reactions", {})]
        moves_30 = [r["reactions"].get("+30", 0) for r in all_eia_reactions
                     if "+30" in r.get("reactions", {})]
        if moves_10:
            avg_10 = mean([abs(m) for m in moves_10])
            print(f"  Avg |price move| at +10 ticks: ${avg_10:.3f}")
            print(f"  -> EIA_EXIT_FAST_TICKS = 10 looks {'good' if avg_10 > 0.3 else 'may need adjustment'}")
        if moves_30:
            avg_30 = mean([abs(m) for m in moves_30])
            print(f"  Avg |price move| at +30 ticks: ${avg_30:.3f}")
        print(f"  Total EIA reactions analyzed: {len(all_eia_reactions)}")
    else:
        print("  No EIA price reactions captured yet. Need more data.")

    print("\n-- Futures Basis --")
    if all_basis_1f:
        avg_basis = mean([s["mean"] for s in all_basis_1f])
        print(f"  Avg CL-1F basis: ${avg_basis:.4f}/bbl")
        print(f"  -> Theoretical at mid-period (~$0.50). "
              f"Actual {'higher' if avg_basis > 0.5 else 'lower'} = "
              f"{'arb opportunity' if abs(avg_basis - 0.5) > 0.1 else 'roughly efficient'}")
    else:
        print("  No basis data yet.")

    print("\n-- Geographic Arb --")
    if all_ak_spreads:
        avg_ak = mean([s["mean"] for s in all_ak_spreads])
        max_ak = max([s["max"] for s in all_ak_spreads])
        print(f"  AK-Cushing spread: avg=${avg_ak:.2f}, max=${max_ak:.2f}")
        print(f"  -> Breakeven=$4.00. {'Viable!' if max_ak > 4.5 else 'Rarely viable.'}")
    if all_nyc_spreads:
        avg_nyc = mean([s["mean"] for s in all_nyc_spreads])
        max_nyc = max([s["max"] for s in all_nyc_spreads])
        print(f"  Cushing-NYC spread: avg=${avg_nyc:.2f}, max=${max_nyc:.2f}")
        print(f"  -> Breakeven=$2.00. {'Viable!' if max_nyc > 2.5 else 'Rarely viable.'}")

    print("\n-- Crack Spread --")
    if all_crack_spreads:
        avg_crack = mean([s["mean"] for s in all_crack_spreads])
        max_crack = max([s["max"] for s in all_crack_spreads])
        print(f"  Gross crack spread: avg=${avg_crack:,.0f}, max=${max_crack:,.0f}")
        print(f"  -> Need > $300K. {'Viable!' if max_crack > 320000 else 'Not viable at observed levels.'}")

    print("\n-- Market Making --")
    for ticker, spreads_list in all_book_spreads.items():
        avg_spread = mean(spreads_list)
        print(f"  {ticker} avg spread: ${avg_spread:.4f}")
        if ticker == "CL-1F":
            recommended_half = avg_spread * 0.4  # capture ~40% of typical spread
            print(f"  -> Recommended MM_HALF_SPREAD = ${recommended_half:.3f}")

    print("\n-- EIA News Body Samples --")
    for r in results_list:
        eia = r.get("eia_analysis", {})
        for body in eia.get("eia_bodies", [])[:3]:
            print(f"  [{body.get('period')}/{body.get('tick')}] "
                  f"{body.get('headline', '')}")
            print(f"    Body: {body.get('body', '')[:300]}")
            print()


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    if len(sys.argv) < 2:
        print("Usage: python analyze_data.py <data_dir_or_parent>")
        print("  python analyze_data.py data/20260327_220000/  # single heat")
        print("  python analyze_data.py data/                   # all heats")
        sys.exit(1)

    target = sys.argv[1]

    # Determine if target is a single heat dir or parent of multiple
    if os.path.exists(os.path.join(target, "securities.csv")):
        # Single heat
        dirs = [target]
    else:
        # Parent dir — find all subdirs with securities.csv
        dirs = []
        for entry in sorted(os.listdir(target)):
            subdir = os.path.join(target, entry)
            if os.path.isdir(subdir) and os.path.exists(
                    os.path.join(subdir, "securities.csv")):
                dirs.append(subdir)

    if not dirs:
        print(f"No heat data found in {target}")
        sys.exit(1)

    print(f"Analyzing {len(dirs)} heat(s)...\n")

    all_results = []
    for d in dirs:
        print(f"--- {d} ---")
        analyzer = HeatAnalyzer(d)
        results = analyzer.analyze_all()
        all_results.append(results)

        cov = results["data_coverage"]
        print(f"  Securities: {cov['securities_rows']} rows")
        print(f"  News: {cov['news_items']} items")
        print(f"  Order book: {cov['orderbook_rows']} rows")
        print(f"  T&S: {cov['tas_trades']} trades")

        # Price summary
        prices = results.get("price_analysis", {})
        for ticker in ["CL", "CL-1F", "CL-AK", "CL-NYC", "RB", "HO"]:
            p = prices.get(ticker, {})
            if p.get("start"):
                print(f"  {ticker:>6}: ${p['start']:.2f} -> ${p['end']:.2f} "
                      f"(range ${p['range']:.2f})")
        print()

    # Cross-heat recommendations
    recommend_parameters(all_results)

    # Save full results
    output_path = os.path.join(target, "analysis_results.json")
    # Convert for JSON serialization
    with open(output_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nFull results saved to {output_path}")


if __name__ == "__main__":
    main()
