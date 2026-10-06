"""本轮功能测试: 删除状态对评审人操作的校验。

会务方删除论文后当前发布版不重写 (历史槽位仍在 plan 中), 但评审人侧必须:
- 确认/回避 (POST /reviewer/assignments/{pid}/decision) 一律 404 拒绝,
  不写新决定、不写硬回避、不推进资料修订号;
- 正式评语 (POST /reviewer/assignments/{pid}/review) 一律 404 拒绝,
  不写新评语、不推进资料修订号 (包括此前已确认未交评语的槽位);
- 评语更正 (POST /reviewer/review-corrections/{pid}) 一律 404 拒绝,
  不更新正式评语、不完成更正记录、不推进资料修订号;
- 取稿列表/本人评语/待更正任务不再呈现删除冻结的历史槽位;
- 删除前的决定、评语、回避与更正记录保留供会务方追溯 (删除、重录契约不变)。

恢复路径: 同编号重新录入但"不"重新发布时仍按旧序号拒绝; 重新发布 (推进发布
序号) 后评审人按新序号重新确认、重新交评语; 多次"删除 -> 重录 -> 发布"亦然。
"""
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="review-reviewer-deleted-test-")
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
            "review_corrections", "assignment_locks", "paper_guarantee_levels",
            "review_deadlines", "paper_deletions", "paper_withdrawals",
            "feedback_snapshots", "feedback_objections",
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


def add_paper(client, pid="P1", manuscript=None, topics=None, institutions=None):
    return client.post(
        "/papers",
        headers=ORG,
        json={
            "paper_id": pid,
            "manuscript": manuscript or f"manuscript-of-{pid}",
            "topics": topics or ["AI"],
            "institutions": institutions or [f"Author-{pid}"],
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


def setup_published(client, pid="P1", reviewers=("R1", "R2"), review=()):
    """录入并发布; review 中的评审人确认并交评语。返回 (serial, receipts)。"""
    for rid in reviewers:
        assert add_reviewer(client, rid).status_code == 201
    assert add_paper(client, pid).status_code == 201
    serial = publish(client)["serial"]
    receipts = {}
    for rid in review:
        assert decide(client, rid, pid, "confirm").status_code == 200
        receipts[rid] = submit_review(client, rid, pid, serial).json()["receipt"]
    return serial, receipts


def assert_no_rows_for(client, serial, pid, *, expect_decisions, expect_reviews):
    """核对决定/评语/硬回避行数 (被拒请求不得写入)。"""
    with db.read_txn() as conn:
        nd = conn.execute(
            "SELECT COUNT(*) c FROM assignment_decisions WHERE serial = ? AND paper_id = ?",
            (serial, pid),
        ).fetchone()["c"]
        nr = conn.execute(
            "SELECT COUNT(*) c FROM submitted_reviews WHERE serial = ? AND paper_id = ?",
            (serial, pid),
        ).fetchone()["c"]
        nrec = conn.execute(
            "SELECT COUNT(*) c FROM reviewer_recusals WHERE paper_id = ?", (pid,)
        ).fetchone()["c"]
    assert nd == expect_decisions
    assert nr == expect_reviews
    return nrec


# ------------------------------------------------------------ 确认/回避

def test_decision_confirm_rejected_after_delete_without_writes(client):
    serial, _ = setup_published(client, review=("R1",))  # R2 仍 pending
    assert client.delete("/papers/P1", headers=ORG).status_code == 200
    rev_before = current_revision(client)

    r = decide(client, "R2", "P1", "confirm")
    assert r.status_code == 404, r.text
    assert "删除" in r.json()["detail"]

    # 不写决定、不推进修订号 (历史槽位中的 R1 旧决定仍在, 供会务方追溯)
    assert current_revision(client) == rev_before
    nrec = assert_no_rows_for(client, serial, "P1", expect_decisions=1, expect_reviews=1)
    assert nrec == 0


def test_decision_recuse_rejected_after_delete_without_hard_recusal(client):
    serial, _ = setup_published(client, review=("R1", "R2"))
    client.delete("/papers/P1", headers=ORG)
    rev_before = current_revision(client)

    r = decide(client, "R2", "P1", "recuse", reason="删除后才发现利益冲突")
    assert r.status_code == 404, r.text
    # 既不写决定也不写硬回避, 不推进修订号
    assert current_revision(client) == rev_before
    nrec = assert_no_rows_for(client, serial, "P1", expect_decisions=2, expect_reviews=2)
    assert nrec == 0


def test_decision_rejected_when_reentered_but_not_republished(client):
    """同编号重新录入但未重新发布: 当前序号仍是删除冻结的旧序号, 仍拒绝。"""
    serial, _ = setup_published(client)
    client.delete("/papers/P1", headers=ORG)
    assert add_paper(client, "P1", manuscript="重录稿").status_code == 201

    r = decide(client, "R1", "P1", "confirm")
    assert r.status_code == 404
    # 旧序号行无新增
    assert_no_rows_for(client, serial, "P1", expect_decisions=0, expect_reviews=0)


# ------------------------------------------------------------ 正式评语

def test_review_rejected_after_delete_for_confirmed_slot_without_writes(client):
    serial, _ = setup_published(client, review=("R1",))  # R1 已交评语; 让 R2 仅确认
    assert decide(client, "R2", "P1", "confirm").status_code == 200
    client.delete("/papers/P1", headers=ORG)
    rev_before = current_revision(client)

    r = submit_review(client, "R2", "P1", serial, score=5, comment="删除后的评语")
    assert r.status_code == 404, r.text
    assert "删除" in r.json()["detail"]
    assert current_revision(client) == rev_before
    assert_no_rows_for(client, serial, "P1", expect_decisions=2, expect_reviews=1)


def test_review_rejected_after_delete_even_with_correct_serial(client):
    """删除不推进发布序号: 请求体携带的 serial 仍等于当前序号, 也必须拒绝。"""
    serial, _ = setup_published(client, review=("R1",))
    client.delete("/papers/P1", headers=ORG)
    r = submit_review(client, "R1", "P1", serial, score=3, comment="再交一份")
    assert r.status_code == 404
    # 原评语行仍只有一份
    assert_no_rows_for(client, serial, "P1", expect_decisions=1, expect_reviews=1)


# ------------------------------------------------------------ 评语更正

def _create_pending_correction(client, pid, rid, serial):
    r = client.post(
        f"/papers/{pid}/review-corrections",
        headers=ORG,
        json={"paper_id": pid, "serial": serial, "reviewer_id": rid, "reason": "评语依据有误"},
    )
    assert r.status_code == 200, r.text
    return r.json()["correction_id"]


def test_correction_submit_rejected_after_delete_without_writes(client):
    serial, receipts = setup_published(client, review=("R1", "R2"))
    cid = _create_pending_correction(client, "P1", "R1", serial)
    client.delete("/papers/P1", headers=ORG)
    rev_before = current_revision(client)

    r = client.post(
        "/reviewer/review-corrections/P1",
        headers=reviewer_headers("R1"),
        json={
            "paper_id": "P1", "serial": serial,
            "original_receipt": receipts["R1"],
            "score": 2, "comment": "删除后的更正评语",
        },
    )
    assert r.status_code == 404, r.text
    assert current_revision(client) == rev_before
    with db.read_txn() as conn:
        c = conn.execute("SELECT * FROM review_corrections WHERE id = ?", (cid,)).fetchone()
        assert c["state"] == "pending" and c["new_comment"] is None
        # 正式评语未被更新
        row = conn.execute(
            "SELECT receipt, comment FROM submitted_reviews"
            " WHERE serial = ? AND paper_id = 'P1' AND reviewer_id = 'R1'",
            (serial,),
        ).fetchone()
        assert row["receipt"] == receipts["R1"]
        assert row["comment"] == "评语-R1-P1"


def test_correction_submit_rejected_after_reentry_without_republish(client):
    serial, receipts = setup_published(client, review=("R1", "R2"))
    _create_pending_correction(client, "P1", "R1", serial)
    client.delete("/papers/P1", headers=ORG)
    add_paper(client, "P1", manuscript="重录稿")

    r = client.post(
        "/reviewer/review-corrections/P1",
        headers=reviewer_headers("R1"),
        json={
            "paper_id": "P1", "serial": serial,
            "original_receipt": receipts["R1"],
            "score": 2, "comment": "重录未发布时的更正",
        },
    )
    assert r.status_code == 404


def test_organizer_cannot_request_correction_for_deleted_slot(client):
    serial, _receipts = setup_published(client, review=("R1", "R2"))
    client.delete("/papers/P1", headers=ORG)
    r = client.post(
        "/papers/P1/review-corrections",
        headers=ORG,
        json={"paper_id": "P1", "serial": serial, "reviewer_id": "R1", "reason": "删除后发起"},
    )
    assert r.status_code == 404, r.text


# ------------------------------------------------------------ 评审人读视图

def test_deleted_slot_disappears_from_reviewer_views_but_history_remains(client):
    serial, _ = setup_published(client, review=("R1", "R2"))
    client.delete("/papers/P1", headers=ORG)

    # 取稿列表/状态表不再呈现删除槽位
    a = client.get("/reviewer/assignments", headers=reviewer_headers("R1")).json()
    assert a["assignments"] == []
    assert "P1" not in a["states"]
    # 本人评语视图不再呈现
    rv = client.get("/reviewer/reviews", headers=reviewer_headers("R1")).json()
    assert rv["reviews"] == []

    # 历史仍由会务方追溯 (GET /papers/{id}/reviews 对已删除稿 404 属既有契约:
    # 资料行已删; 删除凭据保留删除瞬间冻结的序号与槽位)
    deletions = client.get("/papers/P1/deletions", headers=ORG).json()["deletions"]
    assert deletions[0]["published_serial"] == serial
    assert deletions[0]["published_plan"] == ["R1", "R2"]
    with db.read_txn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM submitted_reviews WHERE paper_id = 'P1'"
        ).fetchone()["c"] == 2
        assert conn.execute(
            "SELECT COUNT(*) c FROM assignment_decisions WHERE paper_id = 'P1'"
        ).fetchone()["c"] == 2


def test_pending_correction_hidden_after_delete(client):
    serial, receipts = setup_published(client, review=("R1", "R2"))
    _create_pending_correction(client, "P1", "R1", serial)
    tasks = client.get(
        "/reviewer/review-corrections", headers=reviewer_headers("R1")
    ).json()["corrections"]
    assert len(tasks) == 1

    client.delete("/papers/P1", headers=ORG)
    tasks = client.get(
        "/reviewer/review-corrections", headers=reviewer_headers("R1")
    ).json()["corrections"]
    assert tasks == []

    # 同编号重录但未重新发布: 旧序号待更正任务仍不列出
    add_paper(client, "P1", manuscript="重录稿")
    tasks = client.get(
        "/reviewer/review-corrections", headers=reviewer_headers("R1")
    ).json()["corrections"]
    assert tasks == []


# ------------------------------------------------------------ 重录 + 重新发布恢复

def test_reviewer_can_reconfirm_and_submit_after_reentry_and_republish(client):
    serial, receipts = setup_published(client, review=("R1", "R2"))
    client.delete("/papers/P1", headers=ORG)
    add_paper(client, "P1", manuscript="重录稿")
    new_serial = publish(client)["serial"]
    assert new_serial == serial + 1

    # 新序号下一切按新发布槽位重新进行
    r1 = decide(client, "R1", "P1", "confirm")
    assert r1.status_code == 200 and r1.json()["serial"] == new_serial
    r2 = decide(client, "R2", "P1", "confirm")
    assert r2.status_code == 200
    rv = submit_review(client, "R1", "P1", new_serial, score=3, comment="重录后的评语")
    assert rv.status_code == 200
    new_receipt = rv.json()["receipt"]
    assert new_receipt != receipts["R1"]

    # 新序号行写入, 旧序号历史保留 (旧序号下 R1/R2 各一份评语)
    with db.read_txn() as conn:
        rows = conn.execute(
            "SELECT serial FROM submitted_reviews WHERE paper_id = 'P1' ORDER BY serial"
        ).fetchall()
        assert [r["serial"] for r in rows] == [serial, serial, new_serial]

    # 读视图呈现新槽位
    a = client.get("/reviewer/assignments", headers=reviewer_headers("R1")).json()
    assert [x["paper_id"] for x in a["assignments"]] == ["P1"]
    assert a["states"]["P1"] == "confirmed"


def test_repeated_delete_reentry_cycles_toggle_by_serial(client):
    serial1, _ = setup_published(client, review=("R1", "R2"))
    client.delete("/papers/P1", headers=ORG)
    assert decide(client, "R1", "P1").status_code == 404

    add_paper(client, "P1")
    serial2 = publish(client)["serial"]
    assert decide(client, "R1", "P1").status_code == 200
    assert submit_review(client, "R1", "P1", serial2, comment="第二轮评语").status_code == 200

    client.delete("/papers/P1", headers=ORG)
    # 再次删除立即冻结序号 2 的槽位
    assert decide(client, "R1", "P1").status_code == 404
    assert submit_review(client, "R1", "P1", serial2, comment="删除后的评语").status_code == 404

    add_paper(client, "P1")
    serial3 = publish(client)["serial"]
    assert decide(client, "R1", "P1").status_code == 200
    assert submit_review(client, "R1", "P1", serial3, comment="第三轮评语").status_code == 200

    deletions = client.get("/papers/P1/deletions", headers=ORG).json()["deletions"]
    assert [d["published_serial"] for d in deletions] == [serial1, serial2]


def test_correction_flow_works_again_after_reentry_and_republish(client):
    """重新发布后新序号槽位上的更正流程完整恢复 (删除保护不阻断新序号)。"""
    serial, _ = setup_published(client, review=("R1", "R2"))
    client.delete("/papers/P1", headers=ORG)
    add_paper(client, "P1", manuscript="重录稿")
    new_serial = publish(client)["serial"]

    decide(client, "R1", "P1", "confirm")
    decide(client, "R2", "P1", "confirm")
    receipt = submit_review(
        client, "R1", "P1", new_serial, score=4, comment="重录后的初版评语"
    ).json()["receipt"]

    # 会务方按新序号发起更正 -> 列出 -> 评审人提交更正
    r = client.post(
        "/papers/P1/review-corrections",
        headers=ORG,
        json={"paper_id": "P1", "serial": new_serial, "reviewer_id": "R1", "reason": "需补充依据"},
    )
    assert r.status_code == 200, r.text
    tasks = client.get(
        "/reviewer/review-corrections", headers=reviewer_headers("R1")
    ).json()["corrections"]
    assert len(tasks) == 1 and tasks[0]["serial"] == new_serial

    done = client.post(
        "/reviewer/review-corrections/P1",
        headers=reviewer_headers("R1"),
        json={
            "paper_id": "P1", "serial": new_serial,
            "original_receipt": receipt,
            "score": 5, "comment": "重录后的更正评语",
        },
    )
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["changed"] is True
    assert body["receipt"] != receipt


def test_backfill_publish_after_delete_reentry_releases_old_slots(client):
    """删除 -> 同编号重录 -> 补位发布 (不经过普通发布) 也是重新发布:

    旧序号的确认与评语不得沿用到新序号; 评审人按新序号处于 pending,
    未确认直接交评语 409, 重新确认后才能提交; 旧评语留在旧序号供会务方追溯。
    """
    serial, receipts = setup_published(client, review=("R1", "R2"))
    client.delete("/papers/P1", headers=ORG)
    add_paper(client, "P1", manuscript="重录稿")

    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"]
    frozen = {(s["paper_id"], s["reviewer_id"]) for s in dry["deleted_frozen_slots"]}
    assert frozen == {("P1", "R1"), ("P1", "R2")}

    bp = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    ).json()
    new_serial = bp["serial"]
    assert new_serial == serial + 1
    assert bp["carried_confirmations"] == 0
    assert bp["carried_reviews"] == 0
    assert bp["released_deleted_confirmations"] == 2

    states = client.get(
        "/reviewer/assignments", headers=reviewer_headers("R1")
    ).json()["states"]
    assert states["P1"] == "pending"
    # 未重新确认不能交评语
    r = submit_review(client, "R1", "P1", new_serial, score=5, comment="未确认直接交")
    assert r.status_code == 409
    # 重新确认后按新序号提交, 生成新收据
    assert decide(client, "R1", "P1").status_code == 200
    rv = submit_review(client, "R1", "P1", new_serial, score=5, comment="新序号评语")
    assert rv.status_code == 200
    assert rv.json()["receipt"] != receipts["R1"]
    # 旧序号评语未复制: 新序号此前无评语行
    with db.read_txn() as conn:
        rows = {
            r["serial"]: r["n"]
            for r in conn.execute(
                "SELECT serial, COUNT(*) n FROM submitted_reviews GROUP BY serial"
            ).fetchall()
        }
    assert rows == {serial: 2, new_serial: 1}


def test_delete_without_published_slot_does_not_block_republish_flow(client):
    """删除瞬间该稿不在方案中 (published_serial 冻结为 NULL): 不影响重录后发布。"""
    for rid in ("R1", "R2"):
        assert add_reviewer(client, rid).status_code == 201
    assert add_paper(client, "P2").status_code == 201
    publish(client)  # 方案含 P2
    # 录入 P1 但不发布 -> 删除 (无槽位冻结)
    assert add_paper(client, "P1").status_code == 201
    assert client.delete("/papers/P1", headers=ORG).status_code == 200
    add_paper(client, "P1")
    serial = publish(client)["serial"]
    assert decide(client, "R1", "P1").status_code == 200
    assert submit_review(client, "R1", "P1", serial, comment="正常评语").status_code == 200
