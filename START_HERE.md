# COM5 chart strategy
Run bot.py from this branch in Spyder with your RIT key configured in com5_template.py. Start on a clean account with no other bot. Running starts live simulation orders when the case is ACTIVE. Starting mid-period selects the current leg.

The six photos are overlapping views of CL at ten ticks per candle. The table gives estimated turning ticks counting from zero within each period. The 600 boundary on the combined graph is period 2 tick zero.

| Period | Long from tick | Short from tick | Close all from tick |
|---|---|---|---|
| 1 | 120, 380, 570 | 0, 250, 530 | 588 |
| 2 | 0, 280, 460 | 120, 310 | 585 |

A turn can be off by a candle. The model assumes the CL path repeats and that other crude prices and refined products generally follow CL. Their paths and future bid/ask depth cannot be read from these photos. On rising legs the bot spreads market buys across CL, tradable futures, CL-AK, CL-NYC, HO and RB until live group limits bind. Falling legs close longs and short eligible futures. CL-1F expires after period 1. The limit check uses each security's server group weights. Opposite positions close before entries. Ten-contract entry clips share capacity across names, so price impact, fill latency and lease costs affect realized results.

Spot buys acquire storage. No pipeline/refinery conversions are scheduled because the images do not show the geographic or crack spreads. Ctrl+C or a write error can leave positions or leases open. Review RIT before restarting. The strategy schedules closeout before each period ends.

runs/chart_*/chart.csv records the case tick, target direction, account NLV before the order, request and confirmed fill. A row with an order and zero fills may be the storage preparation step. The offline test sweeps every reversal with synthetic prices and checks exposure, not profitability. Use the RIT performance report for actual P&L.
