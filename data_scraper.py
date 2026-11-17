"""
COM5 Data Scraper & API Stress Tester
=====================================
Collects all available data from RIT practice heats for strategy calibration.
Also measures API performance characteristics (rate limits, concurrency, latency).

Setup:  pip install requests
Usage:  1. Connect to RIT Client
        2. Run: python data_scraper.py
        3. Data saved to ./data/<timestamp>/

Modes:
  python data_scraper.py              # Normal scraping (default)
  python data_scraper.py --stress     # API stress test only (no data saved)
"""

import requests
import time
import json
import csv
import os
import sys
import threading
import argparse
from datetime import datetime
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Reuse API config from template ──────────────────────────────────────────
from com5_template import (
    API_KEY, BASE, HEADERS, TICKERS,
    get_case, get_news, get_securities, get_order_book,
    get_history, get_tas, get_assets, get_assets_history,
    get_leases, get_trader, get_limits,
    EIA_TICKS,
)

# ── Session with connection pooling ─────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

def _get_fast(endpoint, params=None):
    """GET using pooled session for lower latency."""
    while True:
        r = SESSION.get(f"{BASE}{endpoint}", params=params)
        if r.status_code == 429:
            wait = 0.5
            try:
                wait = r.json().get("wait", 0.5)
            except Exception:
                pass
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()


# ═══════════════════════════════════════════════════════════════════════════════
#  DATA SCRAPER
# ═══════════════════════════════════════════════════════════════════════════════

class DataScraper:
    """Collects all RIT data streams and writes to disk."""

    def __init__(self, output_dir):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

        # Track what we've already seen
        self.last_news_id = 0
        self.seen_tas = defaultdict(set)  # ticker -> set of trade IDs
        self.last_tick = -1
        self.last_period = -1

        # CSV writers (opened lazily)
        self._files = {}
        self._writers = {}

        # Stats
        self.stats = {
            "ticks_scraped": 0,
            "news_collected": 0,
            "orderbook_snapshots": 0,
            "tas_trades": 0,
            "api_calls": 0,
            "rate_limits_hit": 0,
            "errors": 0,
            "start_time": time.time(),
        }

    def _get_writer(self, name, fieldnames):
        """Lazily create a CSV writer for a given data stream."""
        if name not in self._writers:
            path = os.path.join(self.output_dir, f"{name}.csv")
            f = open(path, "w", newline="", encoding="utf-8")
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            self._files[name] = f
            self._writers[name] = writer
        return self._writers[name]

    def _append_json(self, name, data):
        """Append a JSON record to a JSONL file."""
        path = os.path.join(self.output_dir, f"{name}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(data) + "\n")

    def flush(self):
        """Flush all open file buffers to disk."""
        for f in self._files.values():
            f.flush()

    def close(self):
        """Flush and close all open files."""
        for f in self._files.values():
            f.flush()
            f.close()
        self._files.clear()
        self._writers.clear()

    # ── Individual collectors ───────────────────────────────────────────────

    def collect_securities(self, tick, period):
        """Snapshot all 7 tickers: bid, ask, last, position, volume, etc."""
        try:
            securities = get_securities()
            self.stats["api_calls"] += 1
            writer = self._get_writer("securities", [
                "period", "tick", "ticker", "type", "size", "position",
                "vwap", "nlv", "last", "bid", "ask",
                "bid_size", "ask_size", "volume", "unrealized_pl",
            ])
            for s in securities:
                row = {"period": period, "tick": tick}
                row.update(s)
                writer.writerow(row)
            return securities
        except Exception as e:
            self.stats["errors"] += 1
            print(f"  [ERR] securities: {e}")
            return []

    def collect_news(self, tick, period):
        """Collect all new news items. Stores raw body verbatim."""
        try:
            news = get_news(since=self.last_news_id)
            self.stats["api_calls"] += 1
            if not news:
                return []

            self.last_news_id = max(n["news_id"] for n in news)
            self.stats["news_collected"] += len(news)

            for n in news:
                # Store full news as JSONL (preserves raw body exactly)
                self._append_json("news", n)
                # Also write summary to CSV for quick review
                writer = self._get_writer("news_summary", [
                    "news_id", "period", "tick", "headline",
                ])
                writer.writerow({
                    "news_id": n.get("news_id"),
                    "period": n.get("period"),
                    "tick": n.get("tick"),
                    "headline": n.get("headline", ""),
                })
                print(f"  [NEWS] P{n.get('period')} T{n.get('tick')}: {n.get('headline', '')}")

            return news
        except Exception as e:
            self.stats["errors"] += 1
            print(f"  [ERR] news: {e}")
            return []

    def collect_order_books(self, tick, period, tickers=None):
        """Snapshot order books for specified tickers."""
        if tickers is None:
            tickers = ["CL", "CL-1F"]  # Most important for strategy

        writer = self._get_writer("orderbook", [
            "period", "tick", "ticker", "side",
            "price", "quantity", "level",
        ])

        for ticker in tickers:
            try:
                book = get_order_book(ticker)
                self.stats["api_calls"] += 1
                self.stats["orderbook_snapshots"] += 1

                for i, bid in enumerate(book.get("bids", [])):
                    writer.writerow({
                        "period": period, "tick": tick, "ticker": ticker,
                        "side": "bid", "price": bid["price"],
                        "quantity": bid["quantity"], "level": i,
                    })
                for i, ask in enumerate(book.get("asks", [])):
                    writer.writerow({
                        "period": period, "tick": tick, "ticker": ticker,
                        "side": "ask", "price": ask["price"],
                        "quantity": ask["quantity"], "level": i,
                    })
            except Exception as e:
                self.stats["errors"] += 1
                print(f"  [ERR] orderbook {ticker}: {e}")

    def collect_tas(self, tick, period):
        """Collect new time & sales trades for all tickers."""
        writer = self._get_writer("tas", [
            "period", "tick", "ticker", "trade_id", "price", "quantity",
        ])

        for ticker in TICKERS:
            try:
                trades = get_tas(ticker)
                self.stats["api_calls"] += 1

                for t in trades:
                    trade_id = t.get("id")
                    if trade_id not in self.seen_tas[ticker]:
                        self.seen_tas[ticker].add(trade_id)
                        self.stats["tas_trades"] += 1
                        writer.writerow({
                            "period": period, "tick": tick,
                            "ticker": ticker, "trade_id": trade_id,
                            "price": t.get("price"), "quantity": t.get("quantity"),
                        })
            except Exception as e:
                self.stats["errors"] += 1
                print(f"  [ERR] tas {ticker}: {e}")

    def collect_assets(self, tick, period):
        """Snapshot asset availability and lease history."""
        try:
            assets = get_assets()
            self.stats["api_calls"] += 1
            self._append_json("assets", {"period": period, "tick": tick, "assets": assets})
        except Exception as e:
            self.stats["errors"] += 1
            print(f"  [ERR] assets: {e}")

        try:
            history = get_assets_history()
            self.stats["api_calls"] += 1
            self._append_json("assets_history", {"period": period, "tick": tick, "history": history})
        except Exception as e:
            self.stats["errors"] += 1
            print(f"  [ERR] assets_history: {e}")

    def collect_trader(self, tick, period):
        """Track our NLV over time."""
        try:
            trader = get_trader()
            self.stats["api_calls"] += 1
            writer = self._get_writer("trader", [
                "period", "tick", "trader_id", "nlv",
            ])
            writer.writerow({
                "period": period, "tick": tick,
                "trader_id": trader.get("trader_id"),
                "nlv": trader.get("nlv"),
            })
        except Exception as e:
            self.stats["errors"] += 1
            print(f"  [ERR] trader: {e}")

    def collect_limits(self, tick, period):
        """Track position limit usage."""
        try:
            limits = get_limits()
            self.stats["api_calls"] += 1
            self._append_json("limits", {"period": period, "tick": tick, "limits": limits})
        except Exception as e:
            self.stats["errors"] += 1
            print(f"  [ERR] limits: {e}")

    # ── Main scraping loop ──────────────────────────────────────────────────

    def run(self):
        """Main scraping loop. Runs until case ends or interrupted."""
        print(f"Data scraper started. Output: {self.output_dir}")
        print("Waiting for case to become ACTIVE...")

        case_started = False
        full_scrape_interval = 5  # full scrape (books, assets) every N ticks

        try:
            while True:
                try:
                    case = get_case()
                    self.stats["api_calls"] += 1
                except Exception as e:
                    print(f"Cannot reach RIT API: {e}")
                    time.sleep(2)
                    continue

                status = case.get("status", "")
                tick = case.get("tick", 0)
                period = case.get("period", 0)

                if status != "ACTIVE":
                    if case_started:
                        print(f"\nCase ended (status={status}). Final stats:")
                        self._print_stats()
                        break
                    time.sleep(0.5)
                    continue

                if not case_started:
                    case_started = True
                    # Save case metadata
                    # NOTE: Pre-comp heats have only ANOM (300K vol market maker).
                    # Order book, T&S, and spread data reflect single-bot behavior,
                    # not competition-day conditions. Still valid for: EIA news format,
                    # price model validation, API performance, baseline spreads.
                    self._append_json("case_meta", case)
                    print(f"Case ACTIVE: {case.get('name', '?')}, "
                          f"Period {period}, Ticks/period={case.get('ticks_per_period')}")
                    print("  NOTE: Pre-comp mode — ANOM bot only. "
                          "Book/T&S data is single-bot, not competitive.")

                # Skip if same tick (avoid redundant data)
                if tick == self.last_tick and period == self.last_period:
                    time.sleep(0.05)
                    continue

                self.last_tick = tick
                self.last_period = period
                self.stats["ticks_scraped"] += 1

                # ── Every tick: securities + news (highest priority) ────
                self.collect_securities(tick, period)
                self.collect_news(tick, period)
                self.collect_trader(tick, period)

                # ── Every N ticks: order books, T&S, assets ─────────────
                if tick % full_scrape_interval == 0:
                    self.collect_order_books(tick, period)
                    self.collect_tas(tick, period)
                    self.collect_assets(tick, period)
                    self.collect_limits(tick, period)

                # ── Near EIA: increase order book frequency ─────────────
                near_eia = any(abs(tick - t) <= 15 for t in EIA_TICKS)
                if near_eia and tick % full_scrape_interval != 0:
                    self.collect_order_books(tick, period,
                                            tickers=["CL", "CL-1F", "CL-2F"])

                # ── Flush to disk every tick (crash durability) ────────
                self.flush()

                # ── Progress output + periodic stats save ───────────────
                if tick % 30 == 0:
                    elapsed = time.time() - self.stats["start_time"]
                    rate = self.stats["api_calls"] / elapsed if elapsed > 0 else 0
                    print(f"  [P{period} T{tick:>3}] "
                          f"news={self.stats['news_collected']} "
                          f"trades={self.stats['tas_trades']} "
                          f"books={self.stats['orderbook_snapshots']} "
                          f"calls={self.stats['api_calls']} "
                          f"({rate:.1f}/sec) "
                          f"errs={self.stats['errors']}")
                    # Save stats checkpoint so we have them even on hard crash
                    self._save_stats()

                # ── Pacing ──────────────────────────────────────────────
                # No sleep between ticks to maximize data capture.
                # Rate limit retries handle API throttling automatically.

        except KeyboardInterrupt:
            print("\nInterrupted by user.")
            self.close()
            self._save_stats()
            print(f"Data saved to {self.output_dir}")
            return False  # signal: don't restart
        except Exception as e:
            print(f"\n[FATAL] Unexpected error: {e}")
            self.stats["errors"] += 1
        finally:
            self.close()
            self._save_stats()
            print(f"Data saved to {self.output_dir}")
        return True  # signal: ok to restart

    def _print_stats(self):
        elapsed = time.time() - self.stats["start_time"]
        print(f"  Duration:     {elapsed:.1f}s")
        print(f"  Ticks:        {self.stats['ticks_scraped']}")
        print(f"  News items:   {self.stats['news_collected']}")
        print(f"  T&S trades:   {self.stats['tas_trades']}")
        print(f"  Book snaps:   {self.stats['orderbook_snapshots']}")
        print(f"  API calls:    {self.stats['api_calls']}")
        print(f"  Rate limits:  {self.stats['rate_limits_hit']}")
        print(f"  Errors:       {self.stats['errors']}")
        print(f"  Avg rate:     {self.stats['api_calls']/elapsed:.1f} calls/sec")

    def _save_stats(self):
        self.stats["end_time"] = time.time()
        self.stats["duration_sec"] = self.stats["end_time"] - self.stats["start_time"]
        path = os.path.join(self.output_dir, "scraper_stats.json")
        with open(path, "w") as f:
            json.dump(self.stats, f, indent=2)


# ═══════════════════════════════════════════════════════════════════════════════
#  API STRESS TESTER
# ═══════════════════════════════════════════════════════════════════════════════

class APIStressTester:
    """Measure API performance: latency, rate limits, concurrency."""

    def __init__(self):
        self.results = {}

    def run(self):
        print("=" * 60)
        print("API STRESS TEST")
        print("=" * 60)

        # Wait for active case
        print("\nWaiting for case to be ACTIVE...")
        while True:
            try:
                case = get_case()
                if case.get("status") == "ACTIVE":
                    break
            except Exception:
                pass
            time.sleep(1)
        print("Case is ACTIVE. Starting tests...\n")

        self.test_single_latency()
        self.test_pooled_latency()
        self.test_rate_limit()
        self.test_concurrent_gets()
        self.test_concurrent_posts()
        self.print_summary()

    def test_single_latency(self):
        """Measure round-trip latency for single GET requests (no pooling)."""
        print("1. Single GET latency (no connection pooling)...")
        latencies = []
        for _ in range(20):
            start = time.perf_counter()
            requests.get(f"{BASE}/case", headers=HEADERS)
            latencies.append((time.perf_counter() - start) * 1000)

        self.results["single_get_ms"] = {
            "min": min(latencies),
            "max": max(latencies),
            "avg": sum(latencies) / len(latencies),
            "p50": sorted(latencies)[len(latencies) // 2],
        }
        print(f"   avg={self.results['single_get_ms']['avg']:.1f}ms, "
              f"min={self.results['single_get_ms']['min']:.1f}ms, "
              f"max={self.results['single_get_ms']['max']:.1f}ms\n")

    def test_pooled_latency(self):
        """Measure round-trip latency with connection pooling."""
        print("2. Pooled GET latency (with connection reuse)...")
        latencies = []
        for _ in range(20):
            start = time.perf_counter()
            SESSION.get(f"{BASE}/case")
            latencies.append((time.perf_counter() - start) * 1000)

        self.results["pooled_get_ms"] = {
            "min": min(latencies),
            "max": max(latencies),
            "avg": sum(latencies) / len(latencies),
            "p50": sorted(latencies)[len(latencies) // 2],
        }
        print(f"   avg={self.results['pooled_get_ms']['avg']:.1f}ms, "
              f"min={self.results['pooled_get_ms']['min']:.1f}ms, "
              f"max={self.results['pooled_get_ms']['max']:.1f}ms\n")

    def test_rate_limit(self):
        """Find the rate limit by sending rapid-fire requests."""
        print("3. Rate limit detection (rapid-fire GETs)...")
        count = 0
        rate_limited_at = None
        start = time.perf_counter()

        for i in range(200):
            r = SESSION.get(f"{BASE}/case")
            count += 1
            if r.status_code == 429:
                elapsed = time.perf_counter() - start
                rate_limited_at = count
                try:
                    wait_time = r.json().get("wait", "?")
                except Exception:
                    wait_time = "?"
                print(f"   Rate limited after {count} requests in {elapsed:.2f}s "
                      f"({count/elapsed:.0f} req/s). Wait={wait_time}s")
                break

        if rate_limited_at is None:
            elapsed = time.perf_counter() - start
            print(f"   No rate limit hit after {count} requests in {elapsed:.2f}s "
                  f"({count/elapsed:.0f} req/s)")

        self.results["rate_limit"] = {
            "requests_before_limit": rate_limited_at or f">{count}",
            "requests_per_sec": count / (time.perf_counter() - start),
        }
        print()

    def test_concurrent_gets(self):
        """Test concurrent GET requests."""
        print("4. Concurrent GET requests (ThreadPool)...")

        def timed_get(endpoint):
            start = time.perf_counter()
            r = SESSION.get(f"{BASE}{endpoint}")
            elapsed = (time.perf_counter() - start) * 1000
            return {"status": r.status_code, "ms": elapsed, "endpoint": endpoint}

        endpoints = [
            "/case", "/securities", "/news",
            "/securities/book?ticker=CL", "/securities/book?ticker=CL-1F",
        ]

        # Fire all 5 concurrently
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(timed_get, ep) for ep in endpoints]
            results = [f.result() for f in as_completed(futures)]
        total = (time.perf_counter() - start) * 1000

        for r in results:
            status_mark = "OK" if r["status"] == 200 else f"ERR:{r['status']}"
            print(f"   {r['endpoint']:40s} {r['ms']:>6.1f}ms [{status_mark}]")
        print(f"   TOTAL wall time: {total:.1f}ms (vs sequential: {sum(r['ms'] for r in results):.1f}ms)")

        self.results["concurrent_gets"] = {
            "wall_time_ms": total,
            "sequential_sum_ms": sum(r["ms"] for r in results),
            "speedup": sum(r["ms"] for r in results) / total if total > 0 else 0,
            "all_succeeded": all(r["status"] == 200 for r in results),
        }
        print()

    def test_concurrent_posts(self):
        """Test concurrent POST /orders — this is the critical test for order splitting."""
        print("5. Concurrent POST /orders (order splitting test)...")
        print("   Placing 4 LIMIT BUY orders for CL-1F at $0.01 (will not fill)...")

        def timed_post(i):
            start = time.perf_counter()
            try:
                r = SESSION.post(f"{BASE}/orders", headers=HEADERS, params={
                    "ticker": "CL-1F",
                    "type": "LIMIT",
                    "quantity": 1,
                    "action": "BUY",
                    "price": 0.01,  # Won't fill — just testing API acceptance
                })
                elapsed = (time.perf_counter() - start) * 1000
                return {"status": r.status_code, "ms": elapsed, "order": i}
            except Exception as e:
                elapsed = (time.perf_counter() - start) * 1000
                return {"status": f"ERR:{e}", "ms": elapsed, "order": i}

        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(timed_post, i) for i in range(4)]
            results = [f.result() for f in as_completed(futures)]
        total = (time.perf_counter() - start) * 1000

        for r in results:
            print(f"   Order {r['order']}: {r['ms']:>6.1f}ms [status={r['status']}]")
        print(f"   TOTAL wall time: {total:.1f}ms")

        accepted = sum(1 for r in results if r["status"] == 200)
        self.results["concurrent_posts"] = {
            "wall_time_ms": total,
            "orders_accepted": accepted,
            "orders_attempted": len(results),
            "all_succeeded": accepted == len(results),
        }

        # Cancel our test orders
        try:
            from com5_template import cancel_all
            cancelled = cancel_all()
            print(f"   Cancelled test orders: {cancelled}")
        except Exception:
            pass
        print()

    def print_summary(self):
        print("=" * 60)
        print("STRESS TEST SUMMARY")
        print("=" * 60)

        print(f"\nLatency:")
        print(f"  Single GET avg:  {self.results['single_get_ms']['avg']:.1f}ms")
        print(f"  Pooled GET avg:  {self.results['pooled_get_ms']['avg']:.1f}ms")
        pool_speedup = self.results['single_get_ms']['avg'] / self.results['pooled_get_ms']['avg']
        print(f"  Pooling speedup: {pool_speedup:.1f}x")

        print(f"\nRate Limiting:")
        rl = self.results["rate_limit"]
        print(f"  Limit hit after: {rl['requests_before_limit']} requests")
        print(f"  Max throughput:  {rl['requests_per_sec']:.0f} req/s")

        print(f"\nConcurrency:")
        cg = self.results["concurrent_gets"]
        print(f"  5 concurrent GETs:  {cg['wall_time_ms']:.0f}ms wall "
              f"(vs {cg['sequential_sum_ms']:.0f}ms sequential, "
              f"{cg['speedup']:.1f}x speedup)")
        print(f"  All succeeded: {cg['all_succeeded']}")

        cp = self.results["concurrent_posts"]
        print(f"  4 concurrent POSTs: {cp['wall_time_ms']:.0f}ms wall, "
              f"{cp['orders_accepted']}/{cp['orders_attempted']} accepted")
        print(f"  All succeeded: {cp['all_succeeded']}")

        print(f"\nRecommendation:")
        if cp["all_succeeded"] and cg["speedup"] > 1.5:
            print("  -> USE ASYNC: API handles concurrency well. Fire split orders in parallel.")
        elif cp["all_succeeded"]:
            print("  -> ASYNC MARGINAL: Concurrency works but speedup is small. Still worth using.")
        else:
            print("  -> USE SEQUENTIAL: API rejects concurrent POSTs. Fire orders one at a time.")

        # Save results
        path = os.path.join("data", "stress_test_results.json")
        os.makedirs("data", exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.results, f, indent=2)
        print(f"\nResults saved to {path}")


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def run_continuous():
    """Run scraper continuously across multiple heats.
    Each heat gets its own timestamped output directory.
    Automatically restarts when a heat ends and waits for the next one.
    """
    heat_count = 0
    print("=" * 60)
    print("CONTINUOUS DATA SCRAPER")
    print("Ctrl+C to stop. Each heat saved to data/<timestamp>/")
    print("=" * 60)

    while True:
        heat_count += 1
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = os.path.join("data", timestamp)
        print(f"\n--- Heat #{heat_count} | Output: {output_dir} ---")

        scraper = DataScraper(output_dir)
        should_continue = scraper.run()

        if not should_continue:
            # KeyboardInterrupt — user wants to stop
            break

        print(f"\nHeat #{heat_count} complete. Waiting 5s before polling for next heat...")
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            print("\nStopped by user between heats.")
            break

    print(f"\nTotal heats scraped: {heat_count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="COM5 Data Scraper & API Stress Tester")
    parser.add_argument("--stress", action="store_true",
                        help="Run API stress test only (no data scraping)")
    parser.add_argument("--continuous", action="store_true",
                        help="Run continuously across multiple heats (for overnight)")
    parser.add_argument("--output", type=str, default=None,
                        help="Output directory (default: data/<timestamp>)")
    args = parser.parse_args()

    if args.stress:
        tester = APIStressTester()
        tester.run()
    elif args.continuous:
        run_continuous()
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = args.output or os.path.join("data", timestamp)
        scraper = DataScraper(output_dir)
        scraper.run()
