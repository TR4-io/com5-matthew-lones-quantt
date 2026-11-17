# Original restoration — November 17, 2026

Baseline: [MatthewLones/QUANTTxCOM5 e260722a](https://github.com/MatthewLones/QUANTTxCOM5/tree/e260722ad2adfbc862c963c2e634b70acdb3c5a4). All eight original files were first restored and checked against the recorded upstream SHA-256 hashes.

Only these executable differences remain:

- Cancel-all supplies `all=1`. The API requires a selector; the original request supplied none.
- Pipeline/refinery use requests supply `from1` and `quantity1`, which the API requires, instead of the unsupported `quantity` field. Sources are CL-AK for Alaska-to-Cushing and CL for Cushing-to-NYC/refinery. The template helper and its callers use the same fields.
- The supplied COM5 refinery batch is 30 CL -> 20 RB + 10 HO. The original 3/2/1 is a ratio, not a complete batch. Input checks, request quantity, revenue/cost quantities, gross bookkeeping and template constants now use the full batch. Estimated purchase/sale fees use 60 contracts (30 input plus 30 output).
- The original author's API key is replaced with a blank user setting supporting RIT_API_KEY. RIT_BASE_URL remains configurable with the original localhost default.

API-field evidence: [RIT REST 1.0.4 schema](https://rit.306w.ca/RIT-REST-API/1.0.4/swagger.yaml), `/commands/cancel` and `/leases/{id}`. Batch evidence: the user's supplied COM5 case and live conversion metadata accepted during earlier runs. These are targeted request/batch corrections, not proof that the full physical strategy works.

Removed all custom runtime/execution/shutdown rewrites, inventory and lease polling patches, command-line modes, and tests of those removed features. Restored original README, strategy documentation, scraper and analysis tool. Original behavior, including its remaining limitations, is preserved outside the explicitly listed fixes. No speculative fixes for the reported invalid lease ID or server DELETE failures remain.

Four focused offline tests verify cancellation arguments, pipeline helper arguments, refinery helper batch, and the bot's refinery input gate/request. Python compilation also passes. No live RIT run was performed. Historical commits retain the earlier implementation for comparison; this restoration does not rewrite Git history.
