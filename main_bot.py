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


def setup_logging(log_file: str | Path, level_name: str = "INFO", base_dir: Path | None = None) -> Path:
    """Configure console + rotating file logging."""
    log_path = Path(log_file)
    if not log_path.is_absolute() and base_dir is not None:
        log_path = base_dir / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, level_name.upper(), logging.INFO)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    file_handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=5)
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
    return log_path


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
        self.runtime_log_path = setup_logging(
            log_cfg.get("file", "logs/bot.log"),
            log_cfg.get("level", "INFO"),
            base_dir=self.config_path.parent,
        )
        self.structured_logging_enabled = bool(log_cfg.get("structured_enabled", True))
        self.order_actions_log_path = Path(log_cfg.get("order_actions_file", "logs/order_actions.jsonl"))
        self.critical_errors_log_path = Path(log_cfg.get("critical_errors_file", "logs/critical_errors.jsonl"))
        if not self.order_actions_log_path.is_absolute():
            self.order_actions_log_path = self.config_path.parent / self.order_actions_log_path
        if not self.critical_errors_log_path.is_absolute():
            self.critical_errors_log_path = self.config_path.parent / self.critical_errors_log_path
        self.order_actions_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.critical_errors_log_path.parent.mkdir(parents=True, exist_ok=True)

        self.stop_event = threading.Event()
        self.running = False
        self.stop_new_orders = False

        self.position_registry: dict[int, tuple[str, float]] = {}
        self.auto_levels: list[float] = []
        self.latest_auto_levels_at: datetime | None = None
        self.current_imbalance: float = 0.0
        self.net_exposure: float = 0.0
        self.initial_balance: float = max(float(self.config.get("trading", {}).get("balance_baseline", 0.0)), 0.0)
        self.grid_anchor_price: float | None = None
        self.state_file = Path(self.config.get("trading", {}).get("state_file", "bot_state.json"))
        if not self.state_file.is_absolute():
            self.state_file = self.config_path.parent / self.state_file

        self.equity_curve: deque[dict[str, float | str]] = deque(maxlen=5000)
        self.action_logs: deque[str] = deque(maxlen=400)
        self._last_action = "Idle"
        self._last_tp_sync_at: datetime | None = None
        self._reconnect_fail_count: int = 0
        self._placed_this_cycle: set[tuple[str, float]] = set()
        self._tp_recovery_after_hard_imbalance = False
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

    def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        """Append one JSON event per line to local structured logs."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(payload, ensure_ascii=True) + "\n")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to write structured log %s: %s", path, exc)

    def _log_order_event(self, event: str, **fields: Any) -> None:
        if not self.structured_logging_enabled:
            return
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "symbol": self.symbol,
        }
        payload.update(fields)
        self._append_jsonl(self.order_actions_log_path, payload)

    def _log_critical_event(self, event: str, error: Any | None = None, **fields: Any) -> None:
        if not self.structured_logging_enabled:
            return
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "symbol": self.symbol,
        }
        if error is not None:
            payload["error"] = str(error)
        payload.update(fields)
        self._append_jsonl(self.critical_errors_log_path, payload)

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
        """Round start price to 3 decimals (e.g. 1.17458 -> 1.175)."""
        return normalize_price(self.symbol, round(raw_price, 3))

    def _grid_levels(self, anchor_price: float) -> list[float]:
        grid_cfg = self.config.get("grid", {})
        levels_each_side = int(grid_cfg.get("grid_levels_each_side", 10))
        step = self._grid_step_price()

        levels: list[float] = [normalize_price(self.symbol, anchor_price)]
        for i in range(1, levels_each_side + 1):
            levels.append(normalize_price(self.symbol, anchor_price + i * step))
            levels.append(normalize_price(self.symbol, anchor_price - i * step))
        return sorted(set(levels))

    def _recenter_threshold_price(self) -> float:
        """Resolve recenter threshold as steps first, then points fallback."""
        grid_cfg = self.config.get("grid", {})
        threshold_steps = float(grid_cfg.get("recenter_threshold_steps", 0))
        if threshold_steps > 0:
            return self._grid_step_price() * threshold_steps
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

    def _prune_duplicate_pending(self) -> int:
        """Cancel duplicate pending orders at the same level+side, keep one per level."""
        tolerance = pips_to_price(self.symbol, 0.5)
        pending = get_pending_orders(symbol=self.symbol, magic=self.magic)
        groups: list[list[Any]] = []
        for order in pending:
            placed = False
            for group in groups:
                first = group[0]
                if first.side == order.side and abs(first.price_open - order.price_open) <= tolerance:
                    group.append(order)
                    placed = True
                    break
            if not placed:
                groups.append([order])
        cancelled = 0
        for group in groups:
            for order in group[1:]:
                if cancel_order(order.ticket):
                    cancelled += 1
                    self._record_action(f"Cancelled duplicate pending {order.side} @ {order.price_open:.5f}")
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

    def _pending_exists(
        self,
        side: str,
        level: float,
        tolerance: float,
        pending_cache: list[Any] | None = None,
        placed_this_cycle: set[tuple[str, float]] | None = None,
    ) -> bool:
        if placed_this_cycle is not None:
            for s, lvl in placed_this_cycle:
                if s == side and abs(lvl - level) <= tolerance:
                    return True
        orders = pending_cache if pending_cache is not None else get_pending_orders(symbol=self.symbol, magic=self.magic)
        for order in orders:
            if order.side != side:
                continue
            if abs(order.price_open - level) <= tolerance:
                return True
        return False

    def _active_position_exists(
        self,
        side: str,
        level: float,
        tolerance: float,
        positions_cache: list[Any] | None = None,
    ) -> bool:
        """Check whether an active position already occupies this grid level."""
        step = self._grid_step_price()
        positions = positions_cache if positions_cache is not None else get_positions(symbol=self.symbol, magic=self.magic)
        for pos in positions:
            if pos.side != side:
                continue
            parsed = _parse_grid_comment(pos.comment)
            if parsed and parsed[0] == side and abs(parsed[1] - level) <= tolerance:
                return True
            if step > 0:
                inferred = normalize_price(self.symbol, round(pos.price_open / step) * step)
                if abs(inferred - level) <= tolerance:
                    return True
            if abs(pos.price_open - level) <= tolerance:
                return True
        return False

    def _sticky_levels_from_positions(self) -> set[float]:
        """Collect levels that must stay in-grid while related positions are open."""
        sticky: set[float] = set()
        step = self._grid_step_price()

        for _, level in self.position_registry.values():
            sticky.add(normalize_price(self.symbol, level))

        for pos in get_positions(symbol=self.symbol, magic=self.magic):
            parsed = _parse_grid_comment(pos.comment)
            if parsed:
                sticky.add(normalize_price(self.symbol, parsed[1]))
                continue
            if step > 0:
                inferred = normalize_price(self.symbol, round(pos.price_open / step) * step)
                sticky.add(inferred)
            else:
                sticky.add(normalize_price(self.symbol, pos.price_open))
        return sticky

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
                self._placed_this_cycle.add((side, normalize_price(self.symbol, level)))
                self._record_action(f"Restored pending {side} @ {level:.5f} after position close")
            self._log_order_event(
                "restore_pending_after_position_close",
                side=side,
                level=level,
                ticket=ticket,
                result=bool(ok),
            )

    def _sync_grid(self) -> None:
        if self.stop_new_orders:
            return
        tick = get_tick(self.symbol)
        if tick is None:
            return
        dup_cancelled = self._prune_duplicate_pending()
        if dup_cancelled:
            self._record_action(f"Pruned {dup_cancelled} duplicate pending order(s)")
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
        levels = set(self._grid_levels(self.grid_anchor_price))
        levels.update(self._sticky_levels_from_positions())
        account = get_account_info()
        if account is None:
            return

        lot = self._calc_lot(balance=account.balance)
        tolerance = pips_to_price(self.symbol, 0.5)
        current_bid = float(tick.bid)
        current_ask = float(tick.ask)
        positions_cache = get_positions(symbol=self.symbol, magic=self.magic)
        pending_cache = get_pending_orders(symbol=self.symbol, magic=self.magic)
        for level in sorted(levels):
            if self.stop_new_orders:
                break
            if not self._active_position_exists("BUY", level, tolerance=tolerance, positions_cache=positions_cache):
                if not self._pending_exists(
                    "BUY", level, tolerance=tolerance, pending_cache=pending_cache, placed_this_cycle=self._placed_this_cycle
                ):
                    comment = f"GRID|BUY|{level:.5f}"
                    placed = place_pending_order(
                        symbol=self.symbol,
                        side="BUY",
                        volume=lot,
                        price=level,
                        magic=self.magic,
                        deviation=self.deviation,
                        comment=comment,
                        current_bid=current_bid,
                        current_ask=current_ask,
                    )
                    if placed:
                        self._placed_this_cycle.add(("BUY", normalize_price(self.symbol, level)))
                        self._record_action(f"Placed BUY pending @ {level:.5f}")
                    self._log_order_event("sync_place_pending", side="BUY", level=level, result=bool(placed))
            if self.stop_new_orders:
                break
            if not self._active_position_exists("SELL", level, tolerance=tolerance, positions_cache=positions_cache):
                if not self._pending_exists(
                    "SELL", level, tolerance=tolerance, pending_cache=pending_cache, placed_this_cycle=self._placed_this_cycle
                ):
                    comment = f"GRID|SELL|{level:.5f}"
                    placed = place_pending_order(
                        symbol=self.symbol,
                        side="SELL",
                        volume=lot,
                        price=level,
                        magic=self.magic,
                        deviation=self.deviation,
                        comment=comment,
                        current_bid=current_bid,
                        current_ask=current_ask,
                    )
                    if placed:
                        self._placed_this_cycle.add(("SELL", normalize_price(self.symbol, level)))
                        self._record_action(f"Placed SELL pending @ {level:.5f}")
                    self._log_order_event("sync_place_pending", side="SELL", level=level, result=bool(placed))

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
                self._log_order_event("level_based_close", ticket=ticket, result=True)
        if closed:
            self._record_action(f"Closed by level logic: {closed} positions")
        return closed

    def _tp_mode(self) -> str:
        """Resolve TP mode with backward-compatible fallback."""
        closing_cfg = self.config.get("closing", {})
        mode = str(closing_cfg.get("tp_mode", "")).strip().lower()
        if mode in {"auto", "manual"}:
            return mode
        return "auto" if bool(closing_cfg.get("auto_tp_enabled", True)) else "manual"

    def set_tp_mode(self, mode: str) -> bool:
        """Update TP mode at runtime: auto or manual."""
        normalized_mode = str(mode).strip().lower()
        if normalized_mode not in {"auto", "manual"}:
            return False
        closing_cfg = self.config.setdefault("closing", {})
        closing_cfg["tp_mode"] = normalized_mode
        closing_cfg["auto_tp_enabled"] = normalized_mode == "auto"
        save_config(self.config_path, self.config)
        self.reload_config()
        self._record_action(f"TP mode switched to {normalized_mode}")
        return True

    def tp_mode(self) -> str:
        """Expose current TP mode for dashboard controls."""
        return self._tp_mode()

    def _tp_policy(self, positions: list[Any]) -> tuple[dict[str, float], set[str], int]:
        """Return min-steps by side + sides requiring TP clear."""
        closing_cfg = self.config.get("closing", {})
        base_steps = float(closing_cfg.get("tp_min_distance_steps", 3.0))
        imbalance_steps = float(closing_cfg.get("tp_imbalanced_min_distance_steps", 5.0))
        soft_threshold = int(closing_cfg.get("tp_imbalance_stepup_threshold", 2))
        hard_threshold = int(closing_cfg.get("tp_imbalance_clear_threshold", 5))
        release_threshold = int(closing_cfg.get("tp_imbalance_release_threshold", 2))

        buy_count = sum(1 for p in positions if p.side == "BUY")
        sell_count = sum(1 for p in positions if p.side == "SELL")
        balance = buy_count - sell_count
        abs_balance = abs(balance)

        min_steps_by_side: dict[str, float] = {"BUY": base_steps, "SELL": base_steps}
        clear_tp_sides: set[str] = set()

        if balance == 0:
            self._tp_recovery_after_hard_imbalance = False
            return min_steps_by_side, clear_tp_sides, balance

        deficit_side = "BUY" if balance < 0 else "SELL"
        if abs_balance >= hard_threshold:
            self._tp_recovery_after_hard_imbalance = True
            clear_tp_sides.add(deficit_side)
            return min_steps_by_side, clear_tp_sides, balance

        if self._tp_recovery_after_hard_imbalance and abs_balance <= release_threshold:
            min_steps_by_side[deficit_side] = imbalance_steps
            return min_steps_by_side, clear_tp_sides, balance

        if abs_balance > soft_threshold:
            min_steps_by_side[deficit_side] = imbalance_steps

        return min_steps_by_side, clear_tp_sides, balance

    def _auto_assign_take_profits(self) -> int:
        """Auto-assign TP by levels with balance-aware distance/clear rules."""
        closing_cfg = self.config.get("closing", {})
        if self._tp_mode() != "auto":
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
        tolerance = pips_to_price(self.symbol, 0.5)
        max_updates = int(closing_cfg.get("tp_max_updates_per_cycle", 30))
        min_steps_by_side, clear_tp_sides, balance = self._tp_policy(positions)

        updated = 0
        cleared = 0
        for pos in positions:
            if pos.side in clear_tp_sides:
                if pos.tp > 0 and clear_position_take_profit(pos.ticket):
                    cleared += 1
                    self._log_order_event(
                        "auto_tp_clear_for_imbalance",
                        side=pos.side,
                        ticket=pos.ticket,
                        result=True,
                    )
                if updated + cleared >= max_updates:
                    break
                continue

            min_distance_steps = max(min_steps_by_side.get(pos.side, 3.0), 0.0)
            min_distance = max(step_price * min_distance_steps, step_price)
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
                self._log_order_event(
                    "auto_tp_set",
                    side=pos.side,
                    ticket=pos.ticket,
                    tp=tp_level,
                    result=True,
                )
            if updated + cleared >= max_updates:
                break

        if updated or cleared:
            clear_label = ",".join(sorted(clear_tp_sides)) if clear_tp_sides else "-"
            self._record_action(
                f"Auto TP sync: updated={updated}, cleared={cleared}, balance={balance}, "
                f"min_steps(BUY/SELL)={min_steps_by_side['BUY']:.1f}/{min_steps_by_side['SELL']:.1f}, "
                f"cleared_sides={clear_label}, interval={sync_interval_sec:.1f}s"
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

        count_imbalance = buy_count - sell_count
        self.current_imbalance = float(count_imbalance)
        overloaded_side = "BUY" if count_imbalance > 0 else "SELL" if count_imbalance < 0 else None
        return self.current_imbalance, overloaded_side

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
                self._log_order_event("dd_close_all", closed=closed, cancelled=cancelled, result=True)

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
        """UI action: stop bot activity, close positions and cancel pending."""
        ensure_connection(self.config.get("mt5", {}))
        self.stop_new_orders = True
        closed = close_all_positions_any(self.symbol, deviation=self.deviation)
        cancelled = cancel_all_pending_any(self.symbol)
        time.sleep(0.8)
        closed += close_all_positions_any(self.symbol, deviation=self.deviation)
        cancelled += cancel_all_pending_any(self.symbol)
        self.position_registry.clear()
        self.stop()
        self._record_action(f"Manual Close All -> closed={closed}, cancelled={cancelled}, bot_stopped=True")
        self._log_order_event("manual_close_all", closed=closed, cancelled=cancelled, result=True)
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
                self._log_order_event(
                    "manual_close_profitable",
                    side=side_upper,
                    ticket=pos.ticket,
                    result=True,
                )
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
                self._log_order_event(
                    "manual_set_tp",
                    side=side_upper,
                    ticket=pos.ticket,
                    tp=normalize_price(self.symbol, tp_price),
                    result=True,
                )
        self._record_action(f"Set TP {tp_price:.5f} for {side_upper} positions: {updated}")
        return updated

    def manual_set_take_profit_selected(self, tickets: list[int], tp_price: float) -> int:
        """UI action: set TP for selected position tickets."""
        ensure_connection(self.config.get("mt5", {}))
        if tp_price <= 0:
            return 0
        selected = {int(t) for t in tickets}
        if not selected:
            return 0
        updated = 0
        for pos in get_positions(symbol=self.symbol, magic=self.magic):
            if pos.ticket not in selected:
                continue
            if set_position_take_profit(pos.ticket, pos.symbol, tp_price):
                updated += 1
                self._log_order_event(
                    "manual_set_tp_selected",
                    side=pos.side,
                    ticket=pos.ticket,
                    tp=normalize_price(self.symbol, tp_price),
                    result=True,
                )
        self._record_action(f"Set TP {tp_price:.5f} for selected positions: {updated}")
        return updated

    def manual_close_selected(self, tickets: list[int]) -> int:
        """UI action: close selected open positions by ticket."""
        ensure_connection(self.config.get("mt5", {}))
        selected = {int(t) for t in tickets}
        if not selected:
            return 0
        closed = 0
        for pos in get_positions(symbol=self.symbol, magic=self.magic):
            if pos.ticket not in selected:
                continue
            if close_position(pos.ticket, deviation=self.deviation):
                closed += 1
                self._log_order_event(
                    "manual_close_selected",
                    side=pos.side,
                    ticket=pos.ticket,
                    result=True,
                )
        self._record_action(f"Manual close selected positions: {closed}")
        return closed

    def reset_grid(self) -> int:
        """UI action: cancel all pending and recreate on next cycle."""
        ensure_connection(self.config.get("mt5", {}))
        cancelled = cancel_all_pending(self.symbol, self.magic)
        self.grid_anchor_price = None
        self.stop_new_orders = False
        self._save_state()
        self._record_action(f"Grid reset -> cancelled pending {cancelled}, new_orders_paused=False")
        self._log_order_event("reset_grid", cancelled=cancelled, result=True)
        return cancelled

    def status(self) -> BotStatus:
        """Return immutable status snapshot."""
        # Do not trigger broker reconnects from passive UI polling while bot is stopped.
        account = get_account_info() if self.running else None
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

    def _reconnect_backoff_sec(self) -> float:
        """Exponential backoff for reconnect retries (5s base, 120s cap)."""
        base = 5.0
        cap = 120.0
        delay = min(base * (2**self._reconnect_fail_count), cap)
        return max(delay, self.poll_interval)

    def _is_network_error(self, exc: BaseException) -> bool:
        """Detect network/DNS errors that warrant reconnect + backoff."""
        chain: list[BaseException] = [exc]
        tail = exc
        while True:
            cause = getattr(tail, "__cause__", None) or getattr(tail, "__context__", None)
            if cause is None or cause in chain:
                break
            chain.append(cause)
            tail = cause
        for e in chain:
            err_str = str(e).lower()
            if any(x in err_str for x in ("nodename nor servname", "connection", "connect", "timeout", "network", "dns")):
                return True
            if type(e).__name__ in ("ApiException", "ConnectError", "ConnectionError", "BrokerTimeoutError"):
                return True
            if isinstance(e, (ConnectionError, OSError)):
                return True
        return False

    async def run_async(self) -> None:
        """Main async control loop."""
        self.running = True
        mt5_cfg = self.config.get("mt5", {})
        if not connect_mt5(mt5_cfg):
            self._record_action("Unable to connect MT5. Bot stopped.", level=logging.ERROR)
            self._log_critical_event("startup_connect_failed")
            self.running = False
            return

        if bool(self.config.get("grid", {}).get("reset_on_start", False)):
            self.reset_grid()

        self._record_action(f"Bot started on {self.symbol}")
        while not self.stop_event.is_set():
            try:
                if not ensure_connection(mt5_cfg):
                    self._reconnect_fail_count += 1
                    backoff = self._reconnect_backoff_sec()
                    self._record_action(
                        f"Reconnect failed (attempt {self._reconnect_fail_count}). Retry in {backoff:.0f}s...",
                        level=logging.WARNING,
                    )
                    self._log_critical_event("reconnect_failed", attempt=self._reconnect_fail_count, backoff_sec=backoff)
                    await asyncio.sleep(backoff)
                    continue

                self._reconnect_fail_count = 0
                self._placed_this_cycle.clear()
                self._risk_guard()
                self._refresh_auto_levels()
                self._track_positions()
                self._auto_assign_take_profits()
                self._imbalance_values()  # update status.current_imbalance for dashboard

                if not self.stop_new_orders:
                    self._sync_grid()

                self._append_equity_point()
            except BrokerTimeoutError as exc:
                self._reconnect_fail_count += 1
                backoff = self._reconnect_backoff_sec()
                self._record_action(
                    f"Broker timeout: {exc}. Reconnecting in {backoff:.0f}s...",
                    level=logging.WARNING,
                )
                self._log_critical_event("broker_timeout", error=exc, backoff_sec=backoff)
                ensure_connection(mt5_cfg)
                await asyncio.sleep(backoff)
                continue
            except Exception as exc:  # noqa: BLE001
                if self._is_network_error(exc):
                    self._reconnect_fail_count += 1
                    backoff = self._reconnect_backoff_sec()
                    self._record_action(
                        f"Network error: {exc}. Reconnecting in {backoff:.0f}s...",
                        level=logging.WARNING,
                    )
                    self._log_critical_event("network_error", error=exc, backoff_sec=backoff)
                    ensure_connection(mt5_cfg)
                    await asyncio.sleep(backoff)
                    continue
                LOGGER.exception("Cycle error: %s", exc)
                self._record_action(f"Cycle error: {exc}", level=logging.ERROR)
                self._log_critical_event("cycle_exception", error=exc)
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
