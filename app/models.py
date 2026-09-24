"""ORM 模型。

同箱号 + 同序号在库中唯一：
- 内容一致  -> 重复上报（幂等，返回已有记录，状态不变）
- 内容不同  -> 冲突（拒绝，HTTP 409）
乱序事件照常入库，查询最新状态时按最大序号取行，因此不依赖到达顺序。
"""
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
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
