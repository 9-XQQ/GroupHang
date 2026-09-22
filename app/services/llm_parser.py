"""可选的 LLM 地点提取器；失败时由路由层回退确定性规则。"""
import json
import re
import time
from collections import Counter
from contextvars import ContextVar
from datetime import datetime, timezone

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
    city: str = Field(default="", max_length=30)


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
        self._last_usage: ContextVar[dict | None] = ContextVar(f"llm_last_usage_{id(self)}", default=None)
        self._request_stats: Counter[str] = Counter()
        self._usage_totals: Counter[str] = Counter()
        self._last_request: dict | None = None

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

    @property
    def last_usage(self) -> dict | None:
        return self._last_usage.get()

    @staticmethod
    def _to_int(value) -> int | None:
        try:
            return max(0, int(value)) if value is not None else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def extract_usage(cls, payload: dict) -> dict | None:
        """统一 OpenAI/DeepSeek 与 Anthropic Messages 的 token usage。"""
        raw = payload.get("usage")
        if not isinstance(raw, dict):
            return None
        input_tokens = cls._to_int(raw.get("prompt_tokens"))
        if input_tokens is None:
            input_tokens = cls._to_int(raw.get("input_tokens"))
        output_tokens = cls._to_int(raw.get("completion_tokens"))
        if output_tokens is None:
            output_tokens = cls._to_int(raw.get("output_tokens"))
        cache_hit_tokens = cls._to_int(raw.get("prompt_cache_hit_tokens"))
        if cache_hit_tokens is None:
            details = raw.get("prompt_tokens_details") or raw.get("input_tokens_details") or {}
            cache_hit_tokens = cls._to_int(details.get("cached_tokens")) if isinstance(details, dict) else None
        if cache_hit_tokens is None:
            cache_hit_tokens = cls._to_int(raw.get("cache_read_input_tokens"))
        cache_creation_tokens = cls._to_int(raw.get("cache_creation_input_tokens")) or 0
        cache_miss_tokens = cls._to_int(raw.get("prompt_cache_miss_tokens"))
        if cache_miss_tokens is None and input_tokens is not None and cache_hit_tokens is not None:
            cache_miss_tokens = max(0, input_tokens - cache_hit_tokens)
        total_tokens = cls._to_int(raw.get("total_tokens"))
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        if all(value is None for value in (input_tokens, output_tokens, total_tokens, cache_hit_tokens, cache_miss_tokens)):
            return None
        hit = cache_hit_tokens or 0
        return {
            "input_tokens": input_tokens or 0,
            "output_tokens": output_tokens or 0,
            "total_tokens": total_tokens or 0,
            "cache_hit_tokens": hit,
            "cache_miss_tokens": cache_miss_tokens or 0,
            "cache_creation_tokens": cache_creation_tokens,
            "cache_hit_rate": round(hit / input_tokens, 4) if input_tokens else 0.0,
        }

    def diagnostics(self) -> dict:
        return {
            "available": self.available,
            "api_style": self._api_style(),
            "model": self.model,
            "requests": dict(self._request_stats),
            "usage_totals": dict(self._usage_totals),
            "last_request": self._last_request,
        }

    def _record_response(self, usage: dict | None, api_style: str, elapsed_ms: int) -> None:
        self._request_stats["response_received"] += 1
        self._request_stats["usage_reported" if usage is not None else "usage_missing"] += 1
        if usage:
            self._usage_totals.update({key: value for key, value in usage.items() if key != "cache_hit_rate"})
        self._last_request = {
            "at": datetime.now(timezone.utc).isoformat(),
            "api_style": api_style,
            "model": self.model,
            "elapsed_ms": elapsed_ms,
            "usage": usage,
        }

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
        self._last_usage.set(None)
        if not self.available:
            self._request_stats["unavailable"] += 1
            return [], "服务端未配置 LLM_API_KEY 或 LLM_MODEL"
        prompt = (
            "从用户提供的出行攻略或聊天文本中提取最多10个明确地点。"
            "只输出JSON对象，格式为 {\"places\":[{\"name\":\"\",\"address\":\"\","
            "\"category\":\"\",\"price\":\"\",\"reason\":\"\",\"city\":\"\"}]}。"
            "不要猜测不存在的地址；不确定字段使用空字符串；不要输出解释。\n\n用户文本：\n"
            + text[:6000]
        )
        started_at = time.perf_counter()
        api_style = self._api_style()
        self._request_stats["attempted"] += 1
        try:
            if api_style == "anthropic":
                url = f"{self.base_url}/v1/messages"
                headers = {
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": self.model,
                    "max_tokens": 2048,
                    "temperature": 0,
                    "system": "你是地点信息结构化提取器，只返回合法JSON。",
                    "messages": [{"role": "user", "content": prompt}],
                }
            else:
                url = f"{self.base_url}/chat/completions"
                headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
                payload = {
                    "model": self.model, "temperature": 0,
                    "messages": [
                        {"role": "system", "content": "你是地点信息结构化提取器，只返回合法JSON。"},
                        {"role": "user", "content": prompt},
                    ],
                }
            response = await self._http_client().post(url, headers=headers, json=payload)
            response.raise_for_status()
            response_payload = response.json()
            usage = self.extract_usage(response_payload)
            self._last_usage.set(usage)
            self._record_response(usage, api_style, round((time.perf_counter() - started_at) * 1000))
            content = self._extract_content(response_payload, api_style)
            places = self.parse_json_content(content)
            self._request_stats["succeeded"] += 1
            return places, None
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
            self._request_stats["failed"] += 1
            return [], f"LLM 解析失败：{type(exc).__name__}"

    def _api_style(self) -> str:
        """DeepSeek 等服务把 Anthropic 兼容入口放在 /anthropic 路径下。"""
        return "anthropic" if "/anthropic" in self.base_url.lower().split("?")[0] else "openai"

    @staticmethod
    def _extract_content(payload: dict, api_style: str) -> str:
        if api_style == "anthropic":
            blocks = payload["content"]
            return "".join(str(block.get("text", "")) for block in blocks if block.get("type") == "text")
        return payload["choices"][0]["message"]["content"]


llm_place_parser = LlmPlaceParser()
