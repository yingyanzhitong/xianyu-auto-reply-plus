from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from app.api.routes.cookies import _build_account_health
from common.utils.time_utils import get_beijing_now_naive


def _account(**overrides):
    values = {
        "status": "active",
        "unb": "user-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _token_cache(expire_offset_hours: int, renew_offset_hours: int | None = None):
    now = get_beijing_now_naive()
    return SimpleNamespace(
        expire_at=now + timedelta(hours=expire_offset_hours),
        renew_expire_at=(now + timedelta(hours=renew_offset_hours)) if renew_offset_hours is not None else None,
    )


def test_expired_token_marks_online_account_as_business_blocked():
    health = _build_account_health(
        _account(),
        is_online=True,
        token_cache=_token_cache(-1, -1),
        has_processing_risk=True,
    )

    assert health["overall"]["status"] == "degraded"
    assert health["token"]["status"] == "blocked"
    assert health["risk_control"]["status"] == "blocked"


def test_valid_token_marks_connection_as_ready():
    health = _build_account_health(
        _account(),
        is_online=True,
        token_cache=_token_cache(1),
        has_processing_risk=False,
    )

    assert health["overall"]["status"] == "ready"
    assert health["connection"]["status"] == "ready"


def test_order_permission_error_marks_online_account_as_business_blocked():
    health = _build_account_health(
        _account(metadata_json={"order_sync_permission": "denied"}),
        is_online=True,
        token_cache=_token_cache(1),
        has_processing_risk=False,
    )

    assert health["overall"] == {
        "status": "degraded",
        "label": "连接在线，业务受阻",
        "detail": "订单同步接口无权限",
    }
