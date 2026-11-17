# Running Matthew Lones' COM5 bot

Reviewed November 17, 2026. This repository contains only the eight original files from MatthewLones/QUANTTxCOM5 at commit e260722ad2adfbc862c963c2e634b70acdb3c5a4, plus this guide and provenance metadata. Original code is unchanged. Syntax checks pass; no live run or profit result has been verified here.

## Can I just run it?

Not as a fully validated plug-and-play bot. The Python entrypoint is bot.py, but you must configure your RIT connection, and the physical-asset API calls need fixes or confirmation before using the full strategy. This is more specific than the earlier collection's general compatibility caveat.

## Setup and entrypoint

Keep all project files together on the computer running the RIT Client. Replace API_KEY in com5_template.py with the key from YOUR client. BASE defaults to http://localhost:9999/v1; change it if your client uses another port. The server address in the original template's header is the author's event setup, not an instruction to join that server. Use your course's assigned RIT server and COM5 case. Python API order entry must be enabled for the session.

The only third-party Python dependency is requests:

```powershell
python -m pip install requests
```

Once configured and the compatibility issues below are resolved, the launch command, from this folder, is:

```powershell
python bot.py
```

Run bot.py, not com5_template.py. The template also provides the configuration imported by the bot. There is no implemented --dry-run, --check or --help mode in bot.py; passing those flags does not prevent it from trading. It waits for an ACTIVE case and then runs its strategies automatically. It skips news already present at startup, so start before the first report on a fresh practice account. Do not run another bot or trade manually on the same account during a comparison.

## Findings that matter before a practice run

1. **Physical-asset calls do not match the documented API.** In bot.py, the pipeline calls around lines 1194 and 1234 and the refinery call around line 1311 submit only a quantity field to POST /leases/{id}. The official RIT REST 1.0.4 schema requires from1 and quantity1. These paths can fail after a lease is created or crude is purchased. The refinery also assumes a three-contract input; confirm the actual case batch and conversion quantities before changing it. Source: https://rit.306w.ca/RIT-REST-API/1.0.4/swagger.yaml (leases/{id} POST). Fix these calls and confirm units or explicitly disable the physical engines before attempting a full automated run; this original snapshot does neither for you.

2. **Ctrl+C does not flatten the account.** The KeyboardInterrupt handler near the end of main() logs a stop and writes a summary. It does not cancel orders, close positions or release leases. Its scheduled end-of-period wind-down is a separate mechanism. After stopping, inspect and manage the account in RIT; do not assume it is flat.

3. **It is aggressive and runs multiple strategies.** EIA, keyword news, futures basis, pipelines, refining and market making are wired into main(). EIA's initial minimum-impact threshold is zero, and its gross allocation can be large; parameters are subsequently retuned. Starting the program is authorization for all enabled engines, not just EIA trading.

4. **Automatic discovery is incomplete.** Despite the header's 'zero hardcoded values' claim, timing, thresholds, EIA impact assumptions and physical conversion logic contain fixed defaults. Limits are represented using a simplified per-ticker scheme. Check the discovered configuration and compare it with your course case; discovery does not prove rule compatibility.

5. **Execution is not fully reconciled.** Requests have no explicit timeout. Split-order placement decrements the requested remaining quantity even if an order returns no result. The main loop handles news before refreshing that tick's securities snapshot, and multiple engines act between refreshes. These are reasons to monitor rejected orders, partial fills and actual account exposure, not evidence of guaranteed losses.

6. **The auxiliary scraper can also act on the account.** data_scraper.py includes an API stress path that cancels orders. Do not launch it blindly alongside the bot. analyze_data.py is an analysis tool, not the trading entrypoint.

## What gets recorded

The bot creates runs/<run_id>/ with bot.log, events.jsonl, nlv.csv, config.json and (after normal shutdown) summary.json. Keep that entire run folder and the official RIT performance report. A crash or interrupt during startup can prevent the usual summary. The author's competition placement is an upstream claim, not an independently verified profit expectation for your class.

## What was changed here

No original algorithm, parameter, credential placeholder or README was changed. UPSTREAM.json records all eight original file hashes and their source URLs. The purpose is to preserve a clean comparison baseline; fixes should be explicitly recorded before testing. Do not commit your personal API key after configuring your local copy.
