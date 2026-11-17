# Setup and behavior after the fixes

Updated November 17, 2026. Use bot.py as the entrypoint; com5_template.py supplies its connection settings. This is now a modified derivative, not an unchanged upstream snapshot. See README.md for commands and CHANGES.md for the changes.

## What you must configure

Install requirements.txt and set your own RIT_API_KEY environment variable. Alternatively, edit API_KEY locally in com5_template.py, but do not commit your key. RIT_BASE_URL defaults to http://localhost:9999/v1. The original template's external competition server address is historical; use your instructor's server.

Run `python bot.py --check` first. It performs GET requests only, discovers the case, and validates supported physical conversion metadata. It does not prove API order entry is enabled. Then `python bot.py --live` starts trading when the case is ACTIVE. `--futures-only` can be supplied to both commands to test the futures components separately. Starting without --live cannot place orders or acquire leases through bot.py.

## Scheduled exits are still part of the strategy

EIA trades retain their tiered exits: initially 50% after 10 ticks, 30% after 30 ticks, and the rest after 60 ticks, with integer rounding. Adaptive tuning can change these delays. News trades retain their initial 70%/30% exits at 15/30 ticks. The quantities now come from confirmed fills, and any unfilled exit remainder stays scheduled. An exit only reduces an existing position; it cannot accidentally open the opposite position.

End-of-period wind-down begins 40 ticks before expiry (tick 560 in a 600-tick period), blocks new news/strategy entries and repeatedly attempts closeout. The physical engines stop starting conversions sufficiently earlier. Period 2 uses CL-2F instead of submitting CL-1F orders.

Ctrl+C is separate from scheduled exits. Previously it stopped the loop without account cleanup. It now triggers a cleanup attempt and records the result. Scheduled exits cannot run after a process has been killed; inspect the account after any forced termination.

## Physical assets and limits

The supplied case's supported batches are 10 CL-AK -> 10 CL through AK-CS-PIPE; 10 CL -> 10 CL-NYC through CS-NYC-PIPE; and 30 CL -> 20 RB + 10 HO through CL-REFINERY. Quantities are contracts. Their base-unit economics use each security's contract size. Live convert_from/convert_to metadata must match; different conventions cause a clear refusal, not an inferred conversion.

The bot uses from1/quantity1 for POST /leases/{id}, and reads containment and ticks_per_conversion rather than confusing total_quantity (number of assets) with storage capacity. It verifies input holdings, reserves destination storage and checks final weighted exposure before conversion. Only one conversion job is opened at a time and destination holdings must initially be zero, so completion accounting is not mixed with unrelated inventory. Conversion records survive a period boundary. Both elapsed time and arrived inventory are required before selling output; empty storage needed by unresolved conversions is retained.

Conversion proceeds are not locked in at entry: destination/product prices can move before completion. Lasting API failure can strand inventory or leases. No code can guarantee cleanup under every server state.

Every order is checked against the server's named limit groups, security weights, current positions and outstanding orders. Exchange-side changes and market fills can still race snapshots; the server remains authoritative. The original aggressive EIA idea remains, but it cannot independently allocate a full net limit to every ticker. Open-order risk is reserved, and rejected/partial orders no longer count as fully executed.

## Execution and logs

REST calls have explicit connect/read timeouts. Rate-limit retries are bounded. A timed-out or server-error write is never automatically resubmitted; all engines stop and cleanup is attempted. An uncertain write is explicitly flagged, even if the final snapshot looks flat.

Market orders are polled for terminal status; unresolved remainder is cancelled and reconciled. Basis hedges match actual first-leg fills and attempt to unwind unmatched quantities. Market-making quotes are cancelled before replacement. This remains a market-order-heavy strategy; slippage, fees and crowded news responses can materially affect profit.

The order and shutdown records are in runs/<run_id>/events.jsonl; server NLV observations are in nlv.csv. bot.log contains explicit logger messages, not a complete transcript of every legacy print statement. Abrupt process termination can prevent the normal summary. data_scraper.py still contains an upstream cancellation stress path, so do not run it blindly alongside the live bot. analyze_data.py is the original data-analysis utility, not the trading entrypoint.

## Validation scope

35 offline tests passed on Python 3.13 with requests 2.32.5. A mocked live loop opened positions, received Ctrl+C, cancelled/closed exposure and wrote its summary. A mocked physical cycle checked the 30/20/10 quantities, output arrival, sale and lease cleanup. No real RIT server trading or profit measurement has been performed. Start with one supervised practice heat and retain the official performance report.
