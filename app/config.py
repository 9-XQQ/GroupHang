"""应用配置：从 .env / 环境变量读取。"""
from typing import Literal
from pydantic import Field, SecretStr, model_validator
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


    # Phone login. No SMS is sent until a provider is explicitly configured.
    auth_secret: str = ""
    sms_provider: Literal["disabled", "mock", "aliyun"] = "disabled"
    aliyun_sms_enabled: bool = False
    aliyun_sms_access_key_id: SecretStr = SecretStr("")
    aliyun_sms_access_key_secret: SecretStr = SecretStr("")
    aliyun_sms_sign_name: str = ""
    aliyun_sms_template_code: str = ""
    aliyun_sms_code_parameter: str = Field(default="code", pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,31}$")
    aliyun_sms_timeout_seconds: float = Field(default=8, ge=1, le=30)
    auth_session_days: int = Field(default=7, ge=1, le=30)
    auth_cookie_secure: bool = False
    cors_origins: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_auth_environment(self):
        self.app_env = self.app_env.lower()
        if self.app_env not in {"development", "test", "production"}:
            raise ValueError("APP_ENV must be development, test or production")
        if "*" in self.cors_origins or "null" in self.cors_origins:
            raise ValueError("CORS origins must be explicit")
        if self.sms_provider == "mock" and self.app_env != "development":
            raise ValueError("Mock SMS is only allowed in development")
        if self.sms_provider == "aliyun" and self.aliyun_sms_enabled:
            if not all((self.aliyun_sms_access_key_id.get_secret_value(),
                        self.aliyun_sms_access_key_secret.get_secret_value(),
                        self.aliyun_sms_sign_name.strip(), self.aliyun_sms_template_code.strip())):
                raise ValueError("Enabled Aliyun SMS requires credentials, sign and template")
        if self.app_env == "production":
            if self.debug or not self.auth_cookie_secure or len(self.auth_secret) < 32:
                raise ValueError("Production requires DEBUG=false, secure cookies and AUTH_SECRET >=32 chars")
        return self


settings = Settings()
