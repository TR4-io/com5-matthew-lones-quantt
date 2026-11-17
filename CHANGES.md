# Original restoration — November 17, 2026

Baseline: [MatthewLones/QUANTTxCOM5 e260722a](https://github.com/MatthewLones/QUANTTxCOM5/tree/e260722ad2adfbc862c963c2e634b70acdb3c5a4). All eight original files were first restored and checked against the recorded upstream SHA-256 hashes.

The restoration retained these initial executable differences (physical-only follow-up below):

- Cancel-all supplies `all=1`. The API requires a selector; the original request supplied none.
- Pipeline/refinery use requests supply `from1` and `quantity1`, which the API requires, instead of the unsupported `quantity` field. Sources are CL-AK for Alaska-to-Cushing and CL for Cushing-to-NYC/refinery. The template helper and its callers use the same fields.
- The supplied COM5 refinery batch is 30 CL -> 20 RB + 10 HO. The original 3/2/1 is a ratio, not a complete batch. Input checks, request quantity, revenue/cost quantities, gross bookkeeping and template constants now use the full batch. Estimated purchase/sale fees use 60 contracts (30 input plus 30 output).
- The original author's API key is replaced with a blank user setting supporting RIT_API_KEY. RIT_BASE_URL remains configurable with the original localhost default.

API-field evidence: [RIT REST 1.0.4 schema](https://rit.306w.ca/RIT-REST-API/1.0.4/swagger.yaml), `/commands/cancel` and `/leases/{id}`. Batch evidence: the user's supplied COM5 case and live conversion metadata accepted during earlier runs. These are targeted request/batch corrections, not proof that the full physical strategy works.

Removed all custom runtime/execution/shutdown rewrites, inventory and lease polling patches, command-line modes, and tests of those removed features. Restored original README, strategy documentation, scraper and analysis tool. Original behavior, including its remaining limitations, is preserved outside the explicitly listed fixes. No speculative fixes for the reported invalid lease ID or server DELETE failures remain.

Four focused offline tests verify cancellation arguments, pipeline helper arguments, refinery helper batch, and the bot's refinery input gate/request. Python compilation also passes. No live RIT run was performed. Historical commits retain the earlier implementation for comparison; this restoration does not rewrite Git history.

## Physical-assets-only follow-up — November 17, 2026

The broader EIA/limit/fill changes in e735ef4 were explicitly rejected by the user and reverted in 64a3913. They are absent from this version. AST comparison with the restored baseline confirms unchanged CaseConfig, ExecutionEngine, EIAEngine, NewsEngine, BasisEngine, MarketMaker, RiskManager, Orchestrator, RunLogger, HTTP helpers and main loop.

Root causes addressed in the physical paths:

- Pipeline creation previously sent only a ticker, followed by a separate use request. It now sends ticker, from1 and quantity1 together, after the input is present. The same COM5 one-step pattern is present in [sparshgup transport.py](https://github.com/sparshgup/AlgorithmicTrading_RIT/blob/1a70e5c5d3fee719907ac224623a87905fe66b87/Commodities/transport.py) and angelmvhill's Oil_Pipeline_Arbitrage.py. The documented /leases endpoint accepts conversion inputs. This is strong evidence for the request correction; the exact cause of the user's server 500 still requires live confirmation.
- AK-to-Cushing now reserves both origin and destination storage and checks input inventory before starting. Cushing-to-NYC requires a full 10-contract batch. No paid pipeline is created when the input purchase fails.
- Refinery creation is tracked before use. Its ID is reconciled to a unique newly active refinery lease; a creation-response ID is not blindly treated as usable. A definitively rejected use releases the idle lease. Ambiguous use waits for server conversion metadata instead of resubmitting.
- Completion keeps delivery/sale/release state separate. Goods are sold only after the configured conversion time, any later server finish time, and sufficient output inventory. A delayed sale inventory update does not trigger another sell. A failed lease release retains tracking and does not block the output sale. Auto-expired pipelines are recognized without an unnecessary DELETE.
- Empty storage release uses fresh server containment_usage and preserves capacity for incoming or unresolved conversions. Physical tracking survives period changes. Physical batches run one at a time; unrelated strategy engines are unchanged.

The original stopping behavior remains: Ctrl+C does not automatically liquidate the account. Unknown physical creation results may still require manual reconciliation; these fixes do not promise cleanup if the server cannot confirm what happened.

Validation: 13 offline tests covering direct pipeline parameters, endpoint storage, failed input purchase, delivery arrival, automatic pipeline expiry, actual refinery-ID selection, rejected use cleanup, failed release retry/tracking, delayed sale snapshots, full-batch gating and period-boundary retention; Python compilation. No live RIT execution performed.
