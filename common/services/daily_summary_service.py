"""按通知渠道汇总前一天的经营数据。"""
from __future__ import annotations

from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import case, func, select

from common.db.redis_client import distributed_lock, get_redis_client
from common.db.session import async_session_maker
from common.models.auto_reply_message_log import XYAutoReplyMessageLog
from common.models.message_notification import MessageNotification
from common.models.notification_channel import NotificationChannel
from common.models.xy_account import XYAccount
from common.models.xy_order import XYOrder
from common.utils.notification_template import is_notification_type_enabled, render_notification_template
from common.utils.notification_utils import parse_notification_config, send_channel_notification
from common.utils.time_utils import BEIJING_TZ, get_beijing_now


class DailySummaryService:
    """北京时间每天 10 点发送，服务重启后当日可补发；成功后不重复发送。"""

    CLOSED_STATUSES = ("cancelled", "closed", "已关闭", "交易关闭", "refunded", "退款成功", "已退款")
    PENDING_STATUSES = ("pending_ship", "pending", "paid", "待发货")
    SHIPPED_STATUSES = ("shipped", "completed", "已发货", "已完成", "交易成功")

    async def execute(self, now: datetime | None = None) -> None:
        now = now or get_beijing_now()
        if now.tzinfo is not None:
            now = now.astimezone(BEIJING_TZ)
        if now.hour < 10:
            return

        end = now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        start = end - timedelta(days=1)
        channels = await self._load_channels()
        if not channels:
            return
        redis = await get_redis_client()
        for channel, account_ids in channels:
            key = f"notification:daily_summary:{channel.id}:{start.date()}"
            try:
                async with distributed_lock(key, expire=120, blocking=False) as lock:
                    if not lock.is_locked or await redis.get(key):
                        continue
                    context = await self._build_context(channel.owner_id, account_ids, start, end)
                    context["time"] = now.strftime("%Y-%m-%d %H:%M:%S")
                    config = parse_notification_config(channel.config_payload)
                    message = render_notification_template(
                        config, "daily_summary", context, self._default_message(context)
                    )
                    if await send_channel_notification(channel.channel_type, config, message):
                        await redis.set(key, "sent", ex=7 * 24 * 60 * 60)
                    else:
                        logger.warning("每日总结渠道 {} 发送失败，稍后重试", channel.id)
            except Exception as exc:
                logger.exception("每日总结渠道 {} 执行失败: {}", channel.id, exc)

    async def _load_channels(self) -> list[tuple[NotificationChannel, list[str]]]:
        async with async_session_maker() as session:
            rows = (await session.execute(
                select(NotificationChannel, XYAccount.account_id)
                .join(MessageNotification, MessageNotification.channel_id == NotificationChannel.id)
                .join(XYAccount, XYAccount.id == MessageNotification.account_pk)
                .where(
                    NotificationChannel.enabled.is_(True),
                    MessageNotification.enabled.is_(True),
                    MessageNotification.owner_id == NotificationChannel.owner_id,
                    XYAccount.owner_id == NotificationChannel.owner_id,
                )
            )).all()
        grouped: dict[int, tuple[NotificationChannel, list[str]]] = {}
        for channel, account_id in rows:
            if not is_notification_type_enabled(channel.config_payload, "daily_summary"):
                continue
            _, accounts = grouped.setdefault(channel.id, (channel, []))
            if account_id not in accounts:
                accounts.append(account_id)
        return list(grouped.values())

    async def _build_context(
        self, owner_id: int, account_ids: list[str], start: datetime, end: datetime
    ) -> dict:
        async with async_session_maker() as session:
            orders = (await session.execute(
                select(
                    func.count().label("order_count"),
                    func.coalesce(func.sum(case(
                        (XYOrder.status.in_((*self.PENDING_STATUSES, *self.SHIPPED_STATUSES)), XYOrder.amount),
                        else_=0,
                    )), 0).label("income"),
                    func.coalesce(func.sum(case((XYOrder.status.in_(self.SHIPPED_STATUSES), 1), else_=0)), 0).label("shipped_count"),
                    func.coalesce(func.sum(case((XYOrder.status.in_(self.PENDING_STATUSES), 1), else_=0)), 0).label("pending_count"),
                ).where(
                    XYOrder.owner_id == owner_id,
                    XYOrder.account_id.in_(account_ids),
                    XYOrder.placed_at >= start,
                    XYOrder.placed_at < end,
                    XYOrder.status.notin_(self.CLOSED_STATUSES),
                )
            )).one()

            # 排除卖家消息、系统提示、自动发货和去重记录；按真实消息时间归属统计日。
            message_time = func.coalesce(XYAutoReplyMessageLog.source_message_time, XYAutoReplyMessageLog.created_at)
            message_conditions = (
                XYAutoReplyMessageLog.owner_id == owner_id,
                XYAutoReplyMessageLog.account_id.in_(account_ids),
                message_time >= start,
                message_time < end,
                XYAutoReplyMessageLog.decision_reason.notin_(
                    ("self_message", "system_message", "auto_delivery_trigger", "duplicate_message")
                ),
                XYAutoReplyMessageLog.reply_strategy != "auto_delivery",
                XYAutoReplyMessageLog.chat_id != "",
            )
            chats = select(
                XYAutoReplyMessageLog.account_id, XYAutoReplyMessageLog.chat_id
            ).where(*message_conditions).distinct().subquery()
            chat_count = (await session.execute(select(func.count()).select_from(chats))).scalar_one()
            messages = (await session.execute(select(
                func.count().label("message_count"),
                func.coalesce(func.sum(case((XYAutoReplyMessageLog.process_status == "success", 1), else_=0)), 0).label("reply_count"),
            ).where(*message_conditions))).one()

        return {
            "date": str(start.date()),
            "accounts": "、".join(account_ids),
            "account_count": len(account_ids),
            "order_count": int(orders.order_count),
            "income": f"{orders.income:.2f}",
            "shipped_count": int(orders.shipped_count),
            "pending_count": int(orders.pending_count),
            "chat_count": int(chat_count),
            "message_count": int(messages.message_count),
            "reply_count": int(messages.reply_count),
        }

    @staticmethod
    def _default_message(context: dict) -> str:
        return (
            f"📊 闲鱼每日总结（{context['date']}）\n\n"
            f"统计账号: {context['accounts']}\n"
            f"有效订单: {context['order_count']} 单\n"
            f"收入: ¥{context['income']}\n"
            f"聊天会话: {context['chat_count']} 个\n"
            f"收到消息: {context['message_count']} 条\n"
            f"自动回复: {context['reply_count']} 条\n"
            f"已发货: {context['shipped_count']} 单\n"
            f"待发货: {context['pending_count']} 单\n"
            f"时间: {context['time']}"
        )
