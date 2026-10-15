"""Deterministic differences between stored snapshots; never recalculates a route."""
def compare_plans(previous: dict, current: dict) -> dict:
    before = {s["destination_id"]: s for s in previous.get("ordered_stops", [])}
    after = {s["destination_id"]: s for s in current.get("ordered_stops", [])}
    common = before.keys() & after.keys()
    def distance(plan):
        return sum(float(leg.get("distance_km") or 0) for leg in plan.get("route_legs", []))
    def returned(plan):
        return next((leg.get("return_location") for leg in plan.get("route_legs", []) if leg.get("kind") == "return"), None)
    return {
        "previous_plan_id": previous["plan_id"], "current_plan_id": current["plan_id"],
        "same_inputs": previous["input_version"] == current["input_version"],
        "added": [{"id": key, "name": after[key]["name"]} for key in after if key not in before],
        "removed": [{"id": key, "name": before[key]["name"]} for key in before if key not in after],
        "moved": [{"id": key, "name": after[key]["name"], "before": before[key]["order_index"],
                   "after": after[key]["order_index"]} for key in after if key in common
                  and before[key]["order_index"] != after[key]["order_index"]],
        "timing_changed": [{"id": key, "name": after[key]["name"], "before_arrival": before[key]["arrival_at"],
                            "after_arrival": after[key]["arrival_at"], "before_leave": before[key]["leave_at"],
                            "after_leave": after[key]["leave_at"]} for key in after if key in common
                           and (before[key]["arrival_at"], before[key]["leave_at"]) != (after[key]["arrival_at"], after[key]["leave_at"])],
        "travel_delta_min": current["total_travel_min"] - previous["total_travel_min"],
        "duration_delta_min": current["total_duration_min"] - previous["total_duration_min"],
        "distance_delta_km": round(distance(current) - distance(previous), 2),
        "return_changed": returned(previous) != returned(current),
        "previous_warning_count": len(previous.get("warnings") or []),
        "current_warning_count": len(current.get("warnings") or []),
    }
