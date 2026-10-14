"""Phase 5：受控的 Trip 对话助手与短期操作草案。"""
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .llm_parser import llm_place_parser


class AgentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal[
        "switch_workflow", "update_planning_settings", "update_destination",
        "prepare_place_import", "compare_itineraries", "generate_itinerary"
    ]
    primary_workflow: Literal["meeting", "itinerary"] | None = None
    planned_start_at: datetime | None = None
    planned_end_at: datetime | None = None
    group_transport_mode: Literal["driving", "transit"] | None = None
    optimization_objective: Literal["balanced", "total_time", "distance"] | None = None
    import_text: str | None = Field(default=None, max_length=4000)
    destination_id: int | None = Field(default=None, gt=0)
    expected_stay_min: int | None = Field(default=None, ge=5, le=720)
    visit_status: Literal["candidate", "must_visit", "optional", "excluded"] | None = None
    visit_window_start: datetime | None = None
    visit_window_end: datetime | None = None
    clear_visit_window: bool = False

    @model_validator(mode="after")
    def validate_action(self):
        if self.type == "switch_workflow" and self.primary_workflow is None:
            raise ValueError("切换方案必须给出 primary_workflow")
        if self.type == "update_planning_settings":
            if not self.planned_start_at or not self.planned_end_at:
                raise ValueError("规划参数必须同时给出开始和结束时间")
            if self.planned_start_at.tzinfo is None or self.planned_end_at.tzinfo is None:
                raise ValueError("规划时间必须包含时区")
            if self.planned_end_at <= self.planned_start_at:
                raise ValueError("结束时间必须晚于开始时间")
        if self.type == "prepare_place_import" and (not self.import_text or len(self.import_text.strip()) < 2):
            raise ValueError("地点导入草案必须包含待解析文本")
        if self.clear_visit_window and self.type != "update_destination":
            raise ValueError("清除时间窗只能用于修改地点")
        if self.clear_visit_window and (self.visit_window_start is not None or self.visit_window_end is not None):
            raise ValueError("不能同时清除和设置地点固定时间窗")
        if self.type == "update_destination":
            if self.destination_id is None:
                raise ValueError("修改地点必须给出 destination_id")
            if not self.clear_visit_window and all(value is None for value in (
                self.expected_stay_min, self.visit_status,
                self.visit_window_start, self.visit_window_end,
            )):
                raise ValueError("修改地点草案至少包含一个变更")
            if (self.visit_window_start is None) != (self.visit_window_end is None):
                raise ValueError("地点固定到访开始和结束时间必须同时提供")
            if self.visit_window_start:
                if self.visit_window_start.tzinfo is None or self.visit_window_end.tzinfo is None:
                    raise ValueError("地点固定到访时间必须包含时区")
                if self.visit_window_end <= self.visit_window_start:
                    raise ValueError("地点固定到访结束时间必须晚于开始时间")
                if (self.visit_window_end - self.visit_window_start).total_seconds() > 24 * 3600:
                    raise ValueError("单个地点固定到访时间窗不能超过 24 小时")
        return self


def normalize_actions(actions: list[AgentAction]) -> list[AgentAction]:
    """Keep every destination edit in order; singleton actions use the last value."""
    order = {
        "switch_workflow": 0, "update_planning_settings": 1, "update_destination": 2,
        "prepare_place_import": 3, "compare_itineraries": 4, "generate_itinerary": 5,
    }
    actions = [action.model_copy(update={"import_text": None})
               if action.type != "prepare_place_import" else action for action in actions]
    singletons = {action.type: action for action in actions if action.type != "update_destination"}
    edits = [action for action in actions if action.type == "update_destination"]
    return sorted([*singletons.values(), *edits], key=lambda action: order[action.type])


class AgentModelAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply: str = Field(min_length=1, max_length=2000)
    actions: list[AgentAction] = Field(default_factory=list, max_length=4)


@dataclass
class StoredProposal:
    proposal_id: str
    trip_id: int
    user_id: int
    input_version: int
    actions: list[dict]
    expires_at: float
    claimed: bool = False


class AgentAssistant:
    def __init__(self) -> None:
        self._proposals: dict[str, StoredProposal] = {}
        self._conversations: dict[tuple[int, int], tuple[float, list[dict]]] = {}
        self._history_epochs: dict[tuple[int, int], str] = {}

    @staticmethod
    def system_prompt() -> str:
        return (
            "你是多人出行规划助手。只能依据给定的结构化 Trip 上下文回答，不得编造地点坐标、道路、"
            "公交或实时事实。只输出 JSON：{\"reply\":\"中文回答\",\"actions\":[]}。"
            "允许的 action 只有 switch_workflow、update_planning_settings、update_destination、"
            "prepare_place_import、compare_itineraries、generate_itinerary。"
            "修改规划参数必须同时提供含时区的 planned_start_at、planned_end_at、group_transport_mode、"
            "optimization_objective。保存这些规划参数不要求参与者出发点或目的地已经填写，"
            "不要把保存参数误判为立即生成路线；用户明确给出日期、时间、交通方式和目标时应生成草案。"
            "北京时间、中国时间、Asia/Shanghai 均使用 +08:00。真正缺少必填值时才追问并让 actions 留空。"
            "用户要求从文字提取或添加地点时，用 prepare_place_import 并把用户原文放入 import_text；"
            "用户明确要求生成当前路线时用 generate_itinerary。地点仍需后续高德与人工确认。最多四个 action。"
            "修改已有地点的停留时长、固定到访时间或必去/可选状态时使用 update_destination，并且只能使用上下文中真实存在的 destination_id；"
            "明确要求取消某地点固定到访时间窗时，使用 update_destination 并设置 clear_visit_window=true，不同时提供时间值；未提到清除时保持false。"
            "用户要求比较、对比不同路线方案时使用 compare_itineraries；该操作只模拟，不保存正式路线。"
            "优先依据 planning_readiness 和 route_summary 回答能否规划、等待、迟到、固定时间窗冲突及路线是否为估算；"
            "不得把 estimated=false 的真实路线说成直线估算，也不得忽略 missing 列表。"
        )

    @staticmethod
    def planning_readiness(context: dict) -> dict:
        """从脱敏上下文确定性判断当前是否具备多地点规划条件。"""
        trip = context.get("trip", {})
        participants = context.get("participants", [])
        destinations = context.get("destinations", [])
        missing = []
        blockers = []
        if trip.get("status") != "active":
            blockers.append({"code": "trip_not_active", "message": "Trip 当前不是规划中状态"})
        if trip.get("primary_workflow") != "itinerary":
            blockers.append({"code": "wrong_workflow", "message": "Trip 当前主方案不是多地点路线"})
        if not trip.get("planned_start_at") or not trip.get("planned_end_at"):
            missing.append({"code": "planning_time_missing", "message": "尚未完整设置行程开始和结束时间"})
        if not participants:
            missing.append({"code": "participants_missing", "message": "当前没有参与者"})
        else:
            names = [item.get("name") or "未命名成员" for item in participants if not item.get("has_start_location")]
            if names:
                missing.append({
                    "code": "participant_origins_missing", "message": f"以下参与者尚未填写出发点：{'、'.join(names)}",
                    "participant_names": names,
                })
        confirmed = [item for item in destinations if item.get("visit_status") in {"must_visit", "optional"}]
        if len(confirmed) < 2:
            missing.append({
                "code": "destinations_insufficient", "message": f"已确认地点只有 {len(confirmed)} 个，至少需要 2 个",
                "confirmed_count": len(confirmed), "required_count": 2,
            })
        if len(confirmed) > 10:
            blockers.append({
                "code": "destinations_exceeded", "message": f"已确认地点有 {len(confirmed)} 个，首版最多支持 10 个",
                "confirmed_count": len(confirmed), "maximum_count": 10,
            })
        return {
            "ready_to_plan": not missing and not blockers,
            "missing": missing, "blockers": blockers,
            "confirmed_destination_count": len(confirmed),
        }

    async def chat(
        self, context: dict, message: str, trip_id: int | None = None, user_id: int | None = None,
    ) -> tuple[AgentModelAnswer, bool, str | None]:
        history = self.history(trip_id, user_id) if trip_id is not None and user_id is not None else []
        prompt = (
            "Trip上下文：\n" + json.dumps(context, ensure_ascii=False, default=str)
            + "\n\n最近对话（仅当前进程短期保存）：\n" + json.dumps(history, ensure_ascii=False)
            + "\n\n用户：" + message
        )
        payload, error = await llm_place_parser.complete_json(self.system_prompt(), prompt)
        if payload is not None:
            try:
                answer = AgentModelAnswer.model_validate(payload)
                inferred = self.infer_actions(message, context)
                existing_types = {action.type for action in answer.actions}
                answer.actions.extend(action for action in inferred if action.type not in existing_types)
                if re.search(r"(?:不要|无需|不需要|别).{0,4}(?:生成|保存).{0,4}(?:正式)?(?:路线|方案)", message):
                    answer.actions = [action for action in answer.actions if action.type != "generate_itinerary"]
                answer.actions = answer.actions[:4]
                if trip_id is not None and user_id is not None:
                    self.remember(trip_id, user_id, message, answer.reply, answer.actions)
                return answer, True, None
            except ValidationError:
                error = "LLM 助手返回内容未通过安全 schema"
        summary = self.fallback_summary(context)
        answer = AgentModelAnswer(
            reply=f"{summary}\n\nAI 暂不可用，已使用本地规则识别明确调整：{error or '响应不可用'}",
            actions=self.infer_actions(message, context),
        )
        if trip_id is not None and user_id is not None:
            self.remember(trip_id, user_id, message, answer.reply, answer.actions)
        return answer, False, error

    @staticmethod
    def infer_actions(message: str, context: dict) -> list[AgentAction]:
        """只为明确修改指令提供窄范围兜底，不把普通问句转换成写操作。"""
        if re.search(r"(?:不要|无需|不需要|别).{0,4}(?:修改|执行|操作)", message):
            return []
        explicit_change = bool(re.search(r"(?:设置|改成|调整|切换|修改草案|生成.*草案)", message))
        actions: list[AgentAction] = []
        if explicit_change and "共同约点" in message:
            actions.append(AgentAction(type="switch_workflow", primary_workflow="meeting"))
        elif explicit_change and "多地点" in message and re.search(r"(?:切换|主方案|路线)", message):
            actions.append(AgentAction(type="switch_workflow", primary_workflow="itinerary"))

        date_match = re.search(r"(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})日?", message)
        times = re.findall(r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)", message)
        if explicit_change and date_match and len(times) >= 2:
            year, month, day = (int(value) for value in date_match.groups())
            start = datetime(year, month, day, int(times[0][0]), int(times[0][1]), tzinfo=timezone(timedelta(hours=8)))
            end = datetime(year, month, day, int(times[1][0]), int(times[1][1]), tzinfo=timezone(timedelta(hours=8)))
            trip = context["trip"]
            mode = "driving" if "自驾" in message else "transit" if re.search(r"公交|地铁|公共交通", message) else trip.get("group_transport_mode", "transit")
            objective = (
                "distance" if re.search(r"距离.*(?:短|优先)|最短距离", message)
                else "total_time" if re.search(r"时间.*(?:短|优先)|最快|总时间", message)
                else "balanced" if "均衡" in message else trip.get("optimization_objective", "balanced")
            )
            actions.append(AgentAction(
                type="update_planning_settings", planned_start_at=start, planned_end_at=end,
                group_transport_mode=mode, optimization_objective=objective,
            ))
        # 仅在消息明确写出唯一地点名称和修改词时生成地点草案，避免“第一家”等指代误改。
        matched_destinations = [
            item for item in context.get("destinations", [])
            if item.get("name") and item["name"] in message
        ]
        clear_window = bool(re.search(r"(?:取消|清除|移除).*(?:固定到访时间|固定时间窗|到访时间窗)", message))
        if re.search(r"不要|别|无需|不需要|是否|能否|可以|怎么|如何|[？?]|吗", message):
            clear_window = False
        if (explicit_change or clear_window) and len(matched_destinations) == 1:
            destination = matched_destinations[0]
            stay_match = re.search(r"停留(?:时间)?(?:改成|设置为|设为)?\s*(\d{1,3})\s*分钟", message)
            status = None
            if re.search(r"(?:设为|改成|设置为).{0,3}必去", message):
                status = "must_visit"
            elif re.search(r"(?:设为|改成|设置为).{0,3}可选", message):
                status = "optional"
            elif re.search(r"(?:设为|改成|设置为).{0,4}(?:不去|排除)", message):
                status = "excluded"
            stay = int(stay_match.group(1)) if stay_match else None
            if stay is not None and not 5 <= stay <= 720:
                stay = None
            if stay is not None or status is not None or clear_window:
                actions.append(AgentAction(
                    type="update_destination", destination_id=destination["id"],
                    expected_stay_min=stay, visit_status=status, clear_visit_window=clear_window,
                ))
        if re.search(r"(?:解析|导入|添加).{0,8}(?:地点|餐厅|景点|店)", message):
            actions.append(AgentAction(type="prepare_place_import", import_text=message[:4000]))
        if re.search(r"(?:生成路线|规划路线|开始规划|生成行程)", message):
            actions.append(AgentAction(type="generate_itinerary"))
        if re.search(r"(?:比较|对比)[^。！？\n]{0,40}(?:路线|方案)|(?:路线|方案)[^。！？\n]{0,40}(?:比较|对比)", message):
            actions.append(AgentAction(type="compare_itineraries"))
        return actions[:4]

    def sync_history_epoch(self, trip_id: int, user_id: int, epoch: str) -> None:
        key = (trip_id, user_id)
        if self._history_epochs.get(key) != epoch:
            self.clear_history(trip_id, user_id)
            self._history_epochs[key] = epoch

    def history(self, trip_id: int, user_id: int) -> list[dict]:
        self.cleanup()
        stored = self._conversations.get((trip_id, user_id))
        return list(stored[1]) if stored else []

    def remember(
        self, trip_id: int, user_id: int, message: str, reply: str,
        actions: list[AgentAction] | None = None,
    ) -> None:
        history = self.history(trip_id, user_id)
        action_summary = [{
            key: value for key, value in action.model_dump(mode="json").items()
            if value is not None and key != "import_text"
        } for action in (actions or [])]
        history.extend([
            {"role": "user", "content": message[:1000]},
            {"role": "assistant", "content": reply[:1500], "proposed_actions": action_summary},
        ])
        self._conversations[(trip_id, user_id)] = (time.time() + 1800, history[-6:])

    def clear_history(self, trip_id: int, user_id: int) -> None:
        self._conversations.pop((trip_id, user_id), None)

    @staticmethod
    def fallback_summary(context: dict) -> str:
        trip = context["trip"]
        destinations = context.get("destinations", [])
        participants = context.get("participants", [])
        plan = context.get("latest_plan")
        text = (
            f"当前行程“{trip['title']}”为{trip['status']}状态，"
            f"共有 {len(participants)} 名参与者、{len(destinations)} 个地点。"
        )
        if plan:
            text += f"最新路线包含 {len(plan.get('ordered_stops') or [])} 站，总行程约 {plan.get('total_duration_min') or 0} 分钟。"
        else:
            text += "目前还没有路线方案。"
        readiness = context.get("planning_readiness") or AgentAssistant.planning_readiness(context)
        issues = [item["message"] for item in [*readiness.get("missing", []), *readiness.get("blockers", [])]]
        if issues:
            text += " 当前还不能生成多地点路线：" + "；".join(issues) + "。"
        else:
            text += " 当前输入已具备生成多地点路线的条件。"
        route_summary = context.get("route_summary") or {}
        if route_summary.get("estimated_segment_count"):
            text += f" 当前方案有 {route_summary['estimated_segment_count']} 段为直线估算，不能作为导航。"
        if route_summary.get("total_wait_min"):
            text += f" 固定到访时间造成约 {route_summary['total_wait_min']} 分钟等待。"
        if route_summary.get("time_window_conflict_count"):
            text += f" 另有 {route_summary['time_window_conflict_count']} 个固定时间窗冲突。"
        return text

    def store(self, trip_id: int, user_id: int, input_version: int, actions: list[AgentAction]) -> str | None:
        self.cleanup()
        if not actions:
            return None
        actions = normalize_actions(actions)
        proposal_id = str(uuid4())
        self._proposals[proposal_id] = StoredProposal(
            proposal_id, trip_id, user_id, input_version,
            [action.model_dump(mode="json") for action in actions], time.time() + 600,
        )
        return proposal_id

    def take(self, proposal_id: str, trip_id: int, user_id: int) -> StoredProposal | None:
        """兼容单步消费；归属不匹配时绝不能删除合法草案。"""
        self.cleanup()
        proposal = self._proposals.get(proposal_id)
        if proposal and proposal.trip_id == trip_id and proposal.user_id == user_id and not proposal.claimed:
            return self._proposals.pop(proposal_id)
        return None

    def claim(self, proposal_id: str, trip_id: int, user_id: int) -> StoredProposal | None:
        """执行前原子占用草案，阻止同一进程内的并发重复执行。"""
        self.cleanup()
        proposal = self._proposals.get(proposal_id)
        if (
            proposal is None or proposal.claimed
            or proposal.trip_id != trip_id or proposal.user_id != user_id
        ):
            return None
        proposal.claimed = True
        return proposal

    def release(self, proposal_id: str) -> None:
        """执行失败时释放占用，允许用户修正外部条件后重试。"""
        proposal = self._proposals.get(proposal_id)
        if proposal:
            proposal.claimed = False

    def consume(self, proposal_id: str) -> None:
        """仅在全部 action 成功后消费草案。"""
        self._proposals.pop(proposal_id, None)

    def cleanup(self) -> None:
        now = time.time()
        self._proposals = {key: value for key, value in self._proposals.items() if value.expires_at > now}
        self._conversations = {
            key: value for key, value in self._conversations.items() if value[0] > now
        }


agent_assistant = AgentAssistant()
