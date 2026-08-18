"""
商品发布 API 路由

功能：
1. 素材库管理（CRUD）
2. 单品发布（调用闲鱼卖家工作台接口）
3. 批量发布（后台任务异步执行，逐条调用闲鱼发布接口）
4. 发布日志查询（分页+过滤）
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from uuid import UUID

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile, status
from loguru import logger
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_active_user, get_db_session
from app.services.product_publish_service import MaterialSpecificationError, ProductMaterialService
from app.services.account_service import AccountService
from app.services.platform_category_service import CategoryRecommendationError, PlatformCategoryService
from app.services.publish_batch_status_service import PublishBatchStatusService
from app.services.publish_execution_service import PublishExecutorService, PublishLogService
from common.services.xianyu_publish_service import detect_publish_account_capability
from common.models.user import User, UserRole
from common.schemas.common import ApiResponse
from common.utils.local_image_upload import ImageUploadError, save_uploaded_image
from common.utils.local_video_upload import VideoUploadError, save_uploaded_video
from app.core.paths import get_upload_path
from common.utils.time_utils import get_beijing_now_naive

def _is_admin(user: User) -> bool:
    """判断用户是否为管理员"""
    return user.role == UserRole.ADMIN

router = APIRouter(prefix="/product-publish", tags=["商品发布"])


# ==================== Pydantic 请求 / 响应模型 ====================

class PlatformAttributeRequest(BaseModel):
    """闲鱼平台属性标签，来源于抓包中的 itemLabelExtList。"""
    property_id: Optional[str] = Field(None, max_length=64)
    property_name: Optional[str] = Field(None, max_length=100)
    value_id: Optional[str] = Field(None, max_length=64)
    value_name: Optional[str] = Field(None, max_length=200)
    text: Optional[str] = Field(None, max_length=200)
    properties: Optional[str] = Field(None, max_length=500)


class PlatformCategoryPathItemRequest(BaseModel):
    """平台分类路径中的一级分类。"""
    id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=100)


class VideoMaterialRequest(BaseModel):
    """视频素材元数据，不保存抓包中的临时上传授权。"""
    url: str = Field(..., min_length=1, max_length=2000)
    path: Optional[str] = Field(None, max_length=1000)
    name: Optional[str] = Field(None, max_length=255)
    size: Optional[int] = Field(None, ge=0, le=200 * 1024 * 1024)
    file_id: Optional[str] = Field(None, max_length=128)
    width: Optional[int] = Field(None, ge=1, le=10000)
    height: Optional[int] = Field(None, ge=1, le=10000)
    duration_ms: Optional[int] = Field(None, ge=1, le=86400000)


class SpecificationValueRequest(BaseModel):
    """单个商品规格值。"""

    name: str = Field(..., min_length=1, max_length=100)
    image: Optional[str] = Field(None, max_length=2000)


class ProductSpecificationRequest(BaseModel):
    """商品规格类型及其可选值。"""

    name: str = Field(..., min_length=1, max_length=100)
    values: List[SpecificationValueRequest] = Field(default_factory=list, max_length=50)
    support_image: bool = False


class PublishSkuRowRequest(BaseModel):
    """规格组合对应的价格和库存。"""

    specs: Dict[str, str] = Field(default_factory=dict, max_length=4)
    price: float = Field(..., gt=0)
    stock: int = Field(0, ge=0, le=999999)

class MaterialCreateRequest(BaseModel):
    """创建素材请求"""
    title: str = Field(..., min_length=1, max_length=200, description="商品标题")
    description: str = Field(..., min_length=1, max_length=1500, description="商品描述")
    price: float = Field(..., gt=0, description="售价")
    original_price: Optional[float] = Field(None, description="原价（划线价）")
    category: Optional[str] = Field(None, max_length=100, description="商品分类")
    platform_category_id: Optional[str] = Field(None, max_length=64, description="平台末级分类ID（catId）")
    platform_category_name: Optional[str] = Field(None, max_length=100, description="平台末级分类名称（catName）")
    platform_channel_category_id: Optional[str] = Field(None, max_length=64, description="平台频道分类ID（channelCatId）")
    platform_channel_category_name: Optional[str] = Field(None, max_length=100, description="平台频道分类名称（channelCatName）")
    platform_leaf_id: Optional[str] = Field(None, max_length=64, description="平台叶子分类ID（leafId）")
    platform_tb_category_id: Optional[str] = Field(None, max_length=64, description="淘宝分类ID（tbCatId）")
    platform_category_path: List[PlatformCategoryPathItemRequest] = Field(default_factory=list)
    platform_attributes: List[PlatformAttributeRequest] = Field(default_factory=list, max_length=30)
    category_source: str = Field("manual", pattern="^(manual|recommendation)$")
    category_confidence: Optional[float] = Field(None, ge=0, le=1)
    images: List[str] = Field(..., min_length=1, max_length=9, description="图片URL列表（至少1张，最多9张）")
    videos: List[VideoMaterialRequest] = Field(default_factory=list, max_length=3)
    specifications: List[ProductSpecificationRequest] = Field(default_factory=list, max_length=2)
    sku_rows: List[PublishSkuRowRequest] = Field(default_factory=list, max_length=200)
    quantity: int = Field(1, ge=1, le=999999, description="发布数量")
    delivery_method: str = Field("express", description="发货方式：express/pickup")
    shipping_method: str = Field("free", pattern="^(free|distance|fixed|template|none)$")
    support_pickup: bool = False
    postage: float = Field(0, ge=0, description="邮费，0表示包邮")
    address: Optional[str] = Field(None, max_length=200, description="宝贝所在地")
    address_expected_text: Optional[str] = Field(None, max_length=200)
    brand: Optional[str] = Field(None, max_length=100, description="品牌")
    condition: str = Field("全新", description="成色")
    remark: Optional[str] = Field(None, max_length=500, description="备注（内部使用）")


class MaterialUpdateRequest(BaseModel):
    """更新素材请求（所有字段均可选）"""
    title: Optional[str] = Field(None, max_length=200)
    description: Optional[str] = Field(None, max_length=1500)
    price: Optional[float] = Field(None, gt=0)
    original_price: Optional[float] = None
    category: Optional[str] = Field(None, max_length=100)
    platform_category_id: Optional[str] = Field(None, max_length=64)
    platform_category_name: Optional[str] = Field(None, max_length=100)
    platform_channel_category_id: Optional[str] = Field(None, max_length=64)
    platform_channel_category_name: Optional[str] = Field(None, max_length=100)
    platform_leaf_id: Optional[str] = Field(None, max_length=64)
    platform_tb_category_id: Optional[str] = Field(None, max_length=64)
    platform_category_path: Optional[List[PlatformCategoryPathItemRequest]] = Field(None)
    platform_attributes: Optional[List[PlatformAttributeRequest]] = Field(None, max_length=30)
    category_source: Optional[str] = Field(None, pattern="^(manual|recommendation)$")
    category_confidence: Optional[float] = Field(None, ge=0, le=1)
    images: Optional[List[str]] = Field(None, min_length=1, max_length=9)
    videos: Optional[List[VideoMaterialRequest]] = Field(None, max_length=3)
    specifications: Optional[List[ProductSpecificationRequest]] = Field(None, max_length=2)
    sku_rows: Optional[List[PublishSkuRowRequest]] = Field(None, max_length=200)
    quantity: Optional[int] = Field(None, ge=1, le=999999)
    delivery_method: Optional[str] = Field(None, pattern="^(express|pickup)$")
    shipping_method: Optional[str] = Field(None, pattern="^(free|distance|fixed|template|none)$")
    support_pickup: Optional[bool] = None
    postage: Optional[float] = Field(None, ge=0)
    address: Optional[str] = Field(None, max_length=200)
    address_expected_text: Optional[str] = Field(None, max_length=200)
    brand: Optional[str] = Field(None, max_length=100)
    condition: Optional[str] = Field(None, max_length=20)
    remark: Optional[str] = Field(None, max_length=500)


class PublishSingleRequest(BaseModel):
    """单品发布请求"""
    account_id: str = Field(..., description="闲鱼账号ID（cookie_id）")
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=1, max_length=1500)
    price: float = Field(..., gt=0)
    original_price: Optional[float] = None
    category: Optional[str] = Field(None, description="商品分类")
    images: List[str] = Field(..., min_length=1, description="图片本地路径列表（至少1张）")
    platform_category_id: Optional[str] = Field(None, max_length=64)
    platform_category_name: Optional[str] = Field(None, max_length=100)
    platform_channel_category_id: Optional[str] = Field(None, max_length=64)
    platform_channel_category_name: Optional[str] = Field(None, max_length=100)
    platform_leaf_id: Optional[str] = Field(None, max_length=64)
    platform_tb_category_id: Optional[str] = Field(None, max_length=64)
    platform_category_path: List[PlatformCategoryPathItemRequest] = Field(default_factory=list)
    platform_attributes: List[PlatformAttributeRequest] = Field(default_factory=list, max_length=30)
    category_source: str = Field("manual", pattern="^(manual|recommendation)$")
    category_confidence: Optional[float] = Field(None, ge=0, le=1)
    videos: List[VideoMaterialRequest] = Field(default_factory=list, max_length=3)
    quantity: int = Field(1, ge=1, le=999999)
    stock: Optional[int] = Field(None, ge=0, le=999999)
    specifications: List[ProductSpecificationRequest] = Field(default_factory=list, max_length=2)
    sku_rows: List[PublishSkuRowRequest] = Field(default_factory=list, max_length=200)
    address: Optional[str] = None
    address_expected_text: Optional[str] = Field(None, max_length=200)
    delivery_method: str = Field("express", description="发货方式：express/pickup")
    shipping_method: str = Field("free", pattern="^(free|distance|fixed|template|none)$")
    support_pickup: bool = False
    postage: float = Field(0, ge=0, description="邮费，0表示包邮")
    brand: Optional[str] = Field(None, description="品牌")
    condition: str = Field("全新", description="成色")


class BatchPublishRequest(BaseModel):
    """批量发布请求"""
    account_ids: List[str] = Field(..., min_length=1, description="账号ID列表")
    material_ids: List[int] = Field(..., min_length=1, description="素材ID列表")
    request_id: Optional[UUID] = Field(None, description="可选的客户端幂等请求ID")


class ExternalMaterialItem(BaseModel):
    """外部系统同步的单条商品素材。"""

    external_id: str = Field(..., min_length=1, max_length=128)
    content_hash: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=1)
    price: float = Field(..., gt=0)
    original_price: Optional[float] = Field(None, gt=0)
    images: List[str] = Field(..., min_length=1, max_length=9)
    category: Optional[str] = Field(None, max_length=100)
    platform_category_id: Optional[str] = Field(None, max_length=64)
    platform_category_name: Optional[str] = Field(None, max_length=100)
    platform_channel_category_id: Optional[str] = Field(None, max_length=64)
    platform_channel_category_name: Optional[str] = Field(None, max_length=100)
    platform_leaf_id: Optional[str] = Field(None, max_length=64)
    platform_tb_category_id: Optional[str] = Field(None, max_length=64)
    platform_category_path: List[PlatformCategoryPathItemRequest] = Field(default_factory=list)
    platform_attributes: List[PlatformAttributeRequest] = Field(default_factory=list, max_length=30)
    category_source: str = Field("manual", pattern="^(manual|recommendation)$")
    category_confidence: Optional[float] = Field(None, ge=0, le=1)
    specifications: List[ProductSpecificationRequest] = Field(default_factory=list, max_length=2)
    sku_rows: List[PublishSkuRowRequest] = Field(default_factory=list, max_length=200)
    quantity: int = Field(1, ge=1, le=999999)
    delivery_method: str = Field("express", pattern=r"^(express|pickup)$")
    shipping_method: str = Field("free", pattern="^(free|distance|fixed|template|none)$")
    support_pickup: bool = False
    postage: float = Field(0, ge=0)
    address: Optional[str] = Field(None, max_length=200)
    address_expected_text: Optional[str] = Field(None, max_length=200)
    brand: Optional[str] = Field(None, max_length=100)
    condition: str = Field("全新", max_length=20)
    remark: Optional[str] = Field(None, max_length=500)

    @field_validator("images")
    @classmethod
    def validate_images(cls, images: List[str]) -> List[str]:
        normalized: List[str] = []
        for image in images:
            value = image.strip()
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("外部素材图片必须是有效的 HTTP/HTTPS URL")
            normalized.append(value)
        return normalized


class ExternalMaterialUpsertRequest(BaseModel):
    """外部素材批量幂等写入请求。"""

    source: str = Field(..., min_length=1, max_length=32, pattern=r"^[a-zA-Z0-9_-]+$")
    items: List[ExternalMaterialItem] = Field(..., min_length=1, max_length=20)


class CategoryRecommendRequest(BaseModel):
    """根据商品标题和描述请求平台分类推荐。"""

    title: str = Field("", max_length=200)
    description: str = Field("", max_length=1500)
    account_id: Optional[str] = Field(None, max_length=80, description="可选，指定用于请求的闲鱼账号")
    current_card_list: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="分类切换时沿用上一次推荐接口返回的完整属性卡",
    )
    selected_list: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="当前已选分类的属性标签列表",
    )
    cat_id: str = Field("", max_length=80, description="当前已选闲鱼末级分类ID")
    cat_name: str = Field("", max_length=200, description="当前已选分类名称")
    channel_cat_id: str = Field("", max_length=80, description="当前已选频道分类ID")


class AccountPublishCapabilityRequest(BaseModel):
    """检测指定账号的批量发布能力。"""

    account_id: str = Field(..., min_length=1, max_length=80)


@router.post("/publish/capability", response_model=ApiResponse)
async def get_publish_capability(
    req: AccountPublishCapabilityRequest,
    current_user: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """按批量发布的同一规则检测账号类型和可用参数。"""
    account_service = AccountService(session)
    owner_scope = None if _is_admin(current_user) else current_user.id
    account = await account_service.get_account_for_user(
        owner_scope,
        req.account_id.strip(),
    )
    if not account:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号不存在或无权使用")
    if not (account.cookie or "").strip():
        return ApiResponse(success=False, message="账号缺少Cookie，请重新登录账号")

    capability = await detect_publish_account_capability(
        cookie=account.cookie,
        account_id=account.account_id,
        owner_id=account.owner_id,
    )
    latest_cookie = capability.get("cookies_str")
    if latest_cookie and latest_cookie != account.cookie:
        account.cookie = latest_cookie
        await session.commit()
    if not capability.get("success"):
        return ApiResponse(success=False, message=capability.get("message") or "账号发布能力检测失败")

    is_fish_shop = bool(capability.get("is_fish_shop"))
    return ApiResponse(
        success=True,
        message="账号发布能力检测成功",
        data={
            "account_id": account.account_id,
            "account_type": "fish-shop" if is_fish_shop else "personal",
            "supports": {
                "quantity": is_fish_shop,
                "specifications": is_fish_shop,
                "sku_rows": is_fish_shop,
                "shipping_methods": ["free", "none"] if is_fish_shop else ["free", "distance", "fixed", "none"],
            },
            "commission_config": capability.get("commission_config") or {},
        },
    )


@router.post("/category/recommend", response_model=ApiResponse)
async def recommend_category(
    req: CategoryRecommendRequest,
    current_user: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """按商品标题和描述调用闲鱼分类推荐接口，素材库自动轮换当前用户已启动账号。"""
    title = req.title.strip()
    description = req.description.strip()
    if not title and not description:
        return ApiResponse(success=False, message="请先填写商品标题或商品描述")

    account_service = AccountService(session)
    requested_account_id = (req.account_id or "").strip()
    if requested_account_id:
        owner_scope = None if _is_admin(current_user) else current_user.id
        account = await account_service.get_account_for_user(owner_scope, requested_account_id)
        if not account:
            return ApiResponse(success=False, message="指定的闲鱼账号不存在或无权使用")
        candidate_accounts = [account] if account.cookie else []
    else:
        # 素材库不指定账号，只能使用当前登录用户自己的已启动账号，管理员也不跨用户取账号。
        current_user_accounts = await account_service.list_accounts(current_user.id)
        started_accounts = [
            item
            for item in current_user_accounts
            if (item.status or "").strip().lower() == "active"
        ]
        if not started_accounts:
            return ApiResponse(success=False, message="当前用户没有已启动的闲鱼账号，请先启动账号")
        candidate_accounts = [item for item in started_accounts if item.cookie and item.cookie.strip()]

    if not candidate_accounts:
        message = (
            "指定的闲鱼账号缺少Cookie，请重新登录账号"
            if requested_account_id
            else "当前用户已启动的闲鱼账号均缺少Cookie，请重新登录账号"
        )
        return ApiResponse(success=False, message=message)

    category_service = PlatformCategoryService()
    last_error = "分类推荐失败，请稍后重试"
    for account_index, account in enumerate(candidate_accounts):
        try:
            data = await category_service.recommend(
                # 闲鱼接口要求两个字段都有值，单独填写描述时用描述作为标题，反之亦然。
                title=title or description[:200],
                description=description or title,
                cookie=account.cookie,
                account_id=account.account_id,
                owner_id=account.owner_id,
                current_card_list=req.current_card_list or None,
                selected_list=req.selected_list or None,
                cat_id=req.cat_id,
                cat_name=req.cat_name,
                channel_cat_id=req.channel_cat_id,
            )
            return ApiResponse(success=True, message="分类推荐成功", data=data)
        except CategoryRecommendationError as exc:
            last_error = str(exc)
            logger.warning(
                f"分类推荐账号不可用: user_id={current_user.id}, "
                f"account_id={account.account_id}, error={last_error}"
            )
        except Exception as exc:
            last_error = "分类推荐失败，请稍后重试"
            logger.error(
                f"分类推荐接口异常: user_id={current_user.id}, "
                f"account_id={account.account_id}, error={exc}"
            )

        if account_index < len(candidate_accounts) - 1:
            logger.info(
                f"分类推荐自动切换下一个已启动账号: user_id={current_user.id}, "
                f"failed_account_id={account.account_id}"
            )

    if requested_account_id:
        return ApiResponse(success=False, message=last_error)
    return ApiResponse(
        success=False,
        message=f"当前用户已启动的闲鱼账号均不可用：{last_error}",
    )


# ==================== 素材库接口 ====================

@router.post("/materials", response_model=ApiResponse)
async def create_material(
    req: MaterialCreateRequest,
    current_user: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """创建商品素材"""
    svc = ProductMaterialService(session)
    try:
        material = await svc.create(current_user.id, req.model_dump())
    except MaterialSpecificationError as exc:
        return ApiResponse(success=False, message=str(exc))
    return ApiResponse(success=True, message="素材创建成功", data={"id": material.id})


@router.post("/materials/external/upsert", response_model=ApiResponse)
async def upsert_external_materials(
    req: ExternalMaterialUpsertRequest,
    current_user: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """按来源商品ID批量幂等创建或更新素材。"""
    svc = ProductMaterialService(session)
    items = await svc.upsert_external(
        user_id=current_user.id,
        source_type=req.source,
        items=[item.model_dump() for item in req.items],
    )
    return ApiResponse(
        success=True,
        message=f"外部素材同步完成，共处理 {len(items)} 条",
        data={"items": items},
    )


@router.get("/materials", response_model=ApiResponse)
async def list_materials(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, description="每页条数"),
    title: str = Query(None, description="标题模糊搜索"),
    category: str = Query(None, description="分类筛选"),
    condition: str = Query(None, description="成色筛选"),
    platform_category_id: str = Query(None, description="平台分类ID筛选"),
    current_user: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """分页查询素材列表（管理员可查看所有用户的素材）"""
    svc = ProductMaterialService(session)
    # 管理员查看全部，普通用户只看自己的
    query_user_id = None if _is_admin(current_user) else current_user.id
    data = await svc.list_materials(
        query_user_id, page=page, page_size=page_size,
        title=title, category=category, condition=condition,
        platform_category_id=platform_category_id,
    )
    # 管理员场景：批量补充用户名
    if _is_admin(current_user) and data.get("list"):
        from sqlalchemy import select
        user_ids = list({m["user_id"] for m in data["list"]})
        stmt = select(User.id, User.username).where(User.id.in_(user_ids))
        rows = (await session.execute(stmt)).all()
        name_map = {r.id: r.username for r in rows}
        for m in data["list"]:
            m["username"] = name_map.get(m["user_id"], "未知用户")
    return ApiResponse(success=True, message="查询成功", data=data)


class BatchDeleteRequest(BaseModel):
    """批量删除素材请求"""
    ids: List[int] = Field(..., min_length=1, description="素材ID列表")


@router.post("/materials/batch-delete", response_model=ApiResponse)
async def batch_delete_materials(
    req: BatchDeleteRequest,
    current_user: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """批量删除素材（管理员可删除任意素材）"""
    svc = ProductMaterialService(session)
    query_user_id = None if _is_admin(current_user) else current_user.id
    count = await svc.batch_delete(req.ids, query_user_id)
    return ApiResponse(success=True, message=f"成功移出 {count} 条素材", data={"deleted_count": count})


@router.get("/materials/{material_id}", response_model=ApiResponse)
async def get_material(
    material_id: int,
    current_user: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """获取单条素材详情（管理员可访问任意素材）"""
    svc = ProductMaterialService(session)
    query_user_id = None if _is_admin(current_user) else current_user.id
    material = await svc.get(material_id, query_user_id)
    if not material:
        return ApiResponse(success=False, message="素材不存在或无权访问")
    from app.services.product_publish_service import _material_to_dict
    return ApiResponse(success=True, message="查询成功", data=_material_to_dict(material))


@router.put("/materials/{material_id}", response_model=ApiResponse)
async def update_material(
    material_id: int,
    req: MaterialUpdateRequest,
    current_user: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """更新素材信息（管理员可修改任意素材）"""
    svc = ProductMaterialService(session)
    query_user_id = None if _is_admin(current_user) else current_user.id
    try:
        updated = await svc.update(
            material_id,
            query_user_id,
            # 只忽略请求中未出现的字段；显式传入的空数组、False 或 null 都要保存，
            # 否则编辑素材时清空规格/属性会被旧值覆盖。
            req.model_dump(exclude_unset=True),
        )
    except MaterialSpecificationError as exc:
        return ApiResponse(success=False, message=str(exc))
    if not updated:
        return ApiResponse(success=False, message="素材不存在或无权修改")
    return ApiResponse(success=True, message="素材更新成功")


@router.delete("/materials/{material_id}", response_model=ApiResponse)
async def delete_material(
    material_id: int,
    current_user: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """删除素材（管理员可删除任意素材）"""
    svc = ProductMaterialService(session)
    query_user_id = None if _is_admin(current_user) else current_user.id
    deleted = await svc.delete(material_id, query_user_id)
    if not deleted:
        return ApiResponse(success=False, message="素材不存在或无权删除")
    return ApiResponse(success=True, message="素材已移出素材库")


# ==================== 发布接口 ====================

@router.post("/publish/single", response_model=ApiResponse)
async def publish_single(
    req: PublishSingleRequest,
    current_user: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """单品发布（同步调用闲鱼卖家工作台接口并返回结果）。"""
    svc = PublishExecutorService(session)
    result = await svc.publish_single(
        user_id=current_user.id,
        account_id=req.account_id,
        item_data=req.model_dump(),
    )
    return ApiResponse(
        success=result.get("success", False),
        message=result.get("message", ""),
        data={
            "item_url": result.get("item_url"),
            "item_id": result.get("item_id"),
            "log_id": result.get("log_id"),
            "sync_status": result.get("sync_status"),
            "sync_message": result.get("sync_message"),
            "sync_total_count": result.get("sync_total_count"),
            "sync_saved_count": result.get("sync_saved_count"),
        },
    )


@router.post("/publish/batch", response_model=ApiResponse)
async def publish_batch(
    req: BatchPublishRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """批量发布（后台异步执行，立即返回 batch_id）
    
    前端通过 GET /publish/batch/{batch_id}/status 查询进度。
    后台会按账号循环，每个账号依次通过闲鱼接口发布所有素材。
    """
    from common.models.publish_batch import PublishBatch
    from common.models.xy_account import XYAccount

    account_ids = list(dict.fromkeys(account_id.strip() for account_id in req.account_ids if account_id.strip()))
    material_ids = list(dict.fromkeys(req.material_ids))
    if not account_ids or not material_ids:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="账号和素材不能为空")

    account_rows = (
        await session.execute(
            select(XYAccount.account_id).where(
                XYAccount.owner_id == current_user.id,
                XYAccount.account_id.in_(account_ids),
            )
        )
    ).scalars().all()
    missing_accounts = [account_id for account_id in account_ids if account_id not in set(account_rows)]
    if missing_accounts:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"账号不存在或无权使用: {', '.join(missing_accounts)}",
        )

    request_id = str(req.request_id or uuid.uuid4())
    existing_batch = (
        await session.execute(
            select(PublishBatch).where(
                PublishBatch.user_id == current_user.id,
                PublishBatch.request_id == request_id,
            )
        )
    ).scalar_one_or_none()
    if existing_batch:
        return ApiResponse(
            success=True,
            message="该请求已提交，返回原批次",
            data={
                "batch_id": existing_batch.batch_id,
                "total": existing_batch.total,
                "idempotent_replay": True,
            },
        )

    mat_svc = ProductMaterialService(session)
    from app.services.product_publish_service import _material_to_dict
    materials = [_material_to_dict(m) for m in await mat_svc.list_by_ids(material_ids, current_user.id)]

    if len(materials) != len(material_ids):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="包含不存在或无权使用的素材")

    batch_id = request_id
    batch = PublishBatch(
        batch_id=batch_id,
        user_id=current_user.id,
        request_id=request_id,
        account_ids=account_ids,
        material_ids=material_ids,
        status="queued",
        total=len(account_ids) * len(materials),
    )
    session.add(batch)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing_batch = (
            await session.execute(
                select(PublishBatch).where(
                    PublishBatch.user_id == current_user.id,
                    PublishBatch.request_id == request_id,
                )
            )
        ).scalar_one()
        return ApiResponse(
            success=True,
            message="该请求已提交，返回原批次",
            data={
                "batch_id": existing_batch.batch_id,
                "total": existing_batch.total,
                "idempotent_replay": True,
            },
        )

    await PublishBatchStatusService.init_batch(
        batch_id=batch_id,
        account_ids=account_ids,
        material_count=len(materials),
    )

    # 创建后台任务
    background_tasks.add_task(
        _run_batch_publish_background,
        user_id=current_user.id,
        account_ids=account_ids,
        materials=materials,
        batch_id=batch_id,
    )

    return ApiResponse(
        success=True,
        message=f"批量发布任务已提交，共 {len(account_ids)} 个账号 × {len(materials)} 件商品",
        data={
            "batch_id": batch_id,
            "total": len(account_ids) * len(materials),
            "idempotent_replay": False,
        },
    )


@router.get("/publish/batch/{batch_id}/status", response_model=ApiResponse)
async def get_batch_status(
    batch_id: str,
    current_user: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """查询持久化批量发布进度和逐商品结果。"""
    from sqlalchemy import func
    from common.models.publish_batch import PublishBatch
    from common.models.publish_log import PublishLog

    batch = (
        await session.execute(
            select(PublishBatch).where(
                PublishBatch.batch_id == batch_id,
                PublishBatch.user_id == current_user.id,
            )
        )
    ).scalar_one_or_none()

    counts_rows = (await session.execute(select(
        PublishLog.status,
        func.count().label("cnt"),
    ).where(
        PublishLog.batch_id == batch_id,
        PublishLog.user_id == current_user.id,
    ).group_by(PublishLog.status))).all()
    counts = {row.status: int(row.cnt) for row in counts_rows}

    if batch is None and not counts:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="批量任务不存在")

    account_rows = (await session.execute(select(
        PublishLog.account_id,
        PublishLog.status,
        func.count().label("cnt"),
    ).where(
        PublishLog.batch_id == batch_id,
        PublishLog.user_id == current_user.id,
    ).group_by(PublishLog.account_id, PublishLog.status))).all()

    account_count_map: Dict[str, Dict[str, int]] = {}
    for row in account_rows:
        status_map = account_count_map.setdefault(row.account_id, {})
        status_map[row.status] = int(row.cnt)

    log_rows = (
        await session.execute(
            select(PublishLog)
            .where(
                PublishLog.batch_id == batch_id,
                PublishLog.user_id == current_user.id,
            )
            .order_by(PublishLog.id)
        )
    ).scalars().all()

    configured_accounts = list(batch.account_ids or []) if batch else list(account_count_map.keys())
    configured_materials = list(batch.material_ids or []) if batch else []
    total = int(batch.total) if batch else sum(counts.values())
    success = counts.get("success", 0)
    failed = counts.get("failed", 0)
    publishing = counts.get("publishing", 0)
    pending = max(total - success - failed - publishing, 0)
    batch_snapshot = await PublishBatchStatusService.get_batch_snapshot(batch_id)

    account_statuses: List[Dict[str, Any]] = []
    material_count = len(configured_materials)
    account_sync_map = (batch_snapshot or {}).get("accounts") or {}
    for account_id in configured_accounts:
        status_map = account_count_map.get(account_id, {})
        account_total = material_count if material_count else sum(status_map.values())
        account_success = int(status_map.get("success", 0))
        account_failed = int(status_map.get("failed", 0))
        account_publishing = int(status_map.get("publishing", 0))
        account_pending = max(account_total - account_success - account_failed - account_publishing, 0)
        sync_info = account_sync_map.get(account_id, {})
        default_sync_status = "unknown" if batch and batch.status in {"completed", "failed"} else "pending"
        account_statuses.append(
            {
                "account_id": account_id,
                "total": account_total,
                "success": account_success,
                "failed": account_failed,
                "publishing": account_publishing,
                "pending": account_pending,
                "sync_status": sync_info.get("sync_status", default_sync_status),
                "sync_message": sync_info.get("sync_message", "批次缓存不可用，逐商品结果仍可查询"),
                "sync_total_count": int(sync_info.get("sync_total_count") or 0),
                "sync_saved_count": int(sync_info.get("sync_saved_count") or 0),
            }
        )

    done = bool(batch and batch.status in {"completed", "failed"})
    if done:
        pending = 0
    sync_finished = all(
        account_status.get("sync_status") in {"success", "failed", "skipped", "unknown"}
        for account_status in account_statuses
    ) if account_statuses else True
    if batch is None:
        done = total > 0 and publishing == 0 and pending == 0 and sync_finished

    items = [
        {
            "log_id": row.id,
            "account_id": row.account_id,
            "material_id": row.material_id,
            "status": row.status,
            "item_id": row.item_id,
            "item_url": row.item_url,
            "error_message": row.error_message,
        }
        for row in log_rows
    ]

    return ApiResponse(
        success=True,
        message="查询成功",
        data={
            "batch_id": batch_id,
            "total": total,
            "success": success,
            "failed": failed,
            "publishing": publishing,
            "pending": pending,
            "status": batch.status if batch else ("completed" if done else "running"),
            "finished": done,
            "done": done,
            "account_statuses": account_statuses,
            "items": items,
        },
    )


# ==================== 发布日志接口 ====================

@router.get("/logs", response_model=ApiResponse)
async def list_publish_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20),
    account_id: Optional[str] = Query(None, description="按账号过滤"),
    status: Optional[str] = Query(None, description="按状态过滤：pending/publishing/success/failed"),
    current_user: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """分页查询发布日志（管理员可查看所有用户的发布日志）"""
    svc = PublishLogService(session)
    # 管理员查看全部，普通用户只看自己的
    query_user_id = None if _is_admin(current_user) else current_user.id
    data = await svc.list_logs(
        user_id=query_user_id,
        page=page,
        page_size=page_size,
        account_id=account_id,
        status=status,
    )
    # 管理员场景：批量补充用户名
    if _is_admin(current_user) and data.get("list"):
        from sqlalchemy import select
        user_ids = list({log["user_id"] for log in data["list"]})
        stmt = select(User.id, User.username).where(User.id.in_(user_ids))
        rows = (await session.execute(stmt)).all()
        name_map = {r.id: r.username for r in rows}
        for log in data["list"]:
            log["username"] = name_map.get(log["user_id"], "未知用户")
    return ApiResponse(success=True, message="查询成功", data=data)


@router.delete("/logs/clear", response_model=ApiResponse)
async def clear_publish_logs(
    current_user: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """清空发布日志（只清空10天前的数据）"""
    from datetime import timedelta

    from loguru import logger
    from sqlalchemy import delete

    from common.models.publish_log import PublishLog

    try:
        ten_days_ago = get_beijing_now_naive() - timedelta(days=10)
        stmt = delete(PublishLog).where(
            PublishLog.user_id == current_user.id,
            PublishLog.created_at < ten_days_ago,
        )

        result = await session.execute(stmt)
        await session.commit()

        deleted_count = result.rowcount or 0
        logger.info(f"[发布日志] 用户 {current_user.id} 已清空 {deleted_count} 条10天前的日志")
        return ApiResponse(
            success=True,
            message=f"已清空 {deleted_count} 条10天前的发布日志",
        )
    except Exception as e:
        await session.rollback()
        logger.error(f"[发布日志] 清空日志失败: {e}")
        return ApiResponse(success=False, message=f"清空发布日志失败: {str(e)}")


# ==================== 图片上传接口 ====================

@router.post("/upload/images", response_model=ApiResponse)
async def upload_product_images(
    files: List[UploadFile] = File(...),
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """上传商品图片（支持多张，最多9张，每张最大5MB）

    返回本地文件路径列表，这些路径将直接传给 Playwright 的 set_input_files。
    """
    upload_dir = get_upload_path("products")

    if len(files) > 9:
        return ApiResponse(success=False, message="最多上传9张图片")

    saved_paths: List[str] = []
    saved_urls: List[str] = []

    for file in files:
        try:
            filepath, filename, _ = await save_uploaded_image(
                file,
                upload_dir,
            )
        except ImageUploadError as exc:
            # 在消息里带上具体哪张图片出错，方便前端展示
            return ApiResponse(
                success=False,
                message=f"文件 {file.filename}: {exc.message}",
            )

        saved_paths.append(str(filepath))                          # 绝对路径，用于 Playwright
        saved_urls.append(f"/static/uploads/products/{filename}")  # URL，用于前端预览

    return ApiResponse(
        success=True,
        message=f"成功上传 {len(saved_paths)} 张图片",
        data={"paths": saved_paths, "urls": saved_urls},
    )


@router.post("/upload/videos", response_model=ApiResponse)
async def upload_product_videos(
    files: List[UploadFile] = File(...),
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """上传商品视频（最多3个，每个最大100MB）。"""
    del current_user
    if len(files) > 3:
        return ApiResponse(success=False, message="最多上传3个视频")

    upload_dir = get_upload_path("products")
    videos: List[dict] = []
    for file in files:
        try:
            filepath, filename, size = await save_uploaded_video(file, upload_dir)
        except VideoUploadError as exc:
            return ApiResponse(success=False, message=f"文件 {file.filename}: {exc.message}")
        videos.append({
            "path": str(filepath),
            "url": f"/static/uploads/products/{filename}",
            "name": file.filename or filename,
            "size": size,
        })

    return ApiResponse(
        success=True,
        message=f"成功上传 {len(videos)} 个视频",
        data={
            "videos": videos,
            "paths": [item["path"] for item in videos],
            "urls": [item["url"] for item in videos],
        },
    )


# ==================== 后台任务函数 ====================

async def _run_batch_publish_background(
    user_id: int,
    account_ids: List[str],
    materials: List[dict],
    batch_id: str,
) -> None:
    """后台异步执行批量发布任务"""
    from common.db.session import async_session_maker
    from common.models.publish_batch import PublishBatch
    from loguru import logger
    from sqlalchemy import select
    import traceback

    async with async_session_maker() as session:
        svc = PublishExecutorService(session)
        try:
            batch = (
                await session.execute(
                    select(PublishBatch).where(PublishBatch.batch_id == batch_id)
                )
            ).scalar_one_or_none()
            if batch:
                batch.status = "running"
                await session.commit()

            # 直接将 batch_id 传给 service，确保日志与路由返回值一致
            result = await svc.batch_publish(
                user_id=user_id,
                account_ids=account_ids,
                materials=materials,
                batch_id=batch_id,
            )
            batch = (
                await session.execute(
                    select(PublishBatch).where(PublishBatch.batch_id == batch_id)
                )
            ).scalar_one_or_none()
            if batch:
                batch.status = "completed"
                batch.success_count = int(result.get("success_count") or 0)
                batch.failed_count = int(result.get("failed_count") or 0)
                batch.finished_at = get_beijing_now_naive()
                await session.commit()
        except Exception as e:
            logger.error(f"批量发布后台任务异常: {e}\n{traceback.format_exc()}")
            await session.rollback()
            batch = (
                await session.execute(
                    select(PublishBatch).where(PublishBatch.batch_id == batch_id)
                )
            ).scalar_one_or_none()
            if batch:
                batch.status = "failed"
                batch.error_message = str(e)[:1000]
                batch.finished_at = get_beijing_now_naive()
                await session.commit()
            await PublishBatchStatusService.clear_batch(batch_id)
