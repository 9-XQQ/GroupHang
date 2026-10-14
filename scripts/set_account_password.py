"""Local administrator only: bind/reset credentials for an exact existing user ID.

No password arguments, stdin piping, SMS bypass or public reset endpoint.
"""
import argparse
import asyncio
import getpass
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.models import AuthSession, PasswordCredential, User
from app.schemas import PasswordRegisterRequest
from app.services.password_auth import hash_password


async def set_account_password(db, user_id: int, username: str, encoded: str) -> None:
    user = (await db.execute(select(User).where(User.id == user_id).with_for_update())).scalar_one_or_none()
    if user is None:
        raise ValueError("目标用户不存在")
    current = await db.get(PasswordCredential, user_id, populate_existing=True)
    if current and current.username != username:
        raise ValueError("该用户已有其他用户名；重置时必须使用原用户名")
    owner = await db.scalar(select(PasswordCredential.user_id).where(PasswordCredential.username == username))
    if owner is not None and owner != user_id:
        raise ValueError("该用户名已属于其他用户")
    if current:
        current.password_hash = encoded
    else:
        db.add(PasswordCredential(user_id=user_id, username=username, password_hash=encoded))
    await db.execute(update(AuthSession).where(AuthSession.user_id == user_id).values(
        revoked_at=datetime.now(timezone.utc)))
    await db.flush()


async def run(user_id: int, username: str, encoded: str):
    engine = create_async_engine(settings.database_url, echo=False, hide_parameters=True)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            await set_account_password(db, user_id, username, encoded)
            await db.commit()
        print(f"已为 user_id={user_id} 设置密码，并撤销其旧会话。账号数据保留。")
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--username", required=True)
    args = parser.parse_args()
    if args.user_id <= 0 or not sys.stdin.isatty():
        raise ValueError("需要正整数用户ID，并在本机交互终端安全输入密码")
    password = getpass.getpass("设置新密码（8～15字符）: ")
    confirmation = getpass.getpass("再次输入: ")
    if password != confirmation:
        raise ValueError("两次密码不一致")
    body = PasswordRegisterRequest(username=args.username, password=password)
    asyncio.run(run(args.user_id, body.username, hash_password(body.password)))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Validation errors may embed password input, SQL errors may carry data.
        print("操作失败：请检查用户ID、用户名归属、密码长度及数据库配置。", file=sys.stderr)
        raise SystemExit(1)
