# Matthew Lones COM5 bot — corrected practice version

Derived from [MatthewLones/QUANTTxCOM5](https://github.com/MatthewLones/QUANTTxCOM5/tree/e260722ad2adfbc862c963c2e634b70acdb3c5a4). This version fixes execution, physical conversion and shutdown bugs. **Scheduled EIA/news exits are preserved.** See [what changed](CHANGES.md) and [the detailed run guide](START_HERE.md).

## Run on your RIT computer

Use Python 3.10+ on the computer running the RIT Client. From this project folder:

```powershell
python -m pip install -r requirements.txt
$env:RIT_API_KEY = "YOUR_RIT_CLIENT_KEY"
python bot.py --check
python bot.py --live
```

The check is read-only. The live command places simulated orders and leases assets. Use your course's assigned server and COM5 case; API Orders must be enabled by the administrator. A check cannot verify order permission. If needed, set `RIT_BASE_URL` to your client's API address (default `http://localhost:9999/v1`). On macOS/Linux, use `export RIT_API_KEY="YOUR_RIT_CLIENT_KEY"`.

To test only the futures EIA/news/market-making components:

```powershell
python bot.py --check --futures-only
python bot.py --live --futures-only
```

`--futures-only` disables spot/product purchases, cash-and-carry, pipelines and refining. Full mode validates the supplied COM5 physical conversion quantities against live asset metadata; it refuses an incompatible setup instead of guessing units.

Running `python bot.py` without flags performs a one-time read-only check. `--dry-run` is an alias for that check, not a fill simulator. `--help` does not connect to RIT.

## Exits and stopping

Start before the first EIA release, with no positions, orders or leases, and run only one bot on the account. Earlier news is deliberately skipped at startup. A local `.bot.lock` prevents duplicate instances from this folder; it cannot detect another bot running elsewhere.

Scheduled EIA/news exits still run while the bot runs, and end-of-period wind-down still runs. The first Ctrl+C now requests additional cleanup: cancel open orders, reconcile positions, attempt closeout and retain storage needed by unfinished conversions. A second Ctrl+C aborts cleanup. The program reports residual positions, orders, leases and uncertain writes; failed API access or an inactive case can prevent full cleanup. Inspect RIT whenever shutdown does not report `flat: true`.

The cleanup loop allows about 60 seconds plus any bounded requests already in progress. Do not repeatedly restart it with residual exposure. After an abrupt process kill, remove `.bot.lock` only after confirming that the old process is gone and reviewing the account.

## Validation and results

```powershell
python -m unittest discover -s tests -v
```

37 offline tests pass, including partial fills, portfolio limits, scheduled exit timing, physical conversion and Ctrl+C lifecycle checks. Tests use mocked REST responses; the code has not been live-tested against your RIT session and profitability is unmeasured.

Each live run records `runs/<run_id>/`: `config.json`, `events.jsonl` (including order acknowledgements/results and shutdown reconciliation), `nlv.csv`, `bot.log`, and `summary.json` after an orderly exit. Keep the official RIT report alongside these files. Use final server/official P&L, including lease costs and fines.

[UPSTREAM_README.md](UPSTREAM_README.md) and STRATEGY.md describe the original strategy; this README and START_HERE.md describe the corrected runtime. UPSTREAM.json records the original commit and file hashes, not hashes of this modified version. Original authorship is retained; no profitability claims from the original README have been independently verified.
