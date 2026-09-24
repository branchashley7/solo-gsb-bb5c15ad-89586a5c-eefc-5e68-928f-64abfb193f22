"""业务逻辑：事件入库、幂等去重、乱序后的最新状态查询、时钟回拨校正、超温积分、轨迹压缩预览、Merkle 包含证明、交接快照冻结。"""
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models, schemas

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# 单次轨迹压缩预览允许读取的最大区间事件数，超过则拒绝（422）
TRAJECTORY_COMPRESSION_MAX_EVENTS = 120


def _content_equal(existing: models.BoxEvent, payload: schemas.EventIn) -> bool:
    """内容是否一致（温度 + 事件时间）。

    event_time 在 schema 层已统一换算为 UTC naive，可直接比较；
    设备重试会原样重发 JSON，float 解析结果相同，故直接判等。
    """
    return (
        existing.temperature == payload.temperature
        and existing.event_time == payload.event_time
    )


def submit_event(db: Session, payload: schemas.EventIn) -> tuple[models.BoxEvent, bool]:
    """写入一个事件。

    返回 (事件行, 是否重复)。
    - 同箱号同序号且内容相同：视为设备重试/延迟的重复上报，幂等返回原行，不改状态
    - 同箱号同序号但内容不同：拒绝（409）
    - 新序号：直接入库（即使乱序，不要求 seq 连续）
    """
    existing = db.scalar(
        select(models.BoxEvent).where(
            models.BoxEvent.box_code == payload.box_code,
            models.BoxEvent.seq == payload.seq,
        )
    )

    if existing is not None:
        if not _content_equal(existing, payload):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "seq_conflict",
                    "message": (
                        f"箱号 {payload.box_code} 的序号 {payload.seq} 已存在，"
                        "但温度/事件时间与本次上报不一致，已拒绝"
                    ),
                    "existing": {
                        "temperature": existing.temperature,
                        "event_time": existing.event_time.isoformat() + "Z",
                    },
                    "received": {
                        "temperature": payload.temperature,
                        "event_time": payload.event_time.isoformat() + "Z",
                    },
                },
            )
        return existing, True

    event = models.BoxEvent(
        box_code=payload.box_code,
        seq=payload.seq,
        temperature=payload.temperature,
        event_time=payload.event_time,
    )
    db.add(event)
    try:
        db.flush()  # 触发唯一约束，兜住并发下的竞态
    except IntegrityError:
        db.rollback()
        # 并发请求抢先写入：以库中记录为准重新判定
        existing = db.scalar(
            select(models.BoxEvent).where(
                models.BoxEvent.box_code == payload.box_code,
                models.BoxEvent.seq == payload.seq,
            )
        )
        if existing is None:  # 理论上不会发生
            raise
        if not _content_equal(existing, payload):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "seq_conflict", "message": "同序号内容冲突，已拒绝"},
            )
        return existing, True

    db.commit()
    db.refresh(event)
    return event, False


def list_boxes(db: Session) -> list[str]:
    rows = db.scalars(
        select(models.BoxEvent.box_code)
        .distinct()
        .order_by(models.BoxEvent.box_code)
    ).all()
    return list(rows)


def _latest_subquery():
    """每个箱号当前最大序号（即最新状态），乱序入库也不受影响。"""
    return (
        select(
            models.BoxEvent.box_code.label("box_code"),
            func.max(models.BoxEvent.seq).label("max_seq"),
        )
        .group_by(models.BoxEvent.box_code)
        .subquery()
    )


def latest_states(db: Session) -> list[models.BoxEvent]:
    latest = _latest_subquery()
    rows = db.scalars(
        select(models.BoxEvent)
        .join(
            latest,
            (models.BoxEvent.box_code == latest.c.box_code)
            & (models.BoxEvent.seq == latest.c.max_seq),
        )
        .order_by(models.BoxEvent.box_code)
    ).all()
    return list(rows)


def latest_state(db: Session, box_code: str) -> models.BoxEvent | None:
    latest = _latest_subquery()
    return db.scalar(
        select(models.BoxEvent)
        .join(
            latest,
            (models.BoxEvent.box_code == latest.c.box_code)
            & (models.BoxEvent.seq == latest.c.max_seq),
        ).where(models.BoxEvent.box_code == box_code)
    )


def list_events(db: Session, box_code: str) -> list[models.BoxEvent]:
    return list(
        db.scalars(
            select(models.BoxEvent)
            .where(models.BoxEvent.box_code == box_code)
            .order_by(models.BoxEvent.seq)
        ).all()
    )


def _to_epoch_micros(dt: datetime) -> int:
    """UTC naive datetime -> 自 Unix 纪元的整数微秒（精确，无浮点误差）。"""
    delta = dt.replace(tzinfo=timezone.utc) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _from_epoch_micros(micros: int) -> datetime:
    """整数微秒 -> UTC naive datetime。"""
    secs, rem = divmod(micros, 1_000_000)
    return (_EPOCH + timedelta(seconds=secs, microseconds=rem)).replace(tzinfo=None)


def _isotonic_blocks(values: list[int]) -> list[tuple[int, int, int]]:
    """等权保序回归（PAVA，Pool Adjacent Violators Algorithm）。

    输入按 seq 升序的整数微秒事件时间；在“建议值非递减”约束下最小化
    sum((建议值 - 原值)^2)，等价于把相邻违规块不断合并、块内取算术均值。

    为避免大整数除法的浮点误差，块以 (序号起点, 序号终点, 总和, 个数)
    维护；块间比较与均值输出均用整数运算。均值四舍五入到微秒
    （半数远离零；微秒均值不可能出现半数情形时同样正确）。

    返回 [(index_from, index_to, rounded_mean_micros), ...]，
    下标对应输入 values 的位置，由调用方映射回事件 seq。
    """
    # 每个元素：[seq_from, seq_to, total_micros, count]
    blocks: list[list[int]] = []
    for i, v in enumerate(values):
        block = [i, i, v, 1]
        # 前一块均值 > 当前块均值即为违规，向前合并直到满足非递减
        while blocks:
            prev = blocks[-1]
            # prev.total / prev.count > block.total / block.count
            if prev[2] * block[3] <= block[2] * prev[3]:
                break
            blocks.pop()
            block = [
                prev[0],
                block[1],
                prev[2] + block[2],
                prev[3] + block[3],
            ]
        blocks.append(block)

    def _round_half_away(total: int, count: int) -> int:
        q, r = divmod(abs(total), count)
        # 余数 * 2 >= 分母即小数部分 >= 0.5，向远离零方向进位
        if 2 * r >= count:
            q += 1
        return q if total >= 0 else -q

    return [
        (b[0], b[1], _round_half_away(b[2], b[3])) for b in blocks
    ]


def _events_in_range(
    db: Session,
    box_code: str,
    seq_from: int,
    seq_to: int,
    limit: int | None = None,
) -> list[models.BoxEvent] | None:
    """按 seq 升序取区间内事件（含端点）。

    箱号不存在（从未上报过）返回 None，由调用方转 404；
    箱号存在但区间内无事件返回 []。缺号不补点，只返回库中实际事件。
    limit 给定时最多返回该数量条（用于压缩预览的 120 条上限校验）。
    """
    box_exists = db.scalar(
        select(func.count())
        .select_from(models.BoxEvent)
        .where(models.BoxEvent.box_code == box_code)
    )
    if not box_exists:
        return None

    stmt = (
        select(models.BoxEvent)
        .where(
            models.BoxEvent.box_code == box_code,
            models.BoxEvent.seq >= seq_from,
            models.BoxEvent.seq <= seq_to,
        )
        .order_by(models.BoxEvent.seq)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt).all())


def _isotonic_suggestions(
    events: list[models.BoxEvent],
) -> tuple[list[int], list[tuple[int, int]]]:
    """对按 seq 升序的事件做等权保序回归校正（clock-audit 同款算法）。

    返回 (逐事件建议时间的整数微秒, 逐事件所属合并块的 (起始序号, 结束序号))，
    下标与 events 一一对应。只读计算，不改动任何原始记录。
    """
    values = [_to_epoch_micros(e.event_time) for e in events]
    blocks = _isotonic_blocks(values)

    # PAVA 输出的块下标对应 events 下标；展开为逐事件的建议
    suggested: list[int] = [0] * len(events)
    block_range: list[tuple[int, int]] = [(0, 0)] * len(events)
    for idx_from, idx_to, mean_micros in blocks:
        seq_from_block = events[idx_from].seq
        seq_to_block = events[idx_to].seq
        for i in range(idx_from, idx_to + 1):
            suggested[i] = mean_micros
            block_range[i] = (seq_from_block, seq_to_block)
    return suggested, block_range


def clock_audit(
    db: Session, box_code: str, seq_from: int, seq_to: int
) -> list[schemas.ClockAuditItem] | None:
    """对指定序号区间（含端点）生成设备时钟回拨校正建议。

    - 箱号不存在（从未上报过）：返回 None，由路由层转 404
    - 箱号存在但区间内无事件：返回 []
    - 校正只在本次查询中即时计算，不改动任何原始记录；乱序补报后再次查询
      会基于最新数据重新计算
    """
    events = _events_in_range(db, box_code, seq_from, seq_to)
    if events is None:
        return None
    if not events:
        return []

    values = [_to_epoch_micros(e.event_time) for e in events]
    suggested, block_range = _isotonic_suggestions(events)

    return [
        schemas.ClockAuditItem(
            seq=event.seq,
            original_time=event.event_time,
            suggested_time=_from_epoch_micros(sugg),
            offset_micros=sugg - values[i],
            block_seq_from=block_range[i][0],
            block_seq_to=block_range[i][1],
        )
        for i, (event, sugg) in enumerate(zip(events, suggested))
    ]


def _merkle_leaf(event: models.BoxEvent) -> tuple[str, bytes]:
    """构造叶子原文并计算叶哈希，返回 (原文, 哈希字节)。

    叶子原文：箱号、序号、UTC 时间字符串（YYYY-MM-DDTHH:MM:SS.ffffffZ）与
    .17g 温度字符串组成的无空格 JSON 数组；中文等非 ASCII 字符原样保留
    （ensure_ascii=False），按 UTF-8 编码参与哈希。
    叶哈希：SHA-256(0x00 || 原文的 UTF-8 字节)。
    """
    time_str = event.event_time.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    temp_str = format(event.temperature, ".17g")
    leaf_data = json.dumps(
        [event.box_code, event.seq, time_str, temp_str],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(b"\x00" + leaf_data.encode("utf-8")).digest()
    return leaf_data, digest


def _merkle_root_and_proof(
    leaf_hashes: list[bytes], target_index: int
) -> tuple[bytes, list[tuple[bytes, str]]]:
    """按查询顺序（seq 升序）的叶哈希列表建树，返回 (根哈希, 目标叶的证明路径)。

    父节点哈希 = SHA-256(0x01 || 左哈希 || 右哈希)；某层节点数为奇数时
    复制末节点再两两配对。证明路径从叶层到根层逐层给出 (兄弟哈希, 兄弟方位)，
    方位 left/right 表示兄弟在当前节点的左/右；单事件（叶即根）时为空路径。
    """
    level = list(leaf_hashes)
    index = target_index
    proof: list[tuple[bytes, str]] = []
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])  # 奇数层复制末节点
        sibling = index ^ 1
        side = "right" if index % 2 == 0 else "left"
        proof.append((level[sibling], side))
        level = [
            hashlib.sha256(b"\x01" + level[i] + level[i + 1]).digest()
            for i in range(0, len(level), 2)
        ]
        index //= 2
    return level[0], proof


def merkle_proof(
    db: Session, box_code: str, seq_from: int, seq_to: int, target_seq: int
) -> schemas.MerkleProofResult | None:
    """区间事件的 Merkle 包含证明（只读即时计算，不补缺号、不写回）。

    - 取区间 [seq_from, seq_to]（含端点）内按 seq 升序的实际已存事件作为叶子，
      缺号不补点；目标序号必须对应区间内实际存在的事件
    - 箱号不存在（从未上报过）返回 None，由路由层转 404
    - 目标序号不在区间实际事件中（含区间为空）：抛 404（target_not_in_range）
    """
    events = _events_in_range(db, box_code, seq_from, seq_to)
    if events is None:
        return None

    target_index = next(
        (i for i, e in enumerate(events) if e.seq == target_seq), None
    )
    if target_index is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "target_not_in_range",
                "message": (
                    f"箱号 {box_code} 在区间 [{seq_from}, {seq_to}] 内"
                    f"不存在序号为 {target_seq} 的事件"
                ),
                "box_code": box_code,
                "seq_from": seq_from,
                "seq_to": seq_to,
                "target_seq": target_seq,
            },
        )

    leaf_data_list: list[str] = []
    leaf_hashes: list[bytes] = []
    for event in events:
        leaf_data, digest = _merkle_leaf(event)
        leaf_data_list.append(leaf_data)
        leaf_hashes.append(digest)

    root, proof = _merkle_root_and_proof(leaf_hashes, target_index)

    return schemas.MerkleProofResult(
        box_code=box_code,
        seq_from=seq_from,
        seq_to=seq_to,
        target_seq=target_seq,
        event_count=len(events),
        leaf_data=leaf_data_list[target_index],
        target_leaf_hash=leaf_hashes[target_index].hex(),
        root_hash=root.hex(),
        proof=[
            schemas.MerkleProofStep(sibling_hash=h.hex(), sibling_side=side)
            for h, side in proof
        ],
    )


def _segment_exposure(
    temp_from: float, temp_to: float, upper_limit: float, gap_seconds: float
) -> float:
    """相邻两点间温度线性变化时，超过 upper_limit 部分对时间的精确积分（℃·秒）。

    超出量 a = temp_from - upper_limit、b = temp_to - upper_limit 同样线性变化，
    在时长 gap_seconds（> 0）上对 max(超出量, 0) 积分：
    - 两端都不超：0
    - 两端都超：梯形面积 (a + b) / 2 * d
    - 一端超：线性过限，超出部分是三角形，面积 超端超出量² / (2 * |b - a|) * d
    """
    a = temp_from - upper_limit
    b = temp_to - upper_limit
    if a <= 0.0 and b <= 0.0:
        return 0.0
    if a >= 0.0 and b >= 0.0:
        return (a + b) / 2.0 * gap_seconds
    over = max(a, b)
    return over * over / (2.0 * abs(b - a)) * gap_seconds


def heat_exposure(
    db: Session,
    box_code: str,
    seq_from: int,
    seq_to: int,
    upper_limit: float,
    max_gap_seconds: float,
) -> schemas.HeatExposureResult | None:
    """区间内相邻事件的超温积分（℃·秒），基于 clock-audit 同款校正时间。

    - 相邻事件（按 seq 升序，缺号不补点）间温度视为线性变化，对超过
      upper_limit 的部分做精确积分
    - 事件时间先按等权保序回归校正；校正后间隔为零或超过 max_gap_seconds
      的段不计入积分（也不出现在明细中）
    - 只在查询时即时计算，不改动任何原始记录；补报后重查即重算
    - 箱号不存在返回 None，由路由层转 404；区间无事件返回零值及空明细
    """
    events = _events_in_range(db, box_code, seq_from, seq_to)
    if events is None:
        return None

    corrected, _ = _isotonic_suggestions(events)

    segments: list[schemas.HeatExposureSegment] = []
    exposures: list[float] = []
    skipped = 0
    for i in range(len(events) - 1):
        gap_micros = corrected[i + 1] - corrected[i]
        gap_seconds = gap_micros / 1_000_000
        # 校正时间非递减，间隔只会 >= 0；为 0（同块）或超上限的段不计入
        if gap_micros <= 0 or gap_seconds > max_gap_seconds:
            skipped += 1
            continue
        exposure = _segment_exposure(
            events[i].temperature, events[i + 1].temperature,
            upper_limit, gap_seconds,
        )
        exposures.append(exposure)
        segments.append(
            schemas.HeatExposureSegment(
                start_seq=events[i].seq,
                end_seq=events[i + 1].seq,
                start_time=_from_epoch_micros(corrected[i]),
                end_time=_from_epoch_micros(corrected[i + 1]),
                gap_seconds=gap_seconds,
                start_temperature=events[i].temperature,
                end_temperature=events[i + 1].temperature,
                celsius_seconds=exposure,
            )
        )

    return schemas.HeatExposureResult(
        box_code=box_code,
        seq_from=seq_from,
        seq_to=seq_to,
        upper_limit=upper_limit,
        max_gap_seconds=max_gap_seconds,
        event_count=len(events),
        segment_count=len(segments),
        skipped_segment_count=skipped,
        total_celsius_seconds=math.fsum(exposures),
        segments=segments,
    )


def _time_weighted_temperature_median(
    valid_segments: list[tuple[Fraction, Fraction, int]],
) -> tuple[float | None, float]:
    """计算“按时间持续加权”的温度中位数与有效总时长。

    每段为 (起点温度, 终点温度, 校正后整数微秒时长)，时长均 > 0。
    段内温度在两端之间按时间线性变化，故时长在温度上是均匀分布；
    恒温段（两端温度相同）退化为该温度上的点质量（atom）。

    中位数定义为：累计时长占比首次达到一半（>= 总时长 / 2）的**最低温度**，
    即该混合分布的 CDF 取下侧广义逆。全程用 Fraction 精确计算
    （温度与微秒时长均可无损转为有理数），避免在半总时长边界上因浮点误差选错点。

    做法：收集所有端点温度作为断点；在每个断点处汇总恒温点质量与
    开区间密度（微秒/℃）的净变化（密度 = 段时长 / 温度跨度），然后从低到高扫描：
    先累计当前温度的点质量，再按均匀密度跨过它到下一断点。
    返回 (中位数温度, 总秒数)；valid_segments 为空时返回 (None, 0.0)。
    """
    if not valid_segments:
        return None, 0.0

    # 断点温度 -> [点质量, 密度净变化]，键全部用 Fraction
    point_mass: dict[Fraction, Fraction] = {}
    density_delta: dict[Fraction, Fraction] = {}
    breaks: set[Fraction] = set()
    total = Fraction(0)

    def _add(target: dict[Fraction, Fraction], key: Fraction, value: Fraction) -> None:
        target[key] = target.get(key, Fraction(0)) + value

    for t0_f, t1_f, d in valid_segments:
        total += d
        breaks.add(t0_f)
        breaks.add(t1_f)
        if t0_f == t1_f:
            # 恒温段：整段时长作为该温度的点质量
            _add(point_mass, t0_f, Fraction(d))
        elif t0_f < t1_f:
            # 升温段：时长 d 在 [t0, t1] 上均匀，密度为 d/(t1-t0)（微秒/℃）
            density = Fraction(d) / (t1_f - t0_f)
            _add(density_delta, t0_f, density)
            _add(density_delta, t1_f, -density)
        else:
            # 降温段：时长 d 在 [t1, t0] 上均匀，密度同样为 d/(t0-t1)
            density = Fraction(d) / (t0_f - t1_f)
            _add(density_delta, t1_f, density)
            _add(density_delta, t0_f, -density)

    ordered = sorted(breaks)
    half = total / 2
    cumulative = Fraction(0)
    rho = Fraction(0)  # 当前断点右侧开区间内的温度密度（微秒/℃）
    median: Fraction | None = None
    for idx, x in enumerate(ordered):
        # 1) 该温度处：先并入进入此断点的密度净变化（首个断点为 +密度），
        #    再统计恒温点质量
        rho += density_delta.get(x, Fraction(0))
        mass = point_mass.get(x, Fraction(0))
        if mass > 0 and cumulative + mass >= half:
            median = x
            break
        cumulative += mass
        # 2) 跨过开区间 (x, next_break)，密度恒定为 rho
        if idx + 1 >= len(ordered):
            break  # 末断点右侧无任何分布
        nxt = ordered[idx + 1]
        if rho > 0:
            need = half - cumulative
            span = rho * (nxt - x)
            if span >= need:
                median = x + need / rho
                break
            cumulative += span
        # rho == 0：两断点间没有有效分布，直接越过，不累计时长

    assert median is not None  # 累计到最高断点必达 half
    return float(median), float(total) / 1_000_000.0


def temperature_median(
    db: Session,
    box_code: str,
    seq_from: int,
    seq_to: int,
    max_gap_seconds: float,
) -> schemas.TemperatureMedianResult | None:
    """区间内按持续时间加权的温度中位数，基于 clock-audit 同款校正时间。

    - 相邻事件（按 seq 升序，缺号不补点）间温度视为线性变化，把每个有效段
      的每一瞬间按其持续时间计入温度分布；恒温段的整段时长是该温度的点质量
    - 事件时间先按等权保序回归校正；校正后间隔为零（同一合并块）或超过
      max_gap_seconds 的段不计入分布（以 skipped_segment_count 计数）
    - 中位数取累计时长占比首次达到一半的最低温度；没有有效段时
      median_temperature=None、total_seconds=0.0
    - 只在查询时即时计算，不改动任何原始记录；补报后重查即重算
    - 箱号不存在返回 None，由路由层转 404；区间无事件返回空结果
    """
    events = _events_in_range(db, box_code, seq_from, seq_to)
    if events is None:
        return None

    corrected, _ = _isotonic_suggestions(events)

    valid_segments: list[tuple[Fraction, Fraction, int]] = []
    skipped = 0
    for i in range(len(events) - 1):
        gap_micros = corrected[i + 1] - corrected[i]
        gap_seconds = gap_micros / 1_000_000
        # 校正时间非递减，间隔只会 >= 0；为 0（同块）或超上限的段不计入
        if gap_micros <= 0 or gap_seconds > max_gap_seconds:
            skipped += 1
            continue
        valid_segments.append(
            (Fraction(events[i].temperature), Fraction(events[i + 1].temperature), gap_micros)
        )

    median_temperature, total_seconds = _time_weighted_temperature_median(valid_segments)

    return schemas.TemperatureMedianResult(
        box_code=box_code,
        seq_from=seq_from,
        seq_to=seq_to,
        max_gap_seconds=max_gap_seconds,
        event_count=len(events),
        segment_count=len(valid_segments),
        skipped_segment_count=skipped,
        total_seconds=total_seconds,
        median_temperature=median_temperature,
    )


def _compress_trajectory(
    xs: list[int], ys: list[float], max_error: float
) -> tuple[list[int], Fraction]:
    """最少保留点温度轨迹压缩（DP 精确求解）。

    以序号为横坐标、温度为纵坐标，相邻保留点之间按温度线性插值重建被省略点。
    可行子序列要求：首末点必保留；每个被省略点的重建温度与原值之差 <= max_error。
    在全部可行子序列中选保留数最少的；保留数相同时取保留序号序列字典序最小者。

    方法：在“段 (i,j) 可行”构成的 DAG 上做最短路径 DP，段可行只取决于两端点，
    贪心“尽量跳最远”不能保证同数量下字典序最小，故用 DP 精确求解。
    误差用 Fraction 精确计算（float 可无损转为有理数，序号为整数），
    避免浮点误差在 max_error 边界上误判；区间事件数 <= 120，O(n^3) 足够快。

    返回 (保留点在输入中的下标序列, 被省略点的最大重建误差)。
    """
    n = len(xs)
    yf = [Fraction(y) for y in ys]
    tol = Fraction(max_error)

    def deviation(i: int, k: int, j: int) -> Fraction:
        """点 k 相对弦 (i,j) 线性插值的重建误差绝对值（精确有理数）。"""
        interp = yf[i] + (yf[j] - yf[i]) * Fraction(xs[k] - xs[i], xs[j] - xs[i])
        return abs(yf[k] - interp)

    if n <= 2:  # 0/1/2 条：首末保留即全部，没有被省略点
        return list(range(n)), Fraction(0)

    def feasible(i: int, j: int) -> bool:
        return all(deviation(i, k, j) <= tol for k in range(i + 1, j))

    # best[i] = (从第 i 个事件到末尾的最少保留数, 对应的最优保留下标序列)
    best: list[tuple[int, tuple[int, ...]]] = [(0, ())] * n
    best[n - 1] = (1, (n - 1,))
    for i in range(n - 2, -1, -1):
        chosen: tuple[int, tuple[int, ...]] | None = None
        for j in range(i + 1, n):
            if not feasible(i, j):
                continue
            # 元组比较：先比保留数（最少），数量相同再比下标序列（字典序最小）；
            # 所有候选序列同以 i 开头，等价于挑后缀最优的可行后继 j
            cand = (best[j][0] + 1, (i,) + best[j][1])
            if chosen is None or cand < chosen:
                chosen = cand
        assert chosen is not None  # 相邻两点构成的段没有中间点，必然可行
        best[i] = chosen
    kept = list(best[0][1])

    actual_max = Fraction(0)
    for a, b in zip(kept, kept[1:]):
        for k in range(a + 1, b):
            dev = deviation(a, k, b)
            if dev > actual_max:
                actual_max = dev
    return kept, actual_max


def trajectory_compression(
    db: Session, box_code: str, seq_from: int, seq_to: int, max_error: float
) -> schemas.TrajectoryCompressionResult | None:
    """单箱温度轨迹压缩预览（只读即时计算，不写回任何原始记录）。

    - 取区间 [seq_from, seq_to]（含端点）内按 seq 升序的事件，缺号不补点；
      区间事件超过 TRAJECTORY_COMPRESSION_MAX_EVENTS（120）条 -> 422
    - 以序号为横坐标做最少保留点压缩，保留点之间线性插值重建被省略事件，
      重建误差不得超过 max_error；同数量选保留序号字典序最小者
    - 箱号不存在（从未上报过）返回 None，由路由层转 404
    - 箱号存在但区间内无事件：返回空结果；仅一条事件：直接保留
    """
    events = _events_in_range(
        db,
        box_code,
        seq_from,
        seq_to,
        limit=TRAJECTORY_COMPRESSION_MAX_EVENTS + 1,
    )
    if events is None:
        return None
    if len(events) > TRAJECTORY_COMPRESSION_MAX_EVENTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "too_many_events",
                "message": (
                    f"序号区间 [{seq_from}, {seq_to}] 内事件超过 "
                    f"{TRAJECTORY_COMPRESSION_MAX_EVENTS} 条，请缩小区间后重试"
                ),
                "seq_from": seq_from,
                "seq_to": seq_to,
                "limit": TRAJECTORY_COMPRESSION_MAX_EVENTS,
            },
        )

    if not events:
        return schemas.TrajectoryCompressionResult(
            box_code=box_code,
            seq_from=seq_from,
            seq_to=seq_to,
            max_error=max_error,
            event_count=0,
            kept_count=0,
            omitted_count=0,
            actual_max_error=None,
            kept_events=[],
        )

    xs = [e.seq for e in events]
    ys = [e.temperature for e in events]
    kept_idx, actual_max = _compress_trajectory(xs, ys, max_error)

    return schemas.TrajectoryCompressionResult(
        box_code=box_code,
        seq_from=seq_from,
        seq_to=seq_to,
        max_error=max_error,
        event_count=len(events),
        kept_count=len(kept_idx),
        omitted_count=len(events) - len(kept_idx),
        actual_max_error=float(actual_max),
        kept_events=[
            schemas.TrajectoryCompressionPoint(
                seq=events[i].seq,
                event_time=events[i].event_time,
                temperature=events[i].temperature,
            )
            for i in kept_idx
        ],
    )


# ---------------------------------------------------------------------------
# 交接快照
# ---------------------------------------------------------------------------


def _same_handoff_request(
    existing: models.Handoff, payload: schemas.HandoffIn
) -> bool:
    """同一交接编号下请求内容是否一致：温度上下限相同且箱号集合相同。

    箱号按去空白、排序后的集合比较——同一次交接的箱号顺序与幂等语义无关；
    温度为 JSON 原样解析的 float，重试原样重发解析结果相同，直接判等。
    """
    if (
        existing.temp_lower != payload.temp_lower
        or existing.temp_upper != payload.temp_upper
    ):
        return False
    existing_codes = {item.box_code for item in existing.items}
    return existing_codes == set(payload.box_codes)


def _handoff_to_snapshot(handoff: models.Handoff) -> schemas.HandoffSnapshot:
    """ORM 快照行 -> 响应模型，明细按冻结时的请求顺序（position）排列。"""
    items = sorted(handoff.items, key=lambda item: item.position)
    counts = {
        schemas.CONCLUSION_MISSING: 0,
        schemas.CONCLUSION_OUT_OF_RANGE: 0,
        schemas.CONCLUSION_NORMAL: 0,
    }
    for item in items:
        counts[item.conclusion] += 1
    return schemas.HandoffSnapshot(
        handoff_no=handoff.handoff_no,
        temp_lower=handoff.temp_lower,
        temp_upper=handoff.temp_upper,
        created_at=handoff.created_at,
        items=[schemas.HandoffItemOut.model_validate(item) for item in items],
        summary=schemas.HandoffSummary(
            total=len(items),
            missing=counts[schemas.CONCLUSION_MISSING],
            out_of_range=counts[schemas.CONCLUSION_OUT_OF_RANGE],
            normal=counts[schemas.CONCLUSION_NORMAL],
        ),
    )


def create_handoff(
    db: Session, payload: schemas.HandoffIn
) -> tuple[schemas.HandoffSnapshot, bool]:
    """创建交接快照（整单在同一事务内读状态 + 冻结）。

    - 交接编号已存在且内容一致：返回原快照（duplicate=True），不重建、不更新
    - 交接编号已存在但内容不同（箱号集合或温度上下限变化）：拒绝（409）
    - 新建：对每个箱按当前最大 seq 读状态并冻结结论；库中无事件的箱记缺报；
      温度严格低于下限或高于上限记越界；闭区间内（含恰等边界值）记正常
    快照只写一次，后续乱序补报进入 box_events 但不回写本表。
    """
    existing = db.scalar(
        select(models.Handoff).where(models.Handoff.handoff_no == payload.handoff_no)
    )
    if existing is not None:
        if not _same_handoff_request(existing, payload):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "handoff_conflict",
                    "message": (
                        f"交接编号 {payload.handoff_no} 已存在快照，"
                        "但箱号列表或温度上下限与本次请求不一致，已拒绝"
                    ),
                    "existing": {
                        "temp_lower": existing.temp_lower,
                        "temp_upper": existing.temp_upper,
                        "box_codes": [
                            item.box_code
                            for item in sorted(existing.items, key=lambda i: i.position)
                        ],
                    },
                    "received": {
                        "temp_lower": payload.temp_lower,
                        "temp_upper": payload.temp_upper,
                        "box_codes": payload.box_codes,
                    },
                },
            )
        return _handoff_to_snapshot(existing), True

    # 一次性取全部已上报箱的最新（最大 seq）状态，键为箱号
    latest_by_code = {event.box_code: event for event in latest_states(db)}

    handoff = models.Handoff(
        handoff_no=payload.handoff_no,
        temp_lower=payload.temp_lower,
        temp_upper=payload.temp_upper,
    )
    db.add(handoff)

    for position, box_code in enumerate(payload.box_codes):
        latest = latest_by_code.get(box_code)
        if latest is None:
            conclusion = schemas.CONCLUSION_MISSING
            item = models.HandoffItem(
                position=position,
                box_code=box_code,
                seq=None,
                temperature=None,
                event_time=None,
                conclusion=conclusion,
            )
        else:
            # 边界值算正常：闭区间 [temp_lower, temp_upper]
            if (
                latest.temperature < payload.temp_lower
                or latest.temperature > payload.temp_upper
            ):
                conclusion = schemas.CONCLUSION_OUT_OF_RANGE
            else:
                conclusion = schemas.CONCLUSION_NORMAL
            item = models.HandoffItem(
                position=position,
                box_code=box_code,
                seq=latest.seq,
                temperature=latest.temperature,
                event_time=latest.event_time,
                conclusion=conclusion,
            )
        handoff.items.append(item)

    db.add(handoff)
    try:
        # 同一事务：读取最新状态与写入快照主/明细行一起提交
        db.flush()  # 触发 handoff_no 唯一约束，兜住并发下的竞态
    except IntegrityError:
        db.rollback()
        # 并发请求抢先创建：以库中快照为准重新判定
        existing = db.scalar(
            select(models.Handoff).where(
                models.Handoff.handoff_no == payload.handoff_no
            )
        )
        if existing is None:  # 理论上不会发生
            raise
        if not _same_handoff_request(existing, payload):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "handoff_conflict", "message": "交接快照内容冲突，已拒绝"},
            )
        return _handoff_to_snapshot(existing), True

    db.commit()
    db.refresh(handoff)
    return _handoff_to_snapshot(handoff), False


def get_handoff(db: Session, handoff_no: str) -> schemas.HandoffSnapshot | None:
    """按交接编号读取冻结快照；不存在返回 None，由路由层转 404。"""
    handoff = db.scalar(
        select(models.Handoff).where(models.Handoff.handoff_no == handoff_no)
    )
    if handoff is None:
        return None
    return _handoff_to_snapshot(handoff)
