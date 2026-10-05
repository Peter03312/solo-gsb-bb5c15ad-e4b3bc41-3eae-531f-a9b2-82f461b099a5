"""本轮功能测试: 评审资格停用/启用与变更记录。

覆盖需求:
- 既有评审人初始视为启用; 会务方凭密钥按 评审人编号 + 资料修订号 + 目标状态 + 非空原因调整;
- 同一事务内先核对修订号: 过期/超前修订号 409 (优先级最高), 未知评审人 404, 空原因 422,
  路径与请求体编号不一致 422;
- 匹配修订号下重复提交同一状态和原因幂等 (changed=false, 不推进修订号、不写记录);
  同状态异原因 409; 有效变更推进修订号并写变更记录, 可查看全部变更记录;
- 停用后凭据仍有效但立即 403: 不能取稿、提交决定或评语 (含查看本人评语);
- 停用不删除既有发布分配/确认/评语, 已发布的作者反馈快照仍按原规则有效;
- 停用者不进入后续普通分配与补位: 普通预演标注 reviewer_disabled;
  补位释放其已确认槽位 (fixed_reviewer_disabled), 其他合格已确认槽位保持固定;
- 启用只恢复当前发布版中仍分配给本人的任务访问, 不找回已被补位替换的槽位。
"""
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="review-eligibility-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["ORGANIZER_KEY"] = "test-organizer-key"

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

ORG = {"X-Organizer-Key": "test-organizer-key"}


def reviewer_headers(rid):
    return {"X-Reviewer-Id": rid, "X-Reviewer-Credential": f"cred-{rid}"}


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_for_tests()
    with db.write_txn() as conn:
        for table in (
            "papers", "reviewers", "published",
            "assignment_decisions", "reviewer_recusals", "submitted_reviews",
            "feedback_snapshots", "reviewer_status_changes", "review_corrections",
            "assignment_locks", "paper_guarantee_levels", "review_deadlines",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'revision'")
    yield


@pytest.fixture
def client():
    return TestClient(app)


def add_reviewer(client, rid, institution=None, capacity=3, topics=None, avoid=None):
    return client.post(
        "/reviewers",
        headers=ORG,
        json={
            "reviewer_id": rid,
            "credential": f"cred-{rid}",
            "topics": topics if topics is not None else ["AI"],
            "institution": institution or f"Inst-{rid}",
            "capacity": capacity,
            "avoid_papers": avoid or [],
        },
    )


def add_paper(client, pid="P1", topics=None, institutions=None):
    return client.post(
        "/papers",
        headers=ORG,
        json={
            "paper_id": pid,
            "manuscript": f"manuscript-of-{pid}",
            "topics": topics if topics else ["AI"],
            "institutions": institutions if institutions is not None else [f"Author-{pid}"],
        },
    )


def setup_and_publish(client, reviewers=("R1", "R2", "R3", "R4"), papers=("P1",)):
    for rid in reviewers:
        assert add_reviewer(client, rid).status_code == 201
    for pid in papers:
        assert add_paper(client, pid).status_code == 201
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 200, r.text
    return r.json()["plan"]


def current_revision(client):
    return client.get("/meta", headers=ORG).json()["revision"]


def set_status(client, rid, active, reason="因会务安排暂停本轮评审", revision=None):
    if revision is None:
        revision = current_revision(client)
    return client.post(
        f"/reviewers/{rid}/status",
        headers=ORG,
        json={
            "reviewer_id": rid,
            "base_revision": revision,
            "active": active,
            "reason": reason,
        },
    )


def decide(client, rid, pid, decision, reason=None):
    body = {"paper_id": pid, "decision": decision}
    if reason is not None:
        body["reason"] = reason
    return client.post(
        f"/reviewer/assignments/{pid}/decision",
        headers=reviewer_headers(rid),
        json=body,
    )


# ------------------------------------------------------------ 初始状态与查看

def test_existing_reviewer_initially_enabled(client):
    add_reviewer(client, "R1")
    listed = client.get("/reviewers", headers=ORG).json()["reviewers"]
    assert listed[0]["active"] is True
    st = client.get("/reviewers/R1/status", headers=ORG).json()
    assert st == {"reviewer_id": "R1", "active": True, "history": []}


def test_status_history_requires_organizer_key_and_existing_reviewer(client):
    add_reviewer(client, "R1")
    assert client.get("/reviewers/R1/status").status_code == 401
    assert client.get("/reviewers/NOPE/status", headers=ORG).status_code == 404


def test_change_endpoint_requires_organizer_key(client):
    add_reviewer(client, "R1")
    r = client.post(
        "/reviewers/R1/status",
        json={"reviewer_id": "R1", "base_revision": 1, "active": False, "reason": "x"},
    )
    assert r.status_code == 401


def test_path_body_mismatch_rejected(client):
    add_reviewer(client, "R1")
    r = client.post(
        "/reviewers/R1/status",
        headers=ORG,
        json={"reviewer_id": "R2", "base_revision": current_revision(client),
              "active": False, "reason": "x"},
    )
    assert r.status_code == 422


# ------------------------------------------------------------ 停用: 校验与修订号

def test_disable_effective_change_bumps_revision_and_records(client):
    add_reviewer(client, "R1")
    rev = current_revision(client)
    r = set_status(client, "R1", False, reason="长期休假, 暂停评审")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["changed"] is True and j["active"] is False
    assert j["revision"] == rev + 1 and j["reason"] == "长期休假, 暂停评审"
    st = client.get("/reviewers/R1/status", headers=ORG).json()
    assert st["active"] is False
    assert len(st["history"]) == 1
    entry = st["history"][0]
    assert entry == {
        "active": False,
        "reason": "长期休假, 暂停评审",
        "revision": j["revision"],
        "changed_at": entry["changed_at"],
    }
    assert entry["changed_at"]
    # 列表同步反映
    assert client.get("/reviewers", headers=ORG).json()["reviewers"][0]["active"] is False


def test_stale_revision_rejected_before_other_checks(client):
    add_reviewer(client, "R1")
    stale = current_revision(client)
    # 制造一次资料变更使修订号前进
    add_paper(client, "P1")
    # 过期修订号 + 未知评审人 -> 仍以 409 为准 (事务内先核对修订号)
    r = client.post(
        "/reviewers/GHOST/status", headers=ORG,
        json={"reviewer_id": "GHOST", "base_revision": stale, "active": False, "reason": "x"},
    )
    assert r.status_code == 409
    # 过期修订号 + 空白原因 -> 仍以 409 为准
    r = client.post(
        "/reviewers/R1/status", headers=ORG,
        json={"reviewer_id": "R1", "base_revision": stale, "active": False, "reason": "   "},
    )
    assert r.status_code == 409
    # 状态未改、修订号未额外前进
    assert client.get("/reviewers/R1/status", headers=ORG).json()["active"] is True


def test_unknown_reviewer_404(client):
    add_reviewer(client, "R1")
    r = set_status(client, "GHOST", False)
    assert r.status_code == 404


def test_blank_reason_422_without_state_change(client):
    add_reviewer(client, "R1")
    rev = current_revision(client)
    r = set_status(client, "R1", False, reason="   ")
    assert r.status_code == 422
    # 缺少 reason 字段 -> 422
    r = client.post(
        "/reviewers/R1/status", headers=ORG,
        json={"reviewer_id": "R1", "base_revision": rev, "active": False},
    )
    assert r.status_code == 422
    assert client.get("/reviewers/R1/status", headers=ORG).json()["active"] is True
    assert current_revision(client) == rev


def test_duplicate_same_status_and_reason_is_idempotent(client):
    add_reviewer(client, "R1")
    rev1 = set_status(client, "R1", False, reason="原因甲").json()["revision"]
    # 匹配当前修订号重复提交同一状态和原因 -> changed=false, 不推进, 不写记录
    r = set_status(client, "R1", False, reason="原因甲", revision=rev1)
    assert r.status_code == 200
    assert r.json()["changed"] is False
    assert r.json()["revision"] == rev1
    history = client.get("/reviewers/R1/status", headers=ORG).json()["history"]
    assert len(history) == 1


def test_same_status_different_reason_conflict(client):
    add_reviewer(client, "R1")
    rev1 = set_status(client, "R1", False, reason="原因甲").json()["revision"]
    r = set_status(client, "R1", False, reason="原因乙", revision=rev1)
    assert r.status_code == 409
    # 状态与修订号不变, 原因保留首次
    assert current_revision(client) == rev1
    st = client.get("/reviewers/R1/status", headers=ORG).json()
    assert st["active"] is False and st["history"][0]["reason"] == "原因甲"


def test_reenable_already_enabled_initial_reviewer_conflict(client):
    add_reviewer(client, "R1")
    # 既有评审人初始启用, 再次启用 (无论任何原因) 均非有效变更 -> 409
    assert set_status(client, "R1", True, reason="想再启用一次").status_code == 409


# ------------------------------------------------------------ 停用即时断稿

def test_disabled_credentials_still_valid_but_reviewer_apis_forbidden(client):
    plan = setup_and_publish(client)
    rid = plan["P1"][0]
    set_status(client, rid, False, reason="暂停")
    h = reviewer_headers(rid)
    # 凭据错误仍是 401; 凭据正确但停用 -> 403
    assert client.get("/reviewer/assignments",
                      headers={"X-Reviewer-Id": rid, "X-Reviewer-Credential": "wrong"}
                      ).status_code == 401
    assert client.get("/reviewer/assignments", headers=h).status_code == 403
    assert decide(client, rid, "P1", "confirm").status_code == 403
    serial = client.get("/assignment", headers=ORG).json()["serial"]
    assert client.post(
        "/reviewer/assignments/P1/review", headers=h,
        json={"paper_id": "P1", "serial": serial, "score": 4, "comment": "评语"},
    ).status_code == 403
    assert client.get("/reviewer/reviews", headers=h).status_code == 403


# ------------------------------------------------------------ 不删除既有数据

def test_disable_keeps_published_assignments_decisions_and_reviews(client):
    plan = setup_and_publish(client)
    rid_a, rid_b = plan["P1"]
    decide(client, rid_a, "P1", "confirm")
    decide(client, rid_b, "P1", "confirm")
    serial = client.get("/assignment", headers=ORG).json()["serial"]
    client.post(
        f"/reviewer/assignments/P1/review", headers=reviewer_headers(rid_a),
        json={"paper_id": "P1", "serial": serial, "score": 5, "comment": "很好"},
    )
    client.post(
        f"/reviewer/assignments/P1/review", headers=reviewer_headers(rid_b),
        json={"paper_id": "P1", "serial": serial, "score": 4, "comment": "可接受"},
    )
    # 发布一份作者快照, 随后停用其中一名评审人
    snap = client.post(
        "/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial}
    )
    assert snap.status_code == 200, snap.text
    code = snap.json()["access_code"]
    set_status(client, rid_a, False, reason="暂停")

    # 当前发布版槽位与确认状态保留
    pub = client.get("/assignment", headers=ORG).json()
    states = {s["reviewer_id"]: s["state"] for s in pub["papers"]["P1"]["slots"]}
    assert set(pub["plan"]["P1"]) == {rid_a, rid_b}
    assert states[rid_a] == "confirmed"
    # 评语仍在, 会务方可查
    pr = client.get("/papers/P1/reviews", headers=ORG).json()
    submitted = {s["reviewer_id"]: s["submitted"] for s in pr["slots"]}
    assert submitted[rid_a] is True
    # 已发布的作者反馈快照仍按原有规则有效
    assert client.get(f"/feedback-snapshots/{code}").status_code == 200


# ------------------------------------------------------------ 不进入后续普通分配

def test_disabled_reviewer_excluded_from_normal_assignment(client):
    setup_and_publish(client, papers=("P1",))
    rid = "R1"
    set_status(client, rid, False, reason="暂停")
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert rid not in body["plan"]["P1"]
    reasons = {e["reviewer_id"]: e["reason"] for e in body["papers"]["P1"]["excluded"]}
    assert reasons[rid] == "reviewer_disabled"
    # 停用使修订号前进: 基于旧修订号的普通发布必须拒绝
    stale = body["revision"] - 1
    assert client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": stale}
    ).status_code == 409


# ------------------------------------------------------------ 补位释放已确认槽位

def test_backfill_releases_disabled_confirmed_slot_but_keeps_other_fixed(client):
    plan = setup_and_publish(client)
    rid_a, rid_b = plan["P1"]
    decide(client, rid_a, "P1", "confirm")
    decide(client, rid_b, "P1", "confirm")
    set_status(client, rid_b, False, reason="暂停")
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"] is True
    # 停用者的已确认槽位释放, 另一合格已确认槽位保持固定
    assert dry["fixed"]["P1"] == [rid_a]
    problems = {(p["reviewer_id"], p["reason"]) for p in dry["fixed_problems"]["P1"]}
    assert (rid_b, "fixed_reviewer_disabled") in problems
    assert rid_b not in dry["plan"]["P1"] and rid_a in dry["plan"]["P1"]

    r = client.post(
        "/assignment/backfill/publish", headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert rid_b not in j["plan"]["P1"]
    # 仅保留的另一合格槽位被沿用
    assert j["carried_confirmations"] == 1
    # 停用者被补位替换后取稿为空 (即便重新启用也不找回该槽位)
    assert client.get("/reviewer/assignments", headers=reviewer_headers(rid_b)).status_code == 403
    rev = current_revision(client)
    assert set_status(client, rid_b, True, reason="恢复", revision=rev).status_code == 200
    view = client.get("/reviewer/assignments", headers=reviewer_headers(rid_b)).json()
    assert view["assignments"] == []


def test_disabled_pending_reviewer_not_in_backfill_plan(client):
    plan = setup_and_publish(client)
    rid_a, rid_b = plan["P1"]
    # rid_a 确认; rid_b 保持 pending, 随后被停用 -> 既非固定也不合格
    decide(client, rid_a, "P1", "confirm")
    set_status(client, rid_b, False, reason="暂停")
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"] is True
    assert rid_b not in dry["plan"]["P1"]
    assert dry["fixed"]["P1"] == [rid_a]
    reasons = {e["reviewer_id"]: e["reason"] for e in dry["papers"]["P1"]["excluded"]}
    assert reasons[rid_b] == "reviewer_disabled"


# ------------------------------------------------------------ 重新启用恢复当前任务

def test_reenable_restores_access_only_while_still_in_current_plan(client):
    plan = setup_and_publish(client)
    rid = plan["P1"][0]
    rev = set_status(client, rid, False, reason="暂停").json()["revision"]
    # 未做补位: 当前发布版中该槽位仍属于本人
    assert client.get("/reviewer/assignments", headers=reviewer_headers(rid)).status_code == 403
    r = set_status(client, rid, True, reason="问题已澄清", revision=rev)
    assert r.status_code == 200 and r.json()["changed"] is True
    view = client.get("/reviewer/assignments", headers=reviewer_headers(rid)).json()
    assert [p["paper_id"] for p in view["assignments"]] == ["P1"]
    # 恢复后可正常提交决定
    assert decide(client, rid, "P1", "confirm").status_code == 200
    # 变更记录完整: 停用、启用各一条, 按时间顺序
    history = client.get(f"/reviewers/{rid}/status", headers=ORG).json()["history"]
    assert [h["active"] for h in history] == [False, True]
    assert [h["reason"] for h in history] == ["暂停", "问题已澄清"]


def test_enable_then_enable_same_reason_idempotent_different_reason_conflict(client):
    plan = setup_and_publish(client)
    rid = plan["P1"][0]
    rev1 = set_status(client, rid, False, reason="暂停").json()["revision"]
    rev2 = set_status(client, rid, True, reason="恢复", revision=rev1).json()["revision"]
    # 同状态同原因 -> 幂等
    r = set_status(client, rid, True, reason="恢复", revision=rev2)
    assert r.json()["changed"] is False and r.json()["revision"] == rev2
    # 同状态异原因 -> 409
    assert set_status(client, rid, True, reason="另一个原因", revision=rev2).status_code == 409
