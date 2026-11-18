"""事件证据对账测试：对账规则、供数门禁、资格撤销、幂等与交错。"""
from __future__ import annotations

import threading
import time

from app.reconcile import (
    ISSUE_AMBIGUOUS_FIRST_DAY,
    ISSUE_EVENT_NOT_RECORDED,
    ISSUE_MISSING_EVIDENCE,
    ISSUE_UNKNOWN_SUBJECT,
    reconcile,
)

from test_api import _csv, _upload, _wait_analysis


# ---------------------------------------------------------------------------
# 对账纯函数单测
# ---------------------------------------------------------------------------

def _types(issues):
    return {i.issue_type for i in issues}


def test_reconcile_exact_match_passes():
    cohort = [("S1", 1, "READMISSION"), ("S2", 3, "DEATH"), ("S3", 10, "CENSORED")]
    evidence = [("S1", 1, "READMISSION"), ("S2", 3, "DEATH")]
    assert reconcile(cohort, evidence) == []


def test_reconcile_first_day_ambiguity():
    """首发日两类事件并存 -> 只报歧义，不再报其他问题。"""
    cohort = [("S1", 5, "DEATH")]
    evidence = [("S1", 5, "READMISSION"), ("S1", 5, "DEATH")]
    issues = reconcile(cohort, evidence)
    assert [i.issue_type for i in issues] == [ISSUE_AMBIGUOUS_FIRST_DAY]
    assert issues[0].evidence_day == 5

    # 歧义只发生在最早日：更早日类型唯一则不构成 day 明确，后续并存不算歧义
    cohort2 = [("S1", 3, "READMISSION")]
    evidence2 = [("S1", 3, "READMISSION"), ("S1", 7, "READMISSION"), ("S1", 7, "DEATH")]
    assert reconcile(cohort2, evidence2) == []


def test_reconcile_event_not_recorded_before_censor():
    """事件不晚于队列随访截止日却未记录 -> 不符（漏记死亡/再入院被记作截止）。"""
    cohort = [("S1", 10, "CENSORED"), ("S2", 10, "CENSORED")]
    evidence = [("S1", 8, "DEATH"), ("S2", 10, "READMISSION")]
    issues = reconcile(cohort, evidence)
    assert _types(issues) == {ISSUE_EVENT_NOT_RECORDED}
    assert {i.subject_id for i in issues} == {"S1", "S2"}

    # 事件晚于随访截止日：不属观察责任范围，不报
    assert reconcile([("S1", 10, "CENSORED")], [("S1", 12, "DEATH")]) == []


def test_reconcile_missing_evidence():
    """队列已记事件却找不到相应证据 -> 缺证据。"""
    cohort = [("S1", 5, "READMISSION"), ("S2", 6, "DEATH")]
    issues = reconcile(cohort, [])
    assert _types(issues) == {ISSUE_MISSING_EVIDENCE}
    assert {i.subject_id for i in issues} == {"S1", "S2"}

    # 证据只有异日事件：队列记录日无同日同型证据 -> 缺证据；更早事件未记录 -> 不符
    issues2 = reconcile(cohort[:1], [("S1", 3, "READMISSION")])
    assert _types(issues2) == {ISSUE_EVENT_NOT_RECORDED, ISSUE_MISSING_EVIDENCE}

    # 同日异型：队列记的再入院无证据 + 证据首发死亡未记录 -> 两类都报
    issues3 = reconcile([("S1", 5, "READMISSION")], [("S1", 5, "DEATH")])
    assert _types(issues3) == {ISSUE_EVENT_NOT_RECORDED, ISSUE_MISSING_EVIDENCE}

    # 证据事件晚于队列结局日：仅缺证据
    issues4 = reconcile([("S1", 5, "READMISSION")], [("S1", 8, "READMISSION")])
    assert _types(issues4) == {ISSUE_MISSING_EVIDENCE}


def test_reconcile_unknown_subject():
    cohort = [("S1", 1, "READMISSION")]
    evidence = [("S1", 1, "READMISSION"), ("S999", 4, "DEATH")]
    issues = reconcile(cohort, evidence)
    assert [i.issue_type for i in issues] == [ISSUE_UNKNOWN_SUBJECT]
    assert issues[0].subject_id == "S999"


def test_reconcile_multiple_events_earliest_governs():
    """同一患者多条证据：按最早事件核对。"""
    cohort = [("S1", 2, "READMISSION")]
    evidence = [("S1", 2, "READMISSION"), ("S1", 9, "DEATH"), ("S1", 15, "READMISSION")]
    assert reconcile(cohort, evidence) == []


# ---------------------------------------------------------------------------
# API：上传、门禁、撤销、幂等、交错
# ---------------------------------------------------------------------------

def _evidence(rows, header="subject_id,day,event"):
    return _csv(rows, header=header)


def _post_evidence(client, rows, revision=None, header="subject_id,day,event"):
    data = {"revision": str(revision)} if revision is not None else {}
    return client.post(
        "/api/evidence/upload",
        data=data,
        files={"file": ("e.csv", _evidence(rows, header), "text/csv")},
    )


def test_evidence_upload_passes_and_opens_serving(app_client):
    client, db, services = app_client
    rows = [("S1", 1, "READMISSION"), ("S2", 2, "DEATH"), ("S3", 10, "CENSORED")]
    assert _upload(client, 1, rows).status_code == 200
    _wait_analysis(db, 1, "COMPLETED")

    # 无证据 -> 不可供数
    assert client.get("/api/analyses/current").status_code == 409
    assert client.get("/api/serving").json()["servable"] is False

    resp = _post_evidence(client, [("S1", 1, "READMISSION"), ("S2", 2, "DEATH")])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is True and body["idempotent"] is False
    assert body["reconciliation"]["status"] == "PASSED"
    assert body["reconciliation"]["n_issues"] == 0
    assert body["serving"]["servable"] is True

    assert client.get("/api/analyses/current").status_code == 200
    state = client.get("/api/evidence/current").json()
    assert state["active_batch"]["batch_id"] == body["batch_id"]
    assert state["reconciliation"]["status"] == "PASSED"


def test_first_day_ambiguity_reported_and_blocks_serving(app_client):
    """首发日歧义：逐人问题可见，且现行结果不可供数。"""
    client, db, services = app_client
    rows = [("S1", 3, "DEATH"), ("S2", 4, "READMISSION")]
    assert _upload(client, 1, rows).status_code == 200
    _wait_analysis(db, 1, "COMPLETED")

    resp = _post_evidence(
        client,
        [("S1", 3, "READMISSION"), ("S1", 3, "DEATH"), ("S2", 4, "READMISSION")],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reconciliation"]["status"] == "FAILED"
    issues = body["reconciliation"]["issues"]
    assert [i["issue_type"] for i in issues] == ["AMBIGUOUS_FIRST_DAY"]
    assert issues[0]["subject_id"] == "S1"
    assert body["serving"]["servable"] is False

    cur = client.get("/api/analyses/current")
    assert cur.status_code == 409
    assert cur.json()["error"] == "EVIDENCE_RECONCILIATION_FAILED"
    assert cur.json()["context"]["n_issues"] == 1

    # 消除歧义后重传 -> 通过并恢复供数
    fixed = _post_evidence(client, [("S1", 3, "DEATH"), ("S2", 4, "READMISSION")])
    assert fixed.json()["reconciliation"]["status"] == "PASSED"
    assert client.get("/api/analyses/current").status_code == 200


def test_all_issue_types_via_api(app_client):
    """不符 / 缺证据 / 未知患者 三类问题逐人返回。"""
    client, db, services = app_client
    rows = [
        ("S1", 10, "CENSORED"),   # 证据第 8 天死亡 -> EVENT_NOT_RECORDED
        ("S2", 5, "READMISSION"),  # 无证据 -> MISSING_EVIDENCE
        ("S3", 6, "DEATH"),       # 有证据 -> 通过
    ]
    assert _upload(client, 1, rows).status_code == 200
    _wait_analysis(db, 1, "COMPLETED")

    resp = _post_evidence(
        client,
        [("S1", 8, "DEATH"), ("S3", 6, "DEATH"), ("S999", 2, "READMISSION")],
    )
    assert resp.status_code == 200
    recon = resp.json()["reconciliation"]
    assert recon["status"] == "FAILED"
    by_subject = {i["subject_id"]: i["issue_type"] for i in recon["issues"]}
    assert by_subject == {
        "S1": "EVENT_NOT_RECORDED",
        "S2": "MISSING_EVIDENCE",
        "S999": "UNKNOWN_SUBJECT",
    }
    # 逐人问题也可经查询接口获取
    state = client.get("/api/evidence/current").json()
    assert state["reconciliation"]["n_issues"] == 3
    assert client.get("/api/analyses/current").status_code == 409


def test_evidence_replacement_revokes_old_batch(app_client):
    """替换证据：旧批次立即撤销，结论以新批次为准。"""
    client, db, services = app_client
    rows = [("S1", 5, "READMISSION")]
    assert _upload(client, 1, rows).status_code == 200
    _wait_analysis(db, 1, "COMPLETED")

    bad = _post_evidence(client, [("S1", 4, "DEATH")])  # 与队列不符
    assert bad.json()["reconciliation"]["status"] == "FAILED"
    bad_batch = bad.json()["batch_id"]
    assert client.get("/api/analyses/current").status_code == 409

    good = _post_evidence(client, [("S1", 5, "READMISSION")])
    assert good.json()["reconciliation"]["status"] == "PASSED"
    assert good.json()["batch_id"] != bad_batch

    state = client.get("/api/evidence/current").json()
    assert state["active_batch"]["batch_id"] == good.json()["batch_id"]
    by_id = {b["batch_id"]: b for b in state["batches"]}
    assert by_id[bad_batch]["status"] == "SUPERSEDED"
    assert by_id[bad_batch]["reconciliation_status"] == "FAILED"  # 历史留痕
    assert by_id[good.json()["batch_id"]]["status"] == "ACTIVE"
    assert client.get("/api/analyses/current").status_code == 200


def test_same_content_evidence_idempotent(app_client):
    """同版同内容证据重送：幂等，批次与结论不变。"""
    client, db, services = app_client
    rows = [("S1", 5, "READMISSION"), ("S2", 9, "CENSORED")]
    assert _upload(client, 1, rows).status_code == 200
    _wait_analysis(db, 1, "COMPLETED")

    r1 = _post_evidence(client, [("S1", 5, "READMISSION")])
    assert r1.json()["accepted"] is True
    r2 = _post_evidence(client, [("S1", 5, "READMISSION")])
    assert r2.status_code == 200
    assert r2.json()["accepted"] is False and r2.json()["idempotent"] is True
    assert r2.json()["batch_id"] == r1.json()["batch_id"]
    assert r2.json()["reconciliation"] == r1.json()["reconciliation"]

    state = client.get("/api/evidence/current").json()
    assert len(state["batches"]) == 1  # 未产生新批次
    assert state["active_batch"]["batch_id"] == r1.json()["batch_id"]
    assert state["active_batch"]["uploaded_at"] is not None


def test_new_cohort_revision_revokes_evidence_qualification(app_client):
    """受理新版队列：旧证据资格立即撤销，新修订必须重新对账，不得借用旧结论。"""
    client, db, services = app_client
    rows1 = [("S1", 1, "READMISSION"), ("S2", 3, "DEATH")]
    assert _upload(client, 1, rows1).status_code == 200
    _wait_analysis(db, 1, "COMPLETED")
    assert _post_evidence(
        client, [("S1", 1, "READMISSION"), ("S2", 3, "DEATH")]
    ).json()["reconciliation"]["status"] == "PASSED"
    assert client.get("/api/analyses/current").status_code == 200

    # 受理 rev2（内容与 rev1 完全不同）
    rows2 = [("S1", 2, "DEATH"), ("S3", 4, "READMISSION")]
    assert _upload(client, 2, rows2).status_code == 200

    # 旧批次立即被撤销（审计仍可见）
    old = client.get("/api/evidence/1").json()
    assert old["active_batch"] is None
    assert old["batches"][0]["status"] == "SUPERSEDED"
    assert old["batches"][0]["reconciliation_status"] == "PASSED"

    # 新修订无任何证据：不借用旧版证据，供数关闭
    _wait_analysis(db, 2, "COMPLETED")
    new_state = client.get("/api/evidence/current").json()
    assert new_state["revision"] == 2 and new_state["active_batch"] is None
    cur = client.get("/api/analyses/current")
    assert cur.status_code == 409 and cur.json()["error"] == "EVIDENCE_MISSING"

    # 历史版本审计查询不受门禁阻断，但标注非现行
    hist = client.get("/api/analyses/1")
    assert hist.status_code == 200 and hist.json()["is_current"] is False

    # 针对 rev2 重新上传证据并通过后才恢复供数
    ok = _post_evidence(client, [("S1", 2, "DEATH"), ("S3", 4, "READMISSION")])
    assert ok.json()["revision"] == 2
    assert ok.json()["reconciliation"]["status"] == "PASSED"
    assert client.get("/api/analyses/current").status_code == 200


def test_evidence_targeting_wrong_revision_rejected(app_client):
    """证据显式指定修订号：与现行不一致即 409，防止挂错版本。"""
    client, db, services = app_client
    assert _upload(client, 1, [("S1", 1, "READMISSION")]).status_code == 200
    _wait_analysis(db, 1, "COMPLETED")

    resp = _post_evidence(client, [("S1", 1, "READMISSION")], revision=2)
    assert resp.status_code == 409
    assert resp.json()["error"] == "REVISION_CONFLICT"
    assert resp.json()["current_revision"] == 1

    # 交错场景：rev2 受理后，仍按 rev1 上传的证据被拒
    assert _upload(client, 2, [("S1", 2, "DEATH")]).status_code == 200
    stale = _post_evidence(client, [("S1", 1, "READMISSION")], revision=1)
    assert stale.status_code == 409
    assert stale.json()["current_revision"] == 2
    # 且 rev2 不会因此获得任何证据
    assert client.get("/api/evidence/current").json()["active_batch"] is None


def test_evidence_upload_interleaved_with_analysis(app_client, monkeypatch):
    """上传/对账/分析交错：分析 PENDING 期间可上传证据，供数仍关闭；
    分析完成后凭已通过的核对结论开放供数。"""
    client, db, services = app_client
    entered = threading.Event()
    release = threading.Event()
    real_load = db.load_records

    def gated_load(revision):
        entered.set()
        assert release.wait(timeout=5)
        return real_load(revision)

    monkeypatch.setattr(db, "load_records", gated_load)

    rows = [("S1", 1, "READMISSION"), ("S2", 2, "DEATH")]
    up = _upload(client, 1, rows)
    assert up.status_code == 200
    assert entered.wait(timeout=5)
    assert db.get_analysis_row(1)["status"] == "PENDING"

    # 分析仍在重算：上传证据并对账（对账不依赖分析结果）
    ev = _post_evidence(client, [("S1", 1, "READMISSION"), ("S2", 2, "DEATH")])
    assert ev.status_code == 200
    assert ev.json()["reconciliation"]["status"] == "PASSED"
    assert ev.json()["serving"]["servable"] is False  # 分析未完成，仍不可供数
    cur = client.get("/api/analyses/current")
    assert cur.status_code == 409 and cur.json()["error"] == "ANALYSIS_PENDING"

    # 放行重算：分析完成后供数自动开放（无需重传证据）
    release.set()
    _wait_analysis(db, 1, "COMPLETED")
    assert client.get("/api/serving").json()["servable"] is True
    assert client.get("/api/analyses/current").status_code == 200


def test_restart_keeps_reconciliation_state(app_client):
    """服务重启（恢复流程）后：对账结论来自数据库，未核实版本不会借用旧结论。"""
    client, db, services = app_client
    rows = [("S1", 1, "READMISSION"), ("S2", 2, "CENSORED")]
    assert _upload(client, 1, rows).status_code == 200
    _wait_analysis(db, 1, "COMPLETED")
    assert _post_evidence(client, [("S1", 1, "READMISSION")]).json()[
        "reconciliation"
    ]["status"] == "PASSED"

    # 模拟重启：启动恢复只重算 PENDING 分析；证据状态完全来自数据库
    services.recover_on_startup()
    serving = db.get_serving_status()
    assert serving["servable"] is True
    assert serving["evidence_status"] == "PASSED"
    assert client.get("/api/analyses/current").status_code == 200

    # 重启后受理新修订：旧证据资格仍被撤销，未核实的新版本不可供数
    assert _upload(client, 2, [("S1", 3, "DEATH")]).status_code == 200
    _wait_analysis(db, 2, "COMPLETED")
    services.recover_on_startup()
    serving2 = db.get_serving_status()
    assert serving2["servable"] is False
    assert serving2["evidence_status"] == "NONE"
    assert client.get("/api/analyses/current").status_code == 409


def test_evidence_csv_validation(app_client):
    client, db, services = app_client
    assert _upload(client, 1, [("S1", 1, "READMISSION")]).status_code == 200
    _wait_analysis(db, 1, "COMPLETED")

    # 缺事件列
    r = client.post(
        "/api/evidence/upload",
        files={"file": ("e.csv", b"subject_id,day\nS1,1\n", "text/csv")},
    )
    assert r.status_code == 400
    # 随访截止不是事件
    assert _post_evidence(client, [("S1", 1, "CENSORED")]).status_code == 400
    # day 越界 / 非整数
    assert _post_evidence(client, [("S1", 0, "DEATH")]).status_code == 400
    assert _post_evidence(client, [("S1", 31, "DEATH")]).status_code == 400
    assert _post_evidence(client, [("S1", "x", "DEATH")]).status_code == 400
    # 非法事件取值
    assert _post_evidence(client, [("S1", 1, "WALKED")]).status_code == 400
    # 空文件
    r = client.post(
        "/api/evidence/upload", files={"file": ("e.csv", b"", "text/csv")}
    )
    assert r.status_code == 400
    # 事件列名歧义（同时给两列）
    r = client.post(
        "/api/evidence/upload",
        files={"file": ("e.csv", b"subject_id,day,event,outcome\nS1,1,DEATH,DEATH\n", "text/csv")},
    )
    assert r.status_code == 400
    # 仅表头（空证据）：合法，用于全队列无事件场景
    r = client.post("/api/evidence/upload", files={"file": ("e.csv", _evidence([]), "text/csv")})
    assert r.status_code == 200
    assert r.json()["n_events"] == 0
    assert r.json()["reconciliation"]["status"] == "FAILED"  # S1 的再入院缺证据


def test_evidence_chinese_aliases(app_client):
    client, db, services = app_client
    rows = [("S1", 1, "再入院"), ("S2", 2, "死亡"), ("S3", 5, "随访截止")]
    resp = client.post(
        "/api/revisions/upload", data={"revision": "1"},
        files={"file": ("c.csv", _csv(rows), "text/csv")},
    )
    assert resp.status_code == 200
    _wait_analysis(db, 1, "COMPLETED")
    r = _post_evidence(client, [("S1", 1, "再入院"), ("S2", 2, "死亡")])
    assert r.status_code == 200
    assert r.json()["reconciliation"]["status"] == "PASSED"
    assert client.get("/api/analyses/current").status_code == 200


def test_evidence_event_column_name_variants(app_client):
    client, db, services = app_client
    assert _upload(client, 1, [("S1", 1, "READMISSION")]).status_code == 200
    _wait_analysis(db, 1, "COMPLETED")
    for header in ("subject_id,day,event_type", "subject_id,day,outcome"):
        r = _post_evidence(client, [("S1", 1, "READMISSION")], header=header)
        assert r.status_code == 200, (header, r.text)
        assert r.json()["reconciliation"]["status"] == "PASSED"
    # 表头大小写不敏感、列序不限（数据行按表头列序给出）
    body = "Day,Event,Subject_Id\n1,READMISSION,S1\n".encode()
    r = client.post("/api/evidence/upload", files={"file": ("e.csv", body, "text/csv")})
    assert r.status_code == 200, r.text
    assert r.json()["reconciliation"]["status"] == "PASSED"


def test_evidence_endpoints_404(app_client):
    client, db, services = app_client
    assert client.get("/api/evidence/99").status_code == 404
    assert client.get("/api/evidence/current").status_code == 404
    # 无队列时上传证据 -> 404
    r = _post_evidence(client, [("S1", 1, "DEATH")])
    assert r.status_code == 404
    assert r.json()["error"] == "NO_CURRENT_REVISION"
