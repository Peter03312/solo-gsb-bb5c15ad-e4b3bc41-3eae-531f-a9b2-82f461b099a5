"""本轮功能测试: 会务方实例迁移 (一致业务数据快照的导出与空实例恢复)。

覆盖需求:
- 仅会务方可导出/恢复 (无密钥/错密钥 401);
- 导出覆盖全部现存业务记录: 论文 (含已撤回) 与评审人资料 (含凭据)、资格变更、
  硬回避、修订号与发布版、锁定、机构归并、保障等级、截止、撤回、决定、评语、
  更正、反馈快照 (含访问码) 与异议 (含查询凭据); 不包含会务方密钥;
  附格式版本与内容校验和;
- 恢复前核对格式/版本/校验和/记录引用 (符合历史保留规则), 失败 422 不留部分数据;
- 仅接受尚无业务记录的空实例; 目标非空或重复恢复 409 且不留部分数据;
- 恢复成功后修订号、发布序号、历史记录、凭据与访问码按原规则继续工作。
"""
import copy
import hashlib
import json
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="review-migration-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["ORGANIZER_KEY"] = "test-organizer-key"

import pytest
from fastapi.testclient import TestClient

from app import db, migration
from app.main import app

ORG = {"X-Organizer-Key": "test-organizer-key"}
BAD_ORG = {"X-Organizer-Key": "wrong-key"}

ALL_TABLES = migration.BUSINESS_TABLES + ("migration_restores",)


def reviewer_headers(rid, cred=None):
    return {"X-Reviewer-Id": rid, "X-Reviewer-Credential": cred or f"cred-{rid}"}


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_for_tests()
    with db.write_txn() as conn:
        for table in ALL_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'revision'")
    yield


@pytest.fixture
def client():
    return TestClient(app)


def wipe_instance():
    """模拟一个全新的空实例: 清空全部业务表与恢复标记, 修订号归零。"""
    with db.write_txn() as conn:
        for table in ALL_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'revision'")


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
    r = client.post(
        f"/reviewer/assignments/{pid}/decision", headers=reviewer_headers(rid), json=body
    )
    assert r.status_code == 200, r.text
    return r.json()


def submit_review(client, rid, pid, serial, score=4, comment=None):
    r = client.post(
        f"/reviewer/assignments/{pid}/review",
        headers=reviewer_headers(rid),
        json={
            "paper_id": pid,
            "serial": serial,
            "score": score,
            "comment": comment or f"评语-{rid}-{pid}",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def export_snapshot(client):
    r = client.get("/migration/export", headers=ORG)
    assert r.status_code == 200, r.text
    return r.json()


def restore_snapshot(client, snapshot):
    return client.post("/migration/restore", headers=ORG, json=snapshot)


def resign(snapshot):
    """篡改 data 后重算校验和 (模拟构造的非法快照)。"""
    snapshot["checksum"] = migration.snapshot_checksum(snapshot["data"])
    return snapshot


def build_rich_state(client):
    """构造覆盖全部业务表的状态, 返回关键句柄 (访问码/凭据/序号等)。"""
    for rid in ("R1", "R2", "R3", "R4"):
        assert add_reviewer(client, rid, capacity=3).status_code == 201
    assert add_paper(client, "P1", institutions=["Author-P1"]).status_code == 201
    assert add_paper(client, "P2", institutions=["Merged-A", "Merged-B"]).status_code == 201

    pub = publish(client)
    serial = pub["serial"]
    assert pub["plan"] == {"P1": ["R1", "R2"], "P2": ["R3", "R4"]}

    # 决定: R1 确认 P1; R2 回避 P1 (硬回避); R3/R4 确认 P2
    decide(client, "R1", "P1", "confirm")
    decide(client, "R2", "P1", "recuse", reason="与第二作者近三年有合作")
    decide(client, "R3", "P2", "confirm")
    decide(client, "R4", "P2", "confirm")

    # 正式评语
    submit_review(client, "R1", "P1", serial, score=5, comment="P1 评语 by R1")
    r3_receipt = submit_review(client, "R3", "P2", serial, score=4, comment="P2 评语 by R3")["receipt"]
    submit_review(client, "R4", "P2", serial, score=3, comment="P2 评语 by R4")

    # P2 反馈快照 v1 -> 访问码 code1
    snap1 = client.post("/papers/P2/feedback-snapshot", headers=ORG, json={"serial": serial})
    assert snap1.status_code == 200, snap1.text
    code1 = snap1.json()["access_code"]

    # 作者异议 (标号 1 = R3) -> 会务方受理 -> 走更正流程, code1 失效
    obj1 = client.post(
        "/feedback-objections",
        json={"access_code": code1, "label": 1, "reason": "评语一引用的实验并非本文方法"},
    )
    assert obj1.status_code == 200, obj1.text
    token1 = obj1.json()["query_token"]
    obj1_id = client.get("/papers/P2/objections", headers=ORG).json()["objections"][0]["objection_id"]
    acc = client.post(
        f"/objections/{obj1_id}/decision",
        headers=ORG,
        json={"decision": "accept", "reason": "异议成立, 请更正评分与评语"},
    )
    assert acc.status_code == 200, acc.text

    # R3 提交更正 (原收据 r3_receipt) -> 新收据
    corr = client.post(
        "/reviewer/review-corrections/P2",
        headers=reviewer_headers("R3"),
        json={
            "paper_id": "P2",
            "serial": serial,
            "original_receipt": r3_receipt,
            "score": 5,
            "comment": "更正后的 P2 评语 by R3",
        },
    )
    assert corr.status_code == 200, corr.text
    corrected_receipt = corr.json()["receipt"]

    # 更正完成后重发快照 -> v2 新访问码 code2
    snap2 = client.post("/papers/P2/feedback-snapshot", headers=ORG, json={"serial": serial})
    assert snap2.status_code == 200, snap2.text
    assert snap2.json()["version"] == 2
    code2 = snap2.json()["access_code"]

    # 第二条异议 (标号 2 = R4) -> 会务方驳回
    obj2 = client.post(
        "/feedback-objections",
        json={"access_code": code2, "label": 2, "reason": "评语二过于简略"},
    )
    assert obj2.status_code == 200, obj2.text
    token2 = obj2.json()["query_token"]
    obj2_id = [
        o for o in client.get("/papers/P2/objections", headers=ORG).json()["objections"]
        if o["state"] == "pending"
    ][0]["objection_id"]
    rej = client.post(
        f"/objections/{obj2_id}/decision",
        headers=ORG,
        json={"decision": "reject", "note": "经复核评语充分, 异议不成立"},
    )
    assert rej.status_code == 200, rej.text

    # 统一评审截止 (serial 1)
    rev = current_revision(client)
    dl = client.post(
        "/review-deadline",
        headers=ORG,
        json={
            "base_revision": rev,
            "base_serial": serial,
            "deadline_at": "2027-01-01T00:00:00Z",
        },
    )
    assert dl.status_code == 200, dl.text

    # 机构归并: Merged-A ~ Merged-B (P2 作者机构原名)
    rev = current_revision(client)
    mg = client.post(
        "/institutions/merge-groups",
        headers=ORG,
        json={"name_a": "Merged-A", "name_b": "Merged-B", "base_revision": rev},
    )
    assert mg.status_code == 200, mg.text

    # 保障等级: P2 = high
    rev = current_revision(client)
    gl = client.post(
        "/papers/P2/guarantee-level",
        headers=ORG,
        json={"paper_id": "P2", "level": "high", "base_revision": rev},
    )
    assert gl.status_code == 200, gl.text

    # 普通分配锁定表: P2 锁 R3
    rev = current_revision(client)
    lk = client.post(
        "/assignment/locks",
        headers=ORG,
        json={"base_revision": rev, "locks": {"P2": ["R3"]}},
    )
    assert lk.status_code == 200, lk.text

    # 资格变更: 停用 R4 (保持停用状态导出)
    rev = current_revision(client)
    st = client.post(
        "/reviewers/R4/status",
        headers=ORG,
        json={"reviewer_id": "R4", "base_revision": rev, "active": False, "reason": "长期休假"},
    )
    assert st.status_code == 200, st.text

    # 撤回 P1 (撤回瞬间的发布序号与槽位冻结)
    rev = current_revision(client)
    wd = client.post(
        "/papers/P1/withdrawal",
        headers=ORG,
        json={"paper_id": "P1", "base_revision": rev, "reason": "作者声明一稿多投"},
    )
    assert wd.status_code == 200, wd.text

    return {
        "serial": serial,
        "revision": current_revision(client),
        "code1": code1,
        "code2": code2,
        "token1": token1,
        "token2": token2,
        "r3_receipt": r3_receipt,
        "corrected_receipt": corrected_receipt,
    }


# ------------------------------------------------------------ 鉴权

def test_export_requires_organizer_key(client):
    assert client.get("/migration/export").status_code == 401
    assert client.get("/migration/export", headers=BAD_ORG).status_code == 401
    # 评审人凭据不能冒充会务方
    assert client.get(
        "/migration/export", headers=reviewer_headers("R1")
    ).status_code == 401


def test_restore_requires_organizer_key(client):
    snapshot = export_snapshot(client)
    assert client.post("/migration/restore", json=snapshot).status_code == 401
    assert client.post(
        "/migration/restore", headers=BAD_ORG, json=snapshot
    ).status_code == 401


# ------------------------------------------------------------ 导出内容与校验和

def test_export_empty_instance(client):
    snap = export_snapshot(client)
    assert snap["format"] == migration.FORMAT
    assert snap["format_version"] == migration.FORMAT_VERSION
    assert snap["checksum"].startswith("sha256:")
    assert "exported_at" in snap
    data = snap["data"]
    assert data["revision"] == 0
    assert data["published"] is None
    for key in migration._LIST_TABLES:
        assert data[key] == []
    # 校验和可独立复算
    assert migration.snapshot_checksum(data) == snap["checksum"]
    expected = "sha256:" + hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert snap["checksum"] == expected


def test_export_covers_all_business_records(client):
    handles = build_rich_state(client)
    r = client.get("/migration/export", headers=ORG)
    assert r.status_code == 200
    snap = r.json()
    data = snap["data"]

    # 修订号与发布版
    assert data["revision"] == handles["revision"]
    assert data["published"]["serial"] == handles["serial"]
    assert data["published"]["plan"] == {"P1": ["R1", "R2"], "P2": ["R3", "R4"]}

    # 论文 (含已撤回) 与评审人 (含凭据)
    assert {p["paper_id"] for p in data["papers"]} == {"P1", "P2"}
    assert next(p for p in data["papers"] if p["paper_id"] == "P1")["withdrawn"] is True
    assert {r["reviewer_id"] for r in data["reviewers"]} == {"R1", "R2", "R3", "R4"}
    assert next(r for r in data["reviewers"] if r["reviewer_id"] == "R1")["credential"] == "cred-R1"
    assert next(r for r in data["reviewers"] if r["reviewer_id"] == "R4")["active"] is False

    # 决定 / 评语 / 硬回避 / 资格变更
    assert len(data["assignment_decisions"]) == 4
    assert len(data["submitted_reviews"]) == 3
    assert data["reviewer_recusals"] == [
        {
            "reviewer_id": "R2",
            "paper_id": "P1",
            "reason": "与第二作者近三年有合作",
            "created_serial": handles["serial"],
            "created_revision": data["reviewer_recusals"][0]["created_revision"],
            "created_at": data["reviewer_recusals"][0]["created_at"],
        }
    ]
    assert len(data["reviewer_status_changes"]) == 1
    assert data["reviewer_status_changes"][0]["reviewer_id"] == "R4"
    assert data["reviewer_status_changes"][0]["active"] is False

    # 快照 (含访问码) / 异议 (含查询凭据) / 更正
    assert len(data["feedback_snapshots"]) == 2
    codes = {s["access_code"] for s in data["feedback_snapshots"]}
    assert codes == {handles["code1"], handles["code2"]}
    assert next(s for s in data["feedback_snapshots"] if s["version"] == 1)["invalidated"] is True
    assert len(data["feedback_objections"]) == 2
    tokens = {o["query_token"] for o in data["feedback_objections"]}
    assert tokens == {handles["token1"], handles["token2"]}
    states = {o["label"]: o["state"] for o in data["feedback_objections"]}
    assert states == {1: "accepted", 2: "rejected"}
    assert len(data["review_corrections"]) == 1
    corr = data["review_corrections"][0]
    assert corr["state"] == "completed"
    assert corr["original_receipt"] == handles["r3_receipt"]
    assert corr["new_receipt"] == handles["corrected_receipt"]

    # 归并 / 锁定 / 保障等级 / 截止 / 撤回
    assert len(data["institution_merges"]) == 1
    assert {data["institution_merges"][0]["name_a"], data["institution_merges"][0]["name_b"]} == {
        "Merged-A",
        "Merged-B",
    }
    assert data["assignment_locks"] == [
        {
            "paper_id": "P2",
            "reviewers": ["R3"],
            "updated_at": data["assignment_locks"][0]["updated_at"],
        }
    ]
    assert data["paper_guarantee_levels"][0]["paper_id"] == "P2"
    assert data["paper_guarantee_levels"][0]["level"] == "high"
    assert data["review_deadlines"][0]["serial"] == handles["serial"]
    assert data["paper_withdrawals"][0]["paper_id"] == "P1"
    assert data["paper_withdrawals"][0]["published_serial"] == handles["serial"]
    assert data["paper_withdrawals"][0]["published_plan"] == ["R1", "R2"]

    # 校验和有效
    assert migration.snapshot_checksum(data) == snap["checksum"]


def test_export_excludes_organizer_key(client):
    build_rich_state(client)
    r = client.get("/migration/export", headers=ORG)
    # 会务方密钥绝不出现在快照中; 评审人凭据/访问码/查询凭据必须保留
    assert "test-organizer-key" not in r.text
    assert "cred-R1" in r.text
    assert "fbk-" in r.text
    assert "obj-" in r.text


# ------------------------------------------------------------ 恢复: 空实例全量回环

def test_restore_roundtrip_empty_snapshot(client):
    snap = export_snapshot(client)
    wipe_instance()
    r = restore_snapshot(client, snap)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["restored"] is True
    assert body["revision"] == 0 and body["serial"] is None
    assert client.get("/meta", headers=ORG).json()["revision"] == 0


def test_restore_roundtrip_revision_zero_publish(client):
    """空资料发布空方案 (revision=0) 并设置截止后, 快照仍可校验与恢复。"""
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": 0})
    assert r.status_code == 200, r.text
    dl = client.post(
        "/review-deadline",
        headers=ORG,
        json={"base_revision": 0, "base_serial": 1, "deadline_at": "2027-01-01T00:00:00Z"},
    )
    assert dl.status_code == 200, dl.text
    snap = export_snapshot(client)
    assert snap["data"]["published"]["revision"] == 0
    assert snap["data"]["review_deadlines"][0]["revision"] == 0
    wipe_instance()
    r = restore_snapshot(client, snap)
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 0 and r.json()["serial"] == 1
    deadline = client.get("/review-deadline", headers=ORG).json()
    assert deadline["review_deadline"]["deadline_at"] == "2027-01-01T00:00:00+00:00"


def test_restore_roundtrip_rich_state(client):
    handles = build_rich_state(client)
    snap = export_snapshot(client)
    wipe_instance()

    r = restore_snapshot(client, snap)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == handles["revision"]
    assert body["serial"] == handles["serial"]
    assert body["checksum"] == snap["checksum"]
    counts = body["counts"]
    assert counts["papers"] == 2 and counts["reviewers"] == 4
    assert counts["assignment_decisions"] == 4 and counts["submitted_reviews"] == 3
    assert counts["feedback_snapshots"] == 2 and counts["feedback_objections"] == 2
    assert counts["review_corrections"] == 1 and counts["paper_withdrawals"] == 1
    assert counts["reviewer_recusals"] == 1 and counts["reviewer_status_changes"] == 1
    assert counts["institution_merges"] == 1 and counts["assignment_locks"] == 1
    assert counts["paper_guarantee_levels"] == 1 and counts["review_deadlines"] == 1

    # 修订号 / 发布序号 / 硬回避
    meta = client.get("/meta", headers=ORG).json()
    assert meta["revision"] == handles["revision"]
    assert meta["published"]["serial"] == handles["serial"]
    assert meta["hard_recusals"] == [
        {
            "reviewer_id": "R2",
            "paper_id": "P1",
            "reason": "与第二作者近三年有合作",
            "created_serial": handles["serial"],
            "created_revision": meta["hard_recusals"][0]["created_revision"],
        }
    ]

    # 当前发布版逐槽位核对 (含撤回标记)
    assignment = client.get("/assignment", headers=ORG).json()
    assert assignment["serial"] == handles["serial"]
    assert assignment["plan"] == {"P1": ["R1", "R2"], "P2": ["R3", "R4"]}
    assert assignment["withdrawn_papers"] == ["P1"]
    assert assignment["papers"]["P1"]["withdrawn"] is True
    p2_states = {s["reviewer_id"]: s["state"] for s in assignment["papers"]["P2"]["slots"]}
    assert p2_states == {"R3": "confirmed", "R4": "confirmed"}

    # 会务方按论文查看评语进度与更正记录 (更正后评语在槽位上)
    reviews = client.get("/papers/P2/reviews", headers=ORG).json()
    assert reviews["progress"]["submitted"] == 2
    r3_slot = [s for s in reviews["slots"] if s["reviewer_id"] == "R3"][0]
    assert r3_slot["review"]["receipt"] == handles["corrected_receipt"]
    assert r3_slot["review"]["score"] == 5
    assert len(reviews["corrections"]) == 1
    assert reviews["corrections"][0]["state"] == "completed"
    assert reviews["corrections"][0]["original"]["receipt"] == handles["r3_receipt"]

    # 撤回记录
    withdrawal = client.get("/papers/P1/withdrawal", headers=ORG).json()
    assert withdrawal["withdrawal"]["reason"] == "作者声明一稿多投"
    assert withdrawal["withdrawal"]["published_plan"] == ["R1", "R2"]

    # 反馈快照: 新访问码可读, 旧访问码已失效 (统一 404)
    snap_view = client.get(f"/feedback-snapshots/{handles['code2']}")
    assert snap_view.status_code == 200
    assert snap_view.json()["paper_id"] == "P2"
    assert {r_["label"] for r_ in snap_view.json()["reviews"]} == {1, 2}
    assert client.get(f"/feedback-snapshots/{handles['code1']}").status_code == 404

    # 会务方追溯快照版本
    versions = client.get("/papers/P2/feedback-snapshots", headers=ORG).json()
    assert [s["version"] for s in versions["snapshots"]] == [1, 2]
    assert versions["snapshots"][1]["active"] is True

    # 异议查询凭据仍可查状态 (accepted / rejected)
    obj1 = client.get(f"/feedback-objections/{handles['token1']}")
    assert obj1.status_code == 200 and obj1.json()["state"] == "accepted"
    obj2 = client.get(f"/feedback-objections/{handles['token2']}")
    assert obj2.status_code == 200 and obj2.json()["state"] == "rejected"
    # 会务方异议视图 (含关联更正)
    objs = client.get("/papers/P2/objections", headers=ORG).json()["objections"]
    assert len(objs) == 2
    accepted = [o for o in objs if o["state"] == "accepted"][0]
    assert accepted["correction"]["state"] == "completed"
    assert accepted["reviewer_id"] == "R3"

    # 锁定表 / 保障等级 / 归并组 / 截止 / 资格变更记录
    locks = client.get("/assignment/locks", headers=ORG).json()
    assert locks["locks"] == {"P2": ["R3"]}
    levels = client.get("/papers/guarantee-levels", headers=ORG).json()
    assert levels["levels"] == {"P2": "high"}
    groups = client.get("/institutions/merge-groups", headers=ORG).json()
    assert groups["groups"][0]["names"] == ["Merged-A", "Merged-B"]
    deadline = client.get("/review-deadline", headers=ORG).json()
    assert deadline["review_deadline"]["deadline_at"] == "2027-01-01T00:00:00+00:00"
    status = client.get("/reviewers/R4/status", headers=ORG).json()
    assert status["active"] is False
    assert len(status["history"]) == 1 and status["history"][0]["reason"] == "长期休假"

    # 评审人凭据按原规则工作: R4 已停用 403; R1 正常 200; 错误凭据 401
    assert client.get("/reviewer/assignments", headers=reviewer_headers("R4")).status_code == 403
    r1_view = client.get("/reviewer/assignments", headers=reviewer_headers("R1"))
    assert r1_view.status_code == 200
    assert client.get(
        "/reviewer/assignments", headers=reviewer_headers("R1, cred-bad".split(", ")[0], cred="bad")
    ).status_code == 401

    # 既有规则继续工作: 同内容重交评语幂等返回原 (更正后) 收据
    retry = client.post(
        "/reviewer/assignments/P2/review",
        headers=reviewer_headers("R3"),
        json={
            "paper_id": "P2",
            "serial": handles["serial"],
            "score": 5,
            "comment": "更正后的 P2 评语 by R3",
        },
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["changed"] is False
    assert retry.json()["receipt"] == handles["corrected_receipt"]

    # 修订号继续推进: 新录论文使修订号 = 恢复值 + 1
    add_paper(client, "P3")
    assert current_revision(client) == handles["revision"] + 1
    # 基于恢复前修订号的发布按原规则 409
    stale = client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": handles["revision"]}
    )
    assert stale.status_code == 409
    # 预演在新实例上正常工作
    assert client.post("/assignment/dry-run", headers=ORG).status_code == 200


def test_restore_preserves_ids_for_references(client):
    """自增 id (快照/更正/异议/资格变更/归并边) 按原值恢复, 引用关系不变。"""
    build_rich_state(client)
    snap = export_snapshot(client)
    wipe_instance()
    assert restore_snapshot(client, snap).status_code == 200
    with db.read_txn() as conn:
        obj = conn.execute(
            "SELECT snapshot_id, correction_id FROM feedback_objections"
            " WHERE state = 'accepted'"
        ).fetchone()
        snap_row = conn.execute(
            "SELECT id, version FROM feedback_snapshots WHERE id = ?", (obj["snapshot_id"],)
        ).fetchone()
        corr = conn.execute(
            "SELECT id, state FROM review_corrections WHERE id = ?", (obj["correction_id"],)
        ).fetchone()
    assert snap_row is not None and snap_row["version"] == 1
    assert corr is not None and corr["state"] == "completed"
    # 新记录 id 不与恢复的 id 冲突 (sqlite_sequence 已推进)
    with db.write_txn() as conn:
        seq = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'feedback_objections'"
        ).fetchone()["seq"]
    assert seq >= max(o["id"] for o in snap["data"]["feedback_objections"])


# ------------------------------------------------------------ 恢复: 拒绝路径

def test_restore_rejects_non_empty_target(client):
    add_paper(client, "P1")
    snap = export_snapshot(client)  # 非空快照 (含 P1)
    # 目标实例非空: 直接拒绝
    r = restore_snapshot(client, snap)
    assert r.status_code == 409
    assert "尚无业务记录" in r.json()["detail"]
    # 实例数据未被改动
    assert [p["paper_id"] for p in client.get("/papers", headers=ORG).json()["papers"]] == ["P1"]
    assert current_revision(client) == 1


def test_restore_rejects_instance_with_only_revision_history(client):
    # 录入后删除: 业务表为空但修订号 > 0, 不算"尚无业务记录"
    add_paper(client, "P1")
    client.delete("/papers/P1", headers=ORG)
    snap = export_snapshot(client)
    wipe_target = snap  # 快照本身为空内容但修订号为 2
    assert wipe_target["data"]["revision"] == 2
    r = restore_snapshot(client, wipe_target)
    assert r.status_code == 409


def test_restore_rejects_duplicate_restore(client):
    handles = build_rich_state(client)
    snap = export_snapshot(client)
    wipe_instance()
    assert restore_snapshot(client, snap).status_code == 200
    # 重复恢复: 目标已非空 -> 409
    assert restore_snapshot(client, snap).status_code == 409
    # 实例状态保持恢复后原样
    assert current_revision(client) == handles["revision"]


def test_restore_rejects_duplicate_restore_of_empty_snapshot(client):
    # 空快照恢复后业务表仍为空, 恢复标记使重复恢复仍被拒绝
    snap = export_snapshot(client)
    assert restore_snapshot(client, snap).status_code == 200
    r = restore_snapshot(client, snap)
    assert r.status_code == 409
    assert "重复恢复" in r.json()["detail"]


def test_restore_rejects_bad_format_and_version(client):
    snap = export_snapshot(client)
    bad = dict(snap, format="other-format")
    assert restore_snapshot(client, bad).status_code == 422
    bad = dict(snap, format_version=999)
    assert restore_snapshot(client, bad).status_code == 422
    bad = {k: v for k, v in snap.items() if k != "format"}
    assert restore_snapshot(client, bad).status_code == 422
    bad = {k: v for k, v in snap.items() if k != "checksum"}
    assert restore_snapshot(client, bad).status_code == 422
    bad = {k: v for k, v in snap.items() if k != "data"}
    assert restore_snapshot(client, bad).status_code == 422
    assert restore_snapshot(client, ["not", "an", "object"]).status_code == 422


def test_restore_rejects_checksum_mismatch(client):
    build_rich_state(client)
    snap = export_snapshot(client)
    wipe_instance()
    # 篡改校验和
    bad = dict(snap, checksum="sha256:" + "0" * 64)
    r = restore_snapshot(client, bad)
    assert r.status_code == 422
    assert "校验和" in r.json()["detail"]
    # 篡改内容但不更新校验和
    bad = copy.deepcopy(snap)
    bad["data"]["revision"] = 999
    r = restore_snapshot(client, bad)
    assert r.status_code == 422
    # 两次失败均未留下任何数据
    assert current_revision(client) == 0
    assert client.get("/papers", headers=ORG).json()["papers"] == []


def _expect_invalid_snapshot(client, snapshot, mutate, fragment=None):
    """在空实例上恢复引用/结构非法的快照 (重签校验和) -> 422 且不留部分数据。"""
    wipe_instance()
    bad = copy.deepcopy(snapshot)
    mutate(bad["data"])
    resign(bad)
    r = restore_snapshot(client, bad)
    assert r.status_code == 422, r.text
    if fragment is not None:
        assert fragment in r.json()["detail"]
    # 校验失败不留任何部分数据: 仍为可恢复的空实例
    assert current_revision(client) == 0
    assert client.get("/papers", headers=ORG).json()["papers"] == []


def test_restore_validates_record_references(client):
    handles = build_rich_state(client)
    snap = export_snapshot(client)

    # 异议指向未知快照版本
    def mutate_objection_snapshot(data):
        data["feedback_objections"][0]["snapshot_id"] = 99999

    _expect_invalid_snapshot(client, snap, mutate_objection_snapshot, "快照")

    # 锁定指向未知论文
    def mutate_lock_unknown_paper(data):
        data["assignment_locks"][0]["paper_id"] = "PX"

    _expect_invalid_snapshot(client, snap, mutate_lock_unknown_paper, "未知论文")

    # 锁定指向已撤回论文
    def mutate_lock_withdrawn_paper(data):
        data["assignment_locks"][0]["paper_id"] = "P1"

    _expect_invalid_snapshot(client, snap, mutate_lock_withdrawn_paper, "已撤回")

    # 撤回记录缺失 (论文带 withdrawn 标记但无撤回记录)
    def mutate_missing_withdrawal(data):
        data["paper_withdrawals"] = []

    _expect_invalid_snapshot(client, snap, mutate_missing_withdrawal, "撤回记录")

    # 撤回记录与 withdrawn 标记不一致
    def mutate_withdrawn_flag(data):
        next(p for p in data["papers"] if p["paper_id"] == "P1")["withdrawn"] = False

    _expect_invalid_snapshot(client, snap, mutate_withdrawn_flag, "withdrawn")

    # 决定引用超过当前发布序号的 serial
    def mutate_decision_serial(data):
        data["assignment_decisions"][0]["serial"] = handles["serial"] + 1

    _expect_invalid_snapshot(client, snap, mutate_decision_serial, "发布序号")

    # 当前序号的决定不在当前发布方案槽位上
    def mutate_decision_slot(data):
        d = next(
            x for x in data["assignment_decisions"]
            if x["serial"] == handles["serial"] and x["paper_id"] == "P2"
        )
        d["reviewer_id"] = "R9"

    _expect_invalid_snapshot(client, snap, mutate_decision_slot, "槽位")

    # 保障等级指向未知论文
    def mutate_level_paper(data):
        data["paper_guarantee_levels"][0]["paper_id"] = "PX"

    _expect_invalid_snapshot(client, snap, mutate_level_paper, "未知论文")

    # 待处理异议携带处理时间 (状态与时间不一致)
    def mutate_pending_objection(data):
        obj = data["feedback_objections"][0]
        obj["state"] = "pending"
        obj["correction_id"] = None
        obj["decided_at"] = "2026-01-01T00:00:00+00:00"

    _expect_invalid_snapshot(client, snap, mutate_pending_objection)

    # 快照访问码重复
    def mutate_duplicate_code(data):
        data["feedback_snapshots"][1]["access_code"] = data["feedback_snapshots"][0]["access_code"]

    _expect_invalid_snapshot(client, snap, mutate_duplicate_code, "访问码")

    # 已受理异议缺少关联更正记录
    def mutate_accepted_without_correction(data):
        obj = next(o for o in data["feedback_objections"] if o["state"] == "accepted")
        obj["correction_id"] = None

    _expect_invalid_snapshot(client, snap, mutate_accepted_without_correction, "更正")

    # 发布版修订号越过当前资料修订号
    def mutate_published_revision(data):
        data["published"]["revision"] = data["revision"] + 1

    _expect_invalid_snapshot(client, snap, mutate_published_revision, "修订号")

    # 缺失业务表字段
    def mutate_missing_table(data):
        del data["submitted_reviews"]

    _expect_invalid_snapshot(client, snap, mutate_missing_table)

    # 全部拒绝后原快照仍可正常恢复 (实例始终未被污染)
    wipe_instance()
    ok = restore_snapshot(client, snap)
    assert ok.status_code == 200, ok.text
    assert current_revision(client) == handles["revision"]


def test_restore_rejects_integrity_violation_without_partial_data(client):
    """绕过引用校验的存储层冲突 (如主键重复) 同样整体回滚。"""
    build_rich_state(client)
    snap = export_snapshot(client)
    wipe_instance()
    bad = copy.deepcopy(snap)
    # 两条相同的决定行 (主键冲突); 引用校验也会拦, 这里验证存储兜底之外
    # 构造一个引用校验通过但违反 CHECK 约束的行: 评分越界
    bad["data"]["submitted_reviews"][0]["score"] = 9
    resign(bad)
    r = restore_snapshot(client, bad)
    assert r.status_code == 422
    assert current_revision(client) == 0
    assert client.get("/papers", headers=ORG).json()["papers"] == []


# ------------------------------------------------------------ 导出与并发写入隔离

def test_export_isolated_from_concurrent_writes(client):
    """导出在 BEGIN IMMEDIATE 事务内读取: 导出期间发起的写请求不会进入快照。"""
    import threading

    build_rich_state(client)
    barrier = threading.Barrier(2)
    results = {}

    original_collect = migration.collect_snapshot

    def slow_collect(conn):
        barrier.wait(timeout=10)
        return original_collect(conn)

    from app import main as main_module

    main_module.migration.collect_snapshot = slow_collect
    try:
        t = threading.Thread(
            target=lambda: results.setdefault("export", client.get("/migration/export", headers=ORG))
        )
        t.start()
        barrier.wait(timeout=10)
        # 导出事务进行中发起写请求: 被串行化, 其内容不得进入本次快照
        add_paper(client, "PX-LATE")
        t.join(timeout=30)
    finally:
        main_module.migration.collect_snapshot = original_collect

    snap = results["export"].json()
    assert "PX-LATE" not in {p["paper_id"] for p in snap["data"]["papers"]}
    assert migration.snapshot_checksum(snap["data"]) == snap["checksum"]
    # 写请求在导出提交后生效
    assert "PX-LATE" in {
        p["paper_id"] for p in client.get("/papers", headers=ORG).json()["papers"]
    }


# -------------------------------- 访问码效力跨记录核验 (复活攻击防护)

def build_corrected_snapshot_state(client):
    """单论文场景: 发布 -> 确认/评语 -> 快照 v1 -> 发起更正并完成 -> 重发 v2。

    返回 (serial, v1_code, v2_code, 旧收据, 新收据); v1 已因更正失效, v2 当前有效。
    """
    for rid in ("R1", "R2"):
        assert add_reviewer(client, rid, capacity=3).status_code == 201
    assert add_paper(client, "P1", institutions=["Author-P1"]).status_code == 201
    serial = publish(client)["serial"]
    decide(client, "R1", "P1", "confirm")
    decide(client, "R2", "P1", "confirm")
    old_receipt = submit_review(client, "R1", "P1", serial, score=3, comment="初版评语 R1")["receipt"]
    submit_review(client, "R2", "P1", serial, score=4, comment="初版评语 R2")
    v1 = client.post("/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial})
    assert v1.status_code == 200
    v1_code = v1.json()["access_code"]

    req = client.post(
        "/papers/P1/review-corrections",
        headers=ORG,
        json={"paper_id": "P1", "serial": serial, "reviewer_id": "R1",
              "reason": "评分与评语不符, 请更正"},
    )
    assert req.status_code == 200 and req.json()["invalidated_snapshots"] == 1
    corr = client.post(
        "/reviewer/review-corrections/P1",
        headers=reviewer_headers("R1"),
        json={"paper_id": "P1", "serial": serial, "original_receipt": old_receipt,
              "score": 5, "comment": "更正后评语 R1"},
    )
    assert corr.status_code == 200
    new_receipt = corr.json()["receipt"]

    v2 = client.post("/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial})
    assert v2.status_code == 200 and v2.json()["version"] == 2
    v2_code = v2.json()["access_code"]
    # 旧码已不可读, 新码可读
    assert client.get(f"/feedback-snapshots/{v1_code}").status_code == 404
    assert client.get(f"/feedback-snapshots/{v2_code}").status_code == 200
    return serial, v1_code, v2_code, old_receipt, new_receipt


def build_pending_correction_state(client):
    """发布 -> 确认/评语 -> 快照 v1 -> 发起更正但不完成 (快照保持失效)。"""
    for rid in ("R1", "R2"):
        assert add_reviewer(client, rid, capacity=3).status_code == 201
    assert add_paper(client, "P1", institutions=["Author-P1"]).status_code == 201
    serial = publish(client)["serial"]
    decide(client, "R1", "P1", "confirm")
    decide(client, "R2", "P1", "confirm")
    receipt = submit_review(client, "R1", "P1", serial, score=3, comment="评语 R1")["receipt"]
    submit_review(client, "R2", "P1", serial, score=4, comment="评语 R2")
    v1 = client.post("/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial})
    assert v1.status_code == 200
    code = v1.json()["access_code"]
    req = client.post(
        "/papers/P1/review-corrections",
        headers=ORG,
        json={"paper_id": "P1", "serial": serial, "reviewer_id": "R1",
              "reason": "评分与评语不符, 请更正"},
    )
    assert req.status_code == 200 and req.json()["invalidated_snapshots"] == 1
    assert client.get(f"/feedback-snapshots/{code}").status_code == 404
    return serial, code, receipt


def test_restore_roundtrip_within_serial_correction_and_republish(client):
    """合法的同发布序号连续更正与再次发布: 旧版失效、新版有效, 可迁移。"""
    serial, v1_code, v2_code, _old, _new = build_corrected_snapshot_state(client)
    snap = export_snapshot(client)
    wipe_instance()
    r = restore_snapshot(client, snap)
    assert r.status_code == 200, r.text
    # 恢复后仅当前有效码可读
    assert client.get(f"/feedback-snapshots/{v2_code}").status_code == 200
    assert client.get(f"/feedback-snapshots/{v1_code}").status_code == 404
    # 当前有效码可提交异议, 失效旧码不可
    ok = client.post(
        "/feedback-objections",
        json={"access_code": v2_code, "label": 1, "reason": "对新评语有异议"},
    )
    assert ok.status_code == 200
    bad = client.post(
        "/feedback-objections",
        json={"access_code": v1_code, "label": 1, "reason": "旧码复活提交异议"},
    )
    assert bad.status_code == 404
    # 历史版本仍供会务方追溯
    versions = client.get("/papers/P1/feedback-snapshots", headers=ORG).json()["snapshots"]
    assert [s["version"] for s in versions] == [1, 2]
    assert versions[0]["active"] is False and versions[1]["active"] is True


def test_restore_roundtrip_cross_serial_historical_snapshots(client):
    """跨发布序号的历史快照 (旧序号失效、新序号有效) 可正常迁移。"""
    for rid in ("R1", "R2", "R3"):
        add_reviewer(client, rid, capacity=3)
    add_paper(client, "P1", institutions=["Author-P1"])
    s1 = publish(client)["serial"]
    decide(client, "R1", "P1", "confirm")
    decide(client, "R2", "P1", "confirm")
    submit_review(client, "R1", "P1", s1, score=4, comment="s1 评语 R1")
    submit_review(client, "R2", "P1", s1, score=4, comment="s1 评语 R2")
    old = client.post("/papers/P1/feedback-snapshot", headers=ORG, json={"serial": s1})
    assert old.status_code == 200
    old_code = old.json()["access_code"]

    # R2 回避 -> 补位 R3, 发布序号推进
    decide(client, "R2", "P1", "recuse", reason="利益冲突")
    bf = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    bp = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": bf["revision"], "base_serial": bf["serial"]},
    )
    assert bp.status_code == 200
    s2 = bp.json()["serial"]
    decide(client, "R3", "P1", "confirm")
    submit_review(client, "R3", "P1", s2, score=5, comment="s2 评语 R3")
    new = client.post("/papers/P1/feedback-snapshot", headers=ORG, json={"serial": s2})
    assert new.status_code == 200
    new_code = new.json()["access_code"]
    assert client.get(f"/feedback-snapshots/{old_code}").status_code == 404

    snap = export_snapshot(client)
    wipe_instance()
    assert restore_snapshot(client, snap).status_code == 200
    assert client.get(f"/feedback-snapshots/{new_code}").status_code == 200
    assert client.get(f"/feedback-snapshots/{old_code}").status_code == 404
    versions = client.get("/papers/P1/feedback-snapshots", headers=ORG).json()["snapshots"]
    assert {s["serial"] for s in versions} == {s1, s2}
    assert [s["active"] for s in versions] == [False, True]


def test_restore_roundtrip_pending_correction_state(client):
    """待更正请求尚未完成时导出: 该组快照全部失效, 迁移后旧码仍 404。"""
    serial, code, _receipt = build_pending_correction_state(client)
    snap = export_snapshot(client)
    wipe_instance()
    assert restore_snapshot(client, snap).status_code == 200
    assert client.get(f"/feedback-snapshots/{code}").status_code == 404
    # 待更正任务仍在, 评审人可在目标实例继续完成更正, 再重新发布
    assert client.get(
        "/reviewer/review-corrections", headers=reviewer_headers("R1")
    ).json()["corrections"][0]["state"] == "pending"


def test_restore_roundtrip_withdrawn_paper_snapshots(client):
    """撤回稿的全部快照失效但保留, 迁移后旧码 404、历史仍可追溯。"""
    serial, _v1_code, v2_code, _old, _new = build_corrected_snapshot_state(client)
    rev = current_revision(client)
    wd = client.post(
        "/papers/P1/withdrawal",
        headers=ORG,
        json={"paper_id": "P1", "base_revision": rev, "reason": "一稿多投"},
    )
    assert wd.status_code == 200 and wd.json()["invalidated_snapshots"] == 1
    assert client.get(f"/feedback-snapshots/{v2_code}").status_code == 404

    snap = export_snapshot(client)
    wipe_instance()
    assert restore_snapshot(client, snap).status_code == 200
    assert client.get(f"/feedback-snapshots/{v2_code}").status_code == 404
    versions = client.get("/papers/P1/feedback-snapshots", headers=ORG).json()["snapshots"]
    assert all(s["active"] is False for s in versions)


def test_restore_rejects_revived_old_snapshot_flag_flip(client):
    """攻击: 把已被更正失效的旧快照 invalidated 改回 0 并重算校验和 -> 422。"""
    _serial, v1_code, v2_code, _old, _new = build_corrected_snapshot_state(client)
    snap = export_snapshot(client)
    wipe_instance()

    def mutate(data):
        s = next(x for x in data["feedback_snapshots"] if x["access_code"] == v1_code)
        s["invalidated"] = False

    _expect_invalid_snapshot(client, snap, mutate, "应当已失效")
    # 失败后目标实例不留任何部分数据, 仍是干净空实例 (可直接恢复原快照)
    assert restore_snapshot(client, snap).status_code == 200
    assert client.get(f"/feedback-snapshots/{v1_code}").status_code == 404
    assert client.get(f"/feedback-snapshots/{v2_code}").status_code == 200


def test_restore_rejects_revived_snapshot_with_single_version(client):
    """攻击: 同序号只有一版, 更正完成后未重发, 把唯一快照标记改回有效 -> 422。

    版本链无法识别 (没有更高版本), 必须由更正记录的完成时刻判定;
    同时该快照收据也已不在现行评语上。
    """
    _serial, code, _receipt = build_pending_correction_state(client)
    # 再让评审人完成更正 (但不重新发布快照)
    corr = client.post(
        "/reviewer/review-corrections/P1",
        headers=reviewer_headers("R1"),
        json={"paper_id": "P1", "serial": _serial, "original_receipt": _receipt,
              "score": 5, "comment": "更正后评语 R1"},
    )
    assert corr.status_code == 200
    snap = export_snapshot(client)
    wipe_instance()

    def mutate(data):
        s = next(x for x in data["feedback_snapshots"] if x["access_code"] == code)
        s["invalidated"] = False

    _expect_invalid_snapshot(client, snap, mutate, "应当已失效")


def test_restore_rejects_revived_pending_correction_snapshot(client):
    """攻击: 待更正请求仍在, 把该组快照失效标记改回有效 -> 422。

    待更正期间不允许发布快照, 故组内全部快照必须保持失效
    (即使把快照时间戳改到未来, "存在待完成更正请求"这一记录仍拦截复活)。
    """
    _serial, code, _receipt = build_pending_correction_state(client)
    snap = export_snapshot(client)
    wipe_instance()

    def flip_only(data):
        next(x for x in data["feedback_snapshots"] if x["access_code"] == code)["invalidated"] = False

    _expect_invalid_snapshot(client, snap, flip_only, "待完成的评语更正请求")

    # 同时伪造创建时间到未来: 仍被待更正请求规则拦截
    def flip_and_fake_time(data):
        s = next(x for x in data["feedback_snapshots"] if x["access_code"] == code)
        s["invalidated"] = False
        s["created_at"] = "2099-01-01T00:00:00+00:00"

    _expect_invalid_snapshot(client, snap, flip_and_fake_time, "待完成的评语更正请求")


def test_restore_rejects_revived_withdrawn_snapshot(client):
    """攻击: 撤回稿快照标记改回有效 -> 422 (撤回记录使该稿全部快照失效)。"""
    _serial, _v1, v2_code, _old, _new = build_corrected_snapshot_state(client)
    rev = current_revision(client)
    assert client.post(
        "/papers/P1/withdrawal",
        headers=ORG,
        json={"paper_id": "P1", "base_revision": rev, "reason": "一稿多投"},
    ).status_code == 200
    snap = export_snapshot(client)
    wipe_instance()

    def mutate(data):
        s = next(x for x in data["feedback_snapshots"] if x["access_code"] == v2_code)
        s["invalidated"] = False

    _expect_invalid_snapshot(client, snap, mutate, "论文已撤回")


def test_restore_rejects_revived_old_snapshot_even_with_fake_timestamp(client):
    """攻击: 旧版标记改回有效且把创建时间伪造到未来 -> 仍 422。

    同发布序号内已有更高版本, 且更正请求发起时刻晚于旧版创建时间,
    版本链与请求时刻都不依赖被伪造的创建时间; 另外其旧收据也不再是现行评语收据。
    """
    _serial, v1_code, _v2, old_receipt, new_receipt = build_corrected_snapshot_state(client)
    snap = export_snapshot(client)
    wipe_instance()

    def mutate(data):
        s = next(x for x in data["feedback_snapshots"] if x["access_code"] == v1_code)
        s["invalidated"] = False
        s["created_at"] = "2099-01-01T00:00:00+00:00"

    _expect_invalid_snapshot(client, snap, mutate, "更新版本")
    assert old_receipt != new_receipt


def test_restore_rejects_active_snapshot_with_stale_receipts(client):
    """当前序号标记有效的快照, 收据与现行槽位评语不一致 (更正后内容) -> 422。

    这道核验独立于失效标记/时间戳: 即使快照标记保持有效, 收据不符即矛盾快照,
    旧访问码不得读取已更正的反馈。
    """
    _serial, _v1, _v2, old_receipt, new_receipt = build_corrected_snapshot_state(client)
    snap = export_snapshot(client)
    wipe_instance()

    def mutate(data):
        # v2 当前有效: 把它的收据伪造成不属于当前槽位评语的伪造收据
        # (保持幂等键不与历史版本冲突, 直达"现行评语收据"跨记录核验)
        v2 = max(data["feedback_snapshots"], key=lambda s: s["version"])
        v2["receipt1"] = "rvw-forged-not-current"

    _expect_invalid_snapshot(client, snap, mutate, "现行评语收据")
    assert old_receipt != new_receipt


def test_restore_rejects_missing_snapshot_version(client):
    """快照版本号缺号 (有版本行被删除) 与历史保留规则矛盾 -> 422。"""
    _serial, v1_code, v2_code, _old, _new = build_corrected_snapshot_state(client)
    snap = export_snapshot(client)
    wipe_instance()

    def mutate(data):
        s = next(x for x in data["feedback_snapshots"] if x["access_code"] == v1_code)
        s["version"] = 3  # 版本号 1/2 -> 2/3, 造成缺号 (幂等键不变)

    _expect_invalid_snapshot(client, snap, mutate, "版本号不连续")


def test_restore_rejects_invalidated_flag_without_cause(client):
    """反向矛盾: 当前有效快照被改成 invalidated=1 却无撤回/更正/更高版本 -> 422。"""
    for rid in ("R1", "R2"):
        add_reviewer(client, rid, capacity=3)
    add_paper(client, "P1", institutions=["Author-P1"])
    serial = publish(client)["serial"]
    decide(client, "R1", "P1", "confirm")
    decide(client, "R2", "P1", "confirm")
    submit_review(client, "R1", "P1", serial, score=4, comment="评语 R1")
    submit_review(client, "R2", "P1", serial, score=4, comment="评语 R2")
    code = client.post(
        "/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial}
    ).json()["access_code"]
    snap = export_snapshot(client)
    wipe_instance()

    def mutate(data):
        next(x for x in data["feedback_snapshots"] if x["access_code"] == code)["invalidated"] = True

    _expect_invalid_snapshot(client, snap, mutate, "无对应的失效记录")

