"""从本人已完成行程反馈生成简单、可解释的派生偏好。"""
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import median


NEGATIVE_TAGS = {"人多拥挤", "交通不便", "停留太短", "停留太长"}


def empty_derived_preferences() -> dict:
    return {
        "category_scores": {},
        "average_actual_stay_min": {},
        "avoid_tags": [],
        "updated_at": None,
        "sample_count": 0,
        "recommendation_ready": False,
    }


def normalized_preferences(raw: dict | None) -> dict:
    value = raw if isinstance(raw, dict) else {}
    return {
        "personalization_enabled": bool(value.get("personalization_enabled", True)),
        "explicit": value.get("explicit") or {
            "preferred_transport_modes": [], "preferred_categories": [], "avoid_tags": [],
        },
        "derived": value.get("derived") or empty_derived_preferences(),
    }


def build_derived_preferences(rows: list[dict]) -> dict:
    ratings: dict[str, list[int]] = defaultdict(list)
    stays: dict[str, list[int]] = defaultdict(list)
    negative_tags: Counter[str] = Counter()
    for row in rows:
        if not row.get("visited", True):
            continue
        category = str(row.get("category") or "").strip()
        if category and row.get("rating") is not None:
            ratings[category].append(int(row["rating"]))
        if category and row.get("actual_stay_min") is not None:
            stays[category].append(int(row["actual_stay_min"]))
        negative_tags.update(tag for tag in (row.get("tags") or []) if tag in NEGATIVE_TAGS)

    sample_count = sum(1 for row in rows if row.get("visited", True))
    return {
        "category_scores": {
            category: round((sum(values) / len(values)) * math.log(1 + len(values)), 3)
            for category, values in sorted(ratings.items())
        },
        "average_actual_stay_min": {
            category: round(float(median(values)), 1)
            for category, values in sorted(stays.items())
        },
        "avoid_tags": sorted(tag for tag, count in negative_tags.items() if count >= 2),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "sample_count": sample_count,
        "recommendation_ready": sample_count >= 3,
    }


def personalize_destinations(destinations: list[dict], raw_preferences: dict | None) -> list[dict]:
    """Add a bounded personal soft score without changing route or must-visit semantics."""
    preferences = normalized_preferences(raw_preferences)
    derived = preferences["derived"]
    active = bool(
        preferences["personalization_enabled"] and derived.get("recommendation_ready")
    )
    category_scores = derived.get("category_scores") or {}
    max_category_score = max((float(value) for value in category_scores.values()), default=0.0)
    explicit_categories = set(preferences["explicit"].get("preferred_categories") or [])

    enriched = []
    for destination in destinations:
        votes = destination.get("votes") or {}
        base_score = int(votes.get("up") or 0) - int(votes.get("down") or 0)
        category = str(destination.get("category") or "").strip()
        adjustment = 0.0
        reasons = []
        if active and category:
            learned_score = float(category_scores.get(category) or 0.0)
            if learned_score > 0 and max_category_score > 0:
                adjustment += 0.3 * learned_score / max_category_score
                reasons.append(f"你过去对{category}类地点评价较高")
            if category in explicit_categories:
                adjustment += 0.2
                reasons.append(f"你主动选择了偏好类别：{category}")
        adjustment = round(min(adjustment, 0.5), 3)
        enriched.append({
            **destination,
            "recommendation": {
                "active": active,
                "base_vote_score": base_score,
                "preference_adjustment": adjustment,
                "score": round(base_score + adjustment, 3),
                "reasons": reasons,
                "rank": None,
            },
        })

    rankable = sorted(
        (item for item in enriched if active and item["visit_status"] == "candidate"),
        key=lambda item: (-item["recommendation"]["score"], item["id"]),
    )
    for rank, item in enumerate(rankable, start=1):
        item["recommendation"]["rank"] = rank
    return enriched
