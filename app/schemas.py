"""Pydantic 响应模型（Pydantic v2）。"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

AnalysisStatus = Literal["PENDING", "COMPLETED", "FAILED", "SUPERSEDED"]
RevisionStatus = Literal["RECEIVED", "CURRENT", "SUPERSEDED"]
OutcomeCode = Literal["READMISSION", "DEATH", "CENSORED"]
EventCode = Literal["READMISSION", "DEATH"]
EvidenceStatus = Literal["NONE", "PASSED", "FAILED"]
ReconciliationStatus = Literal["PASSED", "FAILED"]
IssueType = Literal[
    "AMBIGUOUS_FIRST_DAY", "EVENT_NOT_RECORDED", "MISSING_EVIDENCE", "UNKNOWN_SUBJECT"
]


class UploadResponse(BaseModel):
    revision: int = Field(..., description="修订版本号（递增整数）")
    accepted: bool = Field(..., description="本次请求是否受理了新版本")
    idempotent: bool = Field(..., description="是否为同版同内容的重复送达（未产生任何改动）")
    content_sha256: str = Field(..., description="CSV 原始字节的 SHA-256")
    n_records: int = Field(..., description="记录人数")
    revision_status: RevisionStatus
    analysis_status: AnalysisStatus
    current_revision: Optional[int] = Field(None, description="当前现行修订版本号")
    message: str


class RevisionOut(BaseModel):
    revision: int
    content_sha256: str
    status: RevisionStatus
    n_records: int
    uploaded_at: str
    superseded_at: Optional[str] = None
    analysis_status: AnalysisStatus
    analysis_error: Optional[str] = None


class DayRow(BaseModel):
    day: int = Field(..., description="随访日（1~30）")
    at_risk: int = Field(..., description="日初在险人数（同日两类事件共用此人数）")
    readmission_count: int = Field(..., description="当日再入院事件数")
    death_count: int = Field(..., description="当日死亡事件数（竞争结局）")
    censored_count: int = Field(..., description="当日随访截止（删失）人数，事件后退出")
    readmission_cif: float = Field(..., description="截至当日（含当日）再入院累计发生概率")
    death_cif: float = Field(..., description="截至当日（含当日）死亡累计发生概率")
    survival: float = Field(..., description="当日结束时仍在险的生存概率")


class AnalysisSummary(BaseModel):
    n_subjects: int
    n_readmission: int
    n_death: int
    n_censored: int
    cumulative_readmission: float = Field(..., description="30 天再入院累计发生概率（Aalen-Johansen CIF）")
    cumulative_death: float
    last_activity_day: int


class AnalysisResponse(BaseModel):
    revision: int
    is_current: bool
    analysis_status: AnalysisStatus
    error: Optional[str] = None
    evidence_status: Optional[EvidenceStatus] = Field(
        None, description="该修订事件证据对账状态；现行版本须为 PASSED 才可供数"
    )
    summary: Optional[AnalysisSummary] = None
    daily: list[DayRow] = Field(default_factory=list)


class RecomputeResponse(BaseModel):
    revision: int
    triggered: bool
    already_running: bool
    analysis_status: AnalysisStatus
    message: str


# ---------------------------------------------------------------------------
# 事件证据对账
# ---------------------------------------------------------------------------


class ReconciliationIssue(BaseModel):
    subject_id: str = Field(..., description="涉及的患者号")
    issue_type: IssueType = Field(..., description="问题类型")
    message: str = Field(..., description="问题描述")
    cohort_day: Optional[int] = Field(None, description="队列记录的结局日")
    cohort_outcome: Optional[OutcomeCode] = Field(None, description="队列记录的结局")
    evidence_day: Optional[int] = Field(None, description="证据最早事件日")
    evidence_event: Optional[EventCode] = Field(None, description="证据最早事件类型")


class ReconciliationOut(BaseModel):
    status: ReconciliationStatus
    n_subjects: int = Field(..., description="现行队列人数")
    n_issues: int = Field(..., description="逐人问题数")
    computed_at: str
    issues: list[ReconciliationIssue] = Field(default_factory=list)


class EvidenceBatchOut(BaseModel):
    batch_id: int
    revision: int
    content_sha256: str
    n_events: int
    status: Literal["ACTIVE", "SUPERSEDED"]
    uploaded_at: str
    superseded_at: Optional[str] = None
    reconciliation_status: Optional[ReconciliationStatus] = None
    reconciliation_n_issues: Optional[int] = None


class ServingStatus(BaseModel):
    revision: Optional[int] = Field(None, description="现行修订版本号")
    analysis_status: Optional[AnalysisStatus] = None
    evidence_status: EvidenceStatus = Field(
        ..., description="现行有效证据对账状态：NONE 无有效证据 / PASSED / FAILED"
    )
    servable: bool = Field(
        ..., description="是否可供数：仅分析完成且证据核对通过时为 true"
    )
    message: str


class EvidenceUploadResponse(BaseModel):
    revision: int = Field(..., description="证据所针对的（现行）修订版本号")
    batch_id: int
    accepted: bool = Field(..., description="本次请求是否受理了新证据批次")
    idempotent: bool = Field(..., description="是否为同版同内容重复送达（未产生任何改动）")
    content_sha256: str
    n_events: int = Field(..., description="证据事件条数（同一患者可多条）")
    reconciliation: ReconciliationOut
    serving: ServingStatus
    message: str


class EvidenceStateResponse(BaseModel):
    revision: int
    is_current: bool
    active_batch: Optional[EvidenceBatchOut] = Field(
        None, description="现行有效证据批次（无则 null）"
    )
    reconciliation: Optional[ReconciliationOut] = Field(
        None, description="有效批次的对账结论与逐人问题（无有效批次则 null）"
    )
    batches: list[EvidenceBatchOut] = Field(
        default_factory=list, description="该修订全部证据批次（含已撤销，审计留痕）"
    )
    serving: ServingStatus


class ErrorDetail(BaseModel):
    line: Optional[int] = None
    message: str


class ErrorResponse(BaseModel):
    error: str
    details: list[ErrorDetail] = Field(default_factory=list)
    current_revision: Optional[int] = None
    context: Optional[dict[str, Any]] = Field(
        None, description="附加上下文（如供数状态、问题数等）"
    )
