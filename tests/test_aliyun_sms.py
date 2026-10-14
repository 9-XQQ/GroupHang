"""Aliyun adapter contract tests. Fixed public signing vector, no network."""
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
from pydantic import SecretStr, ValidationError

from app.config import Settings, settings
from app.services import sms


class AliyunSmsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = patch.multiple(settings, sms_provider="aliyun", aliyun_sms_enabled=True,
            aliyun_sms_access_key_id=SecretStr("fixture-id"),
            aliyun_sms_access_key_secret=SecretStr("fixture-secret"),
            aliyun_sms_sign_name="测试签名", aliyun_sms_template_code="SMS_fixture",
            aliyun_sms_code_parameter="code", aliyun_sms_timeout_seconds=1)
        self.config.start()
        self.addCleanup(self.config.stop)
        self.client_type = httpx.AsyncClient

    def transport(self, handler):
        return patch.object(sms.httpx, "AsyncClient", side_effect=lambda **kwargs:
            self.client_type(transport=httpx.MockTransport(handler), **kwargs))

    async def test_official_acs3_signature_vector(self):
        headers = sms.sign_headers({
            "host": "ecs.cn-shanghai.aliyuncs.com", "x-acs-action": "RunInstances",
            "x-acs-version": "2014-05-26", "x-acs-date": "2023-10-26T10:22:32Z",
            "x-acs-signature-nonce": "3156853299f313e23d1673dc12e1703d",
        }, "ImageId=win2019_1809_x64_dtc_zh-cn_40G_alibase_20230811.vhd&RegionId=cn-shanghai",
           b"", "YourAccessKeyId", "YourAccessKeySecret")
        self.assertTrue(headers["authorization"].endswith(
            "Signature=06563a9e1b43f5dfe96b81484da74bceab24a1d853912eee15083a6f0f3283c0"))

    async def test_post_body_parameters_and_unique_nonce(self):
        requests = []
        def handler(request):
            requests.append(request)
            self.assertEqual(str(request.url), sms.ENDPOINT)
            self.assertEqual(request.method, "POST")
            fields = parse_qs(request.content.decode())
            self.assertEqual(fields["PhoneNumbers"], ["13900000001"])
            self.assertEqual(fields["SignName"], ["测试签名"])
            self.assertEqual(json.loads(fields["TemplateParam"][0]), {"code": "123456"})
            self.assertIn("content-type;", request.headers["authorization"])
            return httpx.Response(200, json={"Code": "OK", "BizId": "fixture"})
        with self.transport(handler):
            await sms.send_code("13900000001", "123456")
            await sms.send_code("13900000001", "123456")
        self.assertNotEqual(requests[0].headers["x-acs-signature-nonce"],
                            requests[1].headers["x-acs-signature-nonce"])

    async def test_rejections_http_errors_redirects_and_bad_json_are_sanitized(self):
        responses = [
            (200, '{"Code":"isv.BUSINESS_LIMIT_CONTROL","Message":"fixture-secret"}'),
            (403, '{"Message":"fixture-secret"}'), (500, "fixture-secret"),
            (302, "fixture-secret"), (200, "fixture-secret"), (200, "[]"),
        ]
        for status, body in responses:
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(status, text=body, headers={"Location": "https://evil.example"})
            with self.subTest(status=status, body=body), self.transport(handler):
                with self.assertRaises(sms.SmsUnavailable) as caught:
                    await sms.send_code("13900000001", "123456")
                self.assertNotIn("fixture-secret", str(caught.exception))
                self.assertEqual(len(calls), 1)

    async def test_timeout_is_sanitized_and_not_retried(self):
        calls = []
        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout("fixture-secret", request=request)
        with self.transport(handler), self.assertRaises(sms.SmsUnavailable) as caught:
            await sms.send_code("13900000001", "123456")
        self.assertNotIn("fixture-secret", str(caught.exception))
        self.assertEqual(len(calls), 1)

    async def test_disabled_or_incomplete_configuration_never_creates_client(self):
        for config in ({"aliyun_sms_enabled": False},
                       {"aliyun_sms_access_key_secret": SecretStr("")},
                       {"sms_provider": "disabled"}):
            with patch.multiple(settings, **config), patch.object(sms.httpx, "AsyncClient") as client:
                with self.assertRaises(sms.SmsUnavailable):
                    await sms.send_code("13900000001", "123456")
                client.assert_not_called()

    async def test_config_can_wait_for_qualification_but_cannot_enable_incomplete(self):
        Settings(_env_file=None, sms_provider="aliyun", aliyun_sms_enabled=False)
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, sms_provider="aliyun", aliyun_sms_enabled=True)
        config = Settings(_env_file=None, aliyun_sms_access_key_secret="fixture-secret")
        self.assertNotIn("fixture-secret", repr(config))
