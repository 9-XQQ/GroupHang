"""Reproducible assistant-proposal evaluation. Offline by default, never executes tools."""
from __future__ import annotations
import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services.agent_assistant import AgentAssistant, normalize_actions
from app.services.llm_parser import llm_place_parser


def merge(base, updates):
    result = deepcopy(base)
    for key, value in updates.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else deepcopy(value)
    return result


def load_cases(path):
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data.get("default_context"), dict) or not data.get("cases"):
        raise ValueError("Dataset needs default_context and nonempty cases")
    ids = set()
    for case in data["cases"]:
        if not isinstance(case.get("id"), str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", case["id"]) or case["id"] in ids:
            raise ValueError("Case IDs must be nonempty and unique")
        ids.add(case["id"])
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", case.get("category", "")):
            raise ValueError("Categories must be safe identifiers")
        if case.get("kind") not in {"rules", "contract"} or not isinstance(case.get("message"), str):
            raise ValueError("Invalid case kind/message")
        if not isinstance(case.get("expected", {}).get("actions"), list):
            raise ValueError("Every case needs expected.actions, including empty actions")
    return data, hashlib.sha256(raw).hexdigest()


def score_actions(expected, actual):
    remaining = list(actual)
    matched = 0
    for wanted in expected:
        index = next((i for i, item in enumerate(remaining) if all(item.get(k) == v for k, v in wanted.items())), None)
        if index is not None:
            matched += 1
            remaining.pop(index)
    return {"matched": matched, "expected": len(expected), "predicted": len(actual),
            "exact": matched == len(expected) == len(actual)}


def percentile(values, fraction):
    return round(sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)], 3) if values else None


async def evaluate_case(case, default_context, mode, timeout=45):
    context = merge(default_context, case.get("context_overrides", {}))
    readiness = AgentAssistant.planning_readiness(context)
    context["planning_readiness"] = readiness
    expected = case["expected"]
    started = time.perf_counter()
    usage = None
    error_type = None
    actual = []
    reply = ""
    used = False
    warning = None
    try:
        assistant = AgentAssistant()  # No history crosses cases.
        if mode == "offline":
            payload = case.get("model_payload") if case["kind"] == "contract" else None
            # Hard block any accidental network access, including future adapter changes.
            with (
                patch.object(llm_place_parser, "complete_json", AsyncMock(return_value=(payload, None if payload else "offline_eval"))),
                patch("httpx.AsyncClient.send", AsyncMock(side_effect=AssertionError("Offline eval attempted network"))),
            ):
                answer, used, warning = await asyncio.wait_for(assistant.chat(context, case["message"]), timeout)
        else:
            async def call_with_usage():
                response = await assistant.chat(context, case["message"])
                # wait_for creates a Task; ContextVar updates do not flow back
                # into its parent. Read usage inside the task that made the call.
                return (*response, llm_place_parser.last_usage)
            answer, used, warning, usage = await asyncio.wait_for(call_with_usage(), timeout)
        actual = [a.model_dump(mode="json") for a in normalize_actions(answer.actions)]
        reply = answer.reply
    except Exception as exc:
        error_type = type(exc).__name__  # Never record exception text, inputs or provider payloads.
    provider_error = None
    if not used and warning:
        known = re.search(r"\b(ReadTimeout|ConnectTimeout|PoolTimeout|WriteTimeout|ConnectError|ReadError|HTTPStatusError|JSONDecodeError|ValueError|KeyError|IndexError|TypeError)\b", warning)
        provider_error = known.group(1) if known else "schema_rejected" if "schema" in warning else "unavailable_or_fallback"
    action_score = score_actions(expected["actions"], actual)
    checks = {"no_exception": error_type is None, "actions_exact": action_score["exact"]}
    if "ready_to_plan" in expected:
        checks["readiness"] = readiness["ready_to_plan"] == expected["ready_to_plan"]
    for field, source in (("missing_codes", "missing"), ("blocker_codes", "blockers")):
        if field in expected:
            checks[field] = set(expected[field]) == {item["code"] for item in readiness[source]}
    if "reply_contains" in expected:
        checks["reply_terms"] = all(term in reply for term in expected["reply_contains"])
    if "llm_used" in expected:
        checks["provider_contract"] = used == expected["llm_used"]
    if mode == "live":
        checks["real_model_response"] = used  # Fallback success must not masquerade as model success.
    return {"id":case["id"], "category":case.get("category", "uncategorized"), "kind":case["kind"],
            "passed":all(checks.values()), "checks":checks, "action_score":action_score,
            "predicted_action_types":[a["type"] for a in actual], "llm_used":used,
            "latency_ms":round((time.perf_counter()-started)*1000, 3), "usage":usage, "error_type":error_type, "provider_error":provider_error}


def summarize(results):
    total = len(results)
    passed = sum(r["passed"] for r in results)
    matched = sum(r["action_score"]["matched"] for r in results)
    expected = sum(r["action_score"]["expected"] for r in results)
    predicted = sum(r["action_score"]["predicted"] for r in results)
    categories = {}
    for row in results:
        group = categories.setdefault(row["category"], {"cases":0, "passed":0})
        group["cases"] += 1
        group["passed"] += int(row["passed"])
    tokens = {key:sum((r["usage"] or {}).get(key, 0) for r in results)
              for key in ("input_tokens", "output_tokens", "total_tokens", "cache_hit_tokens")}
    return {"cases":total, "passed":passed, "failed":total-passed,
            "task_pass_rate":round(passed/total,4) if total else None,
            "action_precision":round(matched/predicted,4) if predicted else None,
            "action_recall":round(matched/expected,4) if expected else None,
            "unexpected_actions":sum(r["action_score"]["predicted"] for r in results if r["action_score"]["expected"] == 0),
            "exceptions":sum(r["error_type"] is not None for r in results),
            "fallback_cases":sum(not r["llm_used"] for r in results),
            "p50_latency_ms":percentile([r["latency_ms"] for r in results],.5),
            "p95_latency_ms":percentile([r["latency_ms"] for r in results],.95),
            "usage_reported_cases":sum(r["usage"] is not None for r in results),
            "tokens":tokens, "categories":categories}


def markdown_report(report):
    m = report["metrics"]
    lines = ["# Agent proposal evaluation", "", f"Mode: {report['mode']}; dataset: {report['dataset_version']}",
             "", "This evaluates assistant proposals only. It does not execute business tools or prove recommendation/navigation quality.",
             "Offline latency is local contract/rule latency, not model latency; tokens are measured only when reported by the provider.",
             "", f"Passed: {m['passed']}/{m['cases']}; unexpected actions: {m['unexpected_actions']}; exceptions: {m['exceptions']}",
             f"p50 / p95: {m['p50_latency_ms']} / {m['p95_latency_ms']} ms", "",
             "| Category | Passed | Cases |", "|---|---:|---:|"]
    lines.extend(f"| {name} | {group['passed']} | {group['cases']} |" for name, group in m["categories"].items())
    lines += ["", "| Case | Result | Failed checks |", "|---|---|---|"]
    # IDs and category names are curated identifiers; no prompts/responses appear in reports.
    lines.extend(f"| {row['id']} | {'PASS' if row['passed'] else 'FAIL'} | {', '.join(k for k,v in row['checks'].items() if not v)} |" for row in report["cases"])
    return "\n".join(lines) + "\n"


async def run(args):
    if args.max_cases is not None and args.max_cases <= 0:
        raise ValueError("max-cases must be positive")
    dataset, fingerprint = load_cases(args.cases)
    cases = dataset["cases"]
    if args.mode == "live":
        if not llm_place_parser.available:
            raise ValueError("Live model is not configured")
        if args.max_cases is None or not 1 <= args.max_cases <= 50:
            raise ValueError("Live mode requires explicit --max-cases between 1 and 50")
        cases = [c for c in cases if c["kind"] != "contract"]
    if args.category:
        cases = [c for c in cases if c["category"] == args.category]
    if args.max_cases:
        cases = cases[:args.max_cases]
    if not cases:
        raise ValueError("No matching cases; an empty run is never a pass")
    try:
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True).stdout)
    except (OSError, subprocess.CalledProcessError):
        revision, dirty = None, None
    try:
        results = [await evaluate_case(case, dataset["default_context"], args.mode) for case in cases]
    finally:
        await llm_place_parser.aclose()
    report = {"report_version":1, "mode":args.mode, "network_disabled":args.mode == "offline",
              "real_model_calls":len(cases) if args.mode == "live" else 0, "dataset_version":dataset["version"],
              "dataset_sha256":fingerprint, "dataset_cases":len(dataset["cases"]), "selected_cases":len(cases),
              "prompt_sha256":hashlib.sha256(AgentAssistant.system_prompt().encode()).hexdigest(),
              "agent_source_sha256":hashlib.sha256((ROOT/"app/services/agent_assistant.py").read_bytes()).hexdigest(),
              "evaluator_source_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "git_revision":revision, "git_dirty":dirty, "generated_at":datetime.now(timezone.utc).isoformat(),
              "model":llm_place_parser.model if args.mode == "live" else None,
              "metrics":summarize(results), "cases":results}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (args.output_dir/"report.md").write_text(markdown_report(report),encoding="utf-8")
    print(json.dumps({"mode":args.mode,"metrics":report["metrics"]},ensure_ascii=False))
    return 0 if report["metrics"]["failed"] == 0 else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode",choices=("offline","live"),default="offline")
    parser.add_argument("--cases",type=Path,default=ROOT/"evals/agent_cases.json")
    parser.add_argument("--output-dir",type=Path,default=ROOT/"docs_private/agent_evals/latest")
    parser.add_argument("--max-cases",type=int)
    parser.add_argument("--category")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except Exception as exc:
        print(f"Evaluation failed: {type(exc).__name__}",file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
