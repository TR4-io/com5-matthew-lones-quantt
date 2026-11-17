# Original restoration — November 17, 2026

Baseline: [MatthewLones/QUANTTxCOM5 e260722a](https://github.com/MatthewLones/QUANTTxCOM5/tree/e260722ad2adfbc862c963c2e634b70acdb3c5a4). All eight original files were first restored and checked against the recorded upstream SHA-256 hashes.

The restoration initially retained these executable differences (the subsequent console-output fixes are listed below):

- Cancel-all supplies `all=1`. The API requires a selector; the original request supplied none.
- Pipeline/refinery use requests supply `from1` and `quantity1`, which the API requires, instead of the unsupported `quantity` field. Sources are CL-AK for Alaska-to-Cushing and CL for Cushing-to-NYC/refinery. The template helper and its callers use the same fields.
- The supplied COM5 refinery batch is 30 CL -> 20 RB + 10 HO. The original 3/2/1 is a ratio, not a complete batch. Input checks, request quantity, revenue/cost quantities, gross bookkeeping and template constants now use the full batch. Estimated purchase/sale fees use 60 contracts (30 input plus 30 output).
- The original author's API key is replaced with a blank user setting supporting RIT_API_KEY. RIT_BASE_URL remains configurable with the original localhost default.

API-field evidence: [RIT REST 1.0.4 schema](https://rit.306w.ca/RIT-REST-API/1.0.4/swagger.yaml), `/commands/cancel` and `/leases/{id}`. Batch evidence: the user's supplied COM5 case and live conversion metadata accepted during earlier runs. These are targeted request/batch corrections, not proof that the full physical strategy works.

Removed all custom runtime/execution/shutdown rewrites, inventory and lease polling patches, command-line modes, and tests of those removed features. Restored original README, strategy documentation, scraper and analysis tool. Original behavior, including its remaining limitations, is preserved outside the explicitly listed fixes. No speculative fixes for the reported invalid lease ID or server DELETE failures remain.

Four focused offline tests verify cancellation arguments, pipeline helper arguments, refinery helper batch, and the bot's refinery input gate/request. Python compilation also passes. No live RIT run was performed. Historical commits retain the earlier implementation for comparison; this restoration does not rewrite Git history.

## Confirmed console-output fixes — November 17, 2026

The new user run showed repeated failed CL-2F orders and a reported deployment of 690 contracts despite failures. These fixes preserve strategy signals and exit timings:

- Every order is checked against fresh securities positions, open orders and the server's named limit groups/weights. Shared net and gross limits replace the original execution path's assumption that each instrument independently has room. Nontradeable securities, expired CL-1F and prohibited shorts are blocked.
- Market orders cancel outstanding quotes first, obtain a terminal order result, and reconcile its confirmed fill against inventory before another chunk. Partial/rejected orders stop the split; unresolved fills halt without resubmission. This prevents stale inventory from opening room for the next EIA leg.
- EIA and news counters/exits use confirmed fills. The console now reports confirmed contracts filled, not requested contracts deployed. EIA spot capacity is checked before renting tanks.
- Scheduled exits cap orders to the current position in the closing direction, and carry confirmed unfilled remainders forward. They cannot establish a new opposite position.
- Before original pipeline/refinery leasing, current asset availability is checked. Physical lifecycle logic is otherwise unchanged.
- HTTP errors include the server response body. A write timeout, server 5xx or unresolved response stops new writes and saves read-only failure.json evidence (case, securities, limit groups, open orders, leases and assets). Only rate-limit responses are retried, with a finite bound. No speculative endpoint or lease-ID substitutions were added.

The exact cause of the pipeline HTTP 500 remains unknown. These changes prevent repeated ambiguous requests and expose diagnostic evidence; they are not a claim to have fixed the server. Stopping still requires manual review of positions/orders/leases; automatic shutdown liquidation has not been reintroduced.

Validation: 19 offline tests and compilation. The EIA regression starts with 10 contracts and shared net limit 100, confirms only 90 new fills, preserves 10/30/60-tick exits, and acquires no spot storage. A mocked live main loop verifies one failed pipeline write, no later market-making action, and a saved failure.json. No live RIT or profit validation.
