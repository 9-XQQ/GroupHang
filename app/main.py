"""FastAPI 入口。"""
import os
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import models  # noqa: F401  确保模型注册到 Base.metadata
from .config import settings
from .db import engine
from .services.amap import amap
from .services.llm_parser import llm_place_parser
from .routers import auth, destinations, itineraries, meeting_points, participants, places, shared_text, trips, votes, ws


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await amap.aclose()
    await llm_place_parser.aclose()
    await engine.dispose()


app = FastAPI(title="出行规划 Agent", version="0.2.0", lifespan=lifespan)

# 开发阶段放开跨域，方便前端联调
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(trips.router)
app.include_router(participants.router)
app.include_router(destinations.router)
app.include_router(itineraries.router)
app.include_router(places.router)
app.include_router(shared_text.router)
app.include_router(meeting_points.router)
app.include_router(votes.router)
app.include_router(ws.router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底异常处理：debug 模式返回完整 traceback，方便定位；生产模式隐藏内部细节。

    HTTPException（401/404/409 等）由 FastAPI 更具体的 handler 处理，不会走到这里。
    """
    if settings.debug:
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

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=True)
