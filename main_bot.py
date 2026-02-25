"""Core hedging grid bot for MetaTrader 5."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import yaml

from levels_detector import detect_levels
from mt5_utils import (
    BrokerTimeoutError,
    cancel_all_pending,
    cancel_all_pending_any,
    cancel_order,
    clear_position_take_profit,
    close_all_positions_any,
    close_all_positions,
    close_position,
    connect_mt5,
    count_closed_deals_by_side,
    ensure_connection,
    get_account_info,
    get_pending_orders,
    get_positions,
    get_tick,
    is_connected,
    normalize_price,
    pips_to_price,
    place_pending_order,
    profitable_positions,
    set_position_take_profit,
    shutdown_mt5,
)


LOGGER = logging.getLogger("hedging_grid_bot")
CONFIG_PATH_DEFAULT = Path("config.yaml")


@dataclass
class BotStatus:
    """Snapshot-friendly runtime status model."""

    running: bool
    connected: bool
    symbol: str
    current_imbalance: float
    net_exposure: float
    auto_levels_count: int
    stop_new_orders: bool
    initial_balance: float
    latest_balance: float
    latest_equity: float
    last_action: str


def setup_logging(log_file: str, level_name: str = "INFO") -> None:
    """Configure console + rotating file logging."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    file_handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5)
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Keep third-party transport logs quiet so bot actions stay readable.
    noisy_loggers = [
        "socketio",
        "engineio",
        "aiohttp",
        "urllib3",
        "websockets",
    ]
    for name in noisy_loggers:
        ext_logger = logging.getLogger(name)
        ext_logger.setLevel(logging.WARNING)


def load_config(config_path: str | Path) -> dict[str, Any]:
    """Load YAML config from disk."""
    path = Path(config_path)
    with path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def save_config(config_path: str | Path, config: dict[str, Any]) -> None:
    """Persist YAML config to disk."""
    path = Path(config_path)
    with path.open("w", encoding="utf-8") as file:
        yaml.safe_dump(config, file, sort_keys=False, allow_unicode=False)


def _parse_grid_comment(comment: str) -> tuple[str, float] | None:
    """Parse comment format GRID|SIDE|PRICE."""
    if not comment.startswith("GRID|"):
        return None
    chunks = comment.split("|")
    if len(chunks) < 3:
        return None
    side = chunks[1].upper()
    try:
        level = float(chunks[2])
    except ValueError:
        return None
    if side not in {"BUY", "SELL"}:
        return None
    return side, level


class HedgingGridBot:
    """Stateful bot engine with async loop and UI-friendly methods."""

    def __init__(self, config_path: str | Path = CONFIG_PATH_DEFAULT) -> None:
        self.config_path = Path(config_path)
        self.config: dict[str, Any] = load_config(self.config_path)

        log_cfg = self.config.get("logging", {})
        setup_logging(log_cfg.get("file", "bot.log"), log_cfg.get("level", "INFO"))

        self.stop_event = threading.Event()
        self.running = False
        self.stop_new_orders = False

        self.position_registry: dict[int, tuple[str, float]] = {}
        self.auto_levels: list[float] = []
        self.latest_auto_levels_at: datetime | None = None
        self.current_imbalance: float = 0.0
        self.net_exposure: float = 0.0
        self.initial_balance: float = 0.0
        self.grid_anchor_price: float | None = None
        self.state_file = Path(self.config.get("trading", {}).get("state_file", "bot_state.json"))
        if not self.state_file.is_absolute():
            self.state_file = self.config_path.parent / self.state_file

        self.equity_curve: deque[dict[str, float | str]] = deque(maxlen=5000)
        self.action_logs: deque[str] = deque(maxlen=400)
        self._last_action = "Idle"
        self._last_tp_sync_at: datetime | None = None
        self._load_state()

    @property
    def symbol(self) -> str:
        return str(self.config.get("trading", {}).get("symbol", "EURUSD"))

    @property
    def magic(self) -> int:
        return int(self.config.get("trading", {}).get("magic", 900001))

    @property
    def deviation(self) -> int:
        return int(self.config.get("trading", {}).get("deviation", 20))

    @property
    def poll_interval(self) -> float:
        return float(self.config.get("trading", {}).get("poll_interval_sec", 5))

    def reload_config(self) -> None:
        """Reload config at runtime."""
        self.config = load_config(self.config_path)
        self.state_file = Path(self.config.get("trading", {}).get("state_file", "bot_state.json"))
        if not self.state_file.is_absolute():
            self.state_file = self.config_path.parent / self.state_file
        self._load_state()
        self._record_action("Config reloaded")

    def _record_action(self, message: str, level: int = logging.INFO) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{timestamp} | {message}"
        self.action_logs.appendleft(line)
        self._last_action = message
        LOGGER.log(level, message)

    def _load_state(self) -> None:
        """Load persisted bot state from disk (if exists)."""
        if not self.state_file.exists():
            return
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
            anchor = payload.get("grid_anchor_price")
            if anchor is not None:
                self.grid_anchor_price = normalize_price(self.symbol, float(anchor))
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to load state file %s: %s", self.state_file, exc)

    def _save_state(self) -> None:
        """Persist runtime state needed for stable restart behavior."""
        try:
            payload = {
                "grid_anchor_price": self.grid_anchor_price,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "symbol": self.symbol,
            }
            self.state_file.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to save state file %s: %s", self.state_file, exc)

    def _calc_lot(self, balance: float) -> float:
        risk_cfg = self.config.get("risk", {})
        mode = str(risk_cfg.get("lot_mode", "fixed")).lower()
        if mode == "balance_pct":
            pct = float(risk_cfg.get("balance_pct", 0.25))
            # Lightweight approximation: 0.01 lot per 1k account scaled by pct.
            lots = (balance / 1000.0) * (pct / 100.0) * 0.01 * 100
        else:
            lots = float(risk_cfg.get("fixed_lot", 0.01))
        return max(lots, 0.01)

    def _grid_step_price(self) -> float:
        """Resolve grid step from config (points preferred, pips fallback)."""
        grid_cfg = self.config.get("grid", {})
        step_points = float(grid_cfg.get("grid_step_points", 0))
        if step_points > 0:
            # 10 points = 1 pip for most FX symbols with fractional pricing.
            return pips_to_price(self.symbol, step_points / 10.0)
        step_pips = float(grid_cfg.get("grid_step_pips", 20))
        return pips_to_price(self.symbol, step_pips)

    def _anchor_from_price(self, raw_price: float) -> float:
        """Round start price to nearest grid step to get fixed anchor."""
        step = self._grid_step_price()
        if step <= 0:
            return normalize_price(self.symbol, raw_price)
        anchored = round(raw_price / step) * step
        return normalize_price(self.symbol, anchored)

    def _grid_levels(self, anchor_price: float) -> list[float]:
        grid_cfg = self.config.get("grid", {})
        levels_each_side = int(grid_cfg.get("grid_levels_each_side", 6))
        step = self._grid_step_price()

        levels: list[float] = []
        for i in range(1, levels_each_side + 1):
            levels.append(normalize_price(self.symbol, anchor_price + i * step))
            levels.append(normalize_price(self.symbol, anchor_price - i * step))
        return sorted(set(levels))

    def _recenter_threshold_price(self) -> float:
        """Convert recenter threshold from points to price delta."""
        grid_cfg = self.config.get("grid", {})
        threshold_points = float(grid_cfg.get("recenter_threshold_points", 150))
        return pips_to_price(self.symbol, threshold_points / 10.0)

    def _prune_pending_outside_active_window(self) -> int:
        """Cancel bot pending orders that are outside current anchor window."""
        if self.grid_anchor_price is None:
            return 0
        active_levels = self._grid_levels(self.grid_anchor_price)
        tolerance = pips_to_price(self.symbol, 0.5)
        cancelled = 0
        for order in get_pending_orders(symbol=self.symbol, magic=self.magic):
            in_window = any(abs(order.price_open - lvl) <= tolerance for lvl in active_levels)
            if in_window:
                continue
            if cancel_order(order.ticket):
                cancelled += 1
        return cancelled

    def _maybe_recenter_grid(self, mid_price: float) -> None:
        """Shift anchor when price drifts far away from current base."""
        if self.grid_anchor_price is None:
            return
        grid_cfg = self.config.get("grid", {})
        if not bool(grid_cfg.get("dynamic_recenter_enabled", True)):
            return

        threshold = self._recenter_threshold_price()
        if threshold <= 0:
            return
        drift = abs(mid_price - self.grid_anchor_price)
        if drift < threshold:
            return

        old_anchor = self.grid_anchor_price
        self.grid_anchor_price = self._anchor_from_price(mid_price)
        cancelled = 0
        if bool(grid_cfg.get("cancel_outside_on_recenter", True)):
            cancelled = self._prune_pending_outside_active_window()
        self._save_state()
        self._record_action(
            f"Grid recentered {old_anchor:.5f} -> {self.grid_anchor_price:.5f}, "
            f"drift={drift:.5f}, cancelled_outside={cancelled}"
        )

    def _restore_anchor_from_pending(self) -> float | None:
        """Reconstruct grid anchor from already-existing pending orders."""
        step = self._grid_step_price()
        if step <= 0:
            return None
        pending = get_pending_orders(symbol=self.symbol, magic=self.magic)
        if not pending:
            return None
        sell_levels = sorted(o.price_open for o in pending if o.side == "SELL")
        buy_levels = sorted(o.price_open for o in pending if o.side == "BUY")
        if sell_levels:
            return normalize_price(self.symbol, sell_levels[0])
        if buy_levels:
            return normalize_price(self.symbol, buy_levels[-1] + step)
        return None

    def _pending_exists(self, side: str, level: float, tolerance: float) -> bool:
        for order in get_pending_orders(symbol=self.symbol, magic=self.magic):
            if order.side != side:
                continue
            if abs(order.price_open - level) <= tolerance:
                return True
        return False

    def _active_position_exists(self, side: str, level: float, tolerance: float) -> bool:
        """Check whether an active position already occupies this grid level."""
        for pos in get_positions(symbol=self.symbol, magic=self.magic):
            if pos.side != side:
                continue
            if abs(pos.price_open - level) <= tolerance:
                return True
        return False

    def _track_positions(self) -> None:
        current = get_positions(symbol=self.symbol, magic=self.magic)
        active_tickets = {p.ticket for p in current}

        for p in current:
            parsed = _parse_grid_comment(p.comment)
            if parsed:
                self.position_registry[p.ticket] = parsed
                continue

            step = self._grid_step_price()
            inferred = normalize_price(self.symbol, round(p.price_open / step) * step)
            self.position_registry[p.ticket] = (p.side, inferred)

        closed_tickets = [ticket for ticket in list(self.position_registry.keys()) if ticket not in active_tickets]
        for ticket in closed_tickets:
            side, level = self.position_registry.pop(ticket)
            if self.stop_new_orders:
                continue
            tick = get_tick(self.symbol)
            if tick is None:
                continue
            comment = f"GRID|{side}|{level:.5f}"
            ok = place_pending_order(
                symbol=self.symbol,
                side=side,
                volume=self._calc_lot(balance=max(self.initial_balance, 100.0)),
                price=level,
                magic=self.magic,
                deviation=self.deviation,
                comment=comment,
                current_bid=float(tick.bid),
                current_ask=float(tick.ask),
            )
            if ok:
                self._record_action(f"Restored pending {side} @ {level:.5f} after position close")

    def _sync_grid(self) -> None:
        if self.stop_new_orders:
            return
        tick = get_tick(self.symbol)
        if tick is None:
            return
        mid_price = (float(tick.bid) + float(tick.ask)) / 2.0
        if self.grid_anchor_price is None:
            restored_anchor = self._restore_anchor_from_pending()
            if restored_anchor is not None:
                self.grid_anchor_price = restored_anchor
                self._record_action(f"Grid anchor restored from existing pending @ {self.grid_anchor_price:.5f}")
            else:
                self.grid_anchor_price = self._anchor_from_price(mid_price)
                self._record_action(
                    f"Grid anchor initialized at {self.grid_anchor_price:.5f} (mid {mid_price:.5f})"
                )
            self._save_state()
        else:
            self._maybe_recenter_grid(mid_price)
        levels = self._grid_levels(self.grid_anchor_price)
        account = get_account_info()
        if account is None:
            return

        lot = self._calc_lot(balance=account.balance)
        tolerance = pips_to_price(self.symbol, 0.5)
        current_bid = float(tick.bid)
        current_ask = float(tick.ask)
        for level in levels:
            if self.stop_new_orders:
                break
            if not self._active_position_exists("BUY", level, tolerance=tolerance):
                if not self._pending_exists("BUY", level, tolerance=tolerance):
                    comment = f"GRID|BUY|{level:.5f}"
                    if place_pending_order(
                        symbol=self.symbol,
                        side="BUY",
                        volume=lot,
                        price=level,
                        magic=self.magic,
                        deviation=self.deviation,
                        comment=comment,
                        current_bid=current_bid,
                        current_ask=current_ask,
                    ):
                        self._record_action(f"Placed BUY pending @ {level:.5f}")
            if self.stop_new_orders:
                break
            if not self._active_position_exists("SELL", level, tolerance=tolerance):
                if not self._pending_exists("SELL", level, tolerance=tolerance):
                    comment = f"GRID|SELL|{level:.5f}"
                    if place_pending_order(
                        symbol=self.symbol,
                        side="SELL",
                        volume=lot,
                        price=level,
                        magic=self.magic,
                        deviation=self.deviation,
                        comment=comment,
                        current_bid=current_bid,
                        current_ask=current_ask,
                    ):
                        self._record_action(f"Placed SELL pending @ {level:.5f}")

    def _refresh_auto_levels(self) -> None:
        enabled = bool(self.config.get("levels", {}).get("auto_levels_enabled", True))
        if not enabled:
            self.auto_levels = []
            return
        now = datetime.now(timezone.utc)
        if self.latest_auto_levels_at and (now - self.latest_auto_levels_at).total_seconds() < 3600:
            return
        level_cfg = self.config.get("levels", {})
        grid_cfg = self.config.get("grid", {})
        step_points = float(grid_cfg.get("grid_step_points", 0))
        spacing_multiplier = float(level_cfg.get("level_spacing_multiplier", 3.0))
        if step_points > 0:
            min_distance_pips = (step_points * spacing_multiplier) / 10.0
        else:
            min_distance_pips = float(grid_cfg.get("min_level_distance_pips", 8))
        result = detect_levels(
            symbol=self.symbol,
            lookback_days=int(level_cfg.get("auto_levels_lookback_days", 90)),
            min_distance_pips=min_distance_pips,
        )
        self.auto_levels = result.levels
        self.latest_auto_levels_at = now
        self._record_action(f"Auto-levels refreshed: {len(self.auto_levels)} levels")

    def _level_based_close(self, max_count: int) -> int:
        positions = profitable_positions(get_positions(symbol=self.symbol, magic=self.magic))
        if not positions:
            return 0

        tick = get_tick(self.symbol)
        if tick is None:
            return 0
        manual_levels = [float(v) for v in self.config.get("levels", {}).get("levels_manual", [])]
        all_levels = sorted(set(manual_levels + self.auto_levels))
        if not all_levels:
            return 0

        current_bid = float(tick.bid)
        current_ask = float(tick.ask)
        step_price = self._grid_step_price()
        buffer_multiplier = float(self.config.get("closing", {}).get("level_break_buffer_multiplier", 0.5))
        break_buffer = max(step_price * buffer_multiplier, 0.0)
        close_candidates: list[int] = []
        for pos in sorted(positions, key=lambda x: x.profit, reverse=True):
            if pos.side == "BUY":
                hit = any(
                    current_bid >= (level + break_buffer) and pos.price_open <= level
                    for level in all_levels
                )
            else:
                hit = any(
                    current_ask <= (level - break_buffer) and pos.price_open >= level
                    for level in all_levels
                )
            if hit:
                close_candidates.append(pos.ticket)
            if len(close_candidates) >= max_count:
                break

        closed = 0
        for ticket in close_candidates:
            if close_position(ticket, deviation=self.deviation):
                closed += 1
        if closed:
            self._record_action(f"Closed by level logic: {closed} positions")
        return closed

    def _allowed_tp_sides(self, positions: list[Any]) -> set[str]:
        """Limit TP assignment to overloaded side when side imbalance is large."""
        threshold = int(self.config.get("closing", {}).get("tp_imbalance_threshold", 3))
        if threshold <= 0:
            return {"BUY", "SELL"}
        buy_count = sum(1 for p in positions if p.side == "BUY")
        sell_count = sum(1 for p in positions if p.side == "SELL")
        imbalance = buy_count - sell_count
        if imbalance >= threshold:
            return {"BUY"}
        if imbalance <= -threshold:
            return {"SELL"}
        return {"BUY", "SELL"}

    def _auto_assign_take_profits(self) -> int:
        """Auto-assign TP by distant levels and anti-imbalance side filter."""
        closing_cfg = self.config.get("closing", {})
        if not bool(closing_cfg.get("auto_tp_enabled", True)):
            return 0
        now = datetime.now(timezone.utc)
        sync_interval_sec = float(closing_cfg.get("tp_sync_interval_sec", 15.0))
        if self._last_tp_sync_at is not None and sync_interval_sec > 0:
            elapsed = (now - self._last_tp_sync_at).total_seconds()
            if elapsed < sync_interval_sec:
                return 0
        self._last_tp_sync_at = now

        positions = get_positions(symbol=self.symbol, magic=self.magic)
        if not positions:
            return 0
        manual_levels = [float(v) for v in self.config.get("levels", {}).get("levels_manual", [])]
        all_levels = sorted(set(manual_levels + self.auto_levels))
        if not all_levels:
            return 0

        step_price = self._grid_step_price()
        if step_price <= 0:
            return 0
        min_distance_steps = float(closing_cfg.get("tp_min_distance_steps", 3.0))
        min_distance = max(step_price * max(min_distance_steps, 0.0), step_price)
        tolerance = pips_to_price(self.symbol, 0.5)
        max_updates = int(closing_cfg.get("tp_max_updates_per_cycle", 30))
        allowed_sides = self._allowed_tp_sides(positions)
        clear_disallowed = bool(closing_cfg.get("tp_clear_disallowed_on_imbalance", False))

        updated = 0
        cleared = 0
        for pos in positions:
            if pos.side not in allowed_sides:
                if clear_disallowed and pos.tp > 0:
                    if clear_position_take_profit(pos.ticket):
                        cleared += 1
                    if updated + cleared >= max_updates:
                        break
                continue
            if pos.side == "BUY":
                candidates = [lvl for lvl in all_levels if lvl >= (pos.price_open + min_distance)]
                if not candidates:
                    continue
                tp_level = min(candidates)
            else:
                candidates = [lvl for lvl in all_levels if lvl <= (pos.price_open - min_distance)]
                if not candidates:
                    continue
                tp_level = max(candidates)
            tp_level = normalize_price(self.symbol, tp_level)
            if pos.tp > 0 and abs(pos.tp - tp_level) <= tolerance:
                continue
            if set_position_take_profit(pos.ticket, pos.symbol, tp_level):
                updated += 1
            if updated + cleared >= max_updates:
                break

        if updated or cleared:
            sides_label = "/".join(sorted(allowed_sides))
            self._record_action(
                f"Auto TP sync: updated={updated}, cleared={cleared}, sides={sides_label}, "
                f"min_distance={min_distance_steps:.1f}*step, interval={sync_interval_sec:.1f}s"
            )
        return updated + cleared

    def _imbalance_values(self) -> tuple[float, str | None]:
        positions = get_positions(symbol=self.symbol, magic=self.magic)
        buy_count = sum(1 for p in positions if p.side == "BUY")
        sell_count = sum(1 for p in positions if p.side == "SELL")
        self.net_exposure = round(
            sum(p.volume for p in positions if p.side == "BUY")
            - sum(p.volume for p in positions if p.side == "SELL"),
            4,
        )

        lookback_days = int(self.config.get("levels", {}).get("auto_levels_lookback_days", 90))
        buy_closed, sell_closed = count_closed_deals_by_side(self.symbol, self.magic, lookback_days=lookback_days)
        count_imbalance = (buy_count - sell_count) + (buy_closed - sell_closed)
        self.current_imbalance = float(count_imbalance)
        overloaded_side = "BUY" if count_imbalance > 0 else "SELL" if count_imbalance < 0 else None
        return self.current_imbalance, overloaded_side

    def _imbalance_close_priority(self, max_count: int) -> int:
        imbalance, overloaded = self._imbalance_values()
        threshold = float(self.config.get("closing", {}).get("imbalance_threshold", 5))
        if overloaded is None or abs(imbalance) <= threshold:
            return 0

        candidates = sorted(
            profitable_positions(get_positions(symbol=self.symbol, magic=self.magic), side=overloaded),
            key=lambda p: p.profit,
            reverse=True,
        )
        closed = 0
        for pos in candidates[:max_count]:
            if close_position(pos.ticket, deviation=self.deviation):
                closed += 1

        if closed:
            self._record_action(f"Imbalance priority close: {closed} {overloaded} positions")
        return closed

    def _risk_guard(self) -> None:
        account = get_account_info()
        if account is None:
            return
        if self.initial_balance <= 0:
            self.initial_balance = account.balance

        min_equity = float(self.config.get("risk", {}).get("min_equity_guard", 100.0))
        max_dd_pct = float(self.config.get("risk", {}).get("max_drawdown_pct", 30.0))
        dd_limit_equity = max(self.initial_balance * (1.0 - max_dd_pct / 100.0), min_equity)

        if account.equity <= dd_limit_equity:
            if not self.stop_new_orders:
                self._record_action(
                    f"Max drawdown guard triggered: equity={account.equity:.2f}, threshold={dd_limit_equity:.2f}",
                    level=logging.WARNING,
                )
            self.stop_new_orders = True
            if bool(self.config.get("risk", {}).get("close_all_on_dd", False)):
                closed = close_all_positions(self.symbol, self.magic, deviation=self.deviation)
                cancelled = cancel_all_pending(self.symbol, self.magic)
                self._record_action(f"DD action -> closed={closed}, cancelled_pending={cancelled}", level=logging.WARNING)

    def _append_equity_point(self) -> None:
        account = get_account_info()
        if account is None:
            return
        self.equity_curve.append(
            {
                "time": datetime.now().strftime("%H:%M:%S"),
                "balance": account.balance,
                "equity": account.equity,
            }
        )

    def manual_close_all(self) -> tuple[int, int]:
        """UI action: close all positions and cancel all pending."""
        ensure_connection(self.config.get("mt5", {}))
        # Freeze new order placement so the bot does not instantly recreate grid after manual flatten.
        self.stop_new_orders = True
        # First pass: close/cancel everything on current symbol regardless of magic/comment.
        closed = close_all_positions_any(self.symbol, deviation=self.deviation)
        cancelled = cancel_all_pending_any(self.symbol)
        # Second pass: catch in-flight state changes (filled/cancelled/just created orders).
        time.sleep(0.8)
        closed += close_all_positions_any(self.symbol, deviation=self.deviation)
        cancelled += cancel_all_pending_any(self.symbol)
        self.position_registry.clear()
        self._record_action(f"Manual Close All -> closed={closed}, cancelled={cancelled}, new_orders_paused=True")
        return closed, cancelled

    def manual_close_profitable(self, side: str) -> int:
        """UI action: close profitable positions for selected side."""
        ensure_connection(self.config.get("mt5", {}))
        side_upper = side.upper()
        if side_upper not in {"BUY", "SELL"}:
            return 0
        closed = 0
        for pos in profitable_positions(get_positions(symbol=self.symbol, magic=self.magic), side=side_upper):
            if close_position(pos.ticket, deviation=self.deviation):
                closed += 1
        self._record_action(f"Manual close profitable {side_upper}: {closed}")
        return closed

    def manual_set_take_profit(self, side: str, tp_price: float) -> int:
        """UI action: set TP level for open positions by side."""
        ensure_connection(self.config.get("mt5", {}))
        side_upper = side.upper()
        if side_upper not in {"BUY", "SELL"} or tp_price <= 0:
            return 0
        updated = 0
        for pos in get_positions(symbol=self.symbol, magic=self.magic):
            if pos.side != side_upper:
                continue
            if set_position_take_profit(pos.ticket, pos.symbol, tp_price):
                updated += 1
        self._record_action(f"Set TP {tp_price:.5f} for {side_upper} positions: {updated}")
        return updated

    def reset_grid(self) -> int:
        """UI action: cancel all pending and recreate on next cycle."""
        ensure_connection(self.config.get("mt5", {}))
        cancelled = cancel_all_pending(self.symbol, self.magic)
        self.grid_anchor_price = None
        self.stop_new_orders = False
        self._save_state()
        self._record_action(f"Grid reset -> cancelled pending {cancelled}, new_orders_paused=False")
        return cancelled

    def status(self) -> BotStatus:
        """Return immutable status snapshot."""
        account = get_account_info()
        return BotStatus(
            running=self.running,
            connected=is_connected(),
            symbol=self.symbol,
            current_imbalance=self.current_imbalance,
            net_exposure=self.net_exposure,
            auto_levels_count=len(self.auto_levels),
            stop_new_orders=self.stop_new_orders,
            initial_balance=self.initial_balance,
            latest_balance=account.balance if account else 0.0,
            latest_equity=account.equity if account else 0.0,
            last_action=self._last_action,
        )

    def get_equity_curve(self) -> list[dict[str, float | str]]:
        """Return equity history for plotting."""
        return list(self.equity_curve)

    def get_action_logs(self, limit: int = 50) -> list[str]:
        """Return recent action log lines."""
        return list(self.action_logs)[:limit]

    async def run_async(self) -> None:
        """Main async control loop."""
        self.running = True
        mt5_cfg = self.config.get("mt5", {})
        if not connect_mt5(mt5_cfg):
            self._record_action("Unable to connect MT5. Bot stopped.", level=logging.ERROR)
            self.running = False
            return

        if bool(self.config.get("grid", {}).get("reset_on_start", False)):
            self.reset_grid()

        self._record_action(f"Bot started on {self.symbol}")
        while not self.stop_event.is_set():
            try:
                if not ensure_connection(mt5_cfg):
                    self._record_action("Reconnect failed. Retrying...", level=logging.WARNING)
                    await asyncio.sleep(max(self.poll_interval, 2))
                    continue

                self._risk_guard()
                self._refresh_auto_levels()
                self._track_positions()
                self._auto_assign_take_profits()

                max_closes = int(self.config.get("closing", {}).get("max_closes_per_cycle", 3))
                self._imbalance_close_priority(max_count=max_closes)
                if not bool(self.config.get("closing", {}).get("auto_tp_enabled", True)):
                    self._level_based_close(max_count=max_closes)

                if not self.stop_new_orders:
                    self._sync_grid()

                self._append_equity_point()
            except BrokerTimeoutError as exc:
                self._record_action(f"Broker timeout detected: {exc}. Reconnecting...", level=logging.WARNING)
                ensure_connection(mt5_cfg)
                await asyncio.sleep(max(self.poll_interval, 2))
                continue
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("Cycle error: %s", exc)
                self._record_action(f"Cycle error: {exc}", level=logging.ERROR)
            await asyncio.sleep(max(self.poll_interval, 1))

        shutdown_mt5()
        self.running = False
        self._record_action("Bot stopped")

    def stop(self) -> None:
        """Signal graceful stop."""
        self.stop_event.set()

    def run_forever(self) -> None:
        """Headless runner entrypoint."""
        asyncio.run(self.run_async())


def _cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hedging grid bot for MetaTrader 5")
    parser.add_argument("--config", type=str, default=str(CONFIG_PATH_DEFAULT), help="Path to YAML config")
    return parser.parse_args()


def main() -> None:
    """CLI application entrypoint."""
    args = _cli()
    bot = HedgingGridBot(config_path=args.config)
    try:
        bot.run_forever()
    except KeyboardInterrupt:
        LOGGER.info("Keyboard interrupt received. Stopping bot...")
        bot.stop()


if __name__ == "__main__":
    main()
