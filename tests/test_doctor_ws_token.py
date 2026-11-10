"""Doctor market-WS probe must prefer live tokens over redeemable inventory."""

from polymaker.doctor import _select_ws_probe_token


def test_select_ws_probe_prefers_live_over_held() -> None:
    held = ["redeemable-token-aaa", "redeemable-token-bbb"]
    live = "active-political-token"
    assert _select_ws_probe_token(held, live) == live


def test_select_ws_probe_falls_back_to_held_when_no_live() -> None:
    held = ["only-held-token"]
    assert _select_ws_probe_token(held, None) == "only-held-token"


def test_select_ws_probe_none_when_empty() -> None:
    assert _select_ws_probe_token([], None) is None
