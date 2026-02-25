"""Streamlit dashboard for live/semi-live control of hedging grid bot."""

from __future__ import annotations

import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from main_bot import HedgingGridBot, load_config, save_config
from mt5_utils import get_pending_orders, get_positions


CONFIG_PATH = Path("config.yaml")


def _ensure_state() -> None:
    if "bot" not in st.session_state:
        st.session_state.bot = None
    if "bot_thread" not in st.session_state:
        st.session_state.bot_thread = None


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
    status = bot.status()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Connection", "Connected" if status.connected else "Disconnected")
    c2.metric("Imbalance", f"{status.current_imbalance:.2f}")
    c3.metric("Net Exposure", f"{status.net_exposure:.2f}")
    c4.metric("Equity", f"{status.latest_equity:.2f}")
    st.caption(f"Last action: {status.last_action}")


def _render_positions() -> None:
    bot = st.session_state.bot
    if bot is None:
        st.info("Start the bot to view live positions.")
        return
    positions = get_positions(symbol=bot.symbol, magic=bot.magic)
    st.caption(f"Positions count: {len(positions)}")
    if not positions:
        st.info("No open positions.")
        return
    frame = pd.DataFrame(
        [
            {
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
    st.dataframe(frame, use_container_width=True, hide_index=True)


def _render_pending_orders() -> None:
    bot = st.session_state.bot
    if bot is None:
        st.info("Start the bot to view pending orders.")
        return
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
    st.dataframe(frame, use_container_width=True, hide_index=True)


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
    st.plotly_chart(fig, use_container_width=True)


def _render_manual_controls() -> None:
    bot = st.session_state.bot
    if bot is None:
        st.info("Start the bot to use manual controls.")
        return
    c1, c2, c3, c4 = st.columns(4)
    if c1.button("Close All", use_container_width=True):
        closed, cancelled = bot.manual_close_all()
        st.success(f"Closed positions: {closed}, cancelled pending: {cancelled}")
    if c2.button("Close Profitable Buys", use_container_width=True):
        closed = bot.manual_close_profitable("BUY")
        st.success(f"Closed BUY positions: {closed}")
    if c3.button("Close Profitable Sells", use_container_width=True):
        closed = bot.manual_close_profitable("SELL")
        st.success(f"Closed SELL positions: {closed}")
    if c4.button("Reset Grid", use_container_width=True):
        cancelled = bot.reset_grid()
        st.success(f"Cancelled pending orders: {cancelled}")

    st.markdown("#### Take Profit For Open Positions")
    tp_c1, tp_c2, tp_c3, tp_c4 = st.columns(4)
    buy_tp = tp_c1.number_input("BUY TP price", min_value=0.0, value=0.0, step=0.0001, format="%.5f")
    sell_tp = tp_c2.number_input("SELL TP price", min_value=0.0, value=0.0, step=0.0001, format="%.5f")
    if tp_c3.button("Apply BUY TP", use_container_width=True):
        if buy_tp <= 0:
            st.error("Set BUY TP price > 0")
        else:
            updated = bot.manual_set_take_profit("BUY", buy_tp)
            st.success(f"Updated BUY TP for positions: {updated}")
    if tp_c4.button("Apply SELL TP", use_container_width=True):
        if sell_tp <= 0:
            st.error("Set SELL TP price > 0")
        else:
            updated = bot.manual_set_take_profit("SELL", sell_tp)
            st.success(f"Updated SELL TP for positions: {updated}")


def _render_config_editor() -> None:
    st.subheader("Config Editor")
    cfg: dict[str, Any] = load_config(CONFIG_PATH)

    with st.form("config_form", clear_on_submit=False):
        grid_step = st.number_input("grid_step_pips", min_value=1.0, value=float(cfg["grid"]["grid_step_pips"]), step=1.0)
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

        cfg["grid"]["grid_step_pips"] = grid_step
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

    c1, c2 = st.columns(2)
    if c1.button("Start Bot", use_container_width=True):
        _start_bot()
    if c2.button("Stop Bot", use_container_width=True):
        _stop_bot()

    _render_status()
    st.divider()
    st.subheader("Open Positions")
    _render_positions()
    st.divider()
    st.subheader("Pending Orders")
    _render_pending_orders()
    st.divider()
    st.subheader("Balance / Equity")
    _render_equity_chart()
    st.divider()
    _render_manual_controls()
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
