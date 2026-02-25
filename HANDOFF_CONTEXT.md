# Trading Bot Handoff Context

## Purpose
Quick context file for new agents joining this project, so they can continue without re-reading long chat history.

## Current Stack
- Runtime: Python 3.9
- Broker bridge: `metaapi-cloud-sdk` (macOS-compatible, no native `MetaTrader5` package)
- Platform support via MetaApi: MT5 and MT4 (`mt5.platform` in config).
- UI: Streamlit (`dashboard.py`)
- Core loop: `main_bot.py`
- Broker adapter/utilities: `mt5_utils.py`
- Auto levels: `levels_detector.py`

## High-Level Bot Behavior
- Grid bot with pending orders around an anchor price.
- Grid step uses `grid_step_points` (preferred over `grid_step_pips`).
- Anchor is persisted in `bot_state.json`.
- On restart, anchor restore order:
  1) from `bot_state.json`,
  2) from existing pending orders,
  3) from current market price.
- Dynamic recenter is enabled:
  - if price drift from anchor exceeds threshold, anchor is moved and out-of-window pending can be canceled.

## Key Configuration (current direction)
- `grid.grid_step_points`: 100
- `grid.grid_levels_each_side`: 15
- `grid.dynamic_recenter_enabled`: true
- `grid.recenter_threshold_points`: 150
- `grid.cancel_outside_on_recenter`: true
- `risk.fixed_lot`: 0.05
- Runtime note: bot logic uses `grid_step_points` first, `grid_step_pips` only as fallback.

## Pending Order Logic (latest)
- Bot now tries to place both BUY and SELL pending on each grid level.
- Pending type is chosen automatically:
  - BUY below market -> BUY LIMIT
  - BUY above market -> BUY STOP
  - SELL above market -> SELL LIMIT
  - SELL below market -> SELL STOP
- If an active position already exists on the same level+side, pending is not re-added until that position is closed.

## Closing Logic (latest)
- Auto TP assignment by levels is enabled (`closing.auto_tp_enabled: true`).
- Auto levels are built from D1 fractals + pivots.
- Auto-level spacing is linked to grid step:
  - `min_distance_pips = (grid_step_points * levels.level_spacing_multiplier) / 10`
- TP target level selection:
  - BUY -> nearest level `>= open_price + (tp_min_distance_steps * grid_step_price)`
  - SELL -> nearest level `<= open_price - (tp_min_distance_steps * grid_step_price)`
- Anti-imbalance TP side filter:
  - `imbalance = open_buy_count - open_sell_count`
  - if `imbalance >= tp_imbalance_threshold`, TP is assigned only to BUY positions
  - if `imbalance <= -tp_imbalance_threshold`, TP is assigned only to SELL positions
  - otherwise TP can be assigned to both sides
- Optional hard rebalance mode:
  - when `tp_clear_disallowed_on_imbalance: true`, existing TP is removed from disallowed side during imbalance
- TP sync load guards:
  - sync runs at `tp_sync_interval_sec` cadence (not every bot cycle)
  - max TP modifications per sync is limited by `tp_max_updates_per_cycle`
- Legacy level-based selective close remains in code as optional fallback when auto-TP is disabled.

## UI Features Added
- Start/Stop bot in background thread.
- Open positions table.
- Pending orders table with sorting.
- Equity/balance chart.
- Manual controls:
  - Close All
  - Close Profitable Buys/Sells
  - Reset Grid
- Manual TP tools for open positions:
  - Apply BUY TP level to all open BUY positions
  - Apply SELL TP level to all open SELL positions
- Manual flatten behavior:
  - `Close All` now pauses new grid placements (`stop_new_orders=True`) to avoid immediate re-open.
  - `Close All` closes/cancels all orders for bot symbol regardless of magic/comment and runs two passes.
  - Grid sync loop now re-checks pause flag during placement pass to stop in-flight add-ons faster.
  - `Reset Grid` resumes new order placement (`stop_new_orders=False`) and rebuilds anchor/pending.
- Config editor panel (save + hot reload).
- Recent bot action log panel and raw status payload expander.

## Reliability Fixes Already Applied
- MetaApi `comment + clientId` validation issue resolved by shortening/removing `clientId` usage.
- Magic filtering fallback added using `GRID|...` comments for consistency across MetaApi payloads.
- Added handling for broker RPC timeouts:
  - `BrokerTimeoutError` in `mt5_utils.py`
  - auto reconnect path in `main_bot.py`
- Added more graceful connection shutdown logic to reduce leaked subscriptions.
- Reduced noisy third-party logs in `setup_logging()`.
- Fixed Streamlit auto-refresh via `streamlit-autorefresh`.

## Known Operational Risk
- MetaApi may return 429 `TooManyRequestsException` when subscription quota is exhausted.
- Recovery expectation:
  - stop all running bot instances,
  - wait briefly,
  - run a single instance.
- `config.yaml` currently stores broker/API credentials in plain text. Treat as secret and avoid sharing/committing.

## Current Mismatch / Technical Debt
- No structured close-reason journal yet; runtime events only in text logs.

## Data/Logging Status
- `bot.log` and `bot.log.1` contain runtime events.
- No dedicated structured trade journal yet (reason-per-close not persisted to CSV/database).

## Suggested Next Improvements
1) Add structured `trade_journal.csv` with close reason (`level/imbalance/manual/dd/tp`).
2) Show anchor/drift/recenter diagnostics in UI status.
3) Add stricter anti-duplicate order send guard per cycle.
4) Add optional trend-side TP mode (deferred by user for separate discussion).

