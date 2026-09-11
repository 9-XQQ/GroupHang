"""可选的 LLM 地点提取器；失败时由路由层回退确定性规则。"""
import json
import re

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import settings


class LlmPlace(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    name: str = Field(min_length=1, max_length=120)
    address: str = Field(default="", max_length=300)
    category: str = Field(default="", max_length=80)
    price: str = Field(default="", max_length=50)
    reason: str = Field(default="", max_length=500)


class LlmPlaceResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    places: list[LlmPlace] = Field(default_factory=list, max_length=10)


class LlmPlaceParser:
    def __init__(self) -> None:
        self.api_key = settings.llm_api_key
        self.base_url = settings.llm_base_url.rstrip("/")
        self.model = settings.llm_model
        self.timeout = settings.llm_timeout_seconds
        self._client: httpx.AsyncClient | None = None

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.model) and not self.api_key.startswith("your_")

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=5.0))
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    @staticmethod
    def parse_json_content(content: str) -> list[dict]:
        """只接受通过本地 Pydantic schema 校验的 JSON 结果。"""
        cleaned = content.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.I | re.S)
        if fenced:
            cleaned = fenced.group(1)
        payload = json.loads(cleaned)
        validated = LlmPlaceResponse.model_validate(payload)
        return [place.model_dump() for place in validated.places]

    async def parse(self, text: str) -> tuple[list[dict], str | None]:
        if not self.available:
            return [], "服务端未配置 LLM_API_KEY 或 LLM_MODEL"
        prompt = (
            "从用户提供的出行攻略或聊天文本中提取最多10个明确地点。"
            "只输出JSON对象，格式为 {\"places\":[{\"name\":\"\",\"address\":\"\","
            "\"category\":\"\",\"price\":\"\",\"reason\":\"\"}]}。"
            "不要猜测不存在的地址；不确定字段使用空字符串；不要输出解释。\n\n用户文本：\n"
            + text[:6000]
        )
        try:
            response = await self._http_client().post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.model,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": "你是地点信息结构化提取器，只返回合法JSON。"},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            return self.parse_json_content(content), None
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
            return [], f"LLM 解析失败：{type(exc).__name__}"


llm_place_parser = LlmPlaceParser()
