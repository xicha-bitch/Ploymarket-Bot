"""Chinese user-facing copy helpers for Telegram status/notify (display-only)."""

from __future__ import annotations

from typing import Any, Literal

# Exchange-confirmed resting exit SELL state. Never treat "unknown" as retained.
ExitOrderStatus = Literal["resting", "none", "unknown"]


def fmt_money_pusd(value: float | None, *, digits: int = 6) -> str:
    """Format pUSD; None → 读取失败文案 (never fake 0)."""
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
        return "未知/未提供"
    if status == "resting":
        n = sell_count if sell_count is not None else 0
        return f"已确认有挂单（SELL={n}）"
    if status == "none":
        return "已确认无"
    return "未知（读取失败；不视为仍保留）"


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
) -> str:
    """Build Chinese /status body with real newlines and clear 口径 labels."""
    # Compat: older callers may still pass exit_path_retained.
    if exit_order_status is None and exit_path_retained is not None:
        exit_order_status = "resting" if exit_path_retained else "none"
    if exit_quoting_allowed is None and exit_path_retained is not None:
        exit_quoting_allowed = bool(exit_path_retained)

    lines = [
        "【账户与挂单】",
        f"监控市场数：{markets}",
        f"当前挂单数：{open_orders}",
        f"CLOB 可用余额：{fmt_money_pusd(balance_clob, digits=6)}",
        f"挂单占用（买单名义）：{fmt_money_pusd(resting_buy, digits=6)}",
        "",
        "【仓位】",
        f"仓位标记市值（Data API）：{fmt_money_pusd(position_mark, digits=4)}",
        "说明：标记市值来自持仓索引，可能含历史可赎回仓；≠新买预算。",
        f"本进程记录成交笔数：{fmt_int_or_unknown(fills)}",
        "",
        "【会话记账（非正式账户总资产）】",
    ]
    if session_equity is None:
        lines.append("会话记账权益：未知/读取失败")
    else:
        lines.append(
            f"会话记账权益：{session_equity:.4f} pUSD"
            "（成交现金+本地跟踪库存市值；不是交易所账户总资产）"
        )
    if fills == 0:
        lines.append("会话当日变动：暂不可用（本进程尚无成交，不能解读为已盈利）")
    elif session_daily_delta is None:
        lines.append("会话当日变动：未知/读取失败")
    else:
        sign = "+" if session_daily_delta >= 0 else ""
        lines.append(
            f"会话当日变动：{sign}{session_daily_delta:.4f} pUSD"
            "（相对本进程日切点；勿当作账户总盈亏）"
        )
    lines.extend(
        [
            f"已请求暂停新下单：{fmt_bool_zh(stop_requested)}",
            "",
            "【本地跟踪仓位】",
        ]
    )
    if not positions:
        lines.append("无（本地跟踪为空）")
    else:
        lines.append("以下为本地状态机跟踪，可能与索引仓不一致；成交笔数=0 时勿当作本轮买入。")
        for tok, p in list(positions.items())[:8]:
            size = p.get("size")
            avg = p.get("avg_price")
            size_s = f"{float(size):.4f} 份" if size is not None else "未知/读取失败"
            avg_s = f"{float(avg):.4f} pUSD/份" if avg is not None else "未知/读取失败"
            lines.append(f"· {str(tok)[:12]}… 数量={size_s} 均价={avg_s}")
    if one_sided_inventory is None and positions:
        held = [
            tok
            for tok, pos in positions.items()
            if float((pos or {}).get("size") or 0.0) > 0
        ]
        one_sided_inventory = len(held) == 1
    lines.extend(
        [
            "",
            "【做市防护与可行性】",
            f"· 不可行入场被阻止：{fmt_bool_zh(entry_blocked_infeasible) if entry_blocked_infeasible is not None else '未知/未提供'}（最小下单名义超过可用预算时跳过新买单，避免反复拒单刷屏）",
            f"· 单边库存：{fmt_bool_zh(one_sided_inventory) if one_sided_inventory is not None else '未知/未提供'}"
            + ("（仅持有单侧代币）" if one_sided_inventory else ""),
            f"· 退出报价允许：{fmt_bool_zh(exit_quoting_allowed) if exit_quoting_allowed is not None else '未知/未提供'}"
            "（代码允许对已有持仓挂被动卖单/减仓；≠交易所已有挂单）",
            f"· 交易所退出卖单：{fmt_exit_order_status_zh(exit_order_status, sell_count=exit_sell_count)}"
            "（三态：有挂单 / 无 / 未知；未知不视为仍保留）",
        ]
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
    """Chinese /pnl — session ledger only, never claim 账户总资产/已盈利."""
    lines = [
        "【会话记账 PnL】",
        "口径：本进程风险模块记账，不是交易所账户总资产。",
    ]
    if session_equity is None:
        lines.append("会话记账权益：未知/读取失败")
    else:
        lines.append(f"会话记账权益：{session_equity:.4f} pUSD")
    if net_cash is None:
        lines.append("净现金（会话）：未知/读取失败")
    else:
        lines.append(f"净现金（会话）：{net_cash:.4f} pUSD")
    if inventory is None:
        lines.append("跟踪库存市值：未知/读取失败")
    else:
        lines.append(f"跟踪库存市值：{inventory:.4f} pUSD")
    if fills == 0:
        lines.append("会话当日变动：暂不可用（无本进程成交）")
    elif session_daily_delta is None:
        lines.append("会话当日变动：未知/读取失败")
    else:
        sign = "+" if session_daily_delta >= 0 else ""
        lines.append(f"会话当日变动：{sign}{session_daily_delta:.4f} pUSD")
    return "\n".join(lines)
