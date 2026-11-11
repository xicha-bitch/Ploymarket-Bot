"""Chinese Telegram status copy: compact, fail≠0, exit口径 clear."""

from __future__ import annotations

from polymaker.telegram_copy import (
    format_pnl_report,
    format_status_report,
    fmt_exit_order_status_zh,
    fmt_money_pusd,
)


def test_fmt_money_none_is_unknown_not_zero() -> None:
    assert fmt_money_pusd(None) == "未知/读取失败"
    assert "0" not in fmt_money_pusd(None)
    assert "pUSD" in fmt_money_pusd(1.004087)


def test_status_uses_real_newlines_and_chinese_labels() -> None:
    text = format_status_report(
        markets=1,
        open_orders=1,
        balance_clob=0.529557,
        resting_buy=0.24,
        position_mark=0.4795,
        fills=0,
        session_equity=0.4940,
        session_daily_delta=0.4940,
        stop_requested=False,
        positions={"abc1234567890": {"size": 9.99, "avg_price": 0.048}},
        daily_loss_latched=False,
        recovery_blocked=False,
    )
    assert "\\n" not in text
    assert "\n" in text
    assert "余额" in text and "0.529557" in text
    assert "买单占用" in text
    assert "标记市值" in text
    assert "成交 0" in text
    assert "当日变动 暂不可用（无成交）" in text
    assert "会话权益 0.4940 pUSD" in text
    assert "【账户总资产】" not in text
    assert "仓 abc123456789…" in text
    assert "9.9900@0.0480" in text
    assert "暂停新下单：否" in text
    assert "日亏锁：否" in text
    assert "恢复阻断：否" in text
    # Compact: few lines, no long jargon dumps
    assert text.count("\n") <= 8


def test_status_api_fail_unknown() -> None:
    text = format_status_report(
        markets=0,
        open_orders=0,
        balance_clob=None,
        resting_buy=0.0,
        position_mark=None,
        fills=None,
        session_equity=None,
        session_daily_delta=None,
        stop_requested=True,
        positions=None,
    )
    assert text.count("未知/读取失败") >= 3
    assert "暂停新下单：是" in text


def test_pnl_hides_delta_when_no_fills() -> None:
    text = format_pnl_report(
        session_equity=0.5,
        net_cash=0.0,
        inventory=0.5,
        session_daily_delta=0.5,
        fills=0,
    )
    assert "暂不可用" in text
    assert "非账户总资产" in text
    assert "【账户总资产】" not in text


def test_status_report_distinguishes_exit_quoting_vs_exchange_orders() -> None:
    text = format_status_report(
        markets=1,
        open_orders=1,
        balance_clob=0.5,
        resting_buy=0.0,
        position_mark=0.5,
        fills=0,
        session_equity=0.5,
        session_daily_delta=0.0,
        stop_requested=False,
        positions={"yes-token": {"size": 10.0, "avg_price": 0.05}},
        entry_blocked_infeasible=True,
        one_sided_inventory=True,
        exit_quoting_allowed=True,
        exit_order_status="resting",
        exit_sell_count=1,
    )
    assert "入场受阻：是" in text
    assert "单边库存：是" in text
    assert "退出报价：是" in text
    assert "交易所卖单：有(SELL=1)" in text
    assert "退出路径仍保留：是" not in text  # no longer hardcoded bool success
    assert "【账户总资产】" not in text


def test_exit_order_unknown_never_reads_as_retained() -> None:
    assert "不视为仍保留" in fmt_exit_order_status_zh("unknown")
    text = format_status_report(
        markets=1,
        open_orders=0,
        balance_clob=0.5,
        resting_buy=0.0,
        position_mark=0.5,
        fills=0,
        session_equity=0.5,
        session_daily_delta=0.0,
        stop_requested=False,
        positions={"yes-token": {"size": 10.0, "avg_price": 0.05}},
        exit_quoting_allowed=True,
        exit_order_status="unknown",
    )
    assert "退出报价：是" in text
    assert "未知（不视为仍保留）" in text
    assert "有(SELL=" not in text


def test_exit_order_confirmed_none() -> None:
    text = format_status_report(
        markets=1,
        open_orders=0,
        balance_clob=0.5,
        resting_buy=0.0,
        position_mark=0.5,
        fills=0,
        session_equity=0.5,
        session_daily_delta=0.0,
        stop_requested=False,
        positions={"yes-token": {"size": 10.0, "avg_price": 0.05}},
        exit_quoting_allowed=True,
        exit_order_status="none",
        exit_sell_count=0,
    )
    assert "交易所卖单：无" in text
