"""Phone verification login and per-device session revocation."""
import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from ipaddress import ip_address

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import AccountRecoveryCode, AuthSession, PasswordCredential, User
from ..schemas import (LoginRequest, SmsCodeRequest, PasswordRegisterRequest,
                       PasswordLoginRequest, PasswordChangeRequest, RecoveryCodeCreateRequest, PasswordRecoverRequest)
from ..services.password_auth import (DUMMY_HASH, hash_password, verify_password,
                                     password_work, reserve_auth_attempt)
from ..security import (COOKIE_NAME, check_browser_origin, get_current_user, issue_token,
                        request_token, session_for_token)
from ..services.phone_auth import request_code, verify_code

router = APIRouter(prefix="/auth", tags=["auth"])


def user_summary(user: User) -> dict:
    return {"id": user.id, "name": user.name, "phone": user.phone}


@router.post("/sms-code")
async def sms_code(body: SmsCodeRequest, request: Request, response: Response,
                   db: AsyncSession = Depends(get_db)) -> dict:
    check_browser_origin(request)
    response.headers["Cache-Control"] = "no-store"
    # Do not trust arbitrary X-Forwarded-For. Configure trusted proxy addresses
    # on Uvicorn when deployed; request.client then carries the vetted peer.
    ip = request.client.host if request.client else "unknown"
    if settings.sms_provider == "mock":
        try:
            local = ip_address(ip).is_loopback
        except ValueError:
            local = False
        if not local:
            raise HTTPException(403, "模拟短信仅允许本机访问")
    return await request_code(db, body.phone, ip)


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response,
                db: AsyncSession = Depends(get_db)) -> dict:
    check_browser_origin(request)
    if not body.phone or not body.code or body.wechat_code:
        raise HTTPException(400, "请使用手机号和短信验证码登录")
    await verify_code(db, body.phone, body.code)
    user = (await db.execute(select(User).where(User.phone == body.phone))).scalar_one_or_none()
    if user is None:
        user = User(phone=body.phone, name=body.name or f"用户{body.phone[-4:]}")
        db.add(user)
        await db.flush()
    elif body.name:
        user.name = body.name
    return await complete_login(db, user, body.browser, request, response)


async def complete_login(db: AsyncSession, user: User, browser: bool,
                         request: Request, response: Response) -> dict:
    if browser:
        previous = await session_for_token(db, request.cookies.get(COOKIE_NAME))
        if previous:
            previous.revoked_at = datetime.now(timezone.utc)
    token, expires = await issue_token(db, user.id)
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    result = {"user": user_summary(user), "expires_at": expires.isoformat()}
    if browser:
        response.set_cookie(COOKIE_NAME, token, httponly=True,
                            secure=settings.auth_cookie_secure, samesite="lax",
                            max_age=settings.auth_session_days * 86400, path="/")
    else:
        result["token"] = token
    return result


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/register")
async def register(body: PasswordRegisterRequest, request: Request, response: Response,
                   db: AsyncSession = Depends(get_db)) -> dict:
    check_browser_origin(request)
    await reserve_auth_attempt(db, "register", body.username, client_ip(request))
    if await db.scalar(select(PasswordCredential.user_id).where(
            PasswordCredential.username == body.username)):
        raise HTTPException(409, "用户名已被使用")
    encoded = await password_work(hash_password, body.password)
    user = User(name=body.name or body.username)
    db.add(user)
    await db.flush()
    db.add(PasswordCredential(user_id=user.id, username=body.username, password_hash=encoded))
    try:
        await db.flush()
        return await complete_login(db, user, body.browser, request, response)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "用户名已被使用") from None


@router.post("/password/login")
async def password_login(body: PasswordLoginRequest, request: Request, response: Response,
                         db: AsyncSession = Depends(get_db)) -> dict:
    check_browser_origin(request)
    await reserve_auth_attempt(db, "login", body.username, client_ip(request))
    credential = (await db.execute(select(PasswordCredential).where(
        PasswordCredential.username == body.username))).scalar_one_or_none()
    encoded = credential.password_hash if credential else DUMMY_HASH
    valid = await password_work(verify_password, body.password, encoded)
    if not valid or credential is None:
        raise HTTPException(401, "用户名或密码错误")
    user = (await db.execute(select(User).where(User.id == credential.user_id).with_for_update())).scalar_one()
    current = await db.get(PasswordCredential, user.id, populate_existing=True)
    if current is None or current.password_hash != encoded:
        raise HTTPException(401, "用户名或密码错误")
    return await complete_login(db, user, body.browser, request, response)


@router.post("/password/change")
async def password_change(body: PasswordChangeRequest, request: Request, response: Response,
                          user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)) -> dict:
    await reserve_auth_attempt(db, "change", str(user.id), client_ip(request))
    credential = await db.get(PasswordCredential, user.id)
    encoded = credential.password_hash if credential else DUMMY_HASH
    valid = await password_work(verify_password, body.current_password, encoded)
    if not valid or credential is None:
        raise HTTPException(400, "当前密码错误")
    replacement = await password_work(hash_password, body.new_password)
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    current = await db.get(PasswordCredential, user.id, populate_existing=True)
    if current is None or current.password_hash != encoded:
        raise HTTPException(409, "密码已发生变化，请重新登录")
    current.password_hash = replacement
    await db.execute(delete(AccountRecoveryCode).where(AccountRecoveryCode.user_id == user.id))
    await db.execute(update(AuthSession).where(AuthSession.user_id == user.id).values(
        revoked_at=datetime.now(timezone.utc)))
    return await complete_login(db, user, body.browser, request, response)




@router.get("/recovery-code")
async def recovery_code_status(response: Response, user: User = Depends(get_current_user),
                               db: AsyncSession = Depends(get_db)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return {"enabled": await db.get(AccountRecoveryCode, user.id) is not None}


@router.post("/recovery-code")
async def create_recovery_code(body: RecoveryCodeCreateRequest, request: Request, response: Response,
                               user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await reserve_auth_attempt(db, "recovery-create", str(user.id), client_ip(request))
    credential = await db.get(PasswordCredential, user.id)
    encoded = credential.password_hash if credential else DUMMY_HASH
    valid = await password_work(verify_password, body.current_password, encoded)
    if not valid or credential is None:
        raise HTTPException(400, "当前密码错误")
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    current = await db.get(PasswordCredential, user.id, populate_existing=True)
    if current is None or current.password_hash != encoded:
        raise HTTPException(409, "密码已发生变化，请重新登录")
    code = secrets.token_hex(24)  # 192 random bits; a fast digest is safe for a random secret.
    await db.execute(delete(AccountRecoveryCode).where(AccountRecoveryCode.user_id == user.id))
    db.add(AccountRecoveryCode(user_id=user.id, code_hash=hashlib.sha256(code.encode()).hexdigest()))
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"recovery_code": "-".join(code[i:i+8] for i in range(0, len(code), 8))}


@router.post("/password/recover")
async def recover_password(body: PasswordRecoverRequest, request: Request, response: Response,
                           db: AsyncSession = Depends(get_db)) -> dict:
    check_browser_origin(request)
    await reserve_auth_attempt(db, "recover", body.username, client_ip(request))
    credential = (await db.execute(select(PasswordCredential).where(
        PasswordCredential.username == body.username))).scalar_one_or_none()
    candidate = hashlib.sha256(body.recovery_code.encode()).hexdigest()
    recovery = await db.get(AccountRecoveryCode, credential.user_id if credential else -1)
    valid = hmac.compare_digest(candidate, recovery.code_hash if recovery else "0" * 64)
    if not valid or credential is None or recovery is None:
        raise HTTPException(400, "用户名或恢复码无效，请核对后重试或联系管理员")
    replacement = await password_work(hash_password, body.new_password)
    await db.execute(select(User.id).where(User.id == credential.user_id).with_for_update())
    current = await db.get(AccountRecoveryCode, credential.user_id, populate_existing=True)
    if current is None or not hmac.compare_digest(candidate, current.code_hash):
        raise HTTPException(400, "用户名或恢复码无效，请核对后重试或联系管理员")
    locked_credential = await db.get(PasswordCredential, credential.user_id, populate_existing=True)
    locked_credential.password_hash = replacement
    await db.delete(current)
    await db.execute(update(AuthSession).where(AuthSession.user_id == credential.user_id).values(
        revoked_at=datetime.now(timezone.utc)))
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(COOKIE_NAME, path="/", secure=settings.auth_cookie_secure,
                           httponly=True, samesite="lax")
    return {"recovered": True, "message": "密码已重置，请重新登录并生成新的恢复码"}


@router.get("/me")
async def me(response: Response, user: User = Depends(get_current_user)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return {"user": user_summary(user)}


@router.post("/logout")
async def logout(request: Request, response: Response,
                 db: AsyncSession = Depends(get_db)) -> dict:
    check_browser_origin(request)
    session = await session_for_token(db, request_token(request))
    if session:
        session.revoked_at = datetime.now(timezone.utc)
        await db.commit()
    response.delete_cookie(COOKIE_NAME, path="/", secure=settings.auth_cookie_secure,
                           httponly=True, samesite="lax")
    response.headers["Cache-Control"] = "no-store"
    return {"logged_out": True}
