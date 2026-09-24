"""ORM 模型。

同箱号 + 同序号在库中唯一：
- 内容一致  -> 重复上报（幂等，返回已有记录，状态不变）
- 内容不同  -> 冲突（拒绝，HTTP 409）
乱序事件照常入库，查询最新状态时按最大序号取行，因此不依赖到达顺序。

交接快照（handoff / handoff_item）在建快照的同一事务内冻结每箱当时的
最大序号状态；快照只写一次，之后乱序补报只进 box_events，不会回写快照。
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

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


class Handoff(Base):
    """一次交接的快照主表：交接编号唯一，冻结温度上下限。"""

    __tablename__ = "handoffs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    handoff_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    temp_lower: Mapped[float] = mapped_column(Float, nullable=False)
    temp_upper: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    items: Mapped[list["HandoffItem"]] = relationship(
        back_populates="handoff",
        cascade="all, delete-orphan",
        order_by="HandoffItem.position",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Handoff {self.handoff_no} items={len(self.items)}>"


class HandoffItem(Base):
    """快照明细：每箱冻结一行。

    缺报箱（建快照时库中无任何事件）的 seq/temperature/event_time 均为 NULL，
    conclusion=missing；越界为 out_of_range，边界值（恰等上下限）算 normal。
    """

    __tablename__ = "handoff_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    handoff_id: Mapped[int] = mapped_column(
        ForeignKey("handoffs.id", ondelete="CASCADE"), nullable=False
    )
    # 箱号在请求列表中的下标，用于按请求顺序冻结/返回
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    box_code: Mapped[str] = mapped_column(String(64), nullable=False)
    # 冻结的是该箱建快照事务中读到的最大序号状态；缺报时三列均为 NULL
    seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    event_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    conclusion: Mapped[str] = mapped_column(String(16), nullable=False)

    handoff: Mapped["Handoff"] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint("handoff_id", "box_code", name="uq_handoff_box"),
        Index("ix_handoff_items_handoff_id", "handoff_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<HandoffItem {self.box_code} {self.conclusion}>"
