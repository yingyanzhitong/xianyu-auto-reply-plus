"""通知中使用的商品上下文。"""
from __future__ import annotations

import asyncio

from loguru import logger


async def get_notification_item_name(account_id: str, item_id: str | None) -> str:
    """只查询当前账号的商品缓存，查询失败不影响通知发送。"""
    if not item_id:
        return "未知"
    from common.db.compat import db_manager

    try:
        item = await asyncio.to_thread(db_manager.get_item_info, account_id, item_id)
        return (item or {}).get("item_title") or "未知"
    except Exception as exc:
        logger.warning("读取通知商品名称失败: {}", exc)
        return "未知"
