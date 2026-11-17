# Restored original bot

Restored November 17, 2026 to MatthewLones/QUANTTxCOM5 commit e260722ad2adfbc862c963c2e634b70acdb3c5a4, with the request/batch corrections and console-output fixes listed in CHANGES.md. README.md and STRATEGY.md are the original author's documents, including their original claims; they are not validation of this copy.

Install the dependency with `python -m pip install -r requirements.txt`. Set your own API_KEY in com5_template.py, or set RIT_API_KEY in your environment. RIT_BASE_URL defaults to http://localhost:9999/v1.

Run `python bot.py`, or press Run in Spyder. The original bot waits for ACTIVE and trades automatically. The added --check, --live, --futures-only and --dry-run modes have been removed. Do not rely on those flags to disable trading.

The original strategy signals, exit timings, tuning and physical conversion lifecycle are retained. Execution now enforces live shared limits, counts confirmed market fills, and prevents scheduled exits from reversing a position. A server error or timeout on a write stops the bot because its outcome is unknown; it is not retried. The server error body and read-only account snapshots are saved to runs/<run_id>/failure.json. In particular, Ctrl+C stops the original loop; it does not perform the custom shutdown liquidation that was added in the rewritten version. Inspect positions, orders and leases in RIT after stopping. Start a fresh practice session rather than carrying the previous failed run's positions into this test.

Use a freshly extracted download so that older modules or tests are not mixed in. Copy your API key into the restored template rather than overwriting the entire template with an older copy.

Validation: 19 focused offline tests and Python compilation. Tests reproduce the EIA over-request, partial/rejected fills, scheduled-exit reversal, unnecessary storage requests and an uncertain pipeline write stopping the main loop. No live RIT validation or profitability claim. The broader tests of the previously reverted runtime remain removed.

Download all updated files, including execution_checks.py. Running only a new bot.py beside the old files will not work. If a pipeline still returns HTTP 500, send the new failure.json: the console alone does not identify the server-side cause. This version checks asset availability but does not claim to repair the server.
