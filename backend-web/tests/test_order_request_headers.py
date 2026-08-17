from __future__ import annotations

import pytest

from common.services import order_service as order_service_module
from common.services.order_service import OrderService


class _Response:
    headers: dict[str, str] = {}

    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self) -> dict:
        return self._body


class _PostContext:
    def __init__(self, response: _Response) -> None:
        self._response = response

    async def __aenter__(self) -> _Response:
        return self._response

    async def __aexit__(self, *_: object) -> None:
        return None


def _mock_client_session(monkeypatch: pytest.MonkeyPatch, body: dict) -> dict:
    captured: dict = {}

    class _ClientSession:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> "_ClientSession":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        def post(self, url: str, **kwargs: object) -> _PostContext:
            captured["url"] = url
            captured.update(kwargs)
            return _PostContext(_Response(body))

    monkeypatch.setattr(order_service_module.aiohttp, "ClientSession", _ClientSession)
    monkeypatch.setattr(order_service_module, "get_goofish_connector", lambda: object())
    return captured


@pytest.mark.asyncio
async def test_sold_orders_request_omits_commonpro_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _mock_client_session(
        monkeypatch,
        {"ret": ["SUCCESS::调用成功"], "data": {"module": {"items": [], "nextPage": "false", "totalCount": "0"}}},
    )

    result = await OrderService(None)._fetch_sold_orders_page("_m_h5_tk=test_1", page=1)

    assert result is not None
    assert "idle_site_biz_code" not in captured["headers"]


@pytest.mark.asyncio
async def test_refund_orders_request_omits_commonpro_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _mock_client_session(
        monkeypatch,
        {"ret": ["SUCCESS::调用成功"], "data": {"data": {"items": []}}},
    )

    result = await OrderService(None)._fetch_refund_orders_page("_m_h5_tk=test_1", dispute_status="1")

    assert result is not None
    assert "idle_site_biz_code" not in captured["headers"]
