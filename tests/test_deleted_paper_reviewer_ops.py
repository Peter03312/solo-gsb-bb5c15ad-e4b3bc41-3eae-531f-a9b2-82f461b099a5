"""修复测试: 论文删除后评审人操作的删除状态校验。

需求:
- 会务方删除论文后, 即使当前发布方案仍保留该稿删除前的历史槽位,
  评审人的确认/回避/正式评语 (含既有决定的幂等重试、确认后改回避、
  既有评语的同内容重试) 一律 404 拒绝: 不写新的决定/硬回避/评语,
  不推进资料修订号, 也不接受更正提交或在只读视图中泄露该稿;
- 同编号重新录入但未重新发布 (发布序号仍是删除时冻结的旧序号) 时同样拒绝;
- 同编号重录并重新发布 (普通/补位, 发布序号 +1) 后, 评审人按新发布序号的
  新槽位重新确认并重新提交 (新决定、新评语、新收据); 补位发布绝不沿用删除前
  的旧确认/旧评语 (dropped_deleted_confirmations 计数);
- 删除/重录/评审历史追溯契约保留: 会务方仍可经删除凭据与按论文评语视图追溯
  删除前的旧决定/旧评语; 其他论文的评审规则不受影响。
"""
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="review-deleted-ops-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["ORGANIZER_KEY"] = "test-organizer-key"

import pytest
from fastapi.testclient import TestClient

from app import db, migration
from app.main import app

ORG = {"X-Organizer-Key": "test-organizer-key"}


def reviewer_headers(rid, cred=None):
    return {"X-Reviewer-Id": rid, "X-Reviewer-Credential": cred or f"cred-{rid}"}


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_for_tests()
    with db.write_txn() as conn:
        for table in migration.BUSINESS_TABLES + ("migration_restores",):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'revision'")
    yield


@pytest.fixture
def client():
    return TestClient(app)


def add_reviewer(client, rid, capacity=3):
    return client.post(
        "/reviewers",
        headers=ORG,
        json={
            "reviewer_id": rid,
            "credential": f"cred-{rid}",
            "topics": ["AI"],
            "institution": f"Inst-{rid}",
            "capacity": capacity,
            "avoid_papers": [],
        },
    )


def add_paper(client, pid="DX1", manuscript=None):
    return client.post(
        "/papers",
        headers=ORG,
        json={
            "paper_id": pid,
            "manuscript": manuscript if manuscript is not None else f"manuscript-of-{pid}",
            "topics": ["AI"],
            "institutions": [f"Author-{pid}"],
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


def setup_two_papers(client):
    """4 名评审人 + DX1/DX2: DX1 -> RX1,RX2, DX2 -> RX3,RX4; 发布后返回发布结果。"""
    for rid in ("RX1", "RX2", "RX3", "RX4"):
        assert add_reviewer(client, rid).status_code == 201
    for pid in ("DX1", "DX2"):
        assert add_paper(client, pid).status_code == 201
    return publish(client)


# ------------------------------------------------ 删除后写操作一律拒绝

def test_decisions_rejected_on_deleted_historical_slots(client):
    pub = setup_two_papers(client)
    serial = pub["serial"]
    r1, r2 = pub["plan"]["DX1"]
    # 删除前 RX1 已确认; RX2 仍 pending
    assert decide(client, r1, "DX1", "confirm").status_code == 200
    rev_before = current_revision(client)
    assert client.delete("/papers/DX1", headers=ORG).status_code == 200

    # 已确认者: 幂等重试确认、确认后改回避均拒绝 (不允许走 changed=false 旁路)
    r = decide(client, r1, "DX1", "confirm")
    assert r.status_code == 404, r.text
    r = decide(client, r1, "DX1", "recuse", reason="删除后改回避")
    assert r.status_code == 404, r.text
    # pending 者: 新确认、首次回避均拒绝
    r = decide(client, r2, "DX1", "confirm")
    assert r.status_code == 404, r.text
    r = decide(client, r2, "DX1", "recuse", reason="删除后回避")
    assert r.status_code == 404, r.text

    # 拒绝一律不推进资料修订号
    assert current_revision(client) == rev_before + 1  # 仅删除本身推进过一次
    # 未写入新的硬回避
    hard = client.get("/meta", headers=ORG).json()["hard_recusals"]
    assert all(h["paper_id"] != "DX1" for h in hard)
    # 会务方当前发布版视图保留历史槽位并显式标注删除生命周期;
    # 重新发布推进序号后该标注自动消失
    assignment = client.get("/assignment", headers=ORG).json()
    assert assignment["deleted_papers"] == ["DX1"]
    assert assignment["papers"]["DX1"]["deleted"] is True
    assert assignment["papers"]["DX1"]["withdrawn"] is False


def test_reviews_rejected_on_deleted_historical_slots(client):
    pub = setup_two_papers(client)
    serial = pub["serial"]
    r1, r2 = pub["plan"]["DX1"]
    assert decide(client, r1, "DX1", "confirm").status_code == 200
    old = submit_review(client, r1, "DX1", serial, score=5, comment="删除前评语")
    assert old.status_code == 200
    old_receipt = old.json()["receipt"]
    assert client.delete("/papers/DX1", headers=ORG).status_code == 200
    rev_after_delete = current_revision(client)

    # 已交评语者: 同内容重试必须拒绝 (不得幂等返回原收据), 异内容同样拒绝
    r = submit_review(client, r1, "DX1", serial, score=5, comment="删除前评语")
    assert r.status_code == 404, r.text
    r = submit_review(client, r1, "DX1", serial, score=1, comment="删除后的不同评语")
    assert r.status_code == 404, r.text
    # 从未确认的 pending 槽位也拒绝 (且 404 优先于槽位状态 409)
    assert decide(client, r2, "DX1", "confirm").status_code == 404
    r = submit_review(client, r2, "DX1", serial, score=3, comment="删除后试图交评语")
    assert r.status_code == 404, r.text

    # 不推进资料修订号
    assert current_revision(client) == rev_after_delete
    # 未写入新评语行 (旧收据仍只有删除前那一份)
    with db.read_txn() as conn:
        rows = conn.execute(
            "SELECT receipt FROM submitted_reviews WHERE paper_id = 'DX1'"
        ).fetchall()
    assert [r["receipt"] for r in rows] == [old_receipt]


def test_deleted_paper_disappears_from_reviewer_views(client):
    pub = setup_two_papers(client)
    serial = pub["serial"]
    r1, r2 = pub["plan"]["DX1"]
    assert decide(client, r1, "DX1", "confirm").status_code == 200
    assert submit_review(client, r1, "DX1", serial, comment="删除前评语").status_code == 200
    assert client.delete("/papers/DX1", headers=ORG).status_code == 200

    view = client.get("/reviewer/assignments", headers=reviewer_headers(r1)).json()
    # 取稿列表不含删除稿, states 不含, 手稿内容不泄露 (即使之后同编号重录)
    assert all(a["paper_id"] != "DX1" for a in view["assignments"])
    assert "DX1" not in view.get("states", {})
    assert all(x["paper_id"] != "DX1" for x in view["recused"])

    # 本人评语视图不再显示删除稿上的旧评语 (仅供会务方追溯)
    revs = client.get("/reviewer/reviews", headers=reviewer_headers(r1)).json()["reviews"]
    assert all(r["paper_id"] != "DX1" for r in revs)

    # 删除行不存在期间, 会务方按论文视图沿用既有契约返回 404; 旧评语行仍在库中,
    # 同编号重录后可在会务方视图中按当前发布序号槽位追溯到历史评语内容。
    assert client.get("/papers/DX1/reviews", headers=ORG).status_code == 404
    assert add_paper(client, "DX1", manuscript="重录稿").status_code == 201
    org = client.get("/papers/DX1/reviews", headers=ORG).json()
    comments = [
        s["review"]["comment"] for s in org["slots"] if s["review"] is not None
    ]
    assert "删除前评语" in comments
    # 删除凭据仍可追溯
    deletions = client.get("/papers/DX1/deletions", headers=ORG).json()["deletions"]
    assert len(deletions) == 1 and deletions[0]["published_serial"] == serial


def test_other_papers_unaffected_after_deletion(client):
    pub = setup_two_papers(client)
    serial = pub["serial"]
    r3, r4 = pub["plan"]["DX2"]
    assert client.delete("/papers/DX1", headers=ORG).status_code == 200
    # DX2 的确认/评语正常
    assert decide(client, r3, "DX2", "confirm").status_code == 200
    assert decide(client, r4, "DX2", "confirm").status_code == 200
    assert submit_review(client, r3, "DX2", serial).status_code == 200
    view = client.get("/reviewer/assignments", headers=reviewer_headers(r3)).json()
    assert [a["paper_id"] for a in view["assignments"]] == ["DX2"]


# --------------------------------------- 同编号重录但未重新发布: 仍然拒绝

def test_reentry_without_republish_still_rejects(client):
    pub = setup_two_papers(client)
    serial = pub["serial"]
    r1, r2 = pub["plan"]["DX1"]
    assert decide(client, r1, "DX1", "confirm").status_code == 200
    assert submit_review(client, r1, "DX1", serial, comment="删除前评语").status_code == 200
    assert client.delete("/papers/DX1", headers=ORG).status_code == 200
    assert add_paper(client, "DX1", manuscript="重录稿").status_code == 201

    # 当前发布序号仍是删除时冻结的旧序号: 历史槽位依旧, 全部拒绝
    assert decide(client, r1, "DX1", "confirm").status_code == 404
    assert decide(client, r2, "DX1", "recuse", reason="重录后旧序号回避").status_code == 404
    r = submit_review(client, r1, "DX1", serial, comment="重录后旧序号评语")
    assert r.status_code == 404
    # 重录稿内容也不得通过取稿视图泄露
    view = client.get("/reviewer/assignments", headers=reviewer_headers(r1)).json()
    assert all(a["paper_id"] != "DX1" for a in view["assignments"])


# --------------------------------------- 重录 + 普通重新发布: 按新序号重新操作

def test_reentry_and_normal_republish_allows_fresh_confirm_and_review(client):
    pub = setup_two_papers(client)
    serial = pub["serial"]
    old_pair = pub["plan"]["DX1"]
    assert decide(client, old_pair[0], "DX1", "confirm").status_code == 200
    assert decide(client, old_pair[1], "DX1", "confirm").status_code == 200
    old_receipts = {
        rid: submit_review(client, rid, "DX1", serial, comment=f"旧评语-{rid}").json()["receipt"]
        for rid in old_pair
    }
    assert client.delete("/papers/DX1", headers=ORG).status_code == 200
    assert add_paper(client, "DX1", manuscript="重录稿").status_code == 201

    new_pub = publish(client)
    new_serial = new_pub["serial"]
    assert new_serial == serial + 1
    new_pair = new_pub["plan"]["DX1"]
    # 重新发布后会务方视图不再把该稿标为删除生命周期
    assignment = client.get("/assignment", headers=ORG).json()
    assert assignment["deleted_papers"] == []
    assert assignment["papers"]["DX1"]["deleted"] is False

    # 新序号下槽位全部从 pending 开始: 必须重新确认
    for rid in new_pair:
        view = client.get("/reviewer/assignments", headers=reviewer_headers(rid)).json()
        assert view["states"]["DX1"] == "pending"
        assert decide(client, rid, "DX1", "confirm").status_code == 200
        # 旧序号的评语提交按序号过期 409 拒绝
        stale = submit_review(client, rid, "DX1", serial, comment="拿旧序号交评语")
        assert stale.status_code == 409
        # 新序号下重新提交, 得到全新收据
        r = submit_review(client, rid, "DX1", new_serial, comment=f"新评语-{rid}")
        assert r.status_code == 200
        assert r.json()["receipt"] != old_receipts.get(rid)

    # 新评语在本人视图可见
    revs = client.get("/reviewer/reviews", headers=reviewer_headers(new_pair[0])).json()["reviews"]
    assert [r["paper_id"] for r in revs] == ["DX1"]


# --------------------------------------- 重录 + 补位发布: 不沿用旧确认/旧评语

def test_reentry_and_backfill_publish_drops_old_confirmation_and_review(client):
    pub = setup_two_papers(client)
    serial = pub["serial"]
    r1, r2 = pub["plan"]["DX1"]
    assert decide(client, r1, "DX1", "confirm").status_code == 200
    old_receipt = submit_review(
        client, r1, "DX1", serial, comment="删除前旧评语"
    ).json()["receipt"]
    assert client.delete("/papers/DX1", headers=ORG).status_code == 200
    assert add_paper(client, "DX1", manuscript="重录稿").status_code == 201

    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    bp = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert bp.status_code == 200, bp.text
    body = bp.json()
    new_serial = body["serial"]
    assert new_serial == serial + 1
    assert body["plan"]["DX1"] == [r1, r2]
    # 删除前的旧确认不沿用 (本用例仅 RX1 删除前已确认 => 计数 1; RX2 原本 pending)
    assert body["dropped_deleted_confirmations"] == 1
    assert body["carried_confirmations"] == 0
    assert body["carried_reviews"] == 0

    # 新序号槽位为 pending, 旧评语不沿用、不显示
    view = client.get("/reviewer/assignments", headers=reviewer_headers(r1)).json()
    assert view["states"]["DX1"] == "pending"
    revs = client.get("/reviewer/reviews", headers=reviewer_headers(r1)).json()["reviews"]
    assert all(r["paper_id"] != "DX1" for r in revs)

    # 必须重新确认后才能在新序号交评语 (新收据, 不复用旧评语)
    assert decide(client, r1, "DX1", "confirm").status_code == 200
    r = submit_review(client, r1, "DX1", new_serial, comment="补位重发后的新评语")
    assert r.status_code == 200
    assert r.json()["receipt"] != old_receipt


def test_backfill_keeps_carrying_confirmations_for_other_papers(client):
    """删除重录稿的旧确认不沿用, 但其他论文连续保留槽位的确认/评语沿用规则不变。"""
    pub = setup_two_papers(client)
    serial = pub["serial"]
    r3, r4 = pub["plan"]["DX2"]
    assert decide(client, r3, "DX2", "confirm").status_code == 200
    p2_receipt = submit_review(client, r3, "DX2", serial, comment="DX2 评语").json()["receipt"]
    assert client.delete("/papers/DX1", headers=ORG).status_code == 200
    assert add_paper(client, "DX1", manuscript="重录稿").status_code == 201

    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    bp = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    ).json()
    assert bp["carried_confirmations"] >= 1
    assert bp["carried_reviews"] >= 1
    # DX2 连续保留, 旧评语沿用 (同收据)
    revs = client.get("/reviewer/reviews", headers=reviewer_headers(r3)).json()["reviews"]
    assert [r["receipt"] for r in revs if r["paper_id"] == "DX2"] == [p2_receipt]


# ------------------------------------------------ 更正流程同样受删除状态约束

def test_review_corrections_blocked_for_deleted_historical_slots(client):
    pub = setup_two_papers(client)
    serial = pub["serial"]
    r1 = pub["plan"]["DX1"][0]
    assert decide(client, r1, "DX1", "confirm").status_code == 200
    receipt = submit_review(client, r1, "DX1", serial, comment="原评语").json()["receipt"]
    # 删除前提起更正请求 (pending)
    cr = client.post(
        "/papers/DX1/review-corrections",
        headers=ORG,
        json={"paper_id": "DX1", "serial": serial, "reviewer_id": r1, "reason": "请更正"},
    )
    assert cr.status_code == 200
    assert client.delete("/papers/DX1", headers=ORG).status_code == 200

    # 评审人待更正列表不再包含该稿, 提交更正文被拒
    tasks = client.get(
        "/reviewer/review-corrections", headers=reviewer_headers(r1)
    ).json()["corrections"]
    assert all(t["paper_id"] != "DX1" for t in tasks)
    r = client.post(
        "/reviewer/review-corrections/DX1",
        headers=reviewer_headers(r1),
        json={
            "paper_id": "DX1", "serial": serial, "original_receipt": receipt,
            "score": 5, "comment": "删除后提交更正文",
        },
    )
    assert r.status_code == 404
    # 旧评语未被更新 (同编号重录后经会务方视图追溯, 历史槽位仍为"原评语")
    assert add_paper(client, "DX1", manuscript="重录稿").status_code == 201
    org = client.get("/papers/DX1/reviews", headers=ORG).json()
    comments = [
        s["review"]["comment"] for s in org["slots"] if s["review"] is not None
    ]
    assert "原评语" in comments
    assert "删除后提交更正文" not in comments

    # 会务方在旧序号的历史槽位上再次发起更正也被拒
    r = client.post(
        "/papers/DX1/review-corrections",
        headers=ORG,
        json={"paper_id": "DX1", "serial": serial, "reviewer_id": r1, "reason": "再发起"},
    )
    assert r.status_code == 409


def test_deadline_view_marks_deleted_slots_and_excludes_them_from_summary(client):
    from datetime import datetime, timedelta, timezone

    pub = setup_two_papers(client)
    serial = pub["serial"]
    # 设置一个很近的截止并等待其到期
    dl = (datetime.now(timezone.utc) + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = client.post(
        "/review-deadline",
        headers=ORG,
        json={
            "base_revision": current_revision(client),
            "base_serial": serial,
            "deadline_at": dl,
        },
    )
    assert r.status_code == 200, r.text
    # 删除 DX1 (两个槽位均未确认): 删除后这些历史槽位不再计逾期
    assert client.delete("/papers/DX1", headers=ORG).status_code == 200
    import time
    time.sleep(1.5)
    view = client.get("/review-deadline", headers=ORG).json()
    dx1_slots = [s for s in view["slots"] if s["paper_id"] == "DX1"]
    dx2_slots = [s for s in view["slots"] if s["paper_id"] == "DX2"]
    assert len(dx1_slots) == 2 and all(s["deleted"] for s in dx1_slots)
    assert view["deleted_slots"] == 2
    # 汇总只计 DX2; DX2 的 pending 槽位到期后正常计逾期, 删除稿槽位不计
    assert view["summary"]["unconfirmed"] + view["summary"]["overdue"] == len(dx2_slots)
    overdue_total = sum(1 for s in view["slots"] if s["state"] == "overdue")
    assert overdue_total == len(dx2_slots)  # DX1 槽位状态不是 overdue
    assert all(s["state"] != "overdue" for s in dx1_slots)


# --------------------------------------- 多次 删除 -> 重录 -> 重发 周期
def test_second_delete_cycle_freezes_new_serial_and_third_release_works(client):
    pub = setup_two_papers(client)
    serial1 = pub["serial"]
    pair = pub["plan"]["DX1"]

    # 第一轮: 删除 -> 重录 -> 重新发布 (serial 2), 重新确认
    assert client.delete("/papers/DX1", headers=ORG).status_code == 200
    assert add_paper(client, "DX1").status_code == 201
    pub2 = publish(client)
    assert pub2["serial"] == serial1 + 1
    for rid in pair:
        assert decide(client, rid, "DX1", "confirm").status_code == 200
        assert submit_review(client, rid, "DX1", pub2["serial"], comment=f"二轮评语-{rid}").status_code == 200

    # 第二轮删除: 冻结序号为 serial 2
    assert client.delete("/papers/DX1", headers=ORG).status_code == 200
    deletions = client.get("/papers/DX1/deletions", headers=ORG).json()["deletions"]
    assert [d["published_serial"] for d in deletions] == [serial1, pub2["serial"]]
    # 当前序号 (2) 不晚于第二次冻结序号 (2): 评审操作仍拒绝
    assert decide(client, pair[0], "DX1", "confirm").status_code == 404
    assert submit_review(client, pair[0], "DX1", pub2["serial"], comment="二轮删除后评语").status_code == 404

    # 再次重录并重新发布 (serial 3) 后恢复可操作
    assert add_paper(client, "DX1").status_code == 201
    pub3 = publish(client)
    assert pub3["serial"] == 3
    new_pair = pub3["plan"]["DX1"]
    assert decide(client, new_pair[0], "DX1", "confirm").status_code == 200
    r = submit_review(client, new_pair[0], "DX1", 3, comment="三轮新评语")
    assert r.status_code == 200
