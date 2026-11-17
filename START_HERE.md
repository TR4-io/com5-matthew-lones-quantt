# Restored original bot

Restored November 17, 2026 to MatthewLones/QUANTTxCOM5 commit e260722ad2adfbc862c963c2e634b70acdb3c5a4, with the initial request/batch corrections and physical-asset fixes listed in CHANGES.md. README.md and STRATEGY.md are the original author's documents, including their original claims; they are not validation of this copy.

Install the dependency with `python -m pip install -r requirements.txt`. Set your own API_KEY in com5_template.py, or set RIT_API_KEY in your environment. RIT_BASE_URL defaults to http://localhost:9999/v1.

Run `python bot.py`, or press Run in Spyder. The original bot waits for ACTIVE and trades automatically. The added --check, --live, --futures-only and --dry-run modes have been removed. Do not rely on those flags to disable trading.

The original EIA/news/basis/market-making logic, order execution, scheduled exits, tuning, logging and stopping behavior are restored. Only the transport/refinery lifecycle and related storage cleanup have received the latest fixes. Pipelines submit their input with the initial request; refinery leases are tracked before use and retained until release is confirmed. Completion checks sell delivered goods and release empty storage. A failed lease release does not block selling delivered output. In particular, Ctrl+C stops the original loop; it does not perform the custom shutdown liquidation that was added in the rewritten version. Inspect positions, orders and leases in RIT after stopping. Start a fresh practice session rather than carrying the previous failed run's positions into this test.

Use a freshly extracted download so that older modules or tests are not mixed in. Copy your API key into the restored template rather than overwriting the entire template with an older copy.

Validation: 13 focused offline tests and Python compilation. No live RIT validation or profitability claim. The broader runtime tests were removed because they tested the reverted implementation.

Physical batches are processed one at a time so delivery and cleanup remain attributable. AK-to-Cushing obtains its missing input and reserves storage at both ends. Cushing-to-NYC requires 10 CL already held; refining requires 30 CL already held and the original profitability condition. Unknown physical creation/use results are logged and are not blindly resubmitted. If such a result cannot be reconciled, inspect RIT before restarting. No new automatic account-wide liquidation is added.
