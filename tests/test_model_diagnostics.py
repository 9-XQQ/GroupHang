"""Model diagnosis stays opt-in and never stores HTTP payloads or credentials."""
import argparse
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from scripts import diagnose_model as diagnosis


class ModelDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_dry_run_has_no_network_or_client(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(live=False, case_id="route_generate", output_dir=Path(directory))
            with patch.object(diagnosis.llm_place_parser, "_http_client") as client, patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(await diagnosis.run(args), 0)
            client.assert_not_called()
            report = json.loads((Path(directory) / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["diagnosis"], "not_run")
            self.assertEqual(report["real_model_calls"], 0)

    async def test_event_whitelist_discards_sensitive_info_and_is_bounded(self):
        trace = diagnosis.SafeNetworkTrace()
        await trace.trace("connection.connect_tcp.complete", {"host": "secret-host", "return_value": "secret-token"})
        await trace.trace("custom.secret-key", {"data": "secret-prompt"})
        for _ in range(100):
            await trace.trace("http11.receive_response_headers.failed", {"exception": httpx.ReadTimeout("secret-url")})
        self.assertEqual(len(trace.events), 80)
        self.assertEqual(trace.events[-1]["error_type"], "ReadTimeout")
        self.assertNotIn("secret", json.dumps(trace.events))
        self.assertEqual(diagnosis.classify(trace, {"llm_used": False}), "waiting_response_headers_failed")

    async def test_success_and_http_error_through_actual_parser_with_stub_transport(self):
        for status in (200, 401):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                def handler(request):
                    return httpx.Response(status, json={"choices": [{"message": {"content": json.dumps({
                        "reply": "secret-reply", "actions": [{"type": "generate_itinerary"}]})}}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}},
                        headers={"X-Secret": "secret-header"})
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    parser = diagnosis.llm_place_parser
                    args = argparse.Namespace(live=True, case_id="route_generate", output_dir=Path(directory))
                    with patch.multiple(parser, api_key="secret-key", model="synthetic-model",
                                        base_url="https://synthetic.invalid/v1", _client=client), patch.object(
                                            parser, "_api_style", return_value="openai"), patch("sys.stdout", new=io.StringIO()):
                        self.assertEqual(await diagnosis.run(args), 0 if status == 200 else 1)
                report = json.loads((Path(directory) / "report.json").read_text(encoding="utf-8"))
                self.assertEqual(report["real_model_calls"], 1)  # HTTP attempt counter; transport is a test stub.
                self.assertEqual(report["http_status"], status)
                self.assertEqual(report["diagnosis"], "model_task_passed" if status == 200 else "http_error")
                self.assertNotIn("secret", json.dumps(report))
                self.assertNotIn("synthetic.invalid", json.dumps(report))
                if status == 200:
                    self.assertEqual(report["usage"]["total_tokens"], 120)

    async def test_unconfigured_live_is_failure_without_request(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(live=True, case_id="route_generate", output_dir=Path(directory))
            with patch.object(diagnosis.llm_place_parser, "api_key", ""), patch.object(
                    diagnosis.llm_place_parser, "_http_client") as client, patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(await diagnosis.run(args), 1)
            client.assert_not_called()
            report = json.loads((Path(directory) / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["diagnosis"], "not_configured")
            self.assertEqual(report["real_model_calls"], 0)

    async def test_unknown_and_stub_cases_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for case_id in ("unknown", "contract_invalid_action"):
                with self.assertRaises(ValueError):
                    await diagnosis.run(argparse.Namespace(live=True, case_id=case_id, output_dir=Path(directory)))

    async def test_connection_phase_classification_is_not_task_success(self):
        for event, wanted in (("connection.connect_tcp.failed", "tcp_connection_failed"),
                              ("connection.start_tls.failed", "tls_failed"),
                              ("http11.receive_response_body.failed", "response_body_failed")):
            trace = diagnosis.SafeNetworkTrace()
            await trace.trace(event, {"exception": RuntimeError("secret")})
            self.assertEqual(diagnosis.classify(trace, {"llm_used": False}), wanted)
            self.assertEqual(trace.events[0]["error_type"], "transport_error")
