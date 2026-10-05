# Bitunix alert bots

Alerts only. Nothing here places an order.

## Processes

Procfile starts the spike/drop bot, websocket watch, wick scan, setup bot, daily technical watch, and the paper trader.

## Paper trades

When a bot emits a signal it appends a row to data/signals.jsonl. paper_trader.py follows the price and sends Telegram when the hypothetical trade hits its stop, hits its target, or times out after 6 hours. A scoreboard is written to data/dashboard.txt and posted every 6 hours.

Default risk on a paper trade is a 1.2% stop and a 2.0% target. That is a tracking rule, not a recommendation to size a live position.

## Other pieces

- pipeline.py drops a second alert on the same coin and side inside 90 seconds.
- thresholds.py raises or lowers the late-entry bar from timed-out paper trades. Until there are 8 samples it stays at 0.30%.
- bot_common.py is the shared Telegram send, file log, and signal writer.
- Wick scans run in a small thread pool instead of one symbol at a time.

Logs go to data/bot.log. The data directory is local runtime state and is not committed.
