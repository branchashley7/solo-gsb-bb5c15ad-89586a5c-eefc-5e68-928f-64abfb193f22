"""FastAPI 入口与路由。"""
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from . import models, schemas, services
from .database import Base, engine, get_db

DbSession = Annotated[Session, Depends(get_db)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动即建表，起到“初始化依赖”的作用；已存在则 no-op
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="冷链周转箱事件接收 API",
    description=(
        "接收设备上报的温度事件。\n\n"
        "- 相同箱号 + 相同序号 + 相同内容：重复上报，幂等忽略（200，status=duplicate）\n"
        "- 相同箱号 + 相同序号但内容不同：拒绝（409）\n"
        "- 乱序事件正常入库，最新状态始终按最大序号返回\n"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["system"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/events",
    tags=["events"],
    response_model=schemas.EventAck,
    summary="上报一个温度事件（支持重试，幂等）",
)
def post_event(payload: schemas.EventIn, db: DbSession, response: Response) -> schemas.EventAck:
    event, duplicate = services.submit_event(db, payload)
    # 首次入库 201；重复上报 200，表示请求成功但未产生新状态
    response.status_code = (
        status.HTTP_200_OK if duplicate else status.HTTP_201_CREATED
    )
    return schemas.EventAck(
        status="duplicate" if duplicate else "accepted",
        event=schemas.EventOut.model_validate(event),
    )


@app.get(
    "/boxes",
    tags=["boxes"],
    response_model=list[schemas.BoxListItem],
    summary="所有上报过的箱号",
)
def get_boxes(db: DbSession) -> list[schemas.BoxListItem]:
    return [schemas.BoxListItem(box_code=code) for code in services.list_boxes(db)]


@app.get(
    "/boxes/{box_code}/state",
    tags=["boxes"],
    response_model=schemas.BoxState,
    summary="某箱子按最大序号计算的最新状态（乱序安全）",
)
def get_box_state(box_code: str, db: DbSession) -> schemas.BoxState:
    state = services.latest_state(db, box_code.strip())
    if state is None:
        raise HTTPException(status_code=404, detail={"error": "box_not_found", "box_code": box_code})
    return schemas.BoxState(
        box_code=state.box_code,
        seq=state.seq,
        temperature=state.temperature,
        event_time=state.event_time,
    )


@app.get(
    "/states",
    tags=["boxes"],
    response_model=list[schemas.BoxState],
    summary="所有箱子的最新状态",
)
def get_states(db: DbSession) -> list[schemas.BoxState]:
    return [
        schemas.BoxState(
            box_code=e.box_code,
            seq=e.seq,
            temperature=e.temperature,
            event_time=e.event_time,
        )
        for e in services.latest_states(db)
    ]


@app.get(
    "/boxes/{box_code}/events",
    tags=["events"],
    response_model=list[schemas.EventOut],
    summary="某箱子的全部事件（按序号升序）",
)
def get_box_events(box_code: str, db: DbSession) -> list[schemas.EventOut]:
    events = services.list_events(db, box_code.strip())
    if not events:
        raise HTTPException(status_code=404, detail={"error": "box_not_found", "box_code": box_code})
    return [schemas.EventOut.model_validate(e) for e in events]


@app.get(
    "/boxes/{box_code}/clock-audit",
    tags=["boxes"],
    response_model=list[schemas.ClockAuditItem],
    summary="区间事件的设备时钟回拨校正建议（等权保序回归，不改原始记录）",
)
def get_box_clock_audit(
    box_code: str,
    db: DbSession,
    seq_from: Annotated[int, Query(ge=0, description="起始序号（含）")],
    seq_to: Annotated[int, Query(ge=0, description="结束序号（含）")],
) -> list[schemas.ClockAuditItem]:
    if seq_from > seq_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "invalid_range",
                "message": f"seq_from({seq_from}) 不能大于 seq_to({seq_to})",
                "seq_from": seq_from,
                "seq_to": seq_to,
            },
        )
    items = services.clock_audit(db, box_code.strip(), seq_from, seq_to)
    if items is None:
        raise HTTPException(status_code=404, detail={"error": "box_not_found", "box_code": box_code})
    return items


@app.get(
    "/boxes/{box_code}/heat-exposure",
    tags=["boxes"],
    response_model=schemas.HeatExposureResult,
    summary="区间事件的超温积分（℃·秒，线性插值 + 保序校正时间，不改原始记录）",
)
def get_box_heat_exposure(
    box_code: str,
    db: DbSession,
    seq_from: Annotated[int, Query(ge=0, description="起始序号（含）")],
    seq_to: Annotated[int, Query(ge=0, description="结束序号（含）")],
    upper_limit: Annotated[
        float, Query(allow_inf_nan=False, description="温度上限（℃），超过部分计入积分")
    ],
    max_gap_seconds: Annotated[
        float,
        Query(
            gt=0,
            allow_inf_nan=False,
            description="允许计入的最大相邻校正间隔（秒，正数）；间隔为零或超过它的段不计入",
        ),
    ],
) -> schemas.HeatExposureResult:
    if seq_from > seq_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "invalid_range",
                "message": f"seq_from({seq_from}) 不能大于 seq_to({seq_to})",
                "seq_from": seq_from,
                "seq_to": seq_to,
            },
        )
    result = services.heat_exposure(
        db, box_code.strip(), seq_from, seq_to, upper_limit, max_gap_seconds
    )
    if result is None:
        raise HTTPException(status_code=404, detail={"error": "box_not_found", "box_code": box_code})
    return result


@app.get(
    "/boxes/{box_code}/temperature-median",
    tags=["boxes"],
    response_model=schemas.TemperatureMedianResult,
    summary="区间按持续时间加权的温度中位数（线性插值 + 保序校正时间，不改原始记录）",
)
def get_box_temperature_median(
    box_code: str,
    db: DbSession,
    seq_from: Annotated[int, Query(ge=0, description="起始序号（含）")],
    seq_to: Annotated[int, Query(ge=0, description="结束序号（含）")],
    max_gap_seconds: Annotated[
        float,
        Query(
            gt=0,
            allow_inf_nan=False,
            description="允许计入的最大相邻校正间隔（秒，正数）；间隔为零或超过它的段不计入",
        ),
    ],
) -> schemas.TemperatureMedianResult:
    if seq_from > seq_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "invalid_range",
                "message": f"seq_from({seq_from}) 不能大于 seq_to({seq_to})",
                "seq_from": seq_from,
                "seq_to": seq_to,
            },
        )
    result = services.temperature_median(
        db, box_code.strip(), seq_from, seq_to, max_gap_seconds
    )
    if result is None:
        raise HTTPException(status_code=404, detail={"error": "box_not_found", "box_code": box_code})
    return result


@app.get(
    "/boxes/{box_code}/merkle-proof",
    tags=["boxes"],
    response_model=schemas.MerkleProofResult,
    summary="区间事件的 Merkle 包含证明（按 seq 升序建树，只读不写回）",
)
def get_box_merkle_proof(
    box_code: str,
    db: DbSession,
    seq_from: Annotated[int, Query(ge=0, description="起始序号（含）")],
    seq_to: Annotated[int, Query(ge=0, description="结束序号（含）")],
    target_seq: Annotated[
        int, Query(ge=0, description="目标事件序号（须为区间内实际存在的事件）")
    ],
) -> schemas.MerkleProofResult:
    if seq_from > seq_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "invalid_range",
                "message": f"seq_from({seq_from}) 不能大于 seq_to({seq_to})",
                "seq_from": seq_from,
                "seq_to": seq_to,
            },
        )
    result = services.merkle_proof(db, box_code.strip(), seq_from, seq_to, target_seq)
    if result is None:
        raise HTTPException(status_code=404, detail={"error": "box_not_found", "box_code": box_code})
    return result


@app.get(
    "/boxes/{box_code}/trajectory-compression",
    tags=["boxes"],
    response_model=schemas.TrajectoryCompressionResult,
    summary="区间温度轨迹压缩预览（最少保留点，线性插值误差受限，只读不写回）",
)
def get_box_trajectory_compression(
    box_code: str,
    db: DbSession,
    seq_from: Annotated[int, Query(ge=0, description="起始序号（含）")],
    seq_to: Annotated[int, Query(ge=0, description="结束序号（含）")],
    max_error: Annotated[
        float,
        Query(
            ge=0,
            allow_inf_nan=False,
            description="允许的最大重建误差（℃），非负有限值；被省略事件的重建温度与原值之差不得超过它",
        ),
    ],
) -> schemas.TrajectoryCompressionResult:
    if seq_from > seq_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "invalid_range",
                "message": f"seq_from({seq_from}) 不能大于 seq_to({seq_to})",
                "seq_from": seq_from,
                "seq_to": seq_to,
            },
        )
    result = services.trajectory_compression(
        db, box_code.strip(), seq_from, seq_to, max_error
    )
    if result is None:
        raise HTTPException(status_code=404, detail={"error": "box_not_found", "box_code": box_code})
    return result
