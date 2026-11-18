"""FastAPI 入口：单队列竞争风险（再入院/死亡）30 天累计发生概率分析。

在竞争风险分析之上叠加事件证据对账：数据员上传院内事件记录核实每人终局，
仅"证据核对通过且分析完成"的现行风险结果可供数。
"""
from __future__ import annotations

import logging
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse

from . import config, database as db
from .csv_parser import CsvValidationError, parse_csv_bytes, parse_evidence_csv_bytes
from .schemas import (
    AnalysisResponse,
    AnalysisSummary,
    DayRow,
    ErrorResponse,
    EvidenceBatchOut,
    EvidenceStateResponse,
    EvidenceUploadResponse,
    RecomputeResponse,
    ReconciliationIssue,
    ReconciliationOut,
    RevisionOut,
    ServingStatus,
    UploadResponse,
)
from .services import current_running_revision, ensure_worker, recover_on_startup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("cohort.api")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.ensure_dirs()
    db.init_db()
    recover_on_startup()
    yield


app = FastAPI(
    title="单队列竞争风险分析 API",
    description=(
        "上传出院患者队列 CSV（subject_id, day, outcome），"
        "按天返回日初在险人数、再入院/死亡事件数与 Aalen-Johansen 累计发生概率。"
        "死亡为竞争结局，随访截止按普通删失处理。修订版本递增、整体更换。"
        "现行风险结果须事件证据对账通过且分析完成才可供数。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["运维"], summary="健康检查")
def health() -> dict:
    serving = db.get_serving_status()
    return {
        "status": "ok",
        "running_revision": current_running_revision(),
        "servable": serving["servable"],
    }


async def _stage_upload(file: UploadFile, prefix: str) -> bytes:
    """把上传内容落到独立暂存目录中的新文件，读回字节后立即删除。"""
    config.ensure_dirs()
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=config.UPLOAD_DIR, delete=False, prefix=prefix, suffix=".csv"
        ) as tmp:
            tmp_path = Path(tmp.name)
            size = 0
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > config.MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail={
                            "error": "FILE_TOO_LARGE",
                            "message": f"文件超过大小上限 {config.MAX_UPLOAD_BYTES} 字节",
                        },
                    )
                tmp.write(chunk)
        return tmp_path.read_bytes()
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("暂存文件删除失败：%s", tmp_path)


def _csv_http_error(exc: CsvValidationError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "error": "CSV_VALIDATION_FAILED",
            "message": str(exc),
            "line": exc.line,
        },
    )


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------

@app.post(
    "/api/revisions/upload",
    response_model=UploadResponse,
    tags=["修订与上传"],
    summary="上传某修订版本的完整队列 CSV（整体更换）",
    responses={
        400: {"model": ErrorResponse, "description": "CSV 校验失败"},
        409: {"model": ErrorResponse, "description": "同版异内容 / 版号非递增，拒收"},
        413: {"model": ErrorResponse, "description": "文件超过大小上限"},
    },
)
async def upload_revision(
    revision: int = Form(..., description="修订版本号，必须为递增整数（1, 2, 3, ...）"),
    file: UploadFile = File(..., description="完整 CSV：subject_id,day,outcome"),
) -> UploadResponse:
    data = await _stage_upload(file, prefix=f"rev{revision}-")

    try:
        parsed = parse_csv_bytes(data)
    except CsvValidationError as exc:
        raise _csv_http_error(exc) from exc

    try:
        outcome = db.accept_revision(revision, parsed.content_sha256, parsed.rows)
    except db.RevisionConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "REVISION_CONFLICT",
                "message": str(exc),
                "current_revision": exc.current_revision,
            },
        ) from exc

    if outcome["accepted"]:
        # 受理即触发后台重算；工作线程串行处理，落库前再次校验 CURRENT 身份
        ensure_worker()
        message = f"修订 {revision} 已受理并成为现行版本，分析已进入重算队列。"
    else:
        message = f"修订 {revision} 同版同内容重复送达，结果保持不变（幂等）。"

    return UploadResponse(
        revision=revision,
        accepted=outcome["accepted"],
        idempotent=outcome["idempotent"],
        content_sha256=parsed.content_sha256,
        n_records=outcome["n_records"],
        revision_status=outcome["revision_status"],
        analysis_status=outcome["analysis_status"],
        current_revision=outcome["current_revision"],
        message=message,
    )


@app.get("/api/revisions", response_model=list[RevisionOut], tags=["修订与上传"],
         summary="列出所有修订及其分析状态")
def list_all_revisions() -> list[RevisionOut]:
    return [
        RevisionOut(
            revision=r["revision"],
            content_sha256=r["content_sha256"],
            status=r["status"],
            n_records=r["n_records"],
            uploaded_at=r["uploaded_at"],
            superseded_at=r["superseded_at"],
            analysis_status=r["analysis_status"],
            analysis_error=r["analysis_error"],
        )
        for r in db.list_revisions()
    ]


# ---------------------------------------------------------------------------
# 分析与查询
# ---------------------------------------------------------------------------

def _build_analysis_response(revision: int) -> AnalysisResponse:
    with db.read_session() as conn:
        rev = conn.execute(
            "SELECT status FROM revisions WHERE revision = ?;", (revision,)
        ).fetchone()
    if rev is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "REVISION_NOT_FOUND", "message": f"修订 {revision} 不存在"},
        )
    is_current = rev["status"] == "CURRENT"

    row = db.get_analysis_row(revision)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "ANALYSIS_NOT_FOUND", "message": f"修订 {revision} 尚无分析记录"},
        )

    if row["status"] != "COMPLETED":
        # PENDING / FAILED / SUPERSEDED：绝不返回旧结论，只报状态
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": f"ANALYSIS_{row['status']}",
                "message": {
                    "PENDING": "分析尚待重算或正在重算，现行结论暂不可用（不会返回旧结论）。",
                    "FAILED": "最近一次重算失败，仍待重算；可调用 /api/analyses/recompute 重试。",
                    "SUPERSEDED": "该修订已被新版本取代，结论不再具备现行资格。",
                }[row["status"]],
                "revision": revision,
                "is_current": is_current,
                "analysis_error": row["error"],
                "running_revision": current_running_revision(),
            },
        )

    # 供数门禁：现行风险结果须事件证据对账通过才可供数；
    # 历史版本（is_current=false）仅供审计查询，附证据状态但不阻断
    if is_current:
        ev = db.get_active_evidence(revision)
        if ev is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "EVIDENCE_MISSING",
                    "message": "现行修订尚无有效事件证据（未上传或已被替换/撤销），"
                    "风险结果不可供数；请先上传事件证据完成对账。",
                    "revision": revision,
                    "evidence_status": "NONE",
                },
            )
        recon = ev["reconciliation"]
        if recon is None or recon["status"] != "PASSED":
            n_issues = recon["n_issues"] if recon else None
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "EVIDENCE_RECONCILIATION_FAILED",
                    "message": f"事件证据对账未通过（{n_issues} 个逐人问题），"
                    "风险结果不可供数；逐人问题见 /api/evidence/current。",
                    "revision": revision,
                    "evidence_status": "FAILED",
                    "n_issues": n_issues,
                },
            )
        evidence_status = "PASSED"
    else:
        evidence_status = db.latest_evidence_status(revision)

    daily_rows = db.get_daily_rows(revision)
    return AnalysisResponse(
        revision=revision,
        is_current=is_current,
        analysis_status="COMPLETED",
        error=None,
        evidence_status=evidence_status,
        summary=AnalysisSummary(
            n_subjects=row["n_subjects"],
            n_readmission=row["n_readmission"],
            n_death=row["n_death"],
            n_censored=row["n_censored"],
            cumulative_readmission=row["cumulative_readmission"],
            cumulative_death=row["cumulative_death"],
            last_activity_day=row["last_activity_day"],
        ),
        daily=[
            DayRow(
                day=d["day"],
                at_risk=d["at_risk"],
                readmission_count=d["readmission_count"],
                death_count=d["death_count"],
                censored_count=d["censored_count"],
                readmission_cif=d["readmission_cif"],
                death_cif=d["death_cif"],
                survival=d["survival"],
            )
            for d in daily_rows
        ],
    )


@app.get(
    "/api/analyses/current",
    response_model=AnalysisResponse,
    tags=["分析与查询"],
    summary="查询现行修订的分析结论（按天）",
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def get_current_analysis() -> AnalysisResponse:
    with db.read_session() as conn:
        current = db.current_revision_no(conn)
    if current is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "NO_CURRENT_REVISION", "message": "尚无任何修订被受理"},
        )
    return _build_analysis_response(current)


@app.get(
    "/api/analyses/{revision}",
    response_model=AnalysisResponse,
    tags=["分析与查询"],
    summary="查询指定修订的分析结论（非现行版本会标注 is_current=false）",
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def get_analysis(revision: int) -> AnalysisResponse:
    return _build_analysis_response(revision)


@app.post(
    "/api/analyses/recompute",
    response_model=RecomputeResponse,
    tags=["分析与查询"],
    summary="手动触发重算（默认重算现行修订；重算失败后可用此接口重试）",
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def recompute(revision: int | None = None) -> RecomputeResponse:
    with db.read_session() as conn:
        current = db.current_revision_no(conn)
        if current is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "NO_CURRENT_REVISION", "message": "尚无任何修订被受理"},
            )
        target = revision if revision is not None else current
        rev = db.get_revision(conn, target)
        if rev is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "REVISION_NOT_FOUND", "message": f"修订 {target} 不存在"},
            )
        if rev["status"] != "CURRENT":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "REVISION_SUPERSEDED",
                    "message": f"修订 {target} 已被取代，只有现行修订 {current} 可以重算。",
                    "current_revision": current,
                },
            )
        row = conn.execute(
            "SELECT status FROM analyses WHERE revision = ?;", (target,)
        ).fetchone()
        previous_status = row["status"] if row else None

    if previous_status == "PENDING":
        already = True
        message = f"修订 {target} 已在重算队列/计算中，不重复触发。"
    else:
        already = False
        db.mark_analysis_pending(target)
        message = f"修订 {target} 已标记为待重算（原状态 {previous_status}），后台工作线程将重新计算。"

    started = ensure_worker()
    if started:
        message += " 工作线程已启动。"
    return RecomputeResponse(
        revision=target,
        triggered=not already,
        already_running=already,
        analysis_status="PENDING",
        message=message,
    )


# ---------------------------------------------------------------------------
# 事件证据对账
# ---------------------------------------------------------------------------

def _recon_out(recon: dict | None, issues: list[dict]) -> ReconciliationOut | None:
    if recon is None:
        return None
    return ReconciliationOut(
        status=recon["status"],
        n_subjects=recon["n_subjects"],
        n_issues=recon["n_issues"],
        computed_at=recon["computed_at"],
        issues=[ReconciliationIssue(**i) for i in issues],
    )


def _batch_out(batch: dict) -> EvidenceBatchOut:
    return EvidenceBatchOut(
        batch_id=batch["batch_id"],
        revision=batch["revision"],
        content_sha256=batch["content_sha256"],
        n_events=batch["n_events"],
        status=batch["status"],
        uploaded_at=batch["uploaded_at"],
        superseded_at=batch["superseded_at"],
        reconciliation_status=batch.get("reconciliation_status"),
        reconciliation_n_issues=batch.get("reconciliation_n_issues"),
    )


def _serving_out() -> ServingStatus:
    return ServingStatus(**db.get_serving_status())


def _build_evidence_state(revision: int) -> EvidenceStateResponse:
    with db.read_session() as conn:
        rev = db.get_revision(conn, revision)
    if rev is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "REVISION_NOT_FOUND", "message": f"修订 {revision} 不存在"},
        )
    active = db.get_active_evidence(revision)
    batches = db.list_evidence_batches(revision)
    return EvidenceStateResponse(
        revision=revision,
        is_current=rev["status"] == "CURRENT",
        active_batch=_batch_out(active["batch"]) if active else None,
        reconciliation=(
            _recon_out(active["reconciliation"], active["issues"]) if active else None
        ),
        batches=[_batch_out(b) for b in batches],
        serving=_serving_out(),
    )


@app.post(
    "/api/evidence/upload",
    response_model=EvidenceUploadResponse,
    tags=["事件证据对账"],
    summary="针对现行队列上传完整事件证据 CSV 并立即对账",
    responses={
        400: {"model": ErrorResponse, "description": "CSV 校验失败"},
        404: {"model": ErrorResponse, "description": "尚无任何队列修订"},
        409: {"model": ErrorResponse, "description": "证据指定的修订非现行修订"},
        413: {"model": ErrorResponse, "description": "文件超过大小上限"},
    },
)
async def upload_evidence(
    file: UploadFile = File(
        ..., description="完整事件 CSV：subject_id,day,event（同一患者可多条）"
    ),
    revision: Optional[int] = Form(
        None,
        description="证据针对的队列修订号；留空取现行修订。"
        "建议显式填写：若上传期间队列被更新，将以 409 拒收而不是对错版本对账。",
    ),
) -> EvidenceUploadResponse:
    data = await _stage_upload(file, prefix="evidence-")

    try:
        parsed = parse_evidence_csv_bytes(data)
    except CsvValidationError as exc:
        raise _csv_http_error(exc) from exc

    try:
        outcome = db.accept_evidence(
            parsed.content_sha256, parsed.rows, expected_revision=revision
        )
    except db.NoCurrentRevision as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "NO_CURRENT_REVISION", "message": str(exc)},
        ) from exc
    except db.RevisionConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "REVISION_CONFLICT",
                "message": str(exc),
                "current_revision": exc.current_revision,
            },
        ) from exc

    batch = outcome["batch"]
    recon = _recon_out(outcome["reconciliation"], outcome["issues"])
    serving = _serving_out()
    if outcome["idempotent"]:
        message = (
            f"修订 {outcome['revision']} 同版同内容证据重复送达，"
            "沿用既有批次与对账结论（幂等，未产生任何改动）。"
        )
    elif recon and recon.status == "PASSED":
        message = (
            f"证据批次 {batch['batch_id']} 已受理，对修订 {outcome['revision']} "
            "对账通过；待分析完成后现行风险结果即可供数。"
        )
    else:
        n = recon.n_issues if recon else 0
        message = (
            f"证据批次 {batch['batch_id']} 已受理，对账发现 {n} 个逐人问题，"
            "现行风险结果不可供数；请核实后重新上传证据。"
        )
    return EvidenceUploadResponse(
        revision=outcome["revision"],
        batch_id=batch["batch_id"],
        accepted=outcome["accepted"],
        idempotent=outcome["idempotent"],
        content_sha256=parsed.content_sha256,
        n_events=parsed.n_events,
        reconciliation=recon,
        serving=serving,
        message=message,
    )


@app.get(
    "/api/evidence/current",
    response_model=EvidenceStateResponse,
    tags=["事件证据对账"],
    summary="查询现行修订的证据批次、逐人对账问题与供数状态",
    responses={404: {"model": ErrorResponse}},
)
def get_current_evidence() -> EvidenceStateResponse:
    with db.read_session() as conn:
        current = db.current_revision_no(conn)
    if current is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "NO_CURRENT_REVISION", "message": "尚无任何修订被受理"},
        )
    return _build_evidence_state(current)


@app.get(
    "/api/evidence/{revision}",
    response_model=EvidenceStateResponse,
    tags=["事件证据对账"],
    summary="查询指定修订的证据批次与对账结论（含已撤销批次，审计留痕）",
    responses={404: {"model": ErrorResponse}},
)
def get_evidence(revision: int) -> EvidenceStateResponse:
    return _build_evidence_state(revision)


@app.get(
    "/api/serving",
    response_model=ServingStatus,
    tags=["事件证据对账"],
    summary="供数状态总览：仅证据核对通过且分析完成的现行风险结果可供数",
)
def get_serving() -> ServingStatus:
    return _serving_out()


@app.exception_handler(HTTPException)
async def http_exc_handler(_request, exc: HTTPException) -> JSONResponse:
    """统一错误响应结构；detail 中的附加字段透传到 context 便于调用方取用。"""
    detail = exc.detail
    if isinstance(detail, dict) and "error" in detail:
        context = {
            k: v
            for k, v in detail.items()
            if k not in ("error", "message", "line", "current_revision")
        }
        body = ErrorResponse(
            error=detail.get("error", "ERROR"),
            details=[
                {"line": detail.get("line"), "message": detail.get("message", "")}
            ],
            current_revision=detail.get("current_revision"),
            context=context or None,
        ).model_dump()
    else:
        body = ErrorResponse(error="ERROR", details=[{"message": str(detail)}]).model_dump()
    return JSONResponse(status_code=exc.status_code, content=body)
