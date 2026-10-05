"""本轮功能测试: 作者对匿名反馈快照评语的异议。

覆盖需求:
- 作者凭当前有效的快照访问码对标号 1/2 提交非空异议理由; 同一快照同一标号仅一条,
  相同理由重试返回原记录 (changed=false), 理由不同 409 冲突, 并发提交不生成两条;
- 提交返回随机查询凭据 (obj- 前缀), 持凭据查看处理状态, 即使快照访问码后来失效;
- 作者视图/响应不暴露评审人编号、机构或评语收据;
- 已失效访问码 (旧发布序号/更正请求失效/异议受理失效) 不得提交新异议, 统一 404;
- 未处理异议随发布序号变化在同一事务内标记 expired, 历史供会务方追溯;
- 会务方凭密钥查看异议及冻结的原反馈, 驳回 (不影响快照) 或受理:
  受理须填非空更正原因, 原子核对发布序号仍为当前序号、目标评语收据未变,
  按匿名标号定位评审人并走既有更正请求流程, 原访问码立即失效;
  发布版/目标评语已变化或已有待更正请求时拒绝受理 (409) 且不改异议状态。
"""
import os
import tempfile
import threading

_TMPDIR = tempfile.mkdtemp(prefix="review-objections-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["ORGANIZER_KEY"] = "test-organizer-key"

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

ORG = {"X-Organizer-Key": "test-organizer-key"}
BAD_ORG = {"X-Organizer-Key": "wrong-key"}


def reviewer_headers(rid):
    return {"X-Reviewer-Id": rid, "X-Reviewer-Credential": f"cred-{rid}"}


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_for_tests()
    with db.write_txn() as conn:
        for table in (
            "papers", "reviewers", "published",
            "assignment_decisions", "reviewer_recusals", "submitted_reviews",
            "feedback_snapshots", "review_corrections", "assignment_locks",
            "feedback_objections", "paper_guarantee_levels", "review_deadlines",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'revision'")
    yield


@pytest.fixture
def client():
    return TestClient(app)


def add_reviewer(client, rid, institution, capacity=3, topics=None, avoid=None):
    return client.post(
        "/reviewers",
        headers=ORG,
        json={
            "reviewer_id": rid,
            "credential": f"cred-{rid}",
            "topics": topics if topics is not None else ["AI"],
            "institution": institution,
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


def publish(client):
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 200, r.text
    return r.json()


def decide(client, rid, pid, decision="confirm", reason=None):
    body = {"paper_id": pid, "decision": decision}
    if reason is not None:
        body["reason"] = reason
    return client.post(
        f"/reviewer/assignments/{pid}/decision",
        headers=reviewer_headers(rid),
        json=body,
    )


def submit_review(client, rid, pid, serial, score, comment):
    return client.post(
        f"/reviewer/assignments/{pid}/review",
        headers=reviewer_headers(rid),
        json={"paper_id": pid, "serial": serial, "score": score, "comment": comment},
    )


def backfill_publish(client):
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"], dry
    r = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert r.status_code == 200, r.text
    return r.json()


def object_objection(client, code, label, reason):
    return client.post(
        "/feedback-objections",
        json={"access_code": code, "label": label, "reason": reason},
    )


def setup_snapshot(client, pid="P1", score_a=4, comment_a="甲的评语",
                   score_b=2, comment_b="乙的评语"):
    """建 4 评审人/论文 -> 发布 -> 两人确认交评语 -> 发布快照。"""
    for rid in ("R1", "R2", "R3", "R4"):
        add_reviewer(client, rid, f"Inst-{rid}")
    add_paper(client, pid)
    pub = publish(client)
    serial = pub["serial"]
    rid_a, rid_b = pub["plan"][pid]
    decide(client, rid_a, pid, "confirm")
    decide(client, rid_b, pid, "confirm")
    ra = submit_review(client, rid_a, pid, serial, score_a, comment_a).json()
    rb = submit_review(client, rid_b, pid, serial, score_b, comment_b).json()
    r = client.post(
        f"/papers/{pid}/feedback-snapshot", headers=ORG, json={"serial": serial}
    )
    assert r.status_code == 200, r.text
    return pub, r.json(), (rid_a, rid_b), (ra, rb)


# ------------------------------------------------------------ 提交异议

def test_submit_objection_success(client):
    _, snap, (rid_a, rid_b), _ = setup_snapshot(client)
    r = object_objection(client, snap["access_code"], 1, "评语一存在事实错误, 请核查")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["changed"] is True
    assert j["query_token"].startswith("obj-")
    obj = j["objection"]
    assert obj["paper_id"] == "P1" and obj["label"] == 1
    assert obj["state"] == "pending"
    assert obj["reason"] == "评语一存在事实错误, 请核查"
    assert obj["submitted_at"] and obj["resolved_at"] is None and obj["resolution"] is None
    # 作者响应不暴露评审人编号、机构、评语收据、快照码/序号/版本
    raw = r.text
    for secret in (rid_a, rid_b, "Inst-", "rvw-", "fbk-", '"serial"', '"version"',
                   "receipt", "institution", "Author-P1"):
        assert secret not in raw, f"异议响应泄露内部字段: {secret}"


def test_submit_objection_label_2_independent(client):
    _, snap, _, _ = setup_snapshot(client)
    r1 = object_objection(client, snap["access_code"], 1, "对评语一的异议")
    r2 = object_objection(client, snap["access_code"], 2, "对评语二的异议")
    assert r1.status_code == 200 and r2.status_code == 200
    t1, t2 = r1.json()["query_token"], r2.json()["query_token"]
    assert t1 != t2
    with db.read_txn() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM feedback_objections").fetchone()["c"]
    assert n == 2


def test_submit_objection_blank_reason_422(client):
    _, snap, _, _ = setup_snapshot(client)
    assert object_objection(client, snap["access_code"], 1, "   ").status_code == 422
    # label 越界 -> 422
    assert object_objection(client, snap["access_code"], 3, "x").status_code == 422


def test_submit_objection_invalid_code_uniform_404(client):
    _, snap, _, _ = setup_snapshot(client)
    for bad in ("fbk-deadbeef", "nope", "fbk-" + "00" * 16):
        r = object_objection(client, bad, 1, "理由")
        assert r.status_code == 404
        assert "P1" not in r.text and "不存在" not in r.text


def test_expired_access_code_cannot_submit_objection(client):
    pub, snap, _, _ = setup_snapshot(client)
    code = snap["access_code"]
    # 补位发布推进序号 -> 快照码失效
    out = backfill_publish(client)
    assert out["serial"] == pub["serial"] + 1
    r = object_objection(client, code, 1, "新异议")
    assert r.status_code == 404
    with db.read_txn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM feedback_objections"
        ).fetchone()["c"] == 0


def test_same_reason_retry_returns_original_record(client):
    _, snap, _, _ = setup_snapshot(client)
    first = object_objection(client, snap["access_code"], 1, "同一条理由").json()
    again = object_objection(client, snap["access_code"], 1, "  同一条理由 ")
    assert again.status_code == 200
    j = again.json()
    assert j["changed"] is False
    assert j["query_token"] == first["query_token"]
    assert j["objection"]["reason"] == "同一条理由"
    with db.read_txn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM feedback_objections"
        ).fetchone()["c"] == 1


def test_different_reason_conflict_409(client):
    _, snap, _, _ = setup_snapshot(client)
    object_objection(client, snap["access_code"], 1, "第一个理由")
    r = object_objection(client, snap["access_code"], 1, "另一个理由")
    assert r.status_code == 409
    # 已存异议不变
    with db.read_txn() as conn:
        row = conn.execute("SELECT reason, state FROM feedback_objections").fetchone()
        assert row["reason"] == "第一个理由" and row["state"] == "pending"


def test_concurrent_submissions_create_single_record(client):
    _, snap, _, _ = setup_snapshot(client)
    code = snap["access_code"]
    results = []
    barrier = threading.Barrier(8)

    def worker(i):
        barrier.wait()
        r = object_objection(client, code, 1, f"理由-{i % 2}")  # 两种理由并发
        results.append((r.status_code, r.json() if r.headers.get("content-type", "").startswith("application/json") else None))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with db.read_txn() as conn:
        rows = conn.execute("SELECT query_token, reason FROM feedback_objections").fetchall()
    assert len(rows) == 1  # 并发提交绝不生成两条
    success_tokens = {j["query_token"] for s, j in results if s == 200}
    assert success_tokens == {rows[0]["query_token"]}
    # 同一理由的并发重试全部幂等返回同一凭据; 另一理由全部冲突 409
    assert {s for s, _ in results} <= {200, 409}
    by_reason = {}
    for s, j in results:
        if s == 200:
            by_reason.setdefault(j["objection"]["reason"], s)
    assert len(by_reason) == 1  # 仅一种理由成功, 另一种全部冲突


# ------------------------------------------------------------ 查询凭据

def test_query_token_shows_status_even_after_code_invalidated(client):
    pub, snap, _, _ = setup_snapshot(client)
    code = snap["access_code"]
    submitted = object_objection(client, code, 1, "异议理由").json()
    token = submitted["query_token"]

    # 凭据即时可查 (无需任何请求头)
    r = client.get(f"/feedback-objections/{token}")
    assert r.status_code == 200
    assert r.json()["state"] == "pending"

    # 快照码随后失效 (补位发布), 凭据仍可查看, 状态变为 expired
    backfill_publish(client)
    assert client.get(f"/feedback-snapshots/{code}").status_code == 404
    r = client.get(f"/feedback-objections/{token}")
    assert r.status_code == 200
    j = r.json()
    assert j["state"] == "expired" and j["resolved_at"]
    # 作者视图仍不泄露内部字段
    for secret in ("Inst-", "rvw-", "fbk-", "receipt", '"serial"'):
        assert secret not in r.text


def test_query_token_invalid_uniform_404(client):
    setup_snapshot(client)
    for bad in ("obj-nope", "nope", "obj-" + "00" * 16):
        r = client.get(f"/feedback-objections/{bad}")
        assert r.status_code == 404
        assert "P1" not in r.text


# ------------------------------------------------------------ 发布序号变化 -> 过期

def test_pending_objections_expire_on_serial_change(client):
    pub, snap, _, _ = setup_snapshot(client)
    o1 = object_objection(client, snap["access_code"], 1, "待处理一").json()
    o2 = object_objection(client, snap["access_code"], 2, "待处理二").json()

    out = backfill_publish(client)
    assert out["serial"] == pub["serial"] + 1
    assert client.get(
        f"/feedback-objections/{o1['query_token']}"
    ).json()["state"] == "expired"
    assert client.get(
        f"/feedback-objections/{o2['query_token']}"
    ).json()["state"] == "expired"

    # 会务方追溯: 两条历史均在
    listing = client.get("/papers/P1/objections", headers=ORG).json()
    assert [(o["label"], o["state"]) for o in listing["objections"]] == [
        (1, "expired"), (2, "expired"),
    ]


def test_terminal_objections_not_remarked_on_serial_change(client):
    _, snap, _, _ = setup_snapshot(client)
    token = object_objection(client, snap["access_code"], 1, "x").json()["query_token"]
    oid = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0]["objection_id"]
    # 驳回为终态
    r = client.post(
        f"/objections/{oid}/decision", headers=ORG,
        json={"decision": "reject", "note": "驳回说明"},
    )
    assert r.status_code == 200
    backfill_publish(client)
    assert client.get(f"/feedback-objections/{token}").json()["state"] == "rejected"


def test_pending_objection_expires_on_normal_republish(client):
    pub, snap, _, _ = setup_snapshot(client)
    token = object_objection(client, snap["access_code"], 1, "待处理").json()["query_token"]
    # 普通 (非补位) 重新发布同样推进序号
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    repub = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert repub.status_code == 200
    assert repub.json()["serial"] == pub["serial"] + 1
    j = client.get(f"/feedback-objections/{token}").json()
    assert j["state"] == "expired" and j["resolved_at"]
    # 过期与受理/驳回历史都在会务方追溯中
    listing = client.get("/objections", headers=ORG).json()
    assert [o["state"] for o in listing["objections"]] == ["expired"]


# ------------------------------------------------------------ 会务方查看

def test_organizer_lists_objections_with_frozen_feedback(client):
    _, snap, (rid_a, _), (ra, rb) = setup_snapshot(client)
    object_objection(client, snap["access_code"], 1, "对甲评语的异议")

    assert client.get("/papers/P1/objections").status_code == 401
    assert client.get("/papers/P1/objections", headers=BAD_ORG).status_code == 401

    r = client.get("/papers/P1/objections", headers=ORG)
    assert r.status_code == 200
    j = r.json()
    assert j["current_serial"] == snap["serial"]
    assert len(j["objections"]) == 1
    o = j["objections"][0]
    # 会务方可见按匿名标号定位的评审人、收据、冻结的原反馈
    assert o["reviewer_id"] == rid_a
    assert o["target_receipt"] == ra["receipt"]
    assert o["frozen_feedback"] == {"label": 1, "score": 4, "comment": "甲的评语"}
    assert o["state"] == "pending"
    assert o["snapshot_version"] == snap["version"]


def test_organizer_state_filter_and_global_list(client):
    _, snap, _, _ = setup_snapshot(client)
    object_objection(client, snap["access_code"], 1, "一")
    oid2 = object_objection(client, snap["access_code"], 2, "二").json()
    oid = client.get(
        "/papers/P1/objections", headers=ORG
    ).json()["objections"][1]["objection_id"]
    client.post(f"/objections/{oid}/decision", headers=ORG, json={"decision": "reject"})

    pending = client.get("/papers/P1/objections?state=pending", headers=ORG).json()
    assert [o["label"] for o in pending["objections"]] == [1]
    rejected = client.get("/objections?state=rejected", headers=ORG).json()
    assert len(rejected["objections"]) == 1 and rejected["objections"][0]["label"] == 2
    allj = client.get("/objections", headers=ORG).json()
    assert len(allj["objections"]) == 2


# ------------------------------------------------------------ 驳回

def test_reject_objection_does_not_affect_snapshot(client):
    _, snap, _, _ = setup_snapshot(client)
    code = snap["access_code"]
    object_objection(client, code, 1, "异议")
    oid = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0]["objection_id"]

    r = client.post(
        f"/objections/{oid}/decision", headers=ORG,
        json={"decision": "reject", "note": "经核对评语成立"},
    )
    assert r.status_code == 200
    assert r.json()["objection"]["state"] == "rejected"
    # 驳回不影响快照: 访问码仍可读
    assert client.get(f"/feedback-snapshots/{code}").status_code == 200
    # 作者凭据可见驳回状态与说明
    token = r.json()["objection"]["query_token"]
    view = client.get(f"/feedback-objections/{token}").json()
    assert view["state"] == "rejected"
    assert view["resolution"] == {"decided_at": view["resolved_at"], "note": "经核对评语成立"}
    # 已处理不可再处理
    again = client.post(
        f"/objections/{oid}/decision", headers=ORG, json={"decision": "reject"}
    )
    assert again.status_code == 409


def test_decide_unknown_objection_404(client):
    setup_snapshot(client)
    assert client.post(
        "/objections/9999/decision", headers=ORG, json={"decision": "reject"}
    ).status_code == 404


def test_decision_requires_organizer_key(client):
    _, snap, _, _ = setup_snapshot(client)
    object_objection(client, snap["access_code"], 1, "x")
    oid = 1
    assert client.post(
        f"/objections/{oid}/decision", json={"decision": "reject"}
    ).status_code == 401


# ------------------------------------------------------------ 受理

def test_accept_objection_creates_correction_and_invalidates_code(client):
    _, snap, (rid_a, _), (ra, _) = setup_snapshot(client)
    code = snap["access_code"]
    token = object_objection(client, code, 1, "评分与事实不符").json()["query_token"]
    oid = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0]["objection_id"]

    r = client.post(
        f"/objections/{oid}/decision", headers=ORG,
        json={"decision": "accept", "reason": "作者异议成立, 请核实评分依据"},
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["changed"] is True
    assert j["invalidated_snapshots"] == 1
    o = j["objection"]
    assert o["state"] == "accepted"
    assert o["accepted_reason"] == "作者异议成立, 请核实评分依据"
    assert o["correction"] is not None
    assert o["correction"]["state"] == "pending"
    assert o["correction"]["reviewer_id"] == rid_a
    assert o["correction"]["original"]["receipt"] == ra["receipt"]

    # 原访问码立即失效
    assert client.get(f"/feedback-snapshots/{code}").status_code == 404
    # 作者凭据状态为 accepted, 不暴露评审人/收据
    view = client.get(f"/feedback-objections/{token}")
    assert view.status_code == 200
    assert view.json()["state"] == "accepted"
    assert view.json()["resolution"]["reason"] == "作者异议成立, 请核实评分依据"
    for secret in (rid_a, "Inst-", "rvw-", "receipt"):
        assert secret not in view.text

    # 既有更正流程已启动: 评审人待更正列表含该任务, 快照在更正完成前不可发布
    tasks = client.get("/reviewer/review-corrections", headers=reviewer_headers(rid_a)).json()
    assert len(tasks["corrections"]) == 1
    assert tasks["corrections"][0]["original"]["receipt"] == ra["receipt"]
    assert client.post(
        "/papers/P1/feedback-snapshot", headers=ORG, json={"serial": snap["serial"]}
    ).status_code == 409


def test_accept_objection_locates_reviewer_by_label(client):
    _, snap, (rid_a, rid_b), (ra, rb) = setup_snapshot(client)
    object_objection(client, snap["access_code"], 2, "针对乙评语")
    oid = client.get(
        "/papers/P1/objections", headers=ORG
    ).json()["objections"][0]["objection_id"]
    r = client.post(
        f"/objections/{oid}/decision", headers=ORG,
        json={"decision": "accept", "reason": "标号2异议成立"},
    )
    assert r.status_code == 200
    assert r.json()["objection"]["reviewer_id"] == rid_b
    assert r.json()["objection"]["correction"]["reviewer_id"] == rid_b
    assert r.json()["objection"]["target_receipt"] == rb["receipt"]


def test_accept_objection_requires_nonempty_reason_422(client):
    _, snap, _, _ = setup_snapshot(client)
    object_objection(client, snap["access_code"], 1, "x")
    oid = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0]["objection_id"]
    assert client.post(
        f"/objections/{oid}/decision", headers=ORG,
        json={"decision": "accept", "reason": "   "},
    ).status_code == 422
    # 被拒不改异议状态
    assert client.get(
        "/papers/P1/objections", headers=ORG
    ).json()["objections"][0]["state"] == "pending"


def test_accept_rejected_when_serial_changed_409_and_state_unchanged(client):
    pub, snap, _, _ = setup_snapshot(client)
    token = object_objection(client, snap["access_code"], 1, "x").json()["query_token"]
    oid = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0]["objection_id"]

    # 补位发布推进序号 (方案不变)
    out = backfill_publish(client)
    assert out["serial"] == pub["serial"] + 1
    r = client.post(
        f"/objections/{oid}/decision", headers=ORG,
        json={"decision": "accept", "reason": "试图受理"},
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["snapshot_serial"] == pub["serial"]
    assert detail["current_serial"] == out["serial"]
    # 异议状态保持 expired, 未生成更正请求
    assert client.get(f"/feedback-objections/{token}").json()["state"] == "expired"
    with db.read_txn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM review_corrections"
        ).fetchone()["c"] == 0


def test_accept_rejected_when_target_review_changed_409(client):
    _, snap, (rid_a, _), (ra, _) = setup_snapshot(client)
    object_objection(client, snap["access_code"], 1, "x")
    oid = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0]["objection_id"]

    # 不经异议流程, 会务方直接对该槽位完成一次更正 (同序号, 收据变化)
    cr = client.post(
        "/papers/P1/review-corrections", headers=ORG,
        json={"paper_id": "P1", "serial": snap["serial"],
              "reviewer_id": rid_a, "reason": "会务方独立发现的问题"},
    )
    assert cr.status_code == 200
    client.post(
        "/reviewer/review-corrections/P1", headers=reviewer_headers(rid_a),
        json={"paper_id": "P1", "serial": snap["serial"],
              "original_receipt": ra["receipt"], "score": 5, "comment": "更正后评语"},
    )
    # 目标评语收据已变 -> 拒绝受理且不改异议状态 (仍 pending)
    r = client.post(
        f"/objections/{oid}/decision", headers=ORG,
        json={"decision": "accept", "reason": "异议受理"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["current_receipt"] != ra["receipt"]
    view = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0]
    assert view["state"] == "pending"


def test_accept_rejected_when_pending_correction_exists_409(client):
    _, snap, (rid_a, _), (ra, _) = setup_snapshot(client)
    object_objection(client, snap["access_code"], 1, "x")
    oid = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0]["objection_id"]

    # 会务方已就该槽位发起待完成更正 (尚未由评审人完成)
    cr = client.post(
        "/papers/P1/review-corrections", headers=ORG,
        json={"paper_id": "P1", "serial": snap["serial"],
              "reviewer_id": rid_a, "reason": "先发起的更正"},
    )
    assert cr.status_code == 200
    existing_id = cr.json()["correction_id"]
    # 已有待更正请求一律拒绝受理 (原因不同 409), 异议状态不变
    r = client.post(
        f"/objections/{oid}/decision", headers=ORG,
        json={"decision": "accept", "reason": "异议受理原因"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["existing_correction_id"] == existing_id
    # 即使受理更正原因与既有更正原因完全相同, 仍按"已有待更正请求"拒绝
    r_same = client.post(
        f"/objections/{oid}/decision", headers=ORG,
        json={"decision": "accept", "reason": "先发起的更正"},
    )
    assert r_same.status_code == 409
    view = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0]
    assert view["state"] == "pending" and view["correction"] is None


def test_cannot_resubmit_after_accept_with_invalidated_code(client):
    _, snap, _, _ = setup_snapshot(client)
    code = snap["access_code"]
    object_objection(client, code, 1, "第一条")
    oid = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0]["objection_id"]
    client.post(
        f"/objections/{oid}/decision", headers=ORG,
        json={"decision": "accept", "reason": "受理更正"},
    )
    # 受理使访问码立即失效 -> 不得凭旧码提交新异议 (即便另一标号)
    r = object_objection(client, code, 2, "第二条异议")
    assert r.status_code == 404


def test_retry_after_rejected_returns_original_record(client):
    _, snap, _, _ = setup_snapshot(client)
    code = snap["access_code"]
    first = object_objection(client, code, 1, "理由").json()
    oid = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0]["objection_id"]
    client.post(f"/objections/{oid}/decision", headers=ORG, json={"decision": "reject"})
    # 驳回不影响快照, 同理由重试仍返回原记录 (state=rejected, changed=false)
    again = object_objection(client, code, 1, "理由")
    assert again.status_code == 200
    assert again.json()["changed"] is False
    assert again.json()["query_token"] == first["query_token"]
    assert again.json()["objection"]["state"] == "rejected"


def test_organizer_view_resolves_reviewer_after_correction_completed(client):
    _, snap, (rid_a, _), (ra, _) = setup_snapshot(client)
    object_objection(client, snap["access_code"], 1, "异议")
    oid = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0]["objection_id"]
    client.post(
        f"/objections/{oid}/decision", headers=ORG,
        json={"decision": "accept", "reason": "受理更正"},
    )
    # 评审人完成更正: 槽位评语收据原地更新
    submit = client.post(
        "/reviewer/review-corrections/P1", headers=reviewer_headers(rid_a),
        json={"paper_id": "P1", "serial": snap["serial"],
              "original_receipt": ra["receipt"], "score": 5, "comment": "更正后"},
    )
    assert submit.status_code == 200
    # 会务方视图仍能按冻结收据/更正记录定位评审人, 并展示已完成更正
    o = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0]
    assert o["reviewer_id"] == rid_a
    assert o["state"] == "accepted"
    assert o["correction"]["state"] == "completed"
    assert o["correction"]["original"]["receipt"] == ra["receipt"]
    assert o["correction"]["corrected"]["receipt"] == submit.json()["receipt"]
