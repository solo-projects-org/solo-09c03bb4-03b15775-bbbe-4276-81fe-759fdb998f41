"""事件证据对账：用院内事件记录核实队列中每人的终局。

数据约定：
- 队列（现行修订）每人一条终局记录 (subject_id, day, outcome)，
  outcome ∈ {READMISSION, DEATH, CENSORED}；
- 证据（事件 CSV）每人可有多条 (subject_id, day, event_type)，
  event_type ∈ {READMISSION, DEATH}，只记录事件、不含随访截止。

核对规则（按每患者**最早**证据事件与队列终局比对）：
- 首发日两类事件并存（最早日同时出现再入院与死亡）→ ``AMBIGUOUS_FIRST_DAY``，
  该患者只报歧义，待人工核实后再核对；
- 证据最早事件不晚于队列结局日，但队列未记录该事件
  （队列记为随访截止，或队列事件日更晚，或同日类型不一致）→ ``EVENT_NOT_RECORDED``；
- 队列记录了事件（再入院/死亡），但证据中找不到同日同类事件 → ``MISSING_EVIDENCE``；
- 证据中出现队列外的患者号 → ``UNKNOWN_SUBJECT``。

证据事件晚于队列随访截止日的，不属于本队列观察责任范围，不报问题。
纯函数：不读写数据库，便于单测与在事务内调用。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

ISSUE_AMBIGUOUS_FIRST_DAY = "AMBIGUOUS_FIRST_DAY"
ISSUE_EVENT_NOT_RECORDED = "EVENT_NOT_RECORDED"
ISSUE_MISSING_EVIDENCE = "MISSING_EVIDENCE"
ISSUE_UNKNOWN_SUBJECT = "UNKNOWN_SUBJECT"

ISSUE_TYPES = (
    ISSUE_AMBIGUOUS_FIRST_DAY,
    ISSUE_EVENT_NOT_RECORDED,
    ISSUE_MISSING_EVIDENCE,
    ISSUE_UNKNOWN_SUBJECT,
)

_OUTCOME_ZH = {"READMISSION": "再入院", "DEATH": "死亡", "CENSORED": "随访截止"}


@dataclass(frozen=True)
class Issue:
    subject_id: str
    issue_type: str
    message: str
    cohort_day: Optional[int] = None
    cohort_outcome: Optional[str] = None
    evidence_day: Optional[int] = None
    evidence_event: Optional[str] = None


def _zh(code: str) -> str:
    return _OUTCOME_ZH.get(code, code)


def reconcile(
    cohort_rows: list[Any],
    evidence_rows: list[Any],
) -> list[Issue]:
    """对账主函数。

    :param cohort_rows: 现行队列记录，元素可按下标取 (subject_id, day, outcome)
    :param evidence_rows: 证据事件记录，元素可按下标取 (subject_id, day, event_type)
    :return: 逐人问题列表，按 (subject_id, issue_type) 排序，空列表表示核对通过
    """
    cohort: dict[str, tuple[int, str]] = {}
    for rec in cohort_rows:
        cohort[str(rec[0])] = (int(rec[1]), str(rec[2]))

    events_by_subject: dict[str, list[tuple[int, str]]] = {}
    for rec in evidence_rows:
        sid = str(rec[0])
        events_by_subject.setdefault(sid, []).append((int(rec[1]), str(rec[2])))

    issues: list[Issue] = []

    # 未知患者：证据中出现但不在现行队列
    for sid in sorted(set(events_by_subject) - set(cohort)):
        first_day = min(d for d, _ in events_by_subject[sid])
        first_types = sorted({t for d, t in events_by_subject[sid] if d == first_day})
        issues.append(
            Issue(
                subject_id=sid,
                issue_type=ISSUE_UNKNOWN_SUBJECT,
                message=f"证据患者 {sid!r} 不在现行队列中，须核实是否录错患者号或队列漏收。",
                evidence_day=first_day,
                evidence_event=first_types[0],
            )
        )

    for sid in sorted(cohort):
        c_day, c_outcome = cohort[sid]
        events = events_by_subject.get(sid, [])
        event_set = set(events)

        if events:
            first_day = min(d for d, _ in events)
            first_types = {t for d, t in events if d == first_day}
            if len(first_types) > 1:
                # 首发日两类事件并存：只报歧义，待人工核实后再核对
                issues.append(
                    Issue(
                        subject_id=sid,
                        issue_type=ISSUE_AMBIGUOUS_FIRST_DAY,
                        message=(
                            f"证据首发日（第 {first_day} 天）同时存在再入院与死亡两类事件，"
                            "无法确定首发结局，须人工核实。"
                        ),
                        cohort_day=c_day,
                        cohort_outcome=c_outcome,
                        evidence_day=first_day,
                        evidence_event=None,
                    )
                )
                continue
            first_type = next(iter(first_types))
            if c_outcome == "CENSORED":
                if first_day <= c_day:
                    # 事件不晚于队列随访截止日却未记录（漏记死亡/把已再入院者记作截止）
                    issues.append(
                        Issue(
                            subject_id=sid,
                            issue_type=ISSUE_EVENT_NOT_RECORDED,
                            message=(
                                f"证据显示第 {first_day} 天发生{_zh(first_type)}，"
                                f"不晚于队列随访截止日（第 {c_day} 天），"
                                "但队列记为随访截止，事件漏记。"
                            ),
                            cohort_day=c_day,
                            cohort_outcome=c_outcome,
                            evidence_day=first_day,
                            evidence_event=first_type,
                        )
                    )
                # 事件晚于随访截止日：不属本队列观察责任范围，不报
            else:
                # 队列已记事件 (c_day, c_outcome)
                if first_day < c_day or (first_day == c_day and first_type != c_outcome):
                    issues.append(
                        Issue(
                            subject_id=sid,
                            issue_type=ISSUE_EVENT_NOT_RECORDED,
                            message=(
                                f"证据最早事件为第 {first_day} 天{_zh(first_type)}，"
                                f"与队列记录（第 {c_day} 天{_zh(c_outcome)}）不符，"
                                "队列未记录该首发事件。"
                            ),
                            cohort_day=c_day,
                            cohort_outcome=c_outcome,
                            evidence_day=first_day,
                            evidence_event=first_type,
                        )
                    )

        # 反向核对：队列记录的事件必须能在证据中找到同日同类记录
        if c_outcome in ("READMISSION", "DEATH") and (c_day, c_outcome) not in event_set:
            issues.append(
                Issue(
                    subject_id=sid,
                    issue_type=ISSUE_MISSING_EVIDENCE,
                    message=(
                        f"队列记录第 {c_day} 天{_zh(c_outcome)}，"
                        "但事件证据中找不到同日同类记录，缺证据。"
                    ),
                    cohort_day=c_day,
                    cohort_outcome=c_outcome,
                )
            )

    issues.sort(key=lambda i: (i.subject_id, i.issue_type))
    return issues
