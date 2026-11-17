# Restored original bot

Restored November 17, 2026 to MatthewLones/QUANTTxCOM5 commit e260722ad2adfbc862c963c2e634b70acdb3c5a4, with only the small corrections listed in CHANGES.md. README.md and STRATEGY.md are the original author's documents, including their original claims; they are not validation of this copy.

Install the dependency with `python -m pip install -r requirements.txt`. Set your own API_KEY in com5_template.py, or set RIT_API_KEY in your environment. RIT_BASE_URL defaults to http://localhost:9999/v1.

Run `python bot.py`, or press Run in Spyder. The original bot waits for ACTIVE and trades automatically. The added --check, --live, --futures-only and --dry-run modes have been removed. Do not rely on those flags to disable trading.

The original scheduled exits, strategy selection, tuning, logging, lease lifecycle and stopping behavior are restored. In particular, Ctrl+C stops the original loop; it does not perform the custom shutdown liquidation that was added in the rewritten version. Inspect positions, orders and leases in RIT after stopping. Start a fresh practice session rather than carrying the previous failed run's positions into this test.

Use a freshly extracted download so that older modules or tests are not mixed in. Copy your API key into the restored template rather than overwriting the entire template with an older copy.

Validation: four focused offline tests and Python compilation. No live RIT validation or profitability claim. The broader runtime tests were removed because they tested the reverted implementation.
