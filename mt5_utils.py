"""MetaTrader-compatible utility layer powered by MetaApi for macOS."""

from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FuturesTimeoutError
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence


LOGGER = logging.getLogger(__name__)


class BrokerTimeoutError(Exception):
    """Raised when broker RPC request times out."""


@dataclass
class PositionView:
    """Normalized position model used by bot and dashboard."""

    ticket: int
    symbol: str
    side: str
    volume: float
    price_open: float
    price_current: float
    profit: float
    sl: float
    tp: float
    comment: str
    time_open: int


@dataclass
class PendingOrderView:
    """Normalized pending order model."""

    ticket: int
    symbol: str
    side: str
    kind: str
    order_type: int
    volume_initial: float
    price_open: float
    sl: float
    tp: float
    comment: str
    time_setup: int


@dataclass
class AccountView:
    """Normalized account state."""

    login: int
    balance: float
    equity: float
    margin: float
    margin_free: float
    currency: str


def _get(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:  # noqa: BLE001
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value))
    except Exception:  # noqa: BLE001
        return default


def _side_from_position_type(pos_type: Any) -> str:
    text = str(pos_type).upper()
    if "BUY" in text or text == "0":
        return "BUY"
    return "SELL"


def _side_from_order_type(order_type: Any) -> str:
    text = str(order_type).upper()
    if "BUY" in text or text in {"2", "4"}:
        return "BUY"
    return "SELL"


def _is_bot_comment(comment: Any) -> bool:
    text = str(comment or "")
    return text.startswith("GRID|")


class _MetaApiBridge:
    """Thread-safe wrapper around async MetaApi SDK calls."""

    def __init__(self, mt5_config: dict[str, Any]) -> None:
        self.cfg = mt5_config
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.connect_lock = threading.Lock()

        self.api: Any = None
        self.account: Any = None
        self.connection: Any = None
        self.connected: bool = False
        self.symbol_specs: dict[str, dict[str, Any]] = {}
        self._is_stopping: bool = False

    def start(self) -> bool:
        # Avoid concurrent reconnect storms from bot loop + UI polling threads.
        with self.connect_lock:
            with self.lock:
                if self.connected:
                    return True
                if self.loop is None:
                    self.loop = asyncio.new_event_loop()
                    self.thread = threading.Thread(target=self._loop_runner, daemon=True, name="metaapi-loop")
                    self.thread.start()
            try:
                # If previous RPC/websocket objects exist after sleep/network drop, close first.
                if self.connection is not None or self.api is not None:
                    try:
                        self._call(self._close_async(), timeout=12.0)
                    except Exception:  # noqa: BLE001
                        pass
                    self.connection = None
                    self.account = None
                    self.api = None
                    self.symbol_specs = {}
                return self._call(self._connect_async())
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("MetaApi start failed: %s", exc)
                self.connected = False
                return False

    def stop(self) -> None:
        self.connected = False
        self._is_stopping = True
        with self.lock:
            if self.loop is not None:
                try:
                    # Properly close RPC subscription/client before stopping loop.
                    self._call(self._close_async(), timeout=20.0)
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("MetaApi graceful close failed: %s", exc)
            if self.loop is not None:
                try:
                    self.loop.call_soon_threadsafe(self.loop.stop)
                except Exception:  # noqa: BLE001
                    pass
            self.loop = None
            self.thread = None
            self.connection = None
            self.account = None
            self.api = None
            self.symbol_specs = {}
        self._is_stopping = False

    async def _close_async(self) -> None:
        """Gracefully close active RPC/websocket resources."""
        try:
            if self.connection is not None:
                await self.connection.close()
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("RPC connection close error: %s", exc)
        try:
            if self.api is not None:
                self.api.close()
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("MetaApi client close error: %s", exc)

    def _loop_runner(self) -> None:
        assert self.loop is not None
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _call(self, coro: Any, timeout: float = 40.0) -> Any:
        if self.loop is None:
            raise RuntimeError("MetaApi loop is not initialized")
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError as exc:
            future.cancel()
            self.connected = False
            raise BrokerTimeoutError(f"Broker RPC timeout after {timeout:.1f}s") from exc

    async def _connect_async(self) -> bool:
        token = str(self.cfg.get("metaapi_token", "")).strip()
        account_id = str(self.cfg.get("metaapi_account_id", "")).strip()
        login = str(self.cfg.get("login", "")).strip()
        password = str(self.cfg.get("password", "")).strip()
        server = str(self.cfg.get("server", "")).strip()
        platform = str(self.cfg.get("platform", "mt5")).strip().lower()
        if platform not in {"mt4", "mt5"}:
            LOGGER.warning("Unsupported mt5.platform=%r, fallback to 'mt5'", platform)
            platform = "mt5"

        if not token:
            LOGGER.error("Set mt5.metaapi_token in config.yaml")
            return False

        try:
            from metaapi_cloud_sdk import MetaApi  # imported lazily for clear error on missing package
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("metaapi-cloud-sdk is not installed: %s", exc)
            return False

        self.api = MetaApi(token=token)
        if account_id:
            self.account = await self.api.metatrader_account_api.get_account(account_id)
        else:
            if not login or not password or not server:
                LOGGER.error("Set login/password/server or provide mt5.metaapi_account_id")
                return False
            try:
                self.account = await self.api.metatrader_account_api.create_account(
                    account={
                        "name": f"RoboForex-{login}",
                        "type": "cloud",
                        "login": login,
                        "password": password,
                        "server": server,
                        "platform": platform,
                        "magic": _to_int(self.cfg.get("magic", 900001), 900001),
                        "reliability": "regular",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("Auto account create failed. Create account in MetaApi app and set metaapi_account_id. %s", exc)
                return False

        try:
            await self.account.deploy()
        except Exception:
            # Usually safe if already deployed.
            pass
        try:
            await self.account.wait_connected()
        except Exception:
            pass

        self.connection = self.account.get_rpc_connection()
        await self.connection.connect()
        await self.connection.wait_synchronized()
        self.connected = True
        return True

    def ensure_connected(self) -> bool:
        if self._is_stopping:
            return False
        if self.connected:
            try:
                self._call(self.connection.get_account_information(), timeout=15.0)
                return True
            except Exception:
                self.connected = False
        return self.start()

    def get_account_info(self) -> dict[str, Any] | None:
        if not self.ensure_connected():
            return None
        return self._call(self.connection.get_account_information(), timeout=8.0)

    def get_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        if not self.ensure_connected():
            return []
        items = self._call(self.connection.get_positions(), timeout=8.0) or []
        if not symbol:
            return list(items)
        return [p for p in items if str(_get(p, "symbol", default="")) == symbol]

    def get_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        if not self.ensure_connected():
            return []
        items = self._call(self.connection.get_orders(), timeout=8.0) or []
        if not symbol:
            return list(items)
        return [o for o in items if str(_get(o, "symbol", default="")) == symbol]

    def get_symbol_price(self, symbol: str) -> dict[str, Any] | None:
        if not self.ensure_connected():
            return None
        return self._call(self.connection.get_symbol_price(symbol=symbol), timeout=8.0)

    def get_symbol_spec(self, symbol: str) -> dict[str, Any] | None:
        if symbol in self.symbol_specs:
            return self.symbol_specs[symbol]
        if not self.ensure_connected():
            return None
        spec = self._call(self.connection.get_symbol_specification(symbol=symbol), timeout=10.0)
        if spec:
            self.symbol_specs[symbol] = spec
        return spec

    def get_deals_range(self, from_dt: datetime, to_dt: datetime) -> list[dict[str, Any]]:
        if not self.ensure_connected():
            return []
        return self._call(self.connection.get_deals_by_time_range(start_time=from_dt, end_time=to_dt)) or []

    def create_limit_order(
        self,
        *,
        symbol: str,
        side: str,
        volume: float,
        price: float,
        comment: str,
        sl: float = 0.0,
        tp: float = 0.0,
    ) -> bool:
        if not self.ensure_connected():
            return False
        # MetaApi/MT5 enforces strict combined length for comment+clientId.
        # We send only short comment to avoid validation errors.
        options = {"comment": comment[:31]}
        try:
            if side.upper() == "BUY":
                result = self._call(
                    self.connection.create_limit_buy_order(
                        symbol=symbol,
                        volume=volume,
                        open_price=price,
                        stop_loss=sl if sl else None,
                        take_profit=tp if tp else None,
                        options=options,
                    )
                )
            else:
                result = self._call(
                    self.connection.create_limit_sell_order(
                        symbol=symbol,
                        volume=volume,
                        open_price=price,
                        stop_loss=sl if sl else None,
                        take_profit=tp if tp else None,
                        options=options,
                    )
                )
            return str(_get(result, "stringCode", default="")).upper() in {"OK", "TRADE_RETCODE_DONE"}
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to place %s limit @ %.5f on %s: %s", side, price, symbol, exc)
            return False

    def create_stop_order(
        self,
        *,
        symbol: str,
        side: str,
        volume: float,
        price: float,
        comment: str,
        sl: float = 0.0,
        tp: float = 0.0,
    ) -> bool:
        if not self.ensure_connected():
            return False
        options = {"comment": comment[:31]}
        try:
            if side.upper() == "BUY":
                result = self._call(
                    self.connection.create_stop_buy_order(
                        symbol=symbol,
                        volume=volume,
                        open_price=price,
                        stop_loss=sl if sl else None,
                        take_profit=tp if tp else None,
                        options=options,
                    )
                )
            else:
                result = self._call(
                    self.connection.create_stop_sell_order(
                        symbol=symbol,
                        volume=volume,
                        open_price=price,
                        stop_loss=sl if sl else None,
                        take_profit=tp if tp else None,
                        options=options,
                    )
                )
            return str(_get(result, "stringCode", default="")).upper() in {"OK", "TRADE_RETCODE_DONE"}
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to place %s stop @ %.5f on %s: %s", side, price, symbol, exc)
            return False

    def close_position(self, position_ticket: int) -> bool:
        if not self.ensure_connected():
            return False
        for attempt in range(1, 4):
            try:
                result = self._call(self.connection.close_position(position_id=str(position_ticket)), timeout=12.0)
                return str(_get(result, "stringCode", default="")).upper() in {"OK", "TRADE_RETCODE_DONE"}
            except Exception as exc:  # noqa: BLE001
                err = str(exc).lower()
                if "not found" in err:
                    # Already closed by broker/market - treat as successful outcome.
                    return True
                if "cpu credits" in err and attempt < 3:
                    time.sleep(0.6 * attempt)
                    continue
                LOGGER.warning("Failed to close position %s: %s", position_ticket, exc)
                return False
        return False

    def modify_position_tp(self, position_ticket: int, tp_price: float | None) -> bool:
        """Set/replace TP for an existing open position, or clear it when None."""
        if not self.ensure_connected():
            return False
        try:
            result = self._call(
                self.connection.modify_position(
                    position_id=str(position_ticket),
                    stop_loss=None,
                    take_profit=tp_price,
                ),
                timeout=12.0,
            )
            return str(_get(result, "stringCode", default="")).upper() in {"OK", "TRADE_RETCODE_DONE"}
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to update TP for position %s: %s", position_ticket, exc)
            return False

    def cancel_order(self, order_ticket: int) -> bool:
        if not self.ensure_connected():
            return False
        for attempt in range(1, 4):
            try:
                result = self._call(self.connection.cancel_order(order_id=str(order_ticket)), timeout=12.0)
                return str(_get(result, "stringCode", default="")).upper() in {"OK", "TRADE_RETCODE_DONE"}
            except Exception as exc:  # noqa: BLE001
                err = str(exc).lower()
                if "not found" in err:
                    # Already cancelled/filled at broker side.
                    return True
                if ("cpu credits" in err or "frozen" in err) and attempt < 3:
                    time.sleep(0.6 * attempt)
                    continue
                LOGGER.warning("Failed to cancel order %s: %s", order_ticket, exc)
                return False
        return False

    def get_historical_candles(self, symbol: str, timeframe: str, limit: int) -> list[dict[str, Any]]:
        if not self.ensure_connected():
            return []
        try:
            return self._call(
                self.account.get_historical_candles(
                    symbol=symbol,
                    timeframe=timeframe,
                    start_time=datetime.now(timezone.utc),
                    limit=limit,
                ),
                timeout=60.0,
            ) or []
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to get historical candles for %s: %s", symbol, exc)
            return []


_BRIDGE: _MetaApiBridge | None = None


def _bridge(mt5_config: dict[str, Any] | None = None) -> _MetaApiBridge | None:
    global _BRIDGE
    if _BRIDGE is None and mt5_config is not None:
        _BRIDGE = _MetaApiBridge(mt5_config=mt5_config)
    return _BRIDGE


def connect_mt5(mt5_config: dict[str, Any], retries: int = 5, retry_delay: float = 2.0) -> bool:
    """Initialize and login with retries (MetaApi backend)."""
    for attempt in range(1, retries + 1):
        bridge = _bridge(mt5_config)
        if bridge and bridge.start():
            LOGGER.info("Connected via MetaApi bridge.")
            return True
        LOGGER.warning("MetaApi connect attempt %s/%s failed", attempt, retries)
        # Avoid aggressive reconnect storm on provider-side quotas (429).
        if attempt < retries and retries > 1:
            time.sleep(retry_delay * attempt)
    return False


def shutdown_mt5() -> None:
    """Shutdown backend connection."""
    global _BRIDGE
    if _BRIDGE is not None:
        _BRIDGE.stop()
    _BRIDGE = None


def is_connected() -> bool:
    """Return backend connectivity state."""
    bridge = _bridge()
    return bool(bridge and bridge.connected)


def ensure_connection(mt5_config: dict[str, Any]) -> bool:
    """Reconnect when link is dropped."""
    bridge = _bridge(mt5_config)
    if bridge is None:
        return False
    return bridge.ensure_connected()


def _pip_size(symbol: str) -> float:
    bridge = _bridge()
    if bridge is None:
        return 0.0001
    spec = bridge.get_symbol_spec(symbol)
    if not spec:
        return 0.0001
    digits = _to_int(_get(spec, "digits", default=5), default=5)
    return 0.01 if digits in (2, 3) else 0.0001


def normalize_price(symbol: str, price: float) -> float:
    """Round price to symbol digits."""
    bridge = _bridge()
    if bridge is None:
        return price
    spec = bridge.get_symbol_spec(symbol)
    digits = _to_int(_get(spec or {}, "digits", default=5), default=5)
    return round(price, digits)


def normalize_volume(symbol: str, volume: float) -> float:
    """Snap volume to broker lot step and min/max limits."""
    bridge = _bridge()
    if bridge is None:
        return max(0.01, round(volume, 2))
    spec = bridge.get_symbol_spec(symbol) or {}
    step = _to_float(_get(spec, "volumeStep", "volume_step", default=0.01), default=0.01)
    min_vol = _to_float(_get(spec, "minVolume", "volume_min", default=0.01), default=0.01)
    max_vol = _to_float(_get(spec, "maxVolume", "volume_max", default=100.0), default=100.0)
    if step <= 0:
        step = 0.01
    snapped = round(round(volume / step) * step, 2)
    return min(max(snapped, min_vol), max_vol)


def get_tick(symbol: str) -> Any:
    """Fetch current tick for symbol."""
    bridge = _bridge()
    if bridge is None:
        return None
    raw = bridge.get_symbol_price(symbol)
    if not raw:
        return None
    return type(
        "Tick",
        (),
        {"bid": _to_float(_get(raw, "bid", default=0.0)), "ask": _to_float(_get(raw, "ask", default=0.0))},
    )()


def get_account_info() -> AccountView | None:
    """Read account info in normalized form."""
    bridge = _bridge()
    if bridge is None:
        return None
    try:
        account = bridge.get_account_info()
    except BrokerTimeoutError:
        # UI/status callers should not crash on transient broker latency spikes.
        return None
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Failed to read account info: %s", exc)
        return None
    if not account:
        return None
    return AccountView(
        login=_to_int(_get(account, "login", default=0)),
        balance=_to_float(_get(account, "balance", default=0.0)),
        equity=_to_float(_get(account, "equity", default=0.0)),
        margin=_to_float(_get(account, "margin", default=0.0)),
        margin_free=_to_float(_get(account, "freeMargin", "marginFree", default=0.0)),
        currency=str(_get(account, "currency", default="USD")),
    )


def get_positions(symbol: str | None = None, magic: int | None = None) -> list[PositionView]:
    """Return open positions filtered by symbol/magic."""
    bridge = _bridge()
    if bridge is None:
        return []
    raw_positions = bridge.get_positions(symbol=symbol)
    positions: list[PositionView] = []
    for p in raw_positions:
        comment = str(_get(p, "comment", default=""))
        if magic is not None:
            p_magic = _to_int(_get(p, "magic", default=0))
            # MetaApi may not preserve EA magic the same way as native MT5,
            # so we also accept our bot-tagged comments.
            if p_magic != magic and not _is_bot_comment(comment):
                continue
        side = _side_from_position_type(_get(p, "type", default="BUY"))
        positions.append(
            PositionView(
                ticket=_to_int(_get(p, "id", "ticket", default=0)),
                symbol=str(_get(p, "symbol", default="")),
                side=side,
                volume=_to_float(_get(p, "volume", default=0.0)),
                price_open=_to_float(_get(p, "openPrice", "priceOpen", default=0.0)),
                price_current=_to_float(_get(p, "currentPrice", "priceCurrent", default=0.0)),
                profit=_to_float(_get(p, "profit", default=0.0)),
                sl=_to_float(_get(p, "stopLoss", "sl", default=0.0)),
                tp=_to_float(_get(p, "takeProfit", "tp", default=0.0)),
                comment=comment,
                time_open=_to_int(_get(p, "time", "updateTime", default=0)),
            )
        )
    return positions


def get_pending_orders(symbol: str | None = None, magic: int | None = None) -> list[PendingOrderView]:
    """Return pending orders filtered by symbol/magic."""
    bridge = _bridge()
    if bridge is None:
        return []
    raw_orders = bridge.get_orders(symbol=symbol)
    pending: list[PendingOrderView] = []
    for o in raw_orders:
        o_type = _get(o, "type", default="")
        text_type = str(o_type).upper()
        if all(token not in text_type for token in ("LIMIT", "STOP")) and str(o_type) not in {"2", "3", "4", "5"}:
            continue
        comment = str(_get(o, "comment", default=""))
        if magic is not None:
            o_magic = _to_int(_get(o, "magic", default=0))
            if o_magic != magic and not _is_bot_comment(comment):
                continue
        side = _side_from_order_type(o_type)
        kind = "STOP" if "STOP" in text_type and "LIMIT" not in text_type else "LIMIT"
        order_type_code = 2 if side == "BUY" and kind == "LIMIT" else 3 if side == "SELL" and kind == "LIMIT" else 4 if side == "BUY" else 5
        pending.append(
            PendingOrderView(
                ticket=_to_int(_get(o, "id", "ticket", default=0)),
                symbol=str(_get(o, "symbol", default="")),
                side=side,
                kind=kind,
                order_type=order_type_code,
                volume_initial=_to_float(_get(o, "volume", "currentVolume", default=0.0)),
                price_open=_to_float(_get(o, "openPrice", "priceOpen", default=0.0)),
                sl=_to_float(_get(o, "stopLoss", "sl", default=0.0)),
                tp=_to_float(_get(o, "takeProfit", "tp", default=0.0)),
                comment=comment,
                time_setup=_to_int(_get(o, "time", "updateTime", default=0)),
            )
        )
    return pending


def get_deals_history(from_dt: datetime, to_dt: datetime, symbol: str | None = None) -> Sequence[Any]:
    """Return historical deals in UTC range."""
    bridge = _bridge()
    if bridge is None:
        return []
    deals = bridge.get_deals_range(from_dt.astimezone(timezone.utc), to_dt.astimezone(timezone.utc))
    if symbol is None:
        return deals
    return [d for d in deals if str(_get(d, "symbol", default="")) == symbol]


def get_daily_rates(symbol: str, lookback_days: int) -> list[dict[str, float]]:
    """Retrieve D1 candles as list[high, low, close]."""
    bridge = _bridge()
    if bridge is None:
        return []
    candles = bridge.get_historical_candles(symbol=symbol, timeframe="1d", limit=max(lookback_days + 10, 60))
    rates: list[dict[str, float]] = []
    for candle in candles:
        rates.append(
            {
                "high": _to_float(_get(candle, "high", default=0.0)),
                "low": _to_float(_get(candle, "low", default=0.0)),
                "close": _to_float(_get(candle, "close", default=0.0)),
            }
        )
    return rates[-lookback_days:] if lookback_days > 0 else rates


def pips_to_price(symbol: str, pips: float) -> float:
    """Convert pips to absolute price delta."""
    return pips * _pip_size(symbol)


def place_limit_order(
    *,
    symbol: str,
    side: str,
    volume: float,
    price: float,
    magic: int,
    deviation: int,
    comment: str,
    sl: float = 0.0,
    tp: float = 0.0,
) -> bool:
    """Place a Buy Limit or Sell Limit order."""
    del magic, deviation  # kept for strategy API compatibility
    bridge = _bridge()
    if bridge is None:
        return False
    return bridge.create_limit_order(
        symbol=symbol,
        side=side,
        volume=normalize_volume(symbol, volume),
        price=normalize_price(symbol, price),
        comment=comment,
        sl=normalize_price(symbol, sl) if sl else 0.0,
        tp=normalize_price(symbol, tp) if tp else 0.0,
    )


def place_pending_order(
    *,
    symbol: str,
    side: str,
    volume: float,
    price: float,
    magic: int,
    deviation: int,
    comment: str,
    current_bid: float,
    current_ask: float,
    sl: float = 0.0,
    tp: float = 0.0,
) -> bool:
    """Place LIMIT/STOP automatically by level relative to current market."""
    del magic, deviation
    bridge = _bridge()
    if bridge is None:
        return False
    norm_volume = normalize_volume(symbol, volume)
    norm_price = normalize_price(symbol, price)
    norm_sl = normalize_price(symbol, sl) if sl else 0.0
    norm_tp = normalize_price(symbol, tp) if tp else 0.0
    if side.upper() == "BUY":
        if norm_price < current_ask:
            return bridge.create_limit_order(
                symbol=symbol,
                side="BUY",
                volume=norm_volume,
                price=norm_price,
                comment=comment,
                sl=norm_sl,
                tp=norm_tp,
            )
        return bridge.create_stop_order(
            symbol=symbol,
            side="BUY",
            volume=norm_volume,
            price=norm_price,
            comment=comment,
            sl=norm_sl,
            tp=norm_tp,
        )
    if norm_price > current_bid:
        return bridge.create_limit_order(
            symbol=symbol,
            side="SELL",
            volume=norm_volume,
            price=norm_price,
            comment=comment,
            sl=norm_sl,
            tp=norm_tp,
        )
    return bridge.create_stop_order(
        symbol=symbol,
        side="SELL",
        volume=norm_volume,
        price=norm_price,
        comment=comment,
        sl=norm_sl,
        tp=norm_tp,
    )


def close_position(position_ticket: int, deviation: int = 20) -> bool:
    """Close position by market order."""
    del deviation
    bridge = _bridge()
    if bridge is None:
        return False
    return bridge.close_position(position_ticket)


def set_position_take_profit(position_ticket: int, symbol: str, tp_price: float) -> bool:
    """Set TP on an open position by ticket."""
    bridge = _bridge()
    if bridge is None:
        return False
    normalized_tp = normalize_price(symbol, tp_price)
    if normalized_tp <= 0:
        return False
    return bridge.modify_position_tp(position_ticket, normalized_tp)


def clear_position_take_profit(position_ticket: int) -> bool:
    """Clear TP on an open position by ticket."""
    bridge = _bridge()
    if bridge is None:
        return False
    return bridge.modify_position_tp(position_ticket, None)


def cancel_order(order_ticket: int) -> bool:
    """Cancel pending order by ticket."""
    bridge = _bridge()
    if bridge is None:
        return False
    return bridge.cancel_order(order_ticket)


def cancel_all_pending(symbol: str, magic: int) -> int:
    """Cancel all bot pending orders."""
    cancelled = 0
    for order in get_pending_orders(symbol=symbol, magic=magic):
        if cancel_order(order.ticket):
            cancelled += 1
    return cancelled


def close_all_positions(symbol: str, magic: int, deviation: int) -> int:
    """Close all bot positions and return count of successful closes."""
    closed = 0
    for pos in get_positions(symbol=symbol, magic=magic):
        if close_position(pos.ticket, deviation=deviation):
            closed += 1
    return closed


def cancel_all_pending_any(symbol: str | None = None) -> int:
    """Cancel all pending orders for symbol regardless of magic/comment."""
    cancelled = 0
    for order in get_pending_orders(symbol=symbol, magic=None):
        if cancel_order(order.ticket):
            cancelled += 1
    return cancelled


def close_all_positions_any(symbol: str | None = None, deviation: int = 20) -> int:
    """Close all open positions for symbol regardless of magic/comment."""
    closed = 0
    for pos in get_positions(symbol=symbol, magic=None):
        if close_position(pos.ticket, deviation=deviation):
            closed += 1
    return closed


def count_closed_deals_by_side(symbol: str, magic: int, lookback_days: int = 90) -> tuple[int, int]:
    """Count how many BUY/SELL positions were closed over lookback period."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    deals = get_deals_history(start, end, symbol=symbol)
    buy_closed = 0
    sell_closed = 0
    for deal in deals:
        d_magic = _to_int(_get(deal, "magic", default=magic))
        if d_magic != magic:
            continue
        entry = str(_get(deal, "entryType", "entry", default="")).upper()
        if entry and "OUT" not in entry:
            continue
        d_type = str(_get(deal, "type", default="")).upper()
        if "BUY" in d_type:
            buy_closed += 1
        elif "SELL" in d_type:
            sell_closed += 1
    return buy_closed, sell_closed


def profitable_positions(positions: Iterable[PositionView], side: str | None = None) -> list[PositionView]:
    """Filter only profitable positions and optional side."""
    normalized_side = side.upper() if side else None
    return [
        p
        for p in positions
        if p.profit > 0 and (normalized_side is None or p.side == normalized_side)
    ]
