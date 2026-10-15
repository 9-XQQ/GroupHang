"""Opt-in smoke check using public examples; never writes business data or prints keys."""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.services.amap import amap
from app.services.llm_parser import llm_place_parser
from app.services.agent_assistant import agent_assistant
from app.config import settings


async def check(live=False):
    result = {"configured": {"map_rest": amap.available,
              "map_js": bool(settings.amap_js_key and not settings.amap_js_key.startswith("your_")),
              "map_security": bool(settings.amap_js_security_code), "model": llm_place_parser.available}}
    if not live:
        return result
    try:
        pois = await amap.search_pois("人民公园", "上海市", limit=3)
        result["poi"] = {"ok": bool(pois), "names": [p["name"] for p in pois]}
        # Public landmarks, never member locations.
        for mode in ("driving", "transit"):
            detail = await amap.route_detail((31.2304, 121.4737), (31.2397, 121.4998), mode)
            result[mode] = {"ok": bool(detail and detail.get("polyline")),
                            "duration_min": detail.get("duration_min") if detail else None}
        places, error = await llm_place_parser.parse("上海市：人民公园，想散步；上海博物馆，想看展。不要推测地址。")
        result["extraction"] = {"ok": bool(places) and not error, "names": [p["name"] for p in places]}
        answer, used, error = await agent_assistant.chat(
            {"trip": {"primary_workflow": "itinerary"}, "destinations": [], "participants": []},
            "我还没选地点，请告诉我规划一下午行程还需要提供哪些信息。不要修改或执行操作。",
        )
        result["assistant"] = {"ok": used and not error, "has_reply": bool(answer.reply),
                               "action_types": [a.type for a in answer.actions]}
        result["usage"] = llm_place_parser.last_usage
        result["map_stats"] = amap.request_stats()
    finally:
        await amap.aclose()
        await llm_place_parser.aclose()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="允许少量真实地图/模型调用，可能消耗配额")
    args = parser.parse_args()
    try:
        result = asyncio.run(asyncio.wait_for(check(args.live), timeout=90))
    except Exception as exc:
        # Do not print provider payloads, URLs, credentials or exception text.
        result = {"ok": False, "error_type": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.live and (result.get("ok") is False or any(
            result.get(key, {}).get("ok") is not True for key in ("poi", "driving", "transit", "extraction", "assistant"))):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
