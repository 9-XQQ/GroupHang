"""FastAPI 入口。"""
import asyncio
import os
import traceback
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from . import models  # noqa: F401  确保模型注册到 Base.metadata
from .config import settings
from .db import engine
from .services.agent_state import sweep_agent_data
from .services.amap import amap
from .services.llm_parser import llm_place_parser
from .routers import saved_places, comments, discovery
from .routers import agent, auth, destinations, diagnostics, feedback, itineraries, meeting_points, participants, place_parse_feedback, places, shared_text, trips, users, votes, ws


@asynccontextmanager
async def lifespan(app: FastAPI):
    retention_task = asyncio.create_task(sweep_agent_data())
    try:
        yield
    finally:
        retention_task.cancel()
        with suppress(asyncio.CancelledError):
            await retention_task
        await amap.aclose()
        await llm_place_parser.aclose()
        await engine.dispose()


app = FastAPI(title="出行规划 Agent", version="0.2.0", lifespan=lifespan)

# 默认同源；跨域来源必须显式配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(saved_places.router)
app.include_router(discovery.router)
app.include_router(comments.router)
app.include_router(trips.router)
app.include_router(participants.router)
app.include_router(destinations.router)
app.include_router(feedback.router)
app.include_router(itineraries.router)
app.include_router(places.router)
app.include_router(diagnostics.router)
app.include_router(place_parse_feedback.router)
app.include_router(users.router)
app.include_router(agent.router)
app.include_router(shared_text.router)
app.include_router(meeting_points.router)
app.include_router(votes.router)
app.include_router(ws.router)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/auth/"):
        # FastAPI's default errors echo input values; never return passwords.
        return JSONResponse(status_code=422, content={"detail": "输入格式不正确，请检查用户名和密码长度"})
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底异常处理：debug 模式返回完整 traceback，方便定位；生产模式隐藏内部细节。

    HTTPException（401/404/409 等）由 FastAPI 更具体的 handler 处理，不会走到这里。
    """
    if settings.debug and settings.app_env == "development" and not request.url.path.startswith("/auth/"):
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        return JSONResponse(
            status_code=500,
            content={
                "detail": str(exc) or exc.__class__.__name__,
                "type": exc.__class__.__name__,
                "traceback": tb,
            },
        )
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误"})


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "env": settings.app_env}


READINESS_TIMEOUT_SECONDS = 3


@app.get("/ready")
async def readiness() -> JSONResponse:
    """Database connectivity probe; no credentials or exception details returned."""
    try:
        async with asyncio.timeout(READINESS_TIMEOUT_SECONDS):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(status_code=503, content={"status": "not_ready"},
                            headers={"Cache-Control": "no-store"})
    return JSONResponse(content={"status": "ready"},
                        headers={"Cache-Control": "no-store"})


@app.get("/api/config")
async def get_frontend_config() -> dict:
    """前端初始化配置（高德 JS Key 等），由页面加载时自动拉取。"""
    return {
        "amap_js_key": settings.amap_js_key,
        "amap_js_security_code": settings.amap_js_security_code,
        "llm_parser_available": llm_place_parser.available,
    }


# 前端静态页面（必须放在所有 API 路由之后，否则会覆盖 /health、/docs 等）
FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=settings.app_env == "development")
