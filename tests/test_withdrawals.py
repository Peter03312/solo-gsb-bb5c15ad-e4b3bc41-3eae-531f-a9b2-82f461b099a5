"""本轮功能测试: 会务方撤回现存论文。

覆盖需求:
- 会务方凭现有密钥提交 论文编号 + 当前资料修订号 base_revision + 非空原因撤回;
  事务内先核对修订号: 过期/超前 409 (优先级最高), 未知论文 404, 空原因 422,
  路径与请求体编号不一致 422, 被拒请求无部分改动;
- 匹配当前修订号的同原因重试幂等返回原记录 (changed=false, 不推进修订号、
  不重复失效/过期); 已撤回稿异原因 409; 同编号不能重新录入 (409);
- 撤回同事务生效: 立即停止评审人取稿/确认/回避/提交评语/更正 (404),
  该稿全部反馈访问码失效 (持码者统一 404, 不能再提交异议),
  待处理异议原子标记 expired (作者凭据仍可查状态), 原查询凭据仍可查状态;
  已处理异议、评语、快照、决定/回避与更正记录保留供会务方追溯;
- 当前发布版与发布序号不变, 会务方仍可核对 (带 withdrawn 标记与撤回原因),
  撤回推进资料修订号 +1; 撤回瞬间发布序号与槽位冻结在撤回记录中;
- 后续普通分配与补位均跳过撤回稿: 不出现在方案/求解器输入中,
  撤回稿的锁定随撤回清除且其锁定不阻塞其他论文; 其他论文的确认与评语规则不变;
- 撤回稿不可再更新/删除 (409); 撤回后按原修订号发起的普通发布/补位发布 409;
- 会务方可查看撤回记录 (GET /papers/{id}/withdrawal) 与论文列表中的撤回状态;
- 会务方可追溯撤回稿的评语/更正历史 (/papers/{id}/reviews);
  撤回稿不可再发起更正 (409)、不可再发布快照 (409)。
"""
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="review-withdrawals-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["ORGANIZER_KEY"] = "test-organizer-key"

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

ORG = {"X-Organizer-Key": "test-organizer-key"}
BAD_ORG = {"X-Organizer-Key": "wrong-key"}


def reviewer_headers(rid, cred=None):
    return {"X-Reviewer-Id": rid, "X-Reviewer-Credential": cred or f"cred-{rid}"}


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_for_tests()
    with db.write_txn() as conn:
        for table in (
            "papers", "reviewers", "published",
            "assignment_decisions", "reviewer_recusals", "submitted_reviews",
            "feedback_snapshots", "reviewer_status_changes", "review_corrections",
            "assignment_locks", "feedback_objections", "paper_withdrawals",
            "paper_guarantee_levels", "review_deadlines",
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


def add_paper(client, pid="P1", topics=None, institutions=None, manuscript=None):
    return client.post(
        "/papers",
        headers=ORG,
        json={
            "paper_id": pid,
            "manuscript": manuscript if manuscript is not None else f"manuscript-of-{pid}",
            "topics": topics if topics else ["AI"],
            "institutions": institutions if institutions is not None else [f"Author-{pid}"],
        },
    )


def current_revision(client):
    return client.get("/meta", headers=ORG).json()["revision"]


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


def submit_review(client, rid, pid, serial, score=4, comment=None):
    return client.post(
        f"/reviewer/assignments/{pid}/review",
        headers=reviewer_headers(rid),
        json={
            "paper_id": pid, "serial": serial, "score": score,
            "comment": comment or f"评语-{rid}-{pid}",
        },
    )


def withdraw(client, pid, revision, reason="作者申请撤稿, 经会务组确认"):
    return client.post(
        f"/papers/{pid}/withdrawal",
        headers=ORG,
        json={"paper_id": pid, "base_revision": revision, "reason": reason},
    )


def setup_reviewers(client, n=5):
    for i in range(1, n + 1):
        assert add_reviewer(client, f"R{i}").status_code == 201


def setup_published(client, papers=("P1",), n_reviewers=5):
    setup_reviewers(client, n_reviewers)
    for pid in papers:
        assert add_paper(client, pid).status_code == 201
    return publish(client)


# ------------------------------------------------------------ 校验与拒绝语义

def test_withdraw_requires_organizer_key(client):
    setup_published(client)
    rev = current_revision(client)
    r = client.post(
        "/papers/P1/withdrawal",
        headers=BAD_ORG,
        json={"paper_id": "P1", "base_revision": rev, "reason": "x"},
    )
    assert r.status_code == 401


def test_withdraw_unknown_paper_404(client):
    setup_reviewers(client, 2)
    rev = current_revision(client)
    r = withdraw(client, "GHOST", rev)
    assert r.status_code == 404
    # 未录入过的编号此后仍可正常录入
    assert add_paper(client, "GHOST").status_code == 201


def test_withdraw_stale_revision_409_takes_priority_over_unknown(client):
    setup_reviewers(client, 2)
    rev = current_revision(client)
    assert add_paper(client, "P1").status_code == 201  # 推进修订号
    # 旧修订号 + 未知论文: 修订号核对优先 -> 409, 且无部分改动
    r = withdraw(client, "GHOST", rev)
    assert r.status_code == 409
    assert current_revision(client) == rev + 1


def test_withdraw_future_revision_409_without_changes(client):
    setup_published(client)
    rev = current_revision(client)
    r = withdraw(client, "P1", rev + 5)
    assert r.status_code == 409
    assert current_revision(client) == rev


def test_withdraw_blank_reason_422(client):
    setup_published(client)
    rev = current_revision(client)
    r = client.post(
        "/papers/P1/withdrawal",
        headers=ORG,
        json={"paper_id": "P1", "base_revision": rev, "reason": "   "},
    )
    assert r.status_code == 422
    assert current_revision(client) == rev


def test_withdraw_path_body_mismatch_422(client):
    setup_published(client)
    rev = current_revision(client)
    r = client.post(
        "/papers/P1/withdrawal",
        headers=ORG,
        json={"paper_id": "P2", "base_revision": rev, "reason": "x"},
    )
    assert r.status_code == 422
    assert current_revision(client) == rev


def test_withdraw_rejected_request_has_no_partial_changes(client):
    setup_published(client)
    rev = current_revision(client)
    # 同事务内: 版本不符发生在任何写入之前 -> 无撤回行、无修订号推进
    r = withdraw(client, "P1", rev + 1)
    assert r.status_code == 409
    with db.read_txn() as conn:
        assert conn.execute(
            "SELECT withdrawn FROM papers WHERE paper_id = 'P1'"
        ).fetchone()["withdrawn"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM paper_withdrawals"
        ).fetchone()["c"] == 0
    assert current_revision(client) == rev
    g = client.get("/papers/P1/withdrawal", headers=ORG)
    assert g.status_code == 404


# ------------------------------------------------------------ 成功撤回与幂等

def test_withdraw_success_bumps_revision_and_returns_record(client):
    pub = setup_published(client, papers=("P1",))
    serial = pub["serial"]
    rev_before = current_revision(client)
    r = withdraw(client, "P1", rev_before)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["state"] == "withdrawn"
    assert body["changed"] is True
    assert body["revision"] == rev_before + 1
    w = body["withdrawal"]
    assert w["paper_id"] == "P1"
    assert w["state"] == "withdrawn"
    assert w["reason"] == "作者申请撤稿, 经会务组确认"
    assert w["revision"] == rev_before + 1
    assert w["withdrawn_at"]
    # 撤回瞬间的发布序号与槽位被冻结供追溯
    assert w["published_serial"] == serial
    assert w["published_plan"] == pub["plan"]["P1"]


def test_withdraw_matching_revision_same_reason_is_idempotent(client):
    setup_published(client)
    rev = current_revision(client)
    reason = "同一撤回原因"
    r1 = withdraw(client, "P1", rev, reason=reason)
    assert r1.status_code == 200
    assert r1.json()["changed"] is True
    new_rev = r1.json()["revision"]
    # 用撤回后的当前修订号、相同原因重试 -> 原记录, changed=false, 修订号不推进
    r2 = withdraw(client, "P1", new_rev, reason=reason)
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["changed"] is False
    assert b2["revision"] == new_rev
    assert b2["withdrawal"]["withdrawn_at"] == r1.json()["withdrawal"]["withdrawn_at"]
    assert b2["invalidated_snapshots"] == 0
    assert b2["expired_objections"] == 0
    assert current_revision(client) == new_rev
    with db.read_txn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM paper_withdrawals"
        ).fetchone()["c"] == 1


def test_withdraw_same_reason_but_stale_revision_still_409(client):
    setup_published(client)
    rev = current_revision(client)
    r1 = withdraw(client, "P1", rev)
    assert r1.status_code == 200
    # 撤回后又有资料变更 (录入新论文), 旧修订号 + 同原因 -> 409, 不返回原记录
    assert add_paper(client, "P9").status_code == 201
    r2 = withdraw(client, "P1", rev)
    assert r2.status_code == 409


def test_withdraw_different_reason_conflict_409(client):
    setup_published(client)
    rev = current_revision(client)
    r1 = withdraw(client, "P1", rev, reason="原因甲")
    assert r1.status_code == 200
    new_rev = r1.json()["revision"]
    r2 = withdraw(client, "P1", new_rev, reason="原因乙")
    assert r2.status_code == 409
    detail = r2.json()["detail"]
    assert detail["existing_reason"] == "原因甲"
    # 原原因与原修订号保持不变
    g = client.get("/papers/P1/withdrawal", headers=ORG).json()["withdrawal"]
    assert g["reason"] == "原因甲"
    assert g["revision"] == new_rev
    assert current_revision(client) == new_rev


def test_withdraw_reason_normalized_with_strip(client):
    setup_published(client)
    rev = current_revision(client)
    r1 = withdraw(client, "P1", rev, reason="  首尾空白原因  ")
    assert r1.status_code == 200
    new_rev = r1.json()["revision"]
    # 归一化后同原因重试幂等
    r2 = withdraw(client, "P1", new_rev, reason="首尾空白原因")
    assert r2.status_code == 200
    assert r2.json()["changed"] is False
    # 归一化后不同原因 -> 409
    r3 = withdraw(client, "P1", new_rev, reason="首尾空白原因!")
    assert r3.status_code == 409


def test_withdraw_before_any_publication_freezes_null_serial(client):
    setup_reviewers(client, 2)
    assert add_paper(client, "P1").status_code == 201
    rev = current_revision(client)
    r = withdraw(client, "P1", rev)
    assert r.status_code == 200
    w = r.json()["withdrawal"]
    assert w["published_serial"] is None
    assert w["published_plan"] is None


# ------------------------------------------------------------ 撤回后的资料规则

def test_withdrawn_paper_cannot_be_reentered_with_same_id(client):
    setup_published(client)
    rev = current_revision(client)
    assert withdraw(client, "P1", rev).status_code == 200
    r = add_paper(client, "P1")
    assert r.status_code == 409
    assert "撤回" in r.json()["detail"]


def test_withdrawn_paper_cannot_be_updated_or_deleted(client):
    setup_published(client)
    rev = current_revision(client)
    assert withdraw(client, "P1", rev).status_code == 200
    pu = client.put(
        "/papers/P1", headers=ORG,
        json={"manuscript": "新稿", "topics": ["AI"], "institutions": ["Univ-X"]},
    )
    assert pu.status_code == 409
    pd = client.delete("/papers/P1", headers=ORG)
    assert pd.status_code == 409
    # 资料行仍在
    papers = client.get("/papers", headers=ORG).json()["papers"]
    p1 = [p for p in papers if p["paper_id"] == "P1"][0]
    assert p1["withdrawn"] is True
    assert p1["withdrawal"]["state"] == "withdrawn"
    assert p1["manuscript"] == "manuscript-of-P1"


def test_withdrawn_paper_marked_in_published_view_but_serial_unchanged(client):
    pub = setup_published(client, papers=("P1",))
    serial_before = pub["serial"]
    rev = current_revision(client)
    assert withdraw(client, "P1", rev).status_code == 200
    assignment = client.get("/assignment", headers=ORG).json()
    # 当前发布版与发布序号不变, 撤回稿仍保留在方案中但带 withdrawn 标记
    assert assignment["serial"] == serial_before
    assert "P1" in assignment["plan"]
    p1 = assignment["papers"]["P1"]
    assert p1["withdrawn"] is True
    assert p1["withdrawal_reason"] == "作者申请撤稿, 经会务组确认"
    assert assignment["withdrawn_papers"] == ["P1"]


def test_publish_after_withdraw_with_old_revision_is_409(client):
    setup_published(client, papers=("P1",))
    rev = current_revision(client)
    assert withdraw(client, "P1", rev).status_code == 200
    # 撤回推进了修订号: 基于旧修订号的普通发布/补位发布沿用既有规则 409
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 409
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    rb = client.post(
        "/assignment/backfill/publish", headers=ORG,
        json={"base_revision": rev, "base_serial": dry["serial"]},
    )
    assert rb.status_code == 409


# ------------------------------------------------------------ 评审人侧立即停止

def test_withdraw_immediately_blocks_reviewer_pickup_decisions_and_reviews(client):
    pub = setup_published(client, papers=("P1",))
    serial = pub["serial"]
    ra, rb = pub["plan"]["P1"]
    # 撤回前 R_a 已确认
    assert decide(client, ra, "P1", "confirm").status_code == 200
    rev = current_revision(client)
    assert withdraw(client, "P1", rev).status_code == 200

    # 取稿列表立即不再包含该稿 (也不在 recused 中)
    for rid in (ra, rb):
        view = client.get("/reviewer/assignments", headers=reviewer_headers(rid)).json()
        assert [a["paper_id"] for a in view["assignments"]] == []
        assert "P1" not in view.get("states", {})
        assert all(x["paper_id"] != "P1" for x in view["recused"])

    # 确认 / 回避均被拒 (404)
    r_confirm = client.post(
        "/reviewer/assignments/P1/decision",
        headers=reviewer_headers(rb),
        json={"paper_id": "P1", "decision": "confirm"},
    )
    assert r_confirm.status_code == 404
    r_recuse = client.post(
        "/reviewer/assignments/P1/decision",
        headers=reviewer_headers(rb),
        json={"paper_id": "P1", "decision": "recuse", "reason": "撤稿后回避"},
    )
    assert r_recuse.status_code == 404
    # 提交评语被拒 (404)
    r_review = submit_review(client, ra, "P1", serial)
    assert r_review.status_code == 404
    # 查看本人评语不再返回撤回稿评语
    reviews = client.get("/reviewer/reviews", headers=reviewer_headers(ra)).json()["reviews"]
    assert all(r["paper_id"] != "P1" for r in reviews)
    # 撤回稿不形成硬回避 (撤回不是评审人声明的回避)
    meta = client.get("/meta", headers=ORG).json()
    assert all(h["paper_id"] != "P1" for h in meta["hard_recusals"])


def test_withdraw_blocks_pending_correction_submission_and_listing(client):
    pub = setup_published(client, papers=("P1",))
    serial = pub["serial"]
    ra = pub["plan"]["P1"][0]
    assert decide(client, ra, "P1", "confirm").status_code == 200
    sr = submit_review(client, ra, "P1", serial, score=3, comment="原评语")
    assert sr.status_code == 200
    receipt = sr.json()["receipt"]
    # 会务方发起更正请求 (撤回前)
    cr = client.post(
        "/papers/P1/review-corrections", headers=ORG,
        json={"paper_id": "P1", "serial": serial, "reviewer_id": ra, "reason": "请更正"},
    )
    assert cr.status_code == 200
    rev = current_revision(client)
    assert withdraw(client, "P1", rev).status_code == 200

    # 评审人待更正列表不再包含撤回稿
    tasks = client.get(
        "/reviewer/review-corrections", headers=reviewer_headers(ra)
    ).json()["corrections"]
    assert all(c["paper_id"] != "P1" for c in tasks)
    # 提交更正被拒 (404)
    r = client.post(
        "/reviewer/review-corrections/P1", headers=reviewer_headers(ra),
        json={
            "paper_id": "P1", "serial": serial, "original_receipt": receipt,
            "score": 5, "comment": "更正评语",
        },
    )
    assert r.status_code == 404


def test_withdraw_does_not_block_other_papers_review_rules(client):
    pub = setup_published(client, papers=("P1", "P2"))
    serial = pub["serial"]
    p1_pair = pub["plan"]["P1"]
    p2_pair = pub["plan"]["P2"]
    assert decide(client, p1_pair[0], "P1", "confirm").status_code == 200
    rev = current_revision(client)
    assert withdraw(client, "P1", rev).status_code == 200
    # P2 的确认与评语沿用既有规则: 正常确认 + 交评语
    ra2, rb2 = p2_pair
    assert decide(client, ra2, "P2", "confirm").status_code == 200
    assert decide(client, rb2, "P2", "confirm").status_code == 200
    sr = submit_review(client, ra2, "P2", serial + 0)
    assert sr.status_code == 200
    view = client.get("/reviewer/assignments", headers=reviewer_headers(ra2)).json()
    assert [a["paper_id"] for a in view["assignments"]] == ["P2"]


# ------------------------------------------------------------ 普通分配 / 补位跳过撤回稿

def test_normal_assignment_after_withdraw_skips_withdrawn_paper(client):
    setup_published(client, papers=("P1", "P2"))
    rev = current_revision(client)
    assert withdraw(client, "P1", rev).status_code == 200
    dry = client.post("/assignment/dry-run", headers=ORG).json()
    # 撤回稿不出现在普通分配方案中; 其他论文照常
    assert "P1" not in dry["plan"]
    assert "P1" not in dry.get("unassigned", [])
    assert "P1" not in dry["papers"]
    assert "P2" in dry["plan"]
    assert dry["feasible"] is True
    # 用新修订号可正常发布 (不含撤回稿)
    r = client.post(
        "/assignment/publish", headers=ORG,
        json={"base_revision": dry["revision"]},
    )
    assert r.status_code == 200
    assert "P1" not in r.json()["plan"]


def test_backfill_after_withdraw_skips_withdrawn_paper_and_does_not_fill_it(client):
    pub = setup_published(client, papers=("P1", "P2"), n_reviewers=6)
    # P1 两人确认, P2 两人确认
    for pid in ("P1", "P2"):
        for rid in pub["plan"][pid]:
            assert decide(client, rid, pid, "confirm").status_code == 200
    rev = current_revision(client)
    assert withdraw(client, "P1", rev).status_code == 200
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    # 撤回稿既不在补位方案, 也不作为待补齐论文
    assert "P1" not in dry["plan"]
    assert "P1" not in dry.get("unassigned", [])
    assert dry["feasible"] is True
    rb = client.post(
        "/assignment/backfill/publish", headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert rb.status_code == 200, rb.text
    new_plan = rb.json()["plan"]
    assert "P1" not in new_plan
    assert "P2" in new_plan


def test_withdrawn_papers_locks_are_cleared_and_do_not_block_others(client):
    setup_published(client, papers=("P1", "P2"))
    rev = current_revision(client)
    # 锁定 P1 -> R1 (撤回后该锁定必须清除, 不得因锁定失效阻塞 P2 的普通分配/发布)
    lr = client.post(
        "/assignment/locks", headers=ORG,
        json={"base_revision": rev, "locks": {"P1": ["R1"]}},
    )
    assert lr.status_code == 200
    rev2 = current_revision(client)
    assert withdraw(client, "P1", rev2).status_code == 200
    locks = client.get("/assignment/locks", headers=ORG).json()
    assert "P1" not in locks["locks"]
    # 普通预演不保留任何 P1 锁定, 且 P2 可完整分配
    dry = client.post("/assignment/dry-run", headers=ORG).json()
    assert dry["feasible"] is True
    assert "P1" not in dry.get("locks", {})
    assert "P2" in dry["plan"]
    pr = client.post(
        "/assignment/publish", headers=ORG,
        json={"base_revision": dry["revision"]},
    )
    assert pr.status_code == 200


# ------------------------------------------------------------ 快照码失效 / 异议过期 / 凭据可查

def _publish_snapshot(client, pub, pid):
    serial = pub["serial"]
    for rid in pub["plan"][pid]:
        assert decide(client, rid, pid, "confirm").status_code == 200
        assert submit_review(client, rid, pid, serial).status_code == 200
    r = client.post(
        f"/papers/{pid}/feedback-snapshot", headers=ORG,
        json={"serial": serial},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_code"]


def test_withdraw_invalidates_snapshot_codes_in_same_transaction(client):
    pub = setup_published(client, papers=("P1",))
    code = _publish_snapshot(client, pub, "P1")
    # 撤回前访问码可读
    assert client.get(f"/feedback-snapshots/{code}").status_code == 200
    rev = current_revision(client)
    r = withdraw(client, "P1", rev)
    assert r.status_code == 200
    assert r.json()["invalidated_snapshots"] == 1
    # 访问码立即 404 (不透露论文是否存在)
    assert client.get(f"/feedback-snapshots/{code}").status_code == 404
    # 已失效码不得再提交新异议
    ob = client.post(
        "/feedback-objections",
        json={"access_code": code, "label": 1, "reason": "撤稿后异议"},
    )
    assert ob.status_code == 404
    # 快照版本仍供会务方追溯 (标记为非当前有效)
    snaps = client.get("/papers/P1/feedback-snapshots", headers=ORG).json()
    assert snaps["snapshots"][0]["active"] is False


def test_withdraw_expires_pending_objections_but_keeps_decided_ones(client):
    pub = setup_published(client, papers=("P1",))
    code = _publish_snapshot(client, pub, "P1")
    # 两条异议: 标号 1 待处理, 标号 2 会务方驳回
    o1 = client.post(
        "/feedback-objections",
        json={"access_code": code, "label": 1, "reason": "异议一"},
    ).json()
    client.post(
        "/feedback-objections",
        json={"access_code": code, "label": 2, "reason": "异议二"},
    )
    org_objs = client.get("/papers/P1/objections", headers=ORG).json()["objections"]
    id2 = [o["objection_id"] for o in org_objs if o["label"] == 2][0]
    assert client.post(
        f"/objections/{id2}/decision", headers=ORG,
        json={"decision": "reject", "note": "驳回"},
    ).status_code == 200

    rev = current_revision(client)
    r = withdraw(client, "P1", rev)
    assert r.status_code == 200
    assert r.json()["expired_objections"] == 1

    # 待处理异议变为 expired; 作者持原查询凭据仍可查状态
    token1 = o1["query_token"]
    view = client.get(f"/feedback-objections/{token1}").json()
    assert view["state"] == "expired"
    assert view["resolved_at"]
    # 已驳回异议终态不变
    org_objs2 = client.get("/papers/P1/objections", headers=ORG).json()["objections"]
    states = {o["label"]: o["state"] for o in org_objs2}
    assert states == {1: "expired", 2: "rejected"}
    # 已过期异议不能再受理
    id1 = [o["objection_id"] for o in org_objs2 if o["label"] == 1][0]
    d = client.post(
        f"/objections/{id1}/decision", headers=ORG,
        json={"decision": "accept", "reason": "撤稿后不应受理"},
    )
    assert d.status_code == 409
    # 全部历史仍供会务方在 /objections 追溯
    all_obj = client.get("/objections", headers=ORG).json()["objections"]
    assert {o["label"] for o in all_obj} == {1, 2}


def test_withdraw_preserves_reviews_decisions_snapshots_and_corrections_for_trace(client):
    pub = setup_published(client, papers=("P1",))
    serial = pub["serial"]
    ra, rb = pub["plan"]["P1"]
    assert decide(client, ra, "P1", "confirm").status_code == 200
    assert decide(client, rb, "P1", "recuse", reason="利益冲突回避").status_code == 200
    assert submit_review(client, ra, "P1", serial, score=5, comment="优秀的工作").status_code == 200
    rev = current_revision(client)
    assert withdraw(client, "P1", rev).status_code == 200

    # 评语进度接口: 撤回稿仍在当前发布版时返回槽位快照 + withdrawn 标记
    pr = client.get("/papers/P1/reviews", headers=ORG).json()
    assert pr["withdrawn"] is True
    assert pr["withdrawal"]["reason"] == "作者申请撤稿, 经会务组确认"
    slot_states = {s["reviewer_id"]: s["state"] for s in pr["slots"]}
    assert slot_states == {ra: "confirmed", rb: "recused"}
    submitted = [s for s in pr["slots"] if s["submitted"]]
    assert len(submitted) == 1 and submitted[0]["review"]["comment"] == "优秀的工作"

    # 会务方发布视图仍可逐槽位核对
    assignment = client.get("/assignment", headers=ORG).json()["papers"]["P1"]
    assert {s["state"] for s in assignment["slots"]} == {"confirmed", "recused"}

    # 快照版本历史保留
    snaps = client.get("/papers/P1/feedback-snapshots", headers=ORG).json()["snapshots"]
    # (该用例未发布过快照) -> 无快照也无异常
    assert snaps == []


def test_withdrawn_paper_trace_after_a_later_normal_publish(client):
    # 撤回后又做过普通发布: 撤回稿已不在新方案中, 评语接口转为纯追溯视图
    pub = setup_published(client, papers=("P1", "P2"), n_reviewers=6)
    serial = pub["serial"]
    ra = pub["plan"]["P1"][0]
    assert decide(client, ra, "P1", "confirm").status_code == 200
    assert submit_review(client, ra, "P1", serial, comment="P1 评语").status_code == 200
    rev = current_revision(client)
    assert withdraw(client, "P1", rev).status_code == 200
    dry = client.post("/assignment/dry-run", headers=ORG).json()
    assert "P1" not in dry["plan"]
    assert client.post(
        "/assignment/publish", headers=ORG,
        json={"base_revision": dry["revision"]},
    ).status_code == 200
    # 纯追溯: slots 为空, 原评语在 archived_reviews, 更正记录仍可查
    pr = client.get("/papers/P1/reviews", headers=ORG).json()
    assert pr["withdrawn"] is True
    assert pr["slots"] == []
    assert pr["progress"]["slots"] == 0
    archived = pr["archived_reviews"]
    assert any(r["reviewer_id"] == ra and r["comment"] == "P1 评语" for r in archived)
    # 更正历史接口同样可查 (空列表)
    assert pr["corrections"] == []


def test_withdraw_blocks_new_correction_and_new_snapshot_requests(client):
    pub = setup_published(client, papers=("P1",))
    serial = pub["serial"]
    rev0 = current_revision(client)
    assert withdraw(client, "P1", rev0).status_code == 200
    # 撤回后发起更正 -> 409 (撤回稿不可再发起更正)
    rc = client.post(
        "/papers/P1/review-corrections", headers=ORG,
        json={"paper_id": "P1", "serial": serial, "reviewer_id": pub["plan"]["P1"][0],
              "reason": "撤稿后更正"},
    )
    assert rc.status_code == 409
    # 撤回后发布快照 -> 409
    sp = client.post(
        "/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial}
    )
    assert sp.status_code == 409


def test_get_withdrawal_record_404_for_unknown_and_active_paper(client):
    setup_published(client, papers=("P1", "P2"))
    assert client.get("/papers/GHOST/withdrawal", headers=ORG).status_code == 404
    assert client.get("/papers/P2/withdrawal", headers=ORG).status_code == 404
    rev = current_revision(client)
    assert withdraw(client, "P2", rev).status_code == 200
    g = client.get("/papers/P2/withdrawal", headers=ORG)
    assert g.status_code == 200
    assert g.json()["state"] == "withdrawn"
    assert g.json()["withdrawal"]["reason"]


def test_locks_table_rejects_withdrawn_papers_entirely(client):
    setup_published(client, papers=("P1", "P2"))
    rev = current_revision(client)
    assert withdraw(client, "P1", rev).status_code == 200
    new_rev = current_revision(client)
    # 整表含撤回稿 -> 404 整表拒绝, P2 的锁定也不得写入 (无部分改动)
    r = client.post(
        "/assignment/locks", headers=ORG,
        json={"base_revision": new_rev, "locks": {"P1": ["R1"], "P2": ["R2"]}},
    )
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["withdrawn_papers"] == ["P1"]
    locks = client.get("/assignment/locks", headers=ORG).json()["locks"]
    assert locks == {}
    # 仅锁未撤回论文仍可正常提交
    r2 = client.post(
        "/assignment/locks", headers=ORG,
        json={"base_revision": new_rev, "locks": {"P2": ["R2"]}},
    )
    assert r2.status_code == 200


def test_withdraw_one_paper_does_not_change_others_snapshot_codes(client):
    pub = setup_published(client, papers=("P1", "P2"), n_reviewers=6)
    code1 = _publish_snapshot(client, pub, "P1")
    code2 = _publish_snapshot(client, pub, "P2")
    rev = current_revision(client)
    assert withdraw(client, "P1", rev).status_code == 200
    assert client.get(f"/feedback-snapshots/{code1}").status_code == 404
    # 其他论文的访问码不受影响
    ok = client.get(f"/feedback-snapshots/{code2}")
    assert ok.status_code == 200
    assert ok.json()["paper_id"] == "P2"
