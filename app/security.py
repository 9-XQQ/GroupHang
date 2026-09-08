"""轻量登录态：MVP 用文件持久化的 token，不做正式 JWT。

token -> user_id 落盘到项目根目录 .token_store.json，服务重启后仍有效，
避免开发期 uvicorn --reload 重启导致已登录会话失效。
正式环境应替换为 Redis / JWT。
"""
import json
import uuid
from pathlib import Path

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .models import User

# token -> user_id，文件持久化，服务重启后仍有效
_TOKEN_FILE = Path(__file__).resolve().parent.parent / ".token_store.json"


def _load_tokens() -> dict[str, int]:
    if _TOKEN_FILE.exists():
        try:
            data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


token_store: dict[str, int] = _load_tokens()


def _save_tokens() -> None:
    try:
        _TOKEN_FILE.write_text(json.dumps(token_store), encoding="utf-8")
    except OSError:
        pass


def issue_token(user_id: int) -> str:
    token = uuid.uuid4().hex
    token_store[token] = user_id
    _save_tokens()
    return token


async def get_current_user(
    x_token: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not x_token or x_token not in token_store:
        raise HTTPException(status_code=401, detail="未登录或 token 无效")
    user = await db.get(User, token_store[x_token])
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在")
    return user
