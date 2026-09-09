"""Pydantic 请求/响应模型（v2）。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Location(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    address: str | None = Field(default=None, max_length=300)


class LoginRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    phone: str | None = Field(default=None, pattern=r"^1[3-9]\d{9}$")
    wechat_code: str | None = Field(default=None, min_length=1, max_length=256)
    name: str | None = Field(default=None, min_length=1, max_length=50)


class TripCreate(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    default_mode: Literal["driving", "transit"] = "transit"


class JoinRequest(BaseModel):
    invite_code: str = Field(min_length=6, max_length=8)


class ParticipantUpdate(BaseModel):
    start_location: Location
    transport_mode: Literal["driving", "transit"]


class MeetingPointRequest(BaseModel):
    objective: Literal["minimax", "min_sum"] | None = None


class VoteRequest(BaseModel):
    candidate_type: Literal["meeting_point", "poi", "destination"] = "meeting_point"
    candidate_id: str = Field(min_length=1, max_length=64)
    vote_value: Literal[-1, 0, 1]


class OpeningHours(BaseModel):
    open: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    close: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class DestinationCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    address: str | None = Field(default=None, max_length=300)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    category: str | None = Field(default=None, max_length=80)
    expected_stay_min: int = Field(default=60, ge=5, le=720)
    opening_hours: OpeningHours | None = None
    note: str | None = Field(default=None, max_length=500)


class DestinationUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=120)
    address: str | None = Field(default=None, max_length=300)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    category: str | None = Field(default=None, max_length=80)
    expected_stay_min: int | None = Field(default=None, ge=5, le=720)
    opening_hours: OpeningHours | None = None
    note: str | None = Field(default=None, max_length=500)


class DestinationStatusUpdate(BaseModel):
    visit_status: Literal["candidate", "must_visit", "optional", "excluded"]


class SharedTextParseRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    text: str = Field(min_length=2, max_length=10000)
    default_stay_min: int = Field(default=60, ge=5, le=720)


class PlanningSettingsUpdate(BaseModel):
    planned_start_at: datetime
    planned_end_at: datetime
    group_transport_mode: Literal["driving", "transit"] = "transit"
    optimization_objective: Literal["balanced", "total_time", "distance"] = "balanced"

    @model_validator(mode="after")
    def validate_window(self):
        if self.planned_start_at.tzinfo is None or self.planned_end_at.tzinfo is None:
            raise ValueError("行程时间必须包含时区")
        if self.planned_end_at <= self.planned_start_at:
            raise ValueError("结束时间必须晚于开始时间")
        if (self.planned_end_at - self.planned_start_at).total_seconds() > 24 * 3600:
            raise ValueError("单次行程时间不能超过 24 小时")
        return self
