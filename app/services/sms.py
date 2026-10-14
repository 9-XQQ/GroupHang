"""Aliyun SendSms over HTTPS, ACS3 signing, no automatic paid-send retries."""
import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone
from urllib.parse import urlencode
from uuid import uuid4

import httpx

from ..config import settings

ENDPOINT = "https://dysmsapi.aliyuncs.com/"


class SmsUnavailable(Exception):
    pass


def ensure_available() -> None:
    if settings.sms_provider == "mock" and settings.app_env == "development":
        return
    if settings.sms_provider == "aliyun" and settings.aliyun_sms_enabled:
        if all((settings.aliyun_sms_access_key_id.get_secret_value(),
                settings.aliyun_sms_access_key_secret.get_secret_value(),
                settings.aliyun_sms_sign_name, settings.aliyun_sms_template_code)):
            return
    raise SmsUnavailable("短信服务尚未启用或配置不完整")


def sign_headers(headers: dict[str, str], query: str, body: bytes,
                 access_key_id: str, access_key_secret: str) -> dict[str, str]:
    headers = dict(headers)
    payload_hash = hashlib.sha256(body).hexdigest()
    headers["x-acs-content-sha256"] = payload_hash
    keys = sorted(k for k in headers if k.startswith("x-acs-") or k in {"host", "content-type"})
    signed = ";".join(keys)
    canonical_headers = "".join(f"{k}:{headers[k].strip()}\n" for k in keys)
    canonical = "\n".join(("POST", "/", query, canonical_headers, signed, payload_hash))
    to_sign = "ACS3-HMAC-SHA256\n" + hashlib.sha256(canonical.encode()).hexdigest()
    signature = hmac.new(access_key_secret.encode(), to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        f"ACS3-HMAC-SHA256 Credential={access_key_id},SignedHeaders={signed},Signature={signature}"
    )
    return headers


async def send_code(phone: str, code: str) -> None:
    ensure_available()
    if settings.sms_provider == "mock":
        return
    # POST form keeps phone/code out of URL and common HTTP access logs.
    body = urlencode({
        "PhoneNumbers": phone,
        "SignName": settings.aliyun_sms_sign_name,
        "TemplateCode": settings.aliyun_sms_template_code,
        "TemplateParam": json.dumps({settings.aliyun_sms_code_parameter: code},
                                   ensure_ascii=False, separators=(",", ":")),
    }).encode()
    headers = sign_headers({
        "host": "dysmsapi.aliyuncs.com",
        "content-type": "application/x-www-form-urlencoded",
        "x-acs-action": "SendSms",
        "x-acs-version": "2017-05-25",
        "x-acs-date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "x-acs-signature-nonce": uuid4().hex,
    }, "", body, settings.aliyun_sms_access_key_id.get_secret_value(),
        settings.aliyun_sms_access_key_secret.get_secret_value())
    try:
        # A total deadline also bounds response reading. Redirects/proxy env
        # and retries are disabled so credentials stay on the fixed endpoint.
        async with asyncio.timeout(settings.aliyun_sms_timeout_seconds):
            async with httpx.AsyncClient(
                timeout=settings.aliyun_sms_timeout_seconds,
                follow_redirects=False, trust_env=False,
            ) as client:
                response = await client.post(ENDPOINT, content=body, headers=headers)
                if response.status_code != 200:
                    raise SmsUnavailable("短信服务请求失败")
                result = response.json()
                if not isinstance(result, dict) or result.get("Code") != "OK":
                    raise SmsUnavailable("短信服务拒绝发送")
    except (httpx.HTTPError, TimeoutError, ValueError):
        # Provider messages, request objects and payloads can contain secrets.
        raise SmsUnavailable("短信发送未确认，请稍后重新获取") from None
