"""Single-request opt-in model diagnosis using a public synthetic assistant case."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import evaluate_agent as evaluation
from app.services.llm_parser import llm_place_parser

EVENTS = {f"{prefix}.{stage}.{ending}" for prefix, stages in {
    "connection": ("connect_tcp", "start_tls", "close"),
    "http11": ("send_request_headers", "send_request_body", "receive_response_headers", "receive_response_body", "response_closed"),
    "http2": ("send_connection_init", "send_request_headers", "send_request_body", "receive_response_headers", "receive_response_body", "response_closed"),
}.items() for stage in stages for ending in ("started", "complete", "failed")}
ERRORS = {"ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout", "ConnectError", "ReadError",
          "WriteError", "RemoteProtocolError", "LocalProtocolError", "HTTPStatusError"}


class SafeNetworkTrace:
    def __init__(self):
        self.started = time.perf_counter()
        self.events = []
        self.requests = 0
        self.http_status = None

    async def trace(self, name, info):
        if name not in EVENTS or len(self.events) >= 80:
            return
        event = {"event": name, "elapsed_ms": round((time.perf_counter() - self.started) * 1000, 3)}
        if name.endswith(".failed"):
            kind = type(info.get("exception")).__name__
            event["error_type"] = kind if kind in ERRORS else "transport_error"
        # Never retain info: it may contain hosts, credentials, headers or response data.
        self.events.append(event)

    async def attach(self, request):
        self.requests += 1
        request.extensions["trace"] = self.trace

    async def response(self, response):
        self.http_status = response.status_code


def classify(trace, result):
    if result.get("llm_used"):
        return "model_task_passed" if result.get("passed") else "model_task_checks_failed"
    failed = [e["event"] for e in trace.events if e["event"].endswith(".failed")]
    for stage, label in (("receive_response_body", "response_body_failed"),
                         ("receive_response_headers", "waiting_response_headers_failed"),
                         ("start_tls", "tls_failed"), ("connect_tcp", "tcp_connection_failed"),
                         ("send_request", "request_send_failed")):
        if any(stage in event for event in failed):
            return label
    if trace.http_status is not None and trace.http_status >= 400:
        return "http_error"
    return "model_unavailable_or_contract_rejected"


async def run(args):
    dataset, fingerprint = evaluation.load_cases(ROOT / "evals/agent_cases.json")
    case = next((case for case in dataset["cases"] if case["id"] == args.case_id and case["kind"] == "rules"), None)
    if case is None:
        raise ValueError("Choose a public rules case, not a model stub")
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "case_id": case["id"],
              "dataset_sha256": fingerprint, "live": args.live, "configured": llm_place_parser.available,
              "timeout_seconds": llm_place_parser.timeout, "real_model_calls": 0,
              "proxy_environment_present": any(os.getenv(k) for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")),
              "events": [], "http_status": None, "diagnosis": "not_run", "task_passed": None}
    if args.live:
        if not llm_place_parser.available:
            report["diagnosis"] = "not_configured"
        else:
            trace = SafeNetworkTrace()
            client = llm_place_parser._http_client()
            client.event_hooks["request"].append(trace.attach)
            client.event_hooks["response"].append(trace.response)
            try:
                result = await evaluation.evaluate_case(case, dataset["default_context"], "live")
            finally:
                client.event_hooks["request"].remove(trace.attach)
                client.event_hooks["response"].remove(trace.response)
                await llm_place_parser.aclose()
            report.update(events=trace.events, http_status=trace.http_status, real_model_calls=trace.requests,
                          diagnosis=classify(trace, result), task_passed=result["passed"],
                          llm_used=result["llm_used"], latency_ms=result["latency_ms"],
                          provider_error=result["provider_error"], checks=result["checks"], usage=result["usage"])
    report["diagnostic_source_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report["evaluator_source_sha256"] = hashlib.sha256(Path(evaluation.__file__).read_bytes()).hexdigest()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("configured", "live", "real_model_calls", "diagnosis", "http_status", "task_passed")}))
    return 0 if not args.live or report["task_passed"] is True else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="One real model request; may consume quota, no retry")
    parser.add_argument("--case-id", default="route_generate")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "docs_private/model_diagnostics/latest")
    try:
        return asyncio.run(run(parser.parse_args()))
    except Exception as exc:
        print("Model diagnosis failed: " + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
