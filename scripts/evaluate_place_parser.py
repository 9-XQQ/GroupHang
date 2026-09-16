"""Evaluate deterministic or optional LLM place extraction against a private JSON dataset."""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.amap import amap  # noqa: E402
from app.services.llm_parser import llm_place_parser  # noqa: E402
from app.services.shared_text import parse_shared_text  # noqa: E402

DEFAULT_CASES = ROOT / "docs_private" / "llm_evals" / "place_parser_cases.json"
DEFAULT_REPORT_DIR = ROOT / "docs_private" / "llm_evals" / "reports"


def normalize_name(value: str) -> str:
    return "".join(re.findall(r"[a-z0-9\u4e00-\u9fff]+", value.lower()))


def expected_alias_sets(expected_places: list[dict]) -> list[set[str]]:
    return [
        {normalize_name(name) for name in [place["name"], *place.get("aliases", [])] if normalize_name(name)}
        for place in expected_places
    ]


def score_names(predicted: list[dict], expected: list[dict]) -> dict:
    predicted_names = [normalize_name(item.get("name", "")) for item in predicted]
    predicted_names = [name for name in predicted_names if name]
    aliases = expected_alias_sets(expected)
    matched_expected: set[int] = set()
    true_positive = 0
    false_positive_names = []
    for raw_item, predicted_name in zip(
        [item for item in predicted if normalize_name(item.get("name", ""))], predicted_names
    ):
        match = next(
            (index for index, names in enumerate(aliases) if index not in matched_expected and predicted_name in names),
            None,
        )
        if match is None:
            false_positive_names.append(raw_item.get("name", ""))
        else:
            matched_expected.add(match)
            true_positive += 1
    missed = [place["name"] for index, place in enumerate(expected) if index not in matched_expected]
    return {
        "tp": true_positive,
        "fp": len(false_positive_names),
        "fn": len(missed),
        "false_positives": false_positive_names,
        "missed": missed,
    }


async def parse_case(mode: str, text: str) -> tuple[list[dict], str | None]:
    if mode == "rules":
        return parse_shared_text(text)["candidates"], None
    return await llm_place_parser.parse(text)


async def evaluate(args: argparse.Namespace) -> dict:
    dataset = json.loads(args.cases.read_text(encoding="utf-8"))
    case_results = []
    totals = defaultdict(int)
    total_latency_ms = 0.0
    geocode_success = 0
    geocode_attempts = 0

    for case in dataset["cases"]:
        started = time.perf_counter()
        predicted, error = await parse_case(args.mode, case["input_text"])
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        score = score_names(predicted, case["expected_places"])
        for key in ("tp", "fp", "fn"):
            totals[key] += score[key]
        totals["cases"] += 1
        totals["errors"] += int(bool(error))
        total_latency_ms += latency_ms

        geocoded = []
        if args.geocode:
            for item in predicted:
                query = item.get("address") or item.get("name")
                if not query:
                    continue
                geocode_attempts += 1
                location = await amap.geocode(query)
                geocode_success += int(bool(location))
                geocoded.append({"name": item.get("name"), "resolved": bool(location)})

        case_results.append({
            "id": case["id"], "category": case.get("category", ""),
            "expected": [item["name"] for item in case["expected_places"]],
            "predicted": predicted, "error": error, "latency_ms": latency_ms,
            **score, "geocoding": geocoded,
        })

    precision = totals["tp"] / (totals["tp"] + totals["fp"]) if totals["tp"] + totals["fp"] else 1.0
    recall = totals["tp"] / (totals["tp"] + totals["fn"]) if totals["tp"] + totals["fn"] else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "dataset_version": dataset.get("version"),
        "mode": args.mode,
        "generated_at": datetime.now().astimezone().isoformat(),
        "llm_available": llm_place_parser.available,
        "metrics": {
            "cases": totals["cases"], "true_positives": totals["tp"],
            "false_positives": totals["fp"], "false_negatives": totals["fn"],
            "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
            "errors": totals["errors"], "average_latency_ms": round(total_latency_ms / totals["cases"], 2),
            "geocode_attempts": geocode_attempts, "geocode_success": geocode_success,
            "geocode_success_rate": round(geocode_success / geocode_attempts, 4) if geocode_attempts else None,
        },
        "cases": case_results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="评测地点规则解析器或可选 LLM 解析器")
    parser.add_argument("--mode", choices=("rules", "llm"), default="rules")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--geocode", action="store_true", help="额外调用高德统计候选地理编码成功率")
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    if not args.cases.is_file():
        raise FileNotFoundError(f"评测集不存在：{args.cases}")
    if args.mode == "llm" and not llm_place_parser.available:
        print("LLM 未配置：请在 .env 设置 LLM_API_KEY 和 LLM_MODEL。", file=sys.stderr)
        return 2
    report = await evaluate(args)
    output = args.output or DEFAULT_REPORT_DIR / f"{args.mode}_latest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    metrics = report["metrics"]
    print(
        f"mode={args.mode} cases={metrics['cases']} precision={metrics['precision']:.4f} "
        f"recall={metrics['recall']:.4f} f1={metrics['f1']:.4f} "
        f"errors={metrics['errors']} avg_ms={metrics['average_latency_ms']:.2f}"
    )
    print(f"report={output}")
    await llm_place_parser.aclose()
    await amap.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
