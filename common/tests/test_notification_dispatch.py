import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "websocket"))

from app.services.xianyu.notification_manager import NotificationManager  # noqa: E402
from app.services.xianyu.auto_reply_service import AutoReplyService  # noqa: E402


class NotificationDispatchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 通知回归测试只使用固定商品缓存，不访问真实数据库。
        item_patch = patch("common.db.compat.db_manager.get_item_info", return_value={"item_title": "测试商品"})
        self.item_lookup = item_patch.start()
        self.addCleanup(item_patch.stop)

    async def test_type_switch_only_suppresses_selected_type(self):
        manager = NotificationManager("seller")
        channels = [{"enabled": True, "channel_type": "bark", "channel_config": {"chat_enabled": False}}]
        with patch("app.services.xianyu.notification_manager.send_bark_notification", new=AsyncMock(return_value=True)) as send:
            await manager._send_to_channels(channels, "聊天", template_type="chat")
            send.assert_not_awaited()
            await manager._send_to_channels(channels, "发货", template_type="delivery")
            send.assert_awaited_once()
        for template_type in ("delivery", "account"):
            channels[0]["channel_config"] = {f"{template_type}_enabled": False}
            with patch("app.services.xianyu.notification_manager.send_bark_notification", new=AsyncMock()) as send:
                await manager._send_to_channels(channels, "关闭通知", template_type=template_type)
                send.assert_not_awaited()

    async def test_auto_reply_honors_chat_switch_and_resolves_item_name(self):
        service = object.__new__(AutoReplyService)
        service.cookie_id = "seller"
        config = {"chat_enabled": False, "chat_template": "{{商品名称}}|{{item_name}}"}
        with patch("common.db.compat.db_manager.get_account_notifications", return_value=[
            {"enabled": True, "channel_type": "bark", "channel_config": config}
        ]), patch("common.db.compat.db_manager.get_cookie_details", return_value={}), patch(
            "common.utils.notification_utils.send_bark_notification", new=AsyncMock(return_value=True)
        ) as send:
            await service._send_notification("买家", "buyer", "你好", "chat", "item", "时间")
            send.assert_not_awaited()
            config["chat_enabled"] = True
            await service._send_notification("买家", "buyer", "你好", "chat", "item", "时间")
            self.assertEqual(send.await_args.args[1], "测试商品|测试商品")
        self.item_lookup.assert_called_with("seller", "item")

    async def test_channels_render_their_own_chat_templates(self):
        manager = NotificationManager("seller")
        notifications = [
            {
                "enabled": True,
                "channel_type": "bark",
                "channel_config": {"device_key": "a", "chat_template": "买家={{buyer_nick}}"},
            },
            {
                "enabled": True,
                "channel_type": "feishu",
                "channel_config": {"webhook_url": "https://example.com", "chat_template": "消息={{message}}"},
            },
        ]

        with patch(
            "app.services.xianyu.notification_manager.send_bark_notification",
            new=AsyncMock(return_value=True),
        ) as bark_send, patch(
            "app.services.xianyu.notification_manager.send_feishu_notification",
            new=AsyncMock(return_value=True),
        ) as feishu_send:
            await manager._send_to_channels(
                notifications,
                "系统默认正文",
                template_type="chat",
                template_context={"buyer_nick": "小王", "message": "你好"},
            )

        self.assertEqual(bark_send.await_args.args[1], "买家=小王")
        self.assertEqual(feishu_send.await_args.args[1], "消息=你好")

    async def test_delivery_template_uses_order_nickname_amount_and_quantity(self):
        manager = NotificationManager("seller")
        with patch("common.db.compat.db_manager.get_message_filter_keywords", return_value=[]), patch(
            "common.db.compat.db_manager.get_account_notifications",
            return_value=[
                {
                    "enabled": True,
                    "channel_type": "bark",
                    "channel_config": {
                        "device_key": "a",
                        "delivery_template": "{{buyer_nick}}|{{amount}}|{{quantity}}|{{result}}|{{商品名称}}",
                    },
                }
            ],
        ), patch(
            "common.db.compat.db_manager.get_cookie_details",
            return_value={"remark": "主账号"},
        ), patch(
            "common.db.compat.db_manager.get_order_by_id",
            return_value={"buyer_fish_nick": "金鱼小姐21", "amount": "2", "quantity": 1},
        ), patch(
            "app.services.xianyu.notification_manager.send_bark_notification",
            new=AsyncMock(return_value=True),
        ) as bark_send:
            await manager.send_delivery_failure_notification(
                "等待你发货",
                "342188141",
                "1071626525089",
                "发货成功",
                "chat-1",
                order_id="order-1",
            )

        self.assertEqual(bark_send.await_args.args[1], "金鱼小姐21|¥2.00|1|发货成功|测试商品")

    async def test_auto_reply_service_uses_chat_template(self):
        service = object.__new__(AutoReplyService)
        service.cookie_id = "seller"
        with patch(
            "common.db.compat.db_manager.get_account_notifications",
            return_value=[
                {
                    "enabled": True,
                    "channel_type": "bark",
                    "channel_config": {"device_key": "a", "chat_template": "{{buyer_nick}}: {{message}}|{{reply}}|{{ai_reply}}|{{ai_reply_status}}"},
                }
            ],
        ), patch(
            "common.db.compat.db_manager.get_cookie_details",
            return_value={"remark": "主账号"},
        ), patch(
            "common.utils.notification_utils.send_bark_notification",
            new=AsyncMock(return_value=True),
        ) as bark_send:
            await service._send_notification(
                "小王", "buyer-1", "你好", "chat-1", "item-1", "2026-08-11 11:00:00",
                reply="您好，请问有什么可以帮您？",
                ai_reply="您好，请问有什么可以帮您？",
                ai_reply_status="已禁止（买家已下单）",
            )

        self.assertEqual(
            bark_send.await_args.args[1],
            "小王: 你好|您好，请问有什么可以帮您？|您好，请问有什么可以帮您？|已禁止（买家已下单）",
        )

    def test_ai_reply_status_describes_prohibited_reasons(self):
        self.assertEqual(
            AutoReplyService._get_ai_reply_status({"context_snapshot": {"ai_blocked_reason": "ordered_user"}}),
            "已禁止（买家已下单）",
        )
        self.assertEqual(
            AutoReplyService._get_ai_reply_status({"context_snapshot": {"ai_blocked_reason": "ordered_item"}}),
            "已禁止（买家已下单当前商品）",
        )
        self.assertEqual(
            AutoReplyService._get_ai_reply_status(
                {
                    "context_snapshot": {
                        "ai_blocked_reason": "manual_reply_pause",
                        "manual_reply_ai_pause_ends_at": "2026-08-13 12:00:00",
                    }
                }
            ),
            "已暂停（人工回复介入，恢复时间：2026-08-13 12:00:00）",
        )

    async def test_account_template_receives_verification_link(self):
        manager = NotificationManager("seller-account-template")
        with patch(
            "common.db.compat.db_manager.get_account_notifications",
            return_value=[
                {
                    "enabled": True,
                    "channel_type": "email",
                    "channel_config": {"account_template": "{{title}}|{{detail}}|{{verification_url}}"},
                }
            ],
        ), patch(
            "common.db.compat.db_manager.get_cookie_details",
            return_value={"remark": "主账号"},
        ), patch(
            "app.services.xianyu.notification_manager.send_email_notification",
            new=AsyncMock(return_value=True),
        ) as email_send:
            await manager.send_token_refresh_notification(
                "请完成人脸验证",
                "face_verification_required",
                verification_url="https://example.com/verify",
            )

        self.assertEqual(
            email_send.await_args.args[1],
            "⚠️ 需要人脸验证|请完成人脸验证|https://example.com/verify",
        )

    async def test_account_template_preserves_email_attachment(self):
        manager = NotificationManager("seller")
        with patch(
            "app.services.xianyu.notification_manager.send_email_notification",
            new=AsyncMock(return_value=True),
        ) as email_send:
            await manager._send_to_channels(
                [
                    {
                        "enabled": True,
                        "channel_type": "email",
                        "channel_config": {"account_template": "{{title}}: {{detail}}"},
                    }
                ],
                "系统默认正文",
                attachment_path="/tmp/captcha.png",
                template_type="account",
                template_context={"title": "需要验证", "detail": "请完成人脸验证"},
            )

        self.assertEqual(email_send.await_args.args[1], "需要验证: 请完成人脸验证")
        self.assertEqual(email_send.await_args.args[2], "/tmp/captcha.png")


if __name__ == "__main__":
    unittest.main()
