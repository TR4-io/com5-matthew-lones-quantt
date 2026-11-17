# Runtime corrections — November 17, 2026

Baseline: MatthewLones/QUANTTxCOM5 e260722ad2adfbc862c963c2e634b70acdb3c5a4. Scheduled exits and the EIA/news/basis/market-making strategy families are retained; the physical lifecycle and main-loop orchestration were rewritten to reconcile real account state.

- Live trading is now the default when running bot.py, as requested. Explicit --check or --dry-run selects read-only mode; --live remains compatible. Added --futures-only and local process lock. API credentials come from environment/local configuration rather than the published author's key.
- Bounded REST timeouts/retries. Uncertain writes halt all engines without blind resubmission.
- Per-security order caps and shortability, weighted portfolio limits, open-order reservations, fresh inventory before decisions, confirmed partial-fill accounting.
- Scheduled exits retain timing and unfilled remainders, and cannot reverse a position. Basis legs match fills and unwind mismatches. Market-making quotes no longer accumulate indefinitely.
- Period-2 EIA/news/basis/MM orders target CL-2F. News entries stop during wind-down. Ctrl+C/error exit attempts cancellation, closeout and explicit reconciliation; unresolved exposure is reported.
- Fixed lease-use fields and conversion batch/economics, tank capacity fields, input/output confirmation and lease lifecycle. No fabricated zero-cost missing assets. Physical jobs preserve tracking across period changes and retain destination tanks until resolved.
- Removed duplicate storage-cost subtraction from the cash-and-carry opportunity calculation.
- Order acknowledgements/results and shutdown records added to events.jsonl. Nonzero exit status on fatal errors or incomplete cleanup.

Sources for request/schema corrections: [official RIT REST 1.0.4 schema](https://rit.306w.ca/RIT-REST-API/1.0.4/swagger.yaml), supplied COM5 case assumptions and the original source. Full physical mode rejects live conversion metadata that does not match those assumptions.

Validation: 39 mocked/offline regression tests; syntax checks; safe --help execution. No live case run or profit claim.

HTTP rejection messages now include the server response body (bounded to 1,000 characters), so a failed conversion reports RIT's reason instead of only HTTP 400. This is a diagnostic correction; the cause of the reported lease-16 rejection is not yet established. A regression test verifies that the reason is preserved and the request is not retried.

## Live-feedback follow-up

A live run reported a conversion input mismatch after an apparently completed purchase. The console alone does not establish the cause. Terminal market-order fills are now reconciled against the expected inventory delta with bounded polling before another order or conversion can proceed. Delayed position updates are tolerated without repeating purchases; persistent discrepancies halt with expected/observed quantities and order ID recorded. Other open orders are cancelled before establishing the market-order baseline. A persistent mismatch prevents a clean-shutdown claim. Duplicate STOP output removed. Two regression tests cover delayed and missing inventory updates.
