"""ORM 模型：槽位资料修订、箱清单修订、方案修订与已确认占用。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


from app.database import Base  # noqa: E402


class SlotRevision(Base):
    __tablename__ = "slot_revisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    min_moment: Mapped[float] = mapped_column(Float, nullable=False)
    max_moment: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    slots: Mapped[list["SlotRow"]] = relationship(
        back_populates="rev", cascade="all, delete-orphan"
    )
    adjacencies: Mapped[list["AdjacencyRow"]] = relationship(
        back_populates="rev", cascade="all, delete-orphan"
    )
    incompatibilities: Mapped[list["IncompatRow"]] = relationship(
        back_populates="rev", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("min_moment <= max_moment", name="ck_moment_range"),
    )


class SlotRow(Base):
    __tablename__ = "slots"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("slot_revisions.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    stack: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    capacity: Mapped[float] = mapped_column(Float, nullable=False)
    arm: Mapped[float] = mapped_column(Float, nullable=False)

    rev: Mapped[SlotRevision] = relationship(back_populates="slots")

    __table_args__ = (
        UniqueConstraint("revision_id", "code", name="uq_slot_code_in_rev"),
    )


class AdjacencyRow(Base):
    __tablename__ = "adjacencies"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("slot_revisions.id", ondelete="CASCADE"), index=True
    )
    slot_a: Mapped[str] = mapped_column(String(64), nullable=False)
    slot_b: Mapped[str] = mapped_column(String(64), nullable=False)

    rev: Mapped[SlotRevision] = relationship(back_populates="adjacencies")

    __table_args__ = (
        UniqueConstraint("revision_id", "slot_a", "slot_b", name="uq_adj_in_rev"),
    )


class IncompatRow(Base):
    __tablename__ = "incompatibilities"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("slot_revisions.id", ondelete="CASCADE"), index=True
    )
    class_a: Mapped[str] = mapped_column(String(64), nullable=False)
    class_b: Mapped[str] = mapped_column(String(64), nullable=False)

    rev: Mapped[SlotRevision] = relationship(back_populates="incompatibilities")

    __table_args__ = (
        UniqueConstraint("revision_id", "class_a", "class_b", name="uq_incompat_in_rev"),
    )


class ManifestRevision(Base):
    __tablename__ = "manifest_revisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    containers: Mapped[list["ContainerRow"]] = relationship(
        back_populates="rev", cascade="all, delete-orphan"
    )


class ContainerRow(Base):
    __tablename__ = "containers"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("manifest_revisions.id", ondelete="CASCADE"), index=True
    )
    no: Mapped[str] = mapped_column(String(64), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    hazmat: Mapped[str | None] = mapped_column(String(64), nullable=True)
    discharge_order: Mapped[int] = mapped_column(Integer, nullable=False)

    rev: Mapped[ManifestRevision] = relationship(back_populates="containers")

    __table_args__ = (
        UniqueConstraint("revision_id", "no", name="uq_container_no_in_rev"),
        CheckConstraint("weight >= 0", name="ck_container_weight"),
    )


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    # 当前修订所基于的资料
    slots_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    total_moment: Mapped[float] = mapped_column(Float, nullable=False)
    max_tier: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 修订链
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("plans.id", ondelete="SET NULL"), nullable=True
    )
    root_id: Mapped[str] = mapped_column(String(64), nullable=False)

    items: Mapped[list["PlanItem"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_plans_root", "root_id"),
        Index("ix_plans_status", "status"),
        CheckConstraint(
            "status IN ('draft', 'confirmed', 'superseded')",
            name="ck_plan_status",
        ),
    )


class PlanItem(Base):
    __tablename__ = "plan_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), index=True
    )
    container_no: Mapped[str] = mapped_column(String(64), nullable=False)
    slot_code: Mapped[str] = mapped_column(String(64), nullable=False)
    stack: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)

    plan: Mapped[Plan] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint("plan_id", "container_no", name="uq_item_container_in_plan"),
    )


class ConfirmedSlot(Base):
    """已确认方案对槽位的占用。

    唯一索引保证：并发确认时同一槽位不可能被两个已确认方案占用。
    """

    __tablename__ = "confirmed_slots"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slot_code: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("slot_code", name="uq_confirmed_slot"),
    )


class LoadingSchedule(Base):
    """绑定某个方案修订的装船时间表。

    只允许对已确认方案保存；方案修订被新修订取代（superseded）后，
    其排程仍可查询但状态转为 historical，不再列为当前排程。
    """

    __tablename__ = "loading_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    crane_count: Mapped[int] = mapped_column(Integer, nullable=False)
    makespan: Mapped[int] = mapped_column(Integer, nullable=False)
    # current=绑定方案仍为当前已确认修订；historical=方案修订已被取代
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="current")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    items: Mapped[list["LoadingScheduleItem"]] = relationship(
        back_populates="schedule", cascade="all, delete-orphan"
    )
    plan: Mapped[Plan] = relationship()

    __table_args__ = (
        CheckConstraint("crane_count > 0", name="ck_schedule_cranes"),
        CheckConstraint("makespan >= 0", name="ck_schedule_makespan"),
        CheckConstraint(
            "status IN ('current', 'historical')", name="ck_schedule_status"
        ),
    )


class LoadingScheduleItem(Base):
    """装船时间表中单箱安排：开工/完工分钟与岸桥编号（自 1 起）。"""

    __tablename__ = "loading_schedule_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_id: Mapped[int] = mapped_column(
        ForeignKey("loading_schedules.id", ondelete="CASCADE"), index=True
    )
    container_no: Mapped[str] = mapped_column(String(64), nullable=False)
    stack: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    finish_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    crane_no: Mapped[int] = mapped_column(Integer, nullable=False)

    schedule: Mapped[LoadingSchedule] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint("schedule_id", "container_no",
                         name="uq_schedule_item_container"),
    )


class UnloadingSequence(Base):
    """绑定某个方案修订的逐港卸箱顺序。

    只允许对已确认方案保存；方案修订被新修订取代（superseded）后，
    其卸箱顺序仍可查询但状态转为 historical，不再列为当前卸箱顺序。
    """

    __tablename__ = "unloading_sequences"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    initial_moment: Mapped[float] = mapped_column(Float, nullable=False)
    final_moment: Mapped[float] = mapped_column(Float, nullable=False)
    # current=绑定方案仍为当前已确认修订；historical=方案修订已被取代
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="current")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    steps: Mapped[list["UnloadingStepRow"]] = relationship(
        back_populates="sequence", cascade="all, delete-orphan"
    )
    ports: Mapped[list["UnloadingPortRow"]] = relationship(
        back_populates="sequence", cascade="all, delete-orphan"
    )
    plan: Mapped[Plan] = relationship()

    __table_args__ = (
        CheckConstraint(
            "status IN ('current', 'historical')", name="ck_unloading_status"
        ),
    )


class UnloadingPortRow(Base):
    """请求中各卸港序号的允许剩余总力矩闭区间（随顺序留档）。"""

    __tablename__ = "unloading_ports"

    id: Mapped[int] = mapped_column(primary_key=True)
    sequence_id: Mapped[int] = mapped_column(
        ForeignKey("unloading_sequences.id", ondelete="CASCADE"), index=True
    )
    discharge_order: Mapped[int] = mapped_column(Integer, nullable=False)
    min_remaining_moment: Mapped[float] = mapped_column(Float, nullable=False)
    max_remaining_moment: Mapped[float] = mapped_column(Float, nullable=False)

    sequence: Mapped[UnloadingSequence] = relationship(back_populates="ports")

    __table_args__ = (
        UniqueConstraint("sequence_id", "discharge_order",
                         name="uq_unloading_port_order"),
    )


class UnloadingStepRow(Base):
    """卸箱顺序中的一步：港序、箱号与卸后剩余总力矩（含槽位快照）。"""

    __tablename__ = "unloading_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    sequence_id: Mapped[int] = mapped_column(
        ForeignKey("unloading_sequences.id", ondelete="CASCADE"), index=True
    )
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    discharge_order: Mapped[int] = mapped_column(Integer, nullable=False)
    container_no: Mapped[str] = mapped_column(String(64), nullable=False)
    slot_code: Mapped[str] = mapped_column(String(64), nullable=False)
    stack: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    arm: Mapped[float] = mapped_column(Float, nullable=False)
    remaining_moment: Mapped[float] = mapped_column(Float, nullable=False)

    sequence: Mapped[UnloadingSequence] = relationship(back_populates="steps")

    __table_args__ = (
        UniqueConstraint("sequence_id", "step_no", name="uq_unloading_step_no"),
        UniqueConstraint("sequence_id", "container_no",
                         name="uq_unloading_step_container"),
    )
