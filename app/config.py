"""应用配置：从 .env / 环境变量读取。"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 高德地图开放平台（Web服务 Key，后端 REST 接口用）
    amap_api_key: str = ""
    amap_base_url: str = "https://restapi.amap.com"

    # 高德 JS API（Web端 Key，前端地图显示用；与上面的 Web服务 Key 是不同类型）
    amap_js_key: str = ""
    amap_js_security_code: str = ""

    # 可选 LLM 地点解析（OpenAI-compatible Chat Completions；未配置时自动使用规则解析）
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = ""
    llm_timeout_seconds: float = 20.0

    # 数据库（asyncpg 驱动）
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/trip_planner"

    # 服务
    app_env: str = "development"
    debug: bool = True
    host: str = "0.0.0.0"
    port: int = 8000


settings = Settings()
