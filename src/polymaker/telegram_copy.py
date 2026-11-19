"""Chinese user-facing copy helpers for Telegram status/notify (display-only)."""

from __future__ import annotations

from typing import Any, Literal

# Exchange-confirmed resting exit SELL state. Never treat "unknown" as retained.
ExitOrderStatus = Literal["resting", "none", "unknown"]


def fmt_money_pusd(value: float | None, *, digits: int = 6) -> str:
    """Format pUSD; None → 未知/读取失败 (never fake 0)."""
    if value is None:
        return "未知/读取失败"
    return f"{value:.{digits}f} pUSD"


def fmt_bool_zh(flag: bool) -> str:
    return "是" if flag else "否"


def fmt_int_or_unknown(value: int | None) -> str:
    if value is None:
        return "未知/读取失败"
    return str(value)


def fmt_exit_order_status_zh(
    status: ExitOrderStatus | None,
    *,
    sell_count: int | None = None,
) -> str:
    """Chinese line for exchange exit-order confirmation (not strategy intent)."""
    if status is None:
        return "未知"
    if status == "resting":
        n = sell_count if sell_count is not None else 0
        return f"有(SELL={n})"
    if status == "none":
        return "无"
    return "未知（不视为仍保留）"


def format_status_report(
    *,
    markets: int,
    open_orders: int,
    balance_clob: float | None,
    resting_buy: float,
    position_mark: float | None,
    fills: int | None,
    session_equity: float | None,
    session_daily_delta: float | None,
    stop_requested: bool,
    positions: dict[str, Any] | None,
    entry_blocked_infeasible: bool | None = None,
    one_sided_inventory: bool | None = None,
    exit_quoting_allowed: bool | None = None,
    exit_order_status: ExitOrderStatus | None = None,
    exit_sell_count: int | None = None,
    # Legacy alias: bool True/False maps to resting/none; None stays unknown.
    exit_path_retained: bool | None = None,
    daily_loss_latched: bool | None = None,
    recovery_blocked: bool | None = None,
    active_markets: str | None = None,
) -> str:
    """Compact Chinese /status: key numbers only, clear exit口径."""
    # Compat: older callers may still pass exit_path_retained.
    if exit_order_status is None and exit_path_retained is not None:
        exit_order_status = "resting" if exit_path_retained else "none"
    if exit_quoting_allowed is None and exit_path_retained is not None:
        exit_quoting_allowed = bool(exit_path_retained)

    if session_equity is None:
        equity_s = "未知/读取失败"
    else:
        equity_s = f"{session_equity:.4f} pUSD"
    if fills == 0:
        daily_s = "暂不可用（无成交）"
    elif session_daily_delta is None:
        daily_s = "未知/读取失败"
    else:
        sign = "+" if session_daily_delta >= 0 else ""
        daily_s = f"{sign}{session_daily_delta:.4f} pUSD"

    if one_sided_inventory is None and positions:
        held = [
            tok
            for tok, pos in positions.items()
            if float((pos or {}).get("size") or 0.0) > 0
        ]
        one_sided_inventory = len(held) == 1

    def _opt_bool(v: bool | None) -> str:
        return fmt_bool_zh(v) if v is not None else "未知"

    lines = [
        f"市场 {markets} | 挂单 {open_orders}",
    ]
    if active_markets:
        lines.append(active_markets[:180])
    lines += [
        f"余额 {fmt_money_pusd(balance_clob, digits=6)} | 买单占用 {fmt_money_pusd(resting_buy, digits=6)}",
        f"标记市值 {fmt_money_pusd(position_mark, digits=4)} | 成交 {fmt_int_or_unknown(fills)}",
        f"会话权益 {equity_s} | 当日变动 {daily_s}",
        f"暂停新下单：{fmt_bool_zh(stop_requested)}"
        f" | 日亏锁：{_opt_bool(daily_loss_latched)}"
        f" | 恢复阻断：{_opt_bool(recovery_blocked)}",
    ]
    if not positions:
        lines.append("本地仓：无")
    else:
        for tok, p in list(positions.items())[:8]:
            size = p.get("size")
            avg = p.get("avg_price")
            size_s = f"{float(size):.4f}" if size is not None else "?"
            avg_s = f"{float(avg):.4f}" if avg is not None else "?"
            lines.append(f"仓 {str(tok)[:12]}… {size_s}@{avg_s}")
    lines.append(
        f"入场受阻：{_opt_bool(entry_blocked_infeasible)}"
        f" | 单边库存：{_opt_bool(one_sided_inventory)}"
    )
    lines.append(
        f"退出报价：{_opt_bool(exit_quoting_allowed)}"
        f" | 交易所卖单：{fmt_exit_order_status_zh(exit_order_status, sell_count=exit_sell_count)}"
    )
    return "\n".join(lines)


def format_pnl_report(
    *,
    session_equity: float | None,
    net_cash: float | None,
    inventory: float | None,
    session_daily_delta: float | None,
    fills: int | None,
) -> str:
    """Compact Chinese /pnl — session ledger only, never claim 账户总资产."""
    if session_equity is None:
        equity_s = "未知/读取失败"
    else:
        equity_s = f"{session_equity:.4f} pUSD"
    if net_cash is None:
        cash_s = "未知/读取失败"
    else:
        cash_s = f"{net_cash:.4f} pUSD"
    if inventory is None:
        inv_s = "未知/读取失败"
    else:
        inv_s = f"{inventory:.4f} pUSD"
    if fills == 0:
        daily_s = "暂不可用（无成交）"
    elif session_daily_delta is None:
        daily_s = "未知/读取失败"
    else:
        sign = "+" if session_daily_delta >= 0 else ""
        daily_s = f"{sign}{session_daily_delta:.4f} pUSD"
    return "\n".join(
        [
            "会话记账（非账户总资产）",
            f"权益 {equity_s}",
            f"净现金 {cash_s} | 库存 {inv_s}",
            f"当日变动 {daily_s}",
        ]
    )
