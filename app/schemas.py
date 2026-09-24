"""Pydantic 请求/响应模型。"""
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EventIn(BaseModel):
    box_code: str = Field(..., min_length=1, max_length=64, description="周转箱箱号")
    seq: int = Field(..., ge=0, description="该箱递增的事件序号，从 0 开始")
    temperature: float = Field(..., ge=-100, le=100, description="摄氏温度")
    event_time: datetime = Field(..., description="ISO 8601，建议带时区，如 2026-09-23T10:00:00+08:00")

    @field_validator("box_code")
    @classmethod
    def _strip_box_code(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("box_code 不能为空")
        return v

    @field_validator("event_time")
    @classmethod
    def _to_utc(cls, v: datetime) -> datetime:
        # 无时区信息按 UTC 处理；有时区则换算为 UTC 后去掉 tzinfo 落库
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc).replace(tzinfo=None)
        return v.astimezone(timezone.utc).replace(tzinfo=None)


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    box_code: str
    seq: int
    temperature: float
    event_time: datetime
    created_at: datetime


class EventAck(BaseModel):
    status: str = Field(..., description="accepted=首次入库，duplicate=重复上报（幂等忽略）")
    event: EventOut


class BoxState(BaseModel):
    box_code: str
    seq: int
    temperature: float
    event_time: datetime


class BoxListItem(BaseModel):
    box_code: str


class ClockAuditItem(BaseModel):
    seq: int = Field(..., description="事件序号")
    original_time: datetime = Field(..., description="原始 UTC 事件时间（不改动原始记录）")
    suggested_time: datetime = Field(..., description="保序回归后的建议 UTC 时间")
    offset_micros: int = Field(
        ..., description="建议时间 - 原时间（微秒，正为向前修正，负为回拨修正）"
    )
    block_seq_from: int = Field(..., description="该事件所属合并块的起始序号")
    block_seq_to: int = Field(..., description="该事件所属合并块的结束序号")


class TrajectoryCompressionPoint(BaseModel):
    seq: int = Field(..., description="保留事件的原始序号")
    event_time: datetime = Field(..., description="保留事件的原始 UTC 事件时间")
    temperature: float = Field(..., description="保留事件的原始温度（℃）")


class TrajectoryCompressionResult(BaseModel):
    box_code: str = Field(..., description="周转箱箱号")
    seq_from: int = Field(..., description="查询起始序号（含）")
    seq_to: int = Field(..., description="查询结束序号（含）")
    max_error: float = Field(..., description="允许的最大重建误差（℃），非负有限")
    event_count: int = Field(..., description="区间内事件条数")
    kept_count: int = Field(
        ..., description="保留事件数（满足误差约束的最少值；同数量时保留序号序列字典序最小）"
    )
    omitted_count: int = Field(..., description="被省略事件数（event_count - kept_count）")
    actual_max_error: float | None = Field(
        ...,
        description=(
            "被省略事件的重建温度与原值之差的最大绝对值（℃）；"
            "无被省略事件时为 0.0，区间内无事件时为 null"
        ),
    )
    kept_events: list[TrajectoryCompressionPoint] = Field(
        ..., description="保留事件（按序号升序），原始记录不被改动"
    )


class HeatExposureSegment(BaseModel):
    start_seq: int = Field(..., description="段起点事件序号")
    end_seq: int = Field(..., description="段终点事件序号")
    start_time: datetime = Field(..., description="段起点校正后 UTC 时间（等权保序回归）")
    end_time: datetime = Field(..., description="段终点校正后 UTC 时间（等权保序回归）")
    gap_seconds: float = Field(..., description="校正后段时长（秒）")
    start_temperature: float = Field(..., description="段起点温度（℃）")
    end_temperature: float = Field(..., description="段终点温度（℃）")
    celsius_seconds: float = Field(..., description="该段超过上限部分的精确积分（℃·秒）")


class HeatExposureResult(BaseModel):
    box_code: str = Field(..., description="周转箱箱号")
    seq_from: int = Field(..., description="查询起始序号（含）")
    seq_to: int = Field(..., description="查询结束序号（含）")
    upper_limit: float = Field(..., description="温度上限（℃）")
    max_gap_seconds: float = Field(..., description="允许计入的最大段时长（秒）")
    event_count: int = Field(..., description="区间内事件条数")
    segment_count: int = Field(..., description="计入积分的段数")
    skipped_segment_count: int = Field(
        ..., description="因校正后间隔为零或超过 max_gap_seconds 而未计入的段数"
    )
    total_celsius_seconds: float = Field(..., description="各段积分合计（℃·秒）")
    segments: list[HeatExposureSegment] = Field(..., description="计入积分的逐段明细")


class MerkleProofStep(BaseModel):
    sibling_hash: str = Field(
        ..., description="该层兄弟节点哈希（SHA-256，64 位小写十六进制）"
    )
    sibling_side: str = Field(
        ...,
        description=(
            "兄弟节点相对当前节点的位置：left=兄弟在左"
            "（父哈希 = SHA-256(0x01 || 兄弟 || 当前)）；"
            "right=兄弟在右（父哈希 = SHA-256(0x01 || 当前 || 兄弟)）"
        ),
    )


class MerkleProofResult(BaseModel):
    box_code: str = Field(..., description="周转箱箱号")
    seq_from: int = Field(..., description="查询起始序号（含）")
    seq_to: int = Field(..., description="查询结束序号（含）")
    target_seq: int = Field(..., description="目标事件序号")
    event_count: int = Field(
        ..., description="区间内实际事件条数（即 Merkle 树叶子数，缺号不补）"
    )
    leaf_data: str = Field(
        ...,
        description=(
            "目标叶子原文：由箱号、序号、UTC 时间字符串（YYYY-MM-DDTHH:MM:SS.ffffffZ）"
            "与 .17g 温度字符串组成的无空格 JSON 数组（中文等非 ASCII 字符原样 UTF-8 编码）"
        ),
    )
    target_leaf_hash: str = Field(
        ..., description="目标叶哈希：SHA-256(0x00 || leaf_data 的 UTF-8 字节)，十六进制"
    )
    root_hash: str = Field(..., description="Merkle 根哈希（十六进制）")
    proof: list[MerkleProofStep] = Field(
        ..., description="从叶层到根层逐层的兄弟哈希与方位；单事件时为空数组"
    )


class TemperatureMedianResult(BaseModel):
    box_code: str = Field(..., description="周转箱箱号")
    seq_from: int = Field(..., description="查询起始序号（含）")
    seq_to: int = Field(..., description="查询结束序号（含）")
    max_gap_seconds: float = Field(..., description="允许计入的最大段时长（秒）")
    event_count: int = Field(..., description="区间内事件条数")
    segment_count: int = Field(
        ..., description="计入分布的有效段数（校正后时长大于 0 且不超过 max_gap_seconds）"
    )
    skipped_segment_count: int = Field(
        ..., description="因校正后间隔为零或超过 max_gap_seconds 而未计入的段数"
    )
    total_seconds: float = Field(..., description="各有效段校正后时长合计（秒）")
    median_temperature: float | None = Field(
        ...,
        description=(
            "时间加权温度中位数（℃）：累计时长占比首次达到一半的最低温度；"
            "无有效段时为 null"
        ),
    )


# 交接快照结论：缺报 / 越界（低于下限或高于上限）/ 正常（含恰等边界值）
CONCLUSION_MISSING = "missing"
CONCLUSION_OUT_OF_RANGE = "out_of_range"
CONCLUSION_NORMAL = "normal"


class HandoffIn(BaseModel):
    handoff_no: str = Field(
        ..., min_length=1, max_length=64, description="交接编号（业务唯一）"
    )
    box_codes: list[str] = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="本次交接涉及的箱号列表（不能为空、不能重复，顺序即冻结明细顺序）",
    )
    temp_lower: float = Field(
        ..., allow_inf_nan=False, description="温度下限（℃），不得高于上限"
    )
    temp_upper: float = Field(
        ..., allow_inf_nan=False, description="温度上限（℃），不得低于下限"
    )

    @field_validator("handoff_no")
    @classmethod
    def _strip_handoff_no(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("handoff_no 不能为空")
        return v

    @field_validator("box_codes")
    @classmethod
    def _normalize_box_codes(cls, values: list[str]) -> list[str]:
        # 去空白；空箱号拒绝，重复箱号拒绝（区分大小写）
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in values:
            code = raw.strip()
            if not code:
                raise ValueError("箱号不能为空")
            if len(code) > 64:
                raise ValueError(f"箱号长度不能超过 64 个字符：{code}")
            if code in seen:
                raise ValueError(f"箱号重复：{code}")
            seen.add(code)
            normalized.append(code)
        return normalized

    @model_validator(mode="after")
    def _check_limits(self) -> "HandoffIn":
        if self.temp_lower > self.temp_upper:
            raise ValueError(
                f"温度下限({self.temp_lower}) 不能高于温度上限({self.temp_upper})"
            )
        return self


class HandoffItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    box_code: str = Field(..., description="周转箱箱号")
    seq: int | None = Field(
        ..., description="冻结时该箱的最大事件序号；缺报为 null"
    )
    temperature: float | None = Field(
        ..., description="冻结时该箱最大序号事件的温度（℃）；缺报为 null"
    )
    event_time: datetime | None = Field(
        ..., description="冻结时该箱最大序号事件的 UTC 时间；缺报为 null"
    )
    conclusion: str = Field(
        ...,
        description=(
            "结论：missing=缺报（库中无该箱事件）；"
            "out_of_range=越界（严格低于下限或高于上限）；"
            "normal=正常（闭区间内，恰等边界值也算正常）"
        ),
    )


class HandoffSummary(BaseModel):
    total: int = Field(..., description="明细箱数合计（= 各类数量之和）")
    missing: int = Field(..., description="缺报箱数量")
    out_of_range: int = Field(..., description="越界箱数量")
    normal: int = Field(..., description="正常箱数量（含恰等上下限的边界值）")


class HandoffSnapshot(BaseModel):
    handoff_no: str = Field(..., description="交接编号")
    temp_lower: float = Field(..., description="冻结的温度下限（℃）")
    temp_upper: float = Field(..., description="冻结的温度上限（℃）")
    created_at: datetime = Field(..., description="快照创建时间（UTC）")
    items: list[HandoffItemOut] = Field(
        ..., description="冻结明细，按请求箱号顺序排列"
    )
    summary: HandoffSummary = Field(..., description="各类结论数量")


class HandoffAck(HandoffSnapshot):
    status: str = Field(
        ...,
        description="accepted=首次创建快照，duplicate=同编号同内容重放（返回原快照，不重建）",
    )
