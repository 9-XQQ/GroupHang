"""认证路由：轻量登录/注册。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import User
from ..schemas import LoginRequest
from ..security import issue_token

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)) -> dict:
    """手机号或微信授权登录；首次登录自动注册。"""
    if body.phone:
        result = await db.execute(select(User).where(User.phone == body.phone))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(phone=body.phone, name=body.name or f"用户{body.phone[-4:]}")
            db.add(user)
            await db.commit()
            await db.refresh(user)
        elif body.name and user.name != body.name:
            # 已有账号重新填写昵称时同步更新，参与者列表和地图统一读取该名称。
            user.name = body.name
            await db.commit()
    elif body.wechat_code:
        # MVP：直接把 wechat_code 当 openid 用（真实环境需调微信 code2session 换取 openid）
        openid = body.wechat_code
        result = await db.execute(select(User).where(User.wechat_openid == openid))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(wechat_openid=openid, name=body.name or "微信用户")
            db.add(user)
            await db.commit()
            await db.refresh(user)
        elif body.name and user.name != body.name:
            user.name = body.name
            await db.commit()
    else:
        raise HTTPException(status_code=400, detail="phone 或 wechat_code 至少提供一个")

    token = issue_token(user.id)
    return {"token": token, "user": {"id": user.id, "name": user.name, "phone": user.phone}}
