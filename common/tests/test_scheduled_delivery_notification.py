"""定时发货通过实际内部路由发送通知的回归测试（不触发真实发货）。"""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks, FastAPI
import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "websocket"))

from app.api.routes import internal


class ScheduledDeliveryNotificationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.request = internal.DeliverOrderRequest(
            account_id="seller-scheduled", order_no="order-1", item_id="item-1",
            buyer_id="buyer-1", chat_id="chat-1", card_id=1, delivery_method="scheduled",
        )
        self.result = {"success": True, "code": 200, "message": "发货成功（共 2 张）",
                       "data": {"quantity_sent": 2}}
        self.lock = asyncio.Lock()
        live = SimpleNamespace(_order_locks={"order-1": self.lock})
        self.channel = {"enabled": True, "channel_type": "bark", "channel_config": {
            "device_key": "test-only", "delivery_enabled": True,
            "delivery_template": "{{account_id}}|{{商品名称}}|{{buyer_nick}}|{{amount}}|{{quantity}}|{{order_id}}|{{result}}",
        }}
        patches = [
            ("app.services.xianyu.cookie_manager.get_manager", {"return_value": SimpleNamespace(instances={self.request.account_id: live})}),
            ("common.db.compat.db_manager.get_order_by_id", {"return_value": {
                "account_id": self.request.account_id, "buyer_fish_nick": "测试买家", "amount": "2.50", "quantity": 2,
            }}),
            ("common.db.compat.db_manager.get_item_info", {"return_value": {"item_title": "测试商品"}}),
            ("common.db.compat.db_manager.get_cookie_details", {"return_value": {}}),
            ("common.db.compat.db_manager.get_account_notifications", {"return_value": [self.channel]}),
            ("common.db.compat.db_manager.get_message_filter_keywords", {"return_value": []}),
        ]
        for target, kwargs in patches:
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        impl_patch = patch.object(internal, "_deliver_order_impl", new=AsyncMock(return_value=self.result))
        self.deliver = impl_patch.start()
        self.addCleanup(impl_patch.stop)
        sender_patch = patch("app.services.xianyu.notification_manager.send_bark_notification", new=AsyncMock(return_value=True))
        self.send = sender_patch.start()
        self.addCleanup(sender_patch.stop)
        self.app = FastAPI()
        self.app.include_router(internal.router)

    async def post_delivery(self, **changes):
        payload = self.request.model_copy(update=changes).model_dump()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
            response = await client.post("/internal/orders/deliver", json=payload)
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def test_scheduled_http_result_uses_delivery_template_and_order_context(self):
        async def send(config, message):
            self.assertFalse(self.lock.locked(), "通知发送时必须已经释放发货锁")
            return True

        self.send.side_effect = send
        self.assertEqual(await self.post_delivery(), self.result)
        self.send.assert_awaited_once()
        self.assertEqual(self.send.await_args.args[1],
            "seller-scheduled|测试商品|测试买家|¥2.50|2|order-1|定时发货：发货成功（共 2 张）")

    async def test_response_is_returned_before_notification_runs(self):
        tasks = BackgroundTasks()
        self.assertEqual(await internal.deliver_order(self.request, tasks), self.result)
        self.send.assert_not_awaited()
        self.assertFalse(self.lock.locked())
        await tasks()
        self.send.assert_awaited_once()

    async def test_failed_delivery_sends_failure_reason(self):
        self.result.update(success=False, message="卡券库存不足", data=None)
        self.assertEqual(await self.post_delivery(), self.result)
        self.assertIn("定时发货异常：卡券库存不足", self.send.await_args.args[1])

    async def test_card_only_partial_delivery_and_interception_keep_result_details(self):
        cases = [
            ({"only_send_card": True}, "只发卡券成功：已发送卡券内容，平台订单保持待发货", "定时发货："),
            ({"quantity_sent": 1, "quantity_requested": 2}, "仅发出 1/2 张，剩余 1 张请手动补发", "定时发货："),
            ({"send_intercepted": True}, "卡券疑似被平台风控拦截，请人工核实", "定时发货异常："),
            ({"send_failed_count": 1}, "共 1 张消息发送失败，请手动补发", "定时发货异常："),
        ]
        for data, message, prefix in cases:
            with self.subTest(data=data):
                self.send.reset_mock()
                self.result.update(data=data, message=message)
                await self.post_delivery()
                self.send.assert_awaited_once()
                self.assertIn(prefix + message, self.send.await_args.args[1])

    async def test_disabled_delivery_type_channel_and_message_filter_are_respected(self):
        self.channel["channel_config"]["delivery_enabled"] = False
        await self.post_delivery()
        self.send.assert_not_awaited()
        self.channel["channel_config"]["delivery_enabled"] = True
        self.channel["enabled"] = False
        await self.post_delivery()
        self.send.assert_not_awaited()
        self.channel["enabled"] = True
        with patch("common.db.compat.db_manager.get_message_filter_keywords", return_value=["发货成功"]):
            await self.post_delivery()
        self.send.assert_not_awaited()

    async def test_idempotent_returns_and_busy_responses_do_not_notify_again(self):
        for data, code in [({"already_shipped": True}, 200), ({"already_card_only_delivered": True}, 200), ({}, 409)]:
            with self.subTest(data=data, code=code):
                self.result.update(data=data, code=code)
                await self.post_delivery()
                self.send.assert_not_awaited()

    async def test_lock_timeout_does_not_deliver_or_notify(self):
        def timeout(coroutine, **kwargs):
            coroutine.close()
            raise asyncio.TimeoutError

        tasks = BackgroundTasks()
        with patch.object(internal.asyncio, "wait_for", side_effect=timeout):
            result = await internal.deliver_order(self.request, tasks)
        self.assertEqual(result["code"], 409)
        self.deliver.assert_not_awaited()
        await tasks()
        self.send.assert_not_awaited()

    async def test_existing_auto_and_manual_paths_do_not_gain_duplicate_notifications(self):
        for method in ("auto", "manual"):
            with self.subTest(method=method):
                await self.post_delivery(delivery_method=method)
                self.send.assert_not_awaited()

    async def test_notification_error_does_not_change_successful_delivery(self):
        with patch("app.services.xianyu.notification_manager.NotificationManager.send_delivery_failure_notification",
                   new=AsyncMock(side_effect=RuntimeError("通知服务超时"))):
            self.assertEqual(await self.post_delivery(), self.result)
        self.assertFalse(self.lock.locked())


if __name__ == "__main__":
    unittest.main()
