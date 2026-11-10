"""Chinese Telegram status copy: labels, units, fail≠0, no fake profit."""

from __future__ import annotations

from polymaker.telegram_copy import (
    format_pnl_report,
    format_status_report,
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
    )
    assert "\\n" not in text
    assert "\n" in text
    assert "CLOB 可用余额" in text
    assert "挂单占用" in text
    assert "仓位标记市值" in text
    assert "本进程记录成交笔数：0" in text
    assert "会话当日变动：暂不可用" in text
    assert "不能解读为已盈利" in text
    assert "不是交易所账户总资产" in text
    assert "【账户总资产】" not in text
    assert "数量=" in text and "份" in text
    assert "均价=" in text and "pUSD/份" in text
    assert "已请求暂停新下单：否" in text


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
    assert "已请求暂停新下单：是" in text


def test_pnl_hides_delta_when_no_fills() -> None:
    text = format_pnl_report(
        session_equity=0.5,
        net_cash=0.0,
        inventory=0.5,
        session_daily_delta=0.5,
        fills=0,
    )
    assert "暂不可用" in text
    assert "不是交易所账户总资产" in text
    assert "【账户总资产】" not in text


def test_status_report_includes_protection_and_exit_channel_copy() -> None:
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
        entry_blocked_infeasible=True,
        one_sided_inventory=True,
        exit_path_retained=True,
    )
    assert "不可行入场被阻止：是" in text
    assert "单边库存：是" in text
    assert "退出路径仍保留：是" in text
    assert "【账户总资产】" not in text
