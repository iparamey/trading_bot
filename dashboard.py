"""Streamlit dashboard for live/semi-live control of hedging grid bot."""

from __future__ import annotations

import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from main_bot import HedgingGridBot, load_config, save_config
from mt5_utils import ensure_connection, get_deals_history, get_pending_orders, get_positions


CONFIG_PATH = Path("config.yaml")


def _deal_time_utc(deal: Any) -> datetime | None:
    """Best-effort parse of MetaApi deal execution timestamp."""
    raw = None
    if isinstance(deal, dict):
        raw = deal.get("time") or deal.get("updateTime") or deal.get("brokerTime")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        ts = float(raw)
        if ts > 1e12:  # milliseconds -> seconds
            ts = ts / 1000.0
        if ts <= 0:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        # MetaApi often returns RFC3339 with trailing Z.
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _render_executed_deals() -> None:
    bot = st.session_state.bot
    if bot is None:
        st.info("Start the bot to view executed deals.")
        return
    if not bot.running:
        st.info("Bot is stopped. Start it to fetch executed deals.")
        return

    ensure_connection(bot.config.get("mt5", {}))
    c1, c2, c3 = st.columns(3)
    lookback_days = int(c1.number_input("Executed deals lookback (days)", min_value=1, max_value=365, value=7, step=1))
    ascending = c2.toggle("Ascending execution time", value=False)
    bot_deals_only = c3.toggle("Bot deals only (magic/GRID)", value=True)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    raw_deals = list(get_deals_history(start, end, symbol=bot.symbol))

    rows: list[dict[str, Any]] = []
    for deal in raw_deals:
        if not isinstance(deal, dict):
            continue
        comment = str(deal.get("comment") or "")
        d_magic = int(deal.get("magic", bot.magic) or bot.magic)
        if bot_deals_only and d_magic != bot.magic and not comment.startswith("GRID|"):
            continue

        exec_time = _deal_time_utc(deal)
        entry = str(deal.get("entryType") or deal.get("entry") or "").upper()
        side = str(deal.get("type") or "").upper()
        price = float(deal.get("price") or deal.get("brokerPrice") or 0.0)
        profit = float(deal.get("profit") or 0.0)
        volume = float(deal.get("volume") or 0.0)
        rows.append(
            {
                "execution_time": exec_time.isoformat() if exec_time else "",
                "entry_type": entry,
                "type": side,
                "volume": volume,
                "price": price,
                "profit": round(profit, 2),
                "position_id": str(deal.get("positionId") or deal.get("position_id") or ""),
                "order_id": str(deal.get("orderId") or deal.get("order_id") or ""),
                "comment": comment,
                "deal_id": str(deal.get("id") or deal.get("ticket") or ""),
            }
        )

    # Only closed positions with profit (exclude entry deals, pending fills, and loss closes)
    rows = [r for r in rows if "OUT" in r["entry_type"] and r["profit"] > 0]

    st.caption(f"Closed with profit: {len(rows)}")
    if not rows:
        st.info("No executed deals for selected period.")
        return

    frame = pd.DataFrame(rows)
    frame = frame.sort_values(by="execution_time", ascending=ascending, kind="stable")
    st.dataframe(frame, width="stretch", hide_index=True)


def _ensure_state() -> None:
    if "bot" not in st.session_state:
        st.session_state.bot = None
    if "bot_thread" not in st.session_state:
        st.session_state.bot_thread = None
    if "selected_position_tickets" not in st.session_state:
        st.session_state.selected_position_tickets = []


def _start_bot() -> None:
    if st.session_state.bot is not None and st.session_state.bot.running:
        return
    bot = HedgingGridBot(config_path=CONFIG_PATH)
    thread = threading.Thread(target=bot.run_forever, daemon=True, name="hedging-grid-bot")
    thread.start()
    st.session_state.bot = bot
    st.session_state.bot_thread = thread
    time.sleep(0.5)


def _stop_bot() -> None:
    bot = st.session_state.bot
    thread = st.session_state.bot_thread
    if bot is not None:
        bot.stop()
    if thread is not None and thread.is_alive():
        thread.join(timeout=5)
    st.session_state.bot_thread = None


def _render_status() -> None:
    bot = st.session_state.bot
    if bot is None:
        st.warning("Bot is not started.")
        return
    if bot.running:
        ensure_connection(bot.config.get("mt5", {}))
    status = bot.status()
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Connection", "Connected" if status.connected else "Disconnected")
    c2.metric("Imbalance", f"{status.current_imbalance:.2f}")
    c3.metric("Net Exposure", f"{status.net_exposure:.2f}")
    c4.metric("Equity", f"{status.latest_equity:.2f}")
    baseline = status.initial_balance if status.initial_balance > 0 else None
    pnl_from_baseline = (status.latest_balance - baseline) if baseline is not None else 0.0
    c5.metric("PnL from baseline", f"{pnl_from_baseline:+.2f}")
    if baseline is not None:
        st.caption(f"Baseline balance: {baseline:.2f}")
    st.caption(f"Last action: {status.last_action}")


def _render_positions() -> tuple[list[Any], list[int]]:
    bot = st.session_state.bot
    if bot is None:
        st.info("Start the bot to view live positions.")
        return [], []
    if not bot.running:
        st.info("Bot is stopped. Start it to fetch live positions.")
        return [], []
    ensure_connection(bot.config.get("mt5", {}))
    positions = get_positions(symbol=bot.symbol, magic=bot.magic)
    st.caption(f"Positions count: {len(positions)}")
    if not positions:
        st.info("No open positions.")
        st.session_state.selected_position_tickets = []
        return [], []

    selected_prev = {int(t) for t in st.session_state.get("selected_position_tickets", [])}
    frame = pd.DataFrame(
        [
            {
                "select": p.ticket in selected_prev,
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": p.side,
                "volume": p.volume,
                "profit": round(p.profit, 2),
                "entry": p.price_open,
                "current": p.price_current,
                "SL": p.sl,
                "TP": p.tp,
                "comment": p.comment,
            }
            for p in positions
        ]
    )
    edited = st.data_editor(
        frame,
        width="stretch",
        hide_index=True,
        key="positions_editor",
        disabled=["ticket", "symbol", "type", "volume", "profit", "entry", "current", "SL", "TP", "comment"],
        column_config={
            "select": st.column_config.CheckboxColumn("Select", help="Select position for manual TP/close"),
        },
    )
    selected = [int(t) for t in edited.loc[edited["select"] == True, "ticket"].tolist()]
    st.session_state.selected_position_tickets = selected
    st.caption(f"Selected positions: {len(selected)}")
    return positions, selected


def _render_pending_orders() -> None:
    bot = st.session_state.bot
    if bot is None:
        st.info("Start the bot to view pending orders.")
        return
    if not bot.running:
        st.info("Bot is stopped. Start it to fetch live pending orders.")
        return
    ensure_connection(bot.config.get("mt5", {}))
    pending_orders = get_pending_orders(symbol=bot.symbol, magic=bot.magic)
    st.caption(f"Pending orders count: {len(pending_orders)}")
    if not pending_orders:
        st.info("No pending orders.")
        return

    frame = pd.DataFrame(
        [
            {
                "ticket": o.ticket,
                "symbol": o.symbol,
                "type": o.side,
                "volume": o.volume_initial,
                "entry": o.price_open,
                "SL": o.sl,
                "TP": o.tp,
                "comment": o.comment,
            }
            for o in pending_orders
        ]
    )

    c1, c2 = st.columns(2)
    sort_by = c1.selectbox("Sort pending by", options=["entry", "SL", "TP", "type", "volume", "ticket"], index=0)
    ascending = c2.toggle("Ascending sort", value=True)
    frame = frame.sort_values(by=sort_by, ascending=ascending, kind="stable")
    st.dataframe(frame, width="stretch", hide_index=True)


def _render_equity_chart() -> None:
    bot = st.session_state.bot
    if bot is None:
        return
    points = bot.get_equity_curve()
    if not points:
        st.info("Equity history is empty.")
        return
    frame = pd.DataFrame(points)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=frame["time"], y=frame["balance"], mode="lines", name="Balance"))
    fig.add_trace(go.Scatter(x=frame["time"], y=frame["equity"], mode="lines", name="Equity"))
    fig.update_layout(margin=dict(l=10, r=10, t=30, b=10), height=350)
    st.plotly_chart(fig, config={"responsive": True})


def _render_manual_controls(selected_tickets: list[int]) -> None:
    bot = st.session_state.bot
    if bot is None:
        st.info("Start the bot to use manual controls.")
        return

    mode_c1, mode_c2 = st.columns([2, 1])
    mode_options = {"Auto TP": "auto", "Manual TP": "manual"}
    current_mode = bot.tp_mode()
    selected_mode_label = mode_c1.radio(
        "Take-Profit mode",
        options=list(mode_options.keys()),
        horizontal=True,
        index=0 if current_mode == "auto" else 1,
    )
    if mode_options[selected_mode_label] != current_mode:
        if bot.set_tp_mode(mode_options[selected_mode_label]):
            st.success(f"TP mode switched to {mode_options[selected_mode_label]}")
        else:
            st.error("Failed to switch TP mode")
    mode_c2.caption("Manual mode: bot does not auto-update TP.")

    c1, c2, c3 = st.columns(3)
    if c1.button("Close All", width="stretch"):
        closed, cancelled = bot.manual_close_all()
        _stop_bot()
        st.success(f"Closed positions: {closed}, cancelled pending: {cancelled}. Bot stopped.")
    with c2.popover("Close Selected Orders"):
        st.write(f"Selected positions: {len(selected_tickets)}")
        if not selected_tickets:
            st.info("Select one or more positions in table above.")
        else:
            st.warning("Confirm closing selected positions.")
            if st.button("Confirm Close Selected", width="stretch"):
                closed = bot.manual_close_selected(selected_tickets)
                st.success(f"Closed selected positions: {closed}")
                st.rerun()
    if c3.button("Reset Grid", width="stretch"):
        cancelled = bot.reset_grid()
        st.success(f"Cancelled pending orders: {cancelled}")

    st.markdown("#### Take Profit For Selected Positions")
    tp_c1, tp_c2 = st.columns(2)
    selected_tp = tp_c1.number_input("Selected TP price", min_value=0.0, value=0.0, step=0.0001, format="%.5f")
    if tp_c2.button("Apply TP To Selected", width="stretch"):
        if selected_tp <= 0:
            st.error("Set TP price > 0")
        elif not selected_tickets:
            st.error("Select one or more positions first.")
        else:
            updated = bot.manual_set_take_profit_selected(selected_tickets, selected_tp)
            st.success(f"Updated TP for selected positions: {updated}")


def _render_config_editor() -> None:
    st.subheader("Config Editor")
    cfg: dict[str, Any] = load_config(CONFIG_PATH)

    with st.form("config_form", clear_on_submit=False):
        grid_step_points = st.number_input(
            "grid_step_points",
            min_value=10,
            value=int(cfg["grid"].get("grid_step_points", 100)),
            step=10,
        )
        levels_text = st.text_input(
            "levels_manual (comma-separated)",
            value=", ".join(str(v) for v in cfg["levels"].get("levels_manual", [])),
        )
        lookback_days = st.number_input(
            "auto_levels_lookback_days",
            min_value=30,
            value=int(cfg["levels"]["auto_levels_lookback_days"]),
            step=1,
        )
        imbalance_threshold = st.number_input(
            "imbalance_threshold",
            min_value=1.0,
            value=float(cfg["closing"]["imbalance_threshold"]),
            step=1.0,
        )
        fixed_lot = st.number_input("fixed_lot", min_value=0.01, value=float(cfg["risk"]["fixed_lot"]), step=0.01)
        max_dd = st.number_input("max_drawdown_pct", min_value=1.0, max_value=99.0, value=float(cfg["risk"]["max_drawdown_pct"]), step=1.0)

        save_clicked = st.form_submit_button("Save & Reload")

    if save_clicked:
        parsed_levels = []
        for chunk in levels_text.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                parsed_levels.append(float(chunk))
            except ValueError:
                st.error(f"Invalid level: {chunk}")
                return

        cfg["grid"]["grid_step_points"] = int(grid_step_points)
        cfg["grid"]["grid_step_pips"] = float(grid_step_points) / 10.0
        cfg["levels"]["levels_manual"] = parsed_levels
        cfg["levels"]["auto_levels_lookback_days"] = int(lookback_days)
        cfg["closing"]["imbalance_threshold"] = imbalance_threshold
        cfg["risk"]["fixed_lot"] = fixed_lot
        cfg["risk"]["max_drawdown_pct"] = max_dd
        save_config(CONFIG_PATH, cfg)

        bot = st.session_state.bot
        if bot is not None:
            bot.reload_config()
        st.success("Config saved and reloaded.")


def _render_logs() -> None:
    bot = st.session_state.bot
    if bot is None:
        return
    logs = bot.get_action_logs(limit=30)
    st.text_area("Last action log", value="\n".join(logs), height=220)


def main() -> None:
    st.set_page_config(page_title="MT5 Hedging Grid Bot", layout="wide")
    st.title("MT5 Hedging Grid Bot Dashboard")
    _ensure_state()

    cfg = load_config(CONFIG_PATH)
    refresh_sec = int(cfg.get("trading", {}).get("dashboard_refresh_sec", 15))
    st_autorefresh(interval=max(refresh_sec, 5) * 1000, key="dashboard_refresh")

    c1, c2, c3 = st.columns(3)
    if c1.button("Start Bot", width="stretch"):
        _start_bot()
    if c2.button("Stop Bot", width="stretch"):
        _stop_bot()
    if c3.button("Refresh", width="stretch"):
        st.rerun()

    _render_status()
    st.divider()
    st.subheader("Open Positions")
    _, selected_tickets = _render_positions()
    st.divider()
    st.subheader("Pending Orders")
    _render_pending_orders()
    st.divider()
    st.subheader("Executed Deals")
    _render_executed_deals()
    st.divider()
    st.subheader("Balance / Equity")
    _render_equity_chart()
    st.divider()
    _render_manual_controls(selected_tickets)
    st.divider()
    _render_config_editor()
    st.divider()
    _render_logs()

    bot = st.session_state.bot
    if bot is not None:
        with st.expander("Raw status payload"):
            st.json(asdict(bot.status()))


if __name__ == "__main__":
    main()
