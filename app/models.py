"""ORM 模型。

box_events：同箱号 + 同序号在库中唯一：
- 内容一致  -> 重复上报（幂等，返回已有记录，状态不变）
- 内容不同  -> 冲突（拒绝，HTTP 409）
乱序事件照常入库，查询最新状态时按最大序号取行，因此不依赖到达顺序。

handoff_snapshots / handoff_snapshot_items：交接快照及其逐箱冻结明细。
handoff_code 全局唯一，作为幂等键；明细行在创建时一次性写入，之后只读，
后续事件补报（含乱序）不会改写已建快照。
"""
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class BoxEvent(Base):
    __tablename__ = "box_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    box_code: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    temperature: Mapped[float] = mapped_column(Float, nullable=False)
    # 统一存为 UTC（naive），避免不同时区写入导致比较不一致
    event_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("box_code", "seq", name="uq_box_seq"),
        Index("ix_box_code", "box_code"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BoxEvent {self.box_code}#{self.seq} temp={self.temperature}>"


class HandoffSnapshot(Base):
    """一次交接的冻结快照头：编号唯一，落库后只读。"""

    __tablename__ = "handoff_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    handoff_code: Mapped[str] = mapped_column(String(64), nullable=False)
    temp_lower: Mapped[float] = mapped_column(Float, nullable=False)
    temp_upper: Mapped[float] = mapped_column(Float, nullable=False)
    box_count: Mapped[int] = mapped_column(Integer, nullable=False)
    normal_count: Mapped[int] = mapped_column(Integer, nullable=False)
    out_of_range_count: Mapped[int] = mapped_column(Integer, nullable=False)
    missing_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("handoff_code", name="uq_handoff_code"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<HandoffSnapshot {self.handoff_code} boxes={self.box_count}>"


class HandoffSnapshotItem(Base):
    """快照内单个箱子的冻结明细：结论与状态字段在创建时定格，之后只读。"""

    __tablename__ = "handoff_snapshot_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    handoff_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("handoff_snapshots.id"), nullable=False
    )
    # 箱号在请求列表中的原始位置（从 0 开始），用于按原顺序回放明细与内容比对
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    box_code: Mapped[str] = mapped_column(String(64), nullable=False)
    # normal=正常，out_of_range=越界，missing=缺报
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # 冻结时刻该箱最大序号事件的序号/温度/事件时间；缺报箱无事件，三字段为 NULL
    seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    event_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("handoff_id", "box_code", name="uq_handoff_box"),
        Index("ix_handoff_items_handoff", "handoff_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<HandoffSnapshotItem handoff={self.handoff_id} {self.box_code} {self.status}>"
