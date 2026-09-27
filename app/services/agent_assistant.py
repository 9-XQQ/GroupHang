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
        "switch_workflow", "update_planning_settings", "prepare_place_import", "generate_itinerary"
    ]
    primary_workflow: Literal["meeting", "itinerary"] | None = None
    planned_start_at: datetime | None = None
    planned_end_at: datetime | None = None
    group_transport_mode: Literal["driving", "transit"] | None = None
    optimization_objective: Literal["balanced", "total_time", "distance"] | None = None
    import_text: str | None = Field(default=None, max_length=4000)

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
        return self


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


class AgentAssistant:
    def __init__(self) -> None:
        self._proposals: dict[str, StoredProposal] = {}
        self._conversations: dict[tuple[int, int], tuple[float, list[dict]]] = {}

    @staticmethod
    def system_prompt() -> str:
        return (
            "你是多人出行规划助手。只能依据给定的结构化 Trip 上下文回答，不得编造地点坐标、道路、"
            "公交或实时事实。只输出 JSON：{\"reply\":\"中文回答\",\"actions\":[]}。"
            "允许的 action 只有 switch_workflow、update_planning_settings、prepare_place_import、generate_itinerary。"
            "修改规划参数必须同时提供含时区的 planned_start_at、planned_end_at、group_transport_mode、"
            "optimization_objective。保存这些规划参数不要求参与者出发点或目的地已经填写，"
            "不要把保存参数误判为立即生成路线；用户明确给出日期、时间、交通方式和目标时应生成草案。"
            "北京时间、中国时间、Asia/Shanghai 均使用 +08:00。真正缺少必填值时才追问并让 actions 留空。"
            "用户要求从文字提取或添加地点时，用 prepare_place_import 并把用户原文放入 import_text；"
            "用户明确要求生成当前路线时用 generate_itinerary。地点仍需后续高德与人工确认。最多四个 action。"
        )

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
                answer.actions = answer.actions[:4]
                if trip_id is not None and user_id is not None:
                    self.remember(trip_id, user_id, message, answer.reply)
                return answer, True, None
            except ValidationError:
                error = "LLM 助手返回内容未通过安全 schema"
        summary = self.fallback_summary(context)
        answer = AgentModelAnswer(
            reply=f"{summary}\n\nAI 暂不可用，已使用本地规则识别明确调整：{error or '响应不可用'}",
            actions=self.infer_actions(message, context),
        )
        if trip_id is not None and user_id is not None:
            self.remember(trip_id, user_id, message, answer.reply)
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
        if re.search(r"(?:解析|导入|添加).{0,8}(?:地点|餐厅|景点|店)", message):
            actions.append(AgentAction(type="prepare_place_import", import_text=message[:4000]))
        if re.search(r"(?:生成路线|规划路线|开始规划|生成行程)", message):
            actions.append(AgentAction(type="generate_itinerary"))
        return actions[:4]

    def history(self, trip_id: int, user_id: int) -> list[dict]:
        self.cleanup()
        stored = self._conversations.get((trip_id, user_id))
        return list(stored[1]) if stored else []

    def remember(self, trip_id: int, user_id: int, message: str, reply: str) -> None:
        history = self.history(trip_id, user_id)
        history.extend([
            {"role": "user", "content": message[:1000]},
            {"role": "assistant", "content": reply[:1500]},
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
        return text

    def store(self, trip_id: int, user_id: int, input_version: int, actions: list[AgentAction]) -> str | None:
        self.cleanup()
        if not actions:
            return None
        order = {
            "switch_workflow": 0, "update_planning_settings": 1,
            "prepare_place_import": 2, "generate_itinerary": 3,
        }
        unique: dict[str, AgentAction] = {}
        for action in actions:
            unique[action.type] = action
        actions = sorted(unique.values(), key=lambda action: order[action.type])
        proposal_id = str(uuid4())
        self._proposals[proposal_id] = StoredProposal(
            proposal_id, trip_id, user_id, input_version,
            [action.model_dump(mode="json") for action in actions], time.time() + 600,
        )
        return proposal_id

    def take(self, proposal_id: str, trip_id: int, user_id: int) -> StoredProposal | None:
        self.cleanup()
        proposal = self._proposals.pop(proposal_id, None)
        if proposal and proposal.trip_id == trip_id and proposal.user_id == user_id:
            return proposal
        return None

    def cleanup(self) -> None:
        now = time.time()
        self._proposals = {key: value for key, value in self._proposals.items() if value.expires_at > now}
        self._conversations = {
            key: value for key, value in self._conversations.items() if value[0] > now
        }


agent_assistant = AgentAssistant()
