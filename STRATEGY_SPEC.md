# Strategy Specification (Current Live Logic)

## Purpose
This file describes the exact trading logic currently used by the live bot, in plain language and pseudocode form.  
Use this document as the single source of truth when replicating the strategy in a new backtesting/optimization project.

## Scope
- Symbol-level grid strategy with both BUY and SELL pending orders.
- Dynamic anchor and optional recentering.
- Auto-level generation (D1 fractals + pivots) for TP logic.
- Risk guard, imbalance handling, and manual controls.

## Runtime Model
- Main loop runs every `trading.poll_interval_sec`.
- Core sequence per cycle:
  1) Ensure broker connection
  2) Risk guard
  3) Refresh auto levels (hourly)
  4) Track/restore positions-to-pending mapping
  5) Auto-TP synchronization
  6) Imbalance close priority
  7) Optional legacy level-based close (only when auto-TP is disabled)
  8) Grid sync (place missing pending orders) unless new orders are paused
  9) Append equity point

## Core State
- `grid_anchor_price`: active anchor for grid generation.
- `position_registry`: map of open position ticket -> `(side, level)`.
- `stop_new_orders`: hard pause for new pending placement.
- `auto_levels`: merged list of detected support/resistance levels.
- `equity_curve`: recent balance/equity samples.

## Config-Driven Behavior

### Grid
- `grid.grid_step_points` is primary step input.
- `grid.grid_step_pips` is fallback only if points is invalid/non-positive.
- `grid.grid_levels_each_side` defines number of levels above and below anchor.
- `grid.dynamic_recenter_enabled`: recenter when drift threshold is exceeded.
- `grid.recenter_threshold_points`: threshold for anchor drift.
- `grid.cancel_outside_on_recenter`: cancel out-of-window pending after recenter.

### Levels / TP
- `levels.auto_levels_enabled`: use auto-detected levels from D1 data.
- `levels.auto_levels_lookback_days`: history window for detection.
- `levels.level_spacing_multiplier`: minimum spacing between levels (scaled by grid step).
- `closing.auto_tp_enabled`: enables auto TP assignment engine.
- `closing.tp_min_distance_steps`: TP level must be at least N grid steps from entry.
- `closing.tp_imbalance_threshold`: side filter threshold for TP assignment.
- `closing.tp_clear_disallowed_on_imbalance`: remove TP on disallowed side.
- `closing.tp_sync_interval_sec`: minimum interval between TP sync passes.
- `closing.tp_max_updates_per_cycle`: cap on TP changes per sync pass.

### Risk / Close
- `risk.fixed_lot` or balance-based lot mode.
- Drawdown guard from `risk.max_drawdown_pct` and `risk.min_equity_guard`.
- Optional full liquidation on DD breach via `risk.close_all_on_dd`.
- Legacy close controls:
  - `closing.imbalance_threshold`
  - `closing.max_closes_per_cycle`
  - `closing.level_break_buffer_multiplier`

## Anchor and Grid Construction

### Anchor Initialization
Anchor source priority:
1) persisted state (`bot_state.json`),
2) reconstructed from existing pending orders,
3) current market mid rounded to nearest grid step.

### Grid Levels
For each `i in [1..grid_levels_each_side]`:
- `anchor + i * step`
- `anchor - i * step`
Deduplicated and sorted.

### Recenter Rule
If enabled and `abs(mid - anchor) >= recenter_threshold`:
- set new anchor from current mid,
- optionally cancel pending orders outside new active window.

## Pending Order Placement Logic
- Bot attempts both BUY and SELL pending at each grid level.
- Pending type selection is relative to current price:
  - BUY below market -> BUY LIMIT
  - BUY above market -> BUY STOP
  - SELL above market -> SELL LIMIT
  - SELL below market -> SELL STOP
- Pending is skipped if:
  - matching pending already exists near that level, or
  - active position exists on same side near that level.

## Position Tracking and Refill
- Open positions are tracked by `GRID|SIDE|LEVEL` comment when available.
- If comment is missing, level is inferred by rounding position open price to grid step.
- When a tracked position closes:
  - if `stop_new_orders` is false, bot restores corresponding pending order on that level+side.

## Auto TP Logic (Current Primary Exit Mode)
- Build level universe = `manual levels + auto levels`.
- For each open position:
  - BUY: choose nearest level `>= entry + min_distance`
  - SELL: choose nearest level `<= entry - min_distance`
- TP update happens only if target differs from existing TP by more than tolerance.

### Anti-Imbalance TP Side Filter
- `imbalance = open_buy_count - open_sell_count`
- If `imbalance >= threshold`: allow TP updates only for BUY.
- If `imbalance <= -threshold`: allow TP updates only for SELL.
- Otherwise allow both sides.
- Optional hard mode: clear existing TP for disallowed side.

## Legacy Close Logic (Fallback)
Only active when `closing.auto_tp_enabled = false`.
- `imbalance_close_priority`: closes profitable positions on overloaded side.
- `level_based_close`: closes profitable positions after level breakout with buffer.

## Manual Controls Semantics

### Close All
- Immediately sets `stop_new_orders = true`.
- Closes all positions for current symbol (ignores magic/comment filter).
- Cancels all pending for current symbol (ignores magic/comment filter).
- Executes two passes with short delay to catch in-flight updates.
- Clears position registry.

### Reset Grid
- Cancels bot-filtered pending orders.
- Clears anchor and persists state.
- Resumes new order placement (`stop_new_orders = false`).

## Connection/Resilience Notes
- Reconnect attempts are serialized to prevent parallel reconnect storms.
- Stale RPC/websocket objects are closed before reconnect.
- UI passive refresh does not trigger broker pulls when bot is stopped.

## Pseudocode Snapshot
```text
loop every poll_interval:
  ensure_connection()
  risk_guard()
  refresh_auto_levels_if_due()
  track_positions_and_restore_pending()
  auto_assign_take_profits()
  imbalance_close_priority()
  if not auto_tp_enabled:
    level_based_close()
  if not stop_new_orders:
    sync_grid()
  append_equity_point()
```

## What To Reuse In Backtester
- Grid step/anchor/recenter math.
- Side+level dedup rules for pending placement.
- Position registry refill semantics.
- Auto TP level selection and imbalance filter.
- Manual control semantics (`Close All`, `Reset Grid`) as scenario actions.
