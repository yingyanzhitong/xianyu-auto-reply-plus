"""每日总结的日期边界、数据隔离和发送重试回归测试。"""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from common.models.auto_reply_message_log import XYAutoReplyMessageLog
from common.models.message_notification import MessageNotification
from common.models.notification_channel import NotificationChannel
from common.models.xy_account import XYAccount
from common.models.xy_order import XYOrder
from common.services.daily_summary_service import DailySummaryService


MODULE = "common.services.daily_summary_service"


class DailySummaryQueryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        for model in (XYOrder, XYAutoReplyMessageLog, NotificationChannel, MessageNotification, XYAccount):
            model.__table__.create(self.engine)
        self.db = Session(self.engine)
        session = SimpleNamespace(execute=AsyncMock(side_effect=self.db.execute))

        @asynccontextmanager
        async def session_maker():
            yield session

        patcher = patch(f"{MODULE}.async_session_maker", session_maker)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.service = DailySummaryService()

    async def test_order_and_chat_totals_use_previous_day_and_bound_accounts(self):
        start, end = datetime(2026, 10, 22), datetime(2026, 10, 23)
        for index, (owner, account, placed, status, amount) in enumerate([
            (1, "a", start, "pending_ship", "1.10"),
            (1, "a", datetime(2026, 10, 22, 23, 59, 59), "completed", "2.20"),
            (1, "a", start, "pending_payment", "100"),
            (1, "a", start, "cancelled", "100"),
            (1, "a", start, "refunded", "100"),
            (1, "a", end, "completed", "100"),
            (1, "a", datetime(2026, 10, 21), "completed", "100"),
            (1, "unbound", start, "completed", "100"),
            (2, "a", start, "completed", "100"),
            (1, "a", None, "completed", "100"),
            (1, "b", start, "shipped", "0.30"),
        ], 1):
            self.db.add(XYOrder(id=index, owner_id=owner, account_id=account, order_no=str(index),
                                placed_at=placed, status=status, amount=amount, created_at=start))
        for index, (owner, account, chat, reason, status, strategy, received) in enumerate([
            (1, "a", "same-chat", "reply_sent", "success", "keyword", start),
            (1, "a", "same-chat", "no_rule_matched", "skipped", "none", start),
            (1, "b", "same-chat", "reply_sent", "success", "ai", start),
            (1, "a", "own", "self_message", "skipped", "none", start),
            (1, "a", "system", "system_message", "skipped", "none", start),
            (1, "a", "delivery", "delivery_success", "success", "auto_delivery", start),
            (1, "a", "duplicate", "duplicate_message", "skipped", "none", start),
            (1, "a", "today", "reply_sent", "success", "ai", end),
            (1, "a", "old", "reply_sent", "success", "ai", datetime(2026, 10, 21)),
            (1, "unbound", "other", "reply_sent", "success", "ai", start),
            (2, "a", "other-owner", "reply_sent", "success", "ai", start),
        ], 1):
            self.db.add(XYAutoReplyMessageLog(id=index, owner_id=owner, account_id=account, chat_id=chat,
                sender_user_id="buyer", decision_reason=reason, process_status=status,
                reply_strategy=strategy, source_message_time=received, created_at=start))
        self.db.commit()

        context = await self.service._build_context(1, ["a", "b"], start, end)
        self.assertEqual(context["order_count"], 4)
        self.assertEqual(context["income"], "3.60")
        self.assertEqual(context["pending_count"], 1)
        self.assertEqual(context["shipped_count"], 2)
        self.assertEqual(context["chat_count"], 2)
        self.assertEqual(context["message_count"], 3)
        self.assertEqual(context["reply_count"], 2)
        empty = await self.service._build_context(1, ["empty"], start, end)
        self.assertEqual(empty["income"], "0.00")
        self.assertEqual(empty["chat_count"], 0)
        self.assertEqual(empty["order_count"], 0)

    async def test_only_enabled_channels_and_subscriptions_owned_by_same_user_are_loaded(self):
        for index, owner in [(1, 1), (2, 1), (3, 2)]:
            self.db.add(XYAccount(id=index, owner_id=owner, account_id=f"account-{index}", cookie="", login_method="cookie"))
        for index, enabled, config in [(1, True, {"daily_summary_enabled": True}),
                                        (2, False, {"daily_summary_enabled": True}), (3, True, {})]:
            self.db.add(NotificationChannel(id=index, owner_id=1, name="渠道", channel_type="bark",
                                            enabled=enabled, config_payload=config))
        for index, channel, account, owner, enabled in [(1, 1, 1, 1, True), (2, 1, 2, 1, False),
                (3, 1, 3, 1, True), (4, 1, 2, 2, True), (5, 2, 1, 1, True), (6, 3, 1, 1, True)]:
            self.db.add(MessageNotification(id=index, owner_id=owner, channel_id=channel,
                account_pk=account, account_identifier=f"account-{account}", enabled=enabled))
        self.db.commit()
        channels = await self.service._load_channels()
        self.assertEqual([(c.id, accounts) for c, accounts in channels], [(1, ["account-1"])])


class DailySummaryDispatchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = DailySummaryService()
        self.channel = SimpleNamespace(id=1, owner_id=7, channel_type="bark", config_payload={
            "daily_summary_enabled": True, "daily_summary_template": "{{date}}|{{order_count}}|{{income}}|{{chat_count}}",
        })
        self.service._load_channels = AsyncMock(return_value=[(self.channel, ["a"])])
        self.service._build_context = AsyncMock(return_value={
            "date": "2026-10-22", "order_count": 0, "income": "0.00", "chat_count": 0,
            "accounts": "a", "message_count": 0, "reply_count": 0, "shipped_count": 0, "pending_count": 0,
        })
        self.sent = {}
        self.locked = True
        self.redis = SimpleNamespace(get=AsyncMock(side_effect=lambda key: self.sent.get(key)),
            set=AsyncMock(side_effect=lambda key, value, **kw: self.sent.update({key: value})))

        @asynccontextmanager
        async def lock(*args, **kwargs):
            yield SimpleNamespace(is_locked=self.locked)

        for name, value in [("get_redis_client", AsyncMock(return_value=self.redis)), ("distributed_lock", lock)]:
            patcher = patch(f"{MODULE}.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_before_ten_does_nothing_and_after_ten_sends_yesterday_once(self):
        with patch(f"{MODULE}.send_channel_notification", new=AsyncMock(return_value=True)) as send:
            await self.service.execute(datetime(2026, 10, 23, 1, 59, tzinfo=timezone.utc))
            self.service._load_channels.assert_not_awaited()
            await self.service.execute(datetime(2026, 10, 23, 2, 0, tzinfo=timezone.utc))
            await self.service.execute(datetime(2026, 10, 23, 18))
            send.assert_awaited_once()
            self.assertEqual(send.await_args.args[2], "2026-10-22|0|0.00|0")
            self.service._build_context.assert_awaited_once_with(7, ["a"], datetime(2026, 10, 22), datetime(2026, 10, 23))
            # 新服务实例仍复用 Redis 中的成功标记。
            restarted = DailySummaryService()
            restarted._load_channels = self.service._load_channels
            await restarted.execute(datetime(2026, 10, 23, 19))
            send.assert_awaited_once()
            await self.service.execute(datetime(2026, 10, 24, 10))
            self.assertEqual(send.await_count, 2)

    async def test_failure_retries_but_busy_lock_skips(self):
        with patch(f"{MODULE}.send_channel_notification", new=AsyncMock(side_effect=[False, True])) as send:
            self.locked = False
            await self.service.execute(datetime(2026, 10, 23, 10))
            send.assert_not_awaited()
            self.locked = True
            await self.service.execute(datetime(2026, 10, 23, 10))
            self.redis.set.assert_not_awaited()
            await self.service.execute(datetime(2026, 10, 23, 10, 1))
            await self.service.execute(datetime(2026, 10, 23, 10, 2))
            self.assertEqual(send.await_count, 2)
            self.redis.set.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
