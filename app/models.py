"""ORM 模型（Phase 1 子集）。坐标用普通数值/JSONB 字段，暂不引入 PostGIS。"""
from datetime import datetime

from sqlalchemy import BigInteger, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    wechat_openid: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String(50))
    home_location: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    preferences: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class Trip(Base):
    __tablename__ = "trips"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(100))
    creator_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    invite_code: Mapped[str] = mapped_column(String(8), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | finished
    default_mode: Mapped[str] = mapped_column(String(20), default="transit")  # driving | transit
    group_transport_mode: Mapped[str] = mapped_column(String(20), default="transit")
    planned_start_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    planned_end_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    optimization_objective: Mapped[str] = mapped_column(String(20), default="balanced")
    input_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class TripParticipant(Base):
    __tablename__ = "trip_participants"
    __table_args__ = (UniqueConstraint("trip_id", "user_id", name="uq_trip_participant"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trip_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("trips.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    role: Mapped[str] = mapped_column(String(10), default="member")  # creator | member
    start_location: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # {lat,lng,address}
    transport_mode: Mapped[str] = mapped_column(String(20), default="transit")  # driving | transit
    vote_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | submitted
    joined_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class MeetingPointResult(Base):
    __tablename__ = "meeting_point_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trip_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("trips.id", ondelete="CASCADE"))
    candidates: Mapped[list] = mapped_column(JSONB)  # Top3 候选点快照
    objective: Mapped[str | None] = mapped_column(String(20), nullable=True)  # minimax | min_sum
    computed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class TripVote(Base):
    __tablename__ = "trip_votes"
    __table_args__ = (
        UniqueConstraint("trip_id", "candidate_type", "candidate_id", "user_id", name="uq_trip_vote"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trip_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("trips.id", ondelete="CASCADE"))
    candidate_type: Mapped[str] = mapped_column(String(20), default="meeting_point")  # meeting_point | poi
    candidate_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    vote_value: Mapped[int] = mapped_column(Integer)  # 1 赞 / -1 踩 / 0 取消
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class TripDestination(Base):
    __tablename__ = "trip_destinations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trip_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("trips.id", ondelete="CASCADE"), index=True)
    submitted_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(120))
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    visit_status: Mapped[str] = mapped_column(String(20), default="candidate")
    expected_stay_min: Mapped[int] = mapped_column(Integer, default=60)
    opening_hours: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ItineraryPlan(Base):
    __tablename__ = "itinerary_plans"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trip_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("trips.id", ondelete="CASCADE"), index=True)
    input_version: Mapped[int] = mapped_column(Integer)
    objective: Mapped[str] = mapped_column(String(20))
    first_destination_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("trip_destinations.id", ondelete="SET NULL"), nullable=True
    )
    participant_arrivals: Mapped[list] = mapped_column(JSONB)
    ordered_stops: Mapped[list] = mapped_column(JSONB)
    route_legs: Mapped[list] = mapped_column(JSONB)
    warnings: Mapped[list] = mapped_column(JSONB, default=list)
    total_travel_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_duration_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    computed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
