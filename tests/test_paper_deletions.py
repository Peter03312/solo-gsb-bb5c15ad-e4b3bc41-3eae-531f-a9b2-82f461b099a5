"""本轮功能测试: 删除已发布反馈的论文时的反馈生命周期。

覆盖需求:
- 删除在同一个写事务内原子生效: 该稿此前全部反馈快照访问码立即失效,
  仍 pending 的作者异议标记 expired; 持码读取与异议提交对旧码返回与
  无效码同形的 404 (响应体相同, 不透露论文是否存在);
- 作者的异议查询凭据 (obj-) 失效后仍可查询; 历史快照与异议仍可由会务方追溯
  (快照版本/异议列表/删除凭据);
- 同编号重新录入不恢复旧码效力: 旧码继续 404; 重新发布 (推进发布序号) 后
  的新快照生成新版本与新访问码, 新码可读、旧码仍 404;
- 删除推进资料修订号, 不改变当前发布版与发布序号; 未知论文 404、撤回稿 409;
  删除响应带失效快照数/过期异议数与冻结发布序号/槽位的删除凭据;
- 会务方可经 GET /papers/{id}/deletions 与 GET /papers/deletions 追溯删除事件;
- 迁移: v2 导出含 paper_deletions 删除凭据, 合法历史可恢复; 把旧快照标记
  改回有效 (含同编号重录后) 的伪造快照 422 且不写入部分数据; v1 旧格式快照
  (无删除记载) 沿用原有恢复判断, 不推断删除历史。
"""
import copy
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="review-paper-deletions-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["ORGANIZER_KEY"] = "test-organizer-key"

import pytest
from fastapi.testclient import TestClient

from app import db, migration
from app.main import app

ORG = {"X-Organizer-Key": "test-organizer-key"}
BAD_ORG = {"X-Organizer-Key": "wrong-key"}


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
            "institutions": institutions if institutions else [f"Author-{pid}"],
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
        f"/reviewer/assignments/{pid}/decision",
        headers=reviewer_headers(rid),
        json=body,
    )
    assert r.status_code == 200, r.text
    return r.json()


def submit_review(client, rid, pid, serial, score=4, comment=None):
    r = client.post(
        f"/reviewer/assignments/{pid}/review",
        headers=reviewer_headers(rid),
        json={
            "paper_id": pid, "serial": serial, "score": score,
            "comment": comment or f"评语-{rid}-{pid}",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def setup_published_paper(client, pid="P1", reviewers=("R1", "R2")):
    """录入评审人与论文, 发布, 两评审人确认并交评语; 返回 (serial, receipts)。"""
    for rid in reviewers:
        assert add_reviewer(client, rid).status_code == 201
    assert add_paper(client, pid).status_code == 201
    serial = publish(client)["serial"]
    receipts = []
    for rid, score in zip(reviewers, (4, 5)):
        decide(client, rid, pid, "confirm")
        receipts.append(
            submit_review(client, rid, pid, serial, score=score)["receipt"]
        )
    return serial, receipts


def publish_snapshot(client, pid, serial):
    r = client.post(
        f"/papers/{pid}/feedback-snapshot", headers=ORG, json={"serial": serial}
    )
    assert r.status_code == 200, r.text
    return r.json()


def submit_objection(client, code, label=1, reason="评语引用的对比实验并非本文方法"):
    return client.post(
        "/feedback-objections",
        json={"access_code": code, "label": label, "reason": reason},
    )


# ------------------------------------------------------------ 删除接口基本约定

def test_delete_unknown_paper_404(client):
    assert client.delete("/papers/GHOST", headers=ORG).status_code == 404
    assert client.delete("/papers/GHOST").status_code == 401


def test_delete_withdrawn_paper_409(client):
    setup_published_paper(client)
    rev = current_revision(client)
    wd = client.post(
        "/papers/P1/withdrawal",
        headers=ORG,
        json={"paper_id": "P1", "base_revision": rev, "reason": "一稿多投"},
    )
    assert wd.status_code == 200
    assert client.delete("/papers/P1", headers=ORG).status_code == 409


def test_delete_bumps_revision_but_keeps_serial_and_published_plan(client):
    serial, _ = setup_published_paper(client)
    rev_before = current_revision(client)
    r = client.delete("/papers/P1", headers=ORG)
    assert r.status_code == 200
    body = r.json()
    assert body["revision"] == rev_before + 1
    # 当前发布版与发布序号不变
    assignment = client.get("/assignment", headers=ORG).json()
    assert assignment["serial"] == serial
    assert "P1" in assignment["plan"]
    # 同编号可重新录入 (删除与撤回不同)
    assert add_paper(client, "P1").status_code == 201


def test_delete_without_feedback_has_zero_counts(client):
    setup_published_paper(client)
    body = client.delete("/papers/P1", headers=ORG).json()
    assert body["invalidated_snapshots"] == 0
    assert body["expired_objections"] == 0
    assert body["deletion"]["published_serial"] == 1
    assert body["deletion"]["published_plan"] == ["R1", "R2"]


# ------------------------------------------------------------ 原子失效与统一 404

def test_delete_atomically_invalidates_codes_and_expires_objections(client):
    serial, _ = setup_published_paper(client)
    snap = publish_snapshot(client, "P1", serial)
    code = snap["access_code"]

    # 旧码可读、可提交异议; 取查询凭据
    assert client.get(f"/feedback-snapshots/{code}").status_code == 200
    obj = submit_objection(client, code, label=1)
    assert obj.status_code == 200
    token = obj.json()["query_token"]

    r = client.delete("/papers/P1", headers=ORG)
    assert r.status_code == 200
    assert r.json()["invalidated_snapshots"] == 1
    assert r.json()["expired_objections"] == 1

    # 持码读取与提交新异议: 与"从未存在的无效码"同形 404
    bogus = "fbk-00000000000000000000000000000000"
    read_old = client.get(f"/feedback-snapshots/{code}")
    read_bogus = client.get(f"/feedback-snapshots/{bogus}")
    assert read_old.status_code == read_bogus.status_code == 404
    assert read_old.json() == read_bogus.json()

    obj_old = submit_objection(client, code, label=2, reason="删除后不得再提异议")
    obj_bogus = submit_objection(client, bogus, label=2, reason="无效码不得提异议")
    assert obj_old.status_code == obj_bogus.status_code == 404
    assert obj_old.json() == obj_bogus.json()

    # 异议查询凭据继续可查, 状态为 expired
    status = client.get(f"/feedback-objections/{token}")
    assert status.status_code == 200
    assert status.json()["state"] == "expired"
    assert status.json()["resolved_at"] is not None


def test_deleted_snapshots_and_objections_remain_traceable_to_organizer(client):
    serial, _ = setup_published_paper(client)
    code = publish_snapshot(client, "P1", serial)["access_code"]
    submit_objection(client, code, label=1)
    client.delete("/papers/P1", headers=ORG)

    # 论文资料行已删除, 但会务方仍可追溯快照版本
    versions = client.get("/papers/P1/feedback-snapshots", headers=ORG).json()
    assert len(versions["snapshots"]) == 1
    assert versions["snapshots"][0]["active"] is False
    assert versions["snapshots"][0]["access_code"] == code

    # 会务方异议列表 (含已过期) 仍可查
    objections = client.get("/papers/P1/objections", headers=ORG).json()["objections"]
    assert len(objections) == 1 and objections[0]["state"] == "expired"
    assert objections[0]["snapshot_invalidated"] is True
    assert client.get("/objections", headers=ORG).json()["objections"][0]["state"] == "expired"

    # 删除凭据可按编号/全量追溯
    deletions = client.get("/papers/P1/deletions", headers=ORG).json()["deletions"]
    assert len(deletions) == 1
    d = deletions[0]
    assert d["state"] == "deleted"
    assert d["invalidated_snapshots"] == 1
    assert d["expired_objections"] == 1
    assert d["published_serial"] == serial
    assert d["published_plan"] == ["R1", "R2"]
    all_dels = client.get("/papers/deletions", headers=ORG).json()["deletions"]
    assert [x["paper_id"] for x in all_dels] == ["P1"]
    # 从未删除过的编号 404
    assert client.get("/papers/NOPE/deletions", headers=ORG).status_code == 404
    assert client.get("/papers/deletions").status_code == 401


def test_decided_objections_keep_terminal_state_on_delete(client):
    serial, _ = setup_published_paper(client)
    code = publish_snapshot(client, "P1", serial)["access_code"]
    token_rejected = submit_objection(client, code, label=1, reason="异议一").json()["query_token"]
    oid = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0]["objection_id"]
    assert client.post(
        f"/objections/{oid}/decision",
        headers=ORG,
        json={"decision": "reject", "note": "不成立"},
    ).status_code == 200

    r = client.delete("/papers/P1", headers=ORG)
    assert r.json()["expired_objections"] == 0  # 终态异议不再过期
    assert client.get(f"/feedback-objections/{token_rejected}").json()["state"] == "rejected"


# ------------------------------------------------------------ 同编号重录

def test_reentry_same_id_does_not_revive_old_code(client):
    serial, _ = setup_published_paper(client)
    old_code = publish_snapshot(client, "P1", serial)["access_code"]
    client.delete("/papers/P1", headers=ORG)
    assert client.get(f"/feedback-snapshots/{old_code}").status_code == 404

    # 同编号重新录入 + 重新发布分配 (发布序号推进): 旧码仍失效
    assert add_paper(client, "P1", manuscript="重录稿").status_code == 201
    new_serial = publish(client)["serial"]
    assert new_serial == serial + 1
    assert client.get(f"/feedback-snapshots/{old_code}").status_code == 404

    # 新序号下确认/交评语 (新收据) 后发布新快照: 新版本 + 新访问码
    decide(client, "R1", "P1", "confirm")
    decide(client, "R2", "P1", "confirm")
    submit_review(client, "R1", "P1", new_serial, score=3, comment="重录后的评语 R1")
    submit_review(client, "R2", "P1", new_serial, score=3, comment="重录后的评语 R2")
    new_snap = publish_snapshot(client, "P1", new_serial)
    assert new_snap["version"] == 2
    new_code = new_snap["access_code"]
    assert new_code != old_code
    assert client.get(f"/feedback-snapshots/{new_code}").status_code == 200
    assert client.get(f"/feedback-snapshots/{old_code}").status_code == 404

    # 新码可提异议, 旧码仍不可
    assert submit_objection(client, new_code, label=1, reason="对新快照的异议").status_code == 200
    assert submit_objection(client, old_code, label=1, reason="旧码不得复活").status_code == 404

    # 两版快照均供会务方追溯, 旧版 active=false
    versions = client.get("/papers/P1/feedback-snapshots", headers=ORG).json()["snapshots"]
    assert [s["version"] for s in versions] == [1, 2]
    assert [s["active"] for s in versions] == [False, True]

    # 删除凭据记录两次删除中的一次
    dels = client.get("/papers/P1/deletions", headers=ORG).json()["deletions"]
    assert len(dels) == 1


def test_republishing_snapshot_without_serial_advance_conflicts_after_reentry(client):
    """重录后若不重新发布分配 (序号/槽位/评语收据均为删除前的旧状态),

    相同 (论文,序号,两收据) 的旧快照已随删除永久失效: 接口不返回旧码,
    按 409 提示须重新发布分配, 且不覆盖历史版本行。
    """
    serial, _ = setup_published_paper(client)
    old_snap = publish_snapshot(client, "P1", serial)
    old_code = old_snap["access_code"]
    client.delete("/papers/P1", headers=ORG)
    add_paper(client, "P1", manuscript="重录稿")
    # 不重新发布分配, 直接按当前序号 (仍为旧序号) 重发相同收据快照 -> 409
    r = client.post(
        "/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial}
    )
    assert r.status_code == 409
    assert "删除" in r.json()["detail"]["message"]
    # 旧码仍失效, 历史行未被覆盖
    assert client.get(f"/feedback-snapshots/{old_code}").status_code == 404
    versions = client.get("/papers/P1/feedback-snapshots", headers=ORG).json()["snapshots"]
    assert len(versions) == 1 and versions[0]["access_code"] == old_code


def test_multiple_delete_reentry_cycles_append_evidence(client):
    serial, _ = setup_published_paper(client)
    code1 = publish_snapshot(client, "P1", serial)["access_code"]
    client.delete("/papers/P1", headers=ORG)

    add_paper(client, "P1")
    publish(client)  # serial 2
    decide(client, "R1", "P1")
    decide(client, "R2", "P1")
    submit_review(client, "R1", "P1", 2, score=2, comment="二次评语 R1")
    submit_review(client, "R2", "P1", 2, score=2, comment="二次评语 R2")
    code2 = publish_snapshot(client, "P1", 2)["access_code"]
    r2 = client.delete("/papers/P1", headers=ORG)
    assert r2.json()["invalidated_snapshots"] == 1  # 仅第二次删除失效的 v2
    assert client.get(f"/feedback-snapshots/{code1}").status_code == 404
    assert client.get(f"/feedback-snapshots/{code2}").status_code == 404

    deletions = client.get("/papers/P1/deletions", headers=ORG).json()["deletions"]
    assert len(deletions) == 2
    assert deletions[0]["id"] < deletions[1]["id"]
    assert deletions[0]["published_serial"] == serial
    assert deletions[1]["published_serial"] == 2
    assert client.get("/papers/deletions", headers=ORG).json()["deletions"][1]["paper_id"] == "P1"


# ------------------------------------------------------------ 迁移

def export_snapshot(client):
    r = client.get("/migration/export", headers=ORG)
    assert r.status_code == 200, r.text
    return r.json()


def wipe_instance():
    with db.write_txn() as conn:
        for table in migration.BUSINESS_TABLES + ("migration_restores",):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'revision'")


def restore(client, snapshot):
    return client.post("/migration/restore", headers=ORG, json=snapshot)


def resign(snapshot):
    snapshot["checksum"] = migration.snapshot_checksum(snapshot["data"])
    return snapshot


def build_deleted_paper_state(client):
    """发布+确认+评语+快照+待处理异议 -> 删除 P1; 返回 (serial, code, token)。"""
    serial, _ = setup_published_paper(client)
    code = publish_snapshot(client, "P1", serial)["access_code"]
    token = submit_objection(client, code, label=1).json()["query_token"]
    r = client.delete("/papers/P1", headers=ORG)
    assert r.status_code == 200
    return serial, code, token


def test_export_v2_contains_deletion_evidence(client):
    serial, code, token = build_deleted_paper_state(client)
    snap = export_snapshot(client)
    assert snap["format_version"] == 2
    deletions = snap["data"]["paper_deletions"]
    assert len(deletions) == 1
    d = deletions[0]
    assert d["paper_id"] == "P1"
    assert d["invalidated_snapshots"] == 1
    assert d["expired_objections"] == 1
    assert d["published_serial"] == serial
    assert d["published_plan"] == ["R1", "R2"]
    # 快照失效标记与异议过期状态一并导出
    s = next(x for x in snap["data"]["feedback_snapshots"] if x["access_code"] == code)
    assert s["invalidated"] is True
    assert snap["data"]["feedback_objections"][0]["state"] == "expired"
    assert migration.snapshot_checksum(snap["data"]) == snap["checksum"]


def test_restore_roundtrip_deleted_paper_history(client):
    serial, code, token = build_deleted_paper_state(client)
    snap = export_snapshot(client)
    wipe_instance()
    r = restore(client, snap)
    assert r.status_code == 200, r.text
    assert r.json()["format_version"] == 2
    assert r.json()["counts"]["paper_deletions"] == 1

    # 恢复后旧码仍 404; 历史快照供会务方追溯; 异议凭据可查 expired
    assert client.get(f"/feedback-snapshots/{code}").status_code == 404
    assert submit_objection(client, code, label=2, reason="恢复后旧码仍不可提").status_code == 404
    versions = client.get("/papers/P1/feedback-snapshots", headers=ORG).json()["snapshots"]
    assert len(versions) == 1 and versions[0]["active"] is False
    assert client.get(f"/feedback-objections/{token}").json()["state"] == "expired"
    deletions = client.get("/papers/P1/deletions", headers=ORG).json()["deletions"]
    assert len(deletions) == 1 and deletions[0]["published_serial"] == serial


def test_restore_rejects_revived_deleted_snapshot_flag_flip(client):
    """复活攻击: 删除后把旧快照 invalidated 改回 0 重签 -> 422 且不留部分数据。"""
    _serial, code, _token = build_deleted_paper_state(client)
    snap = export_snapshot(client)
    wipe_instance()

    bad = copy.deepcopy(snap)
    next(x for x in bad["data"]["feedback_snapshots"] if x["access_code"] == code)["invalidated"] = False
    resign(bad)
    r = restore(client, bad)
    assert r.status_code == 422
    assert "删除" in r.json()["detail"]
    # 不留部分数据: 仍为空实例
    assert client.get("/papers", headers=ORG).json()["papers"] == []
    with db.read_txn() as conn:
        assert db.get_revision(conn) == 0
        assert conn.execute("SELECT COUNT(*) c FROM paper_deletions").fetchone()["c"] == 0
    # 原合法快照仍可恢复, 恢复后旧码 404
    assert restore(client, snap).status_code == 200
    assert client.get(f"/feedback-snapshots/{code}").status_code == 404


def test_restore_rejects_pending_objection_after_deletion(client):
    """矛盾快照: 删除凭据在, 却保留一条 pending 异议 -> 422 (删除同事务应已过期)。"""
    build_deleted_paper_state(client)
    snap = export_snapshot(client)
    wipe_instance()

    bad = copy.deepcopy(snap)
    o = bad["data"]["feedback_objections"][0]
    o["state"] = "pending"
    o["decided_at"] = None
    resign(bad)
    r = restore(client, bad)
    assert r.status_code == 422
    assert "删除" in r.json()["detail"]


def test_restore_roundtrip_reentry_history_old_code_dead_new_code_live(client):
    """删除 -> 同编号重录 -> 重新发布 -> 新快照: 迁移后旧码死、新码活。"""
    serial, _ = setup_published_paper(client)
    old_code = publish_snapshot(client, "P1", serial)["access_code"]
    client.delete("/papers/P1", headers=ORG)
    add_paper(client, "P1")
    new_serial = publish(client)["serial"]
    decide(client, "R1", "P1")
    decide(client, "R2", "P1")
    submit_review(client, "R1", "P1", new_serial, score=3, comment="重录评语 R1")
    submit_review(client, "R2", "P1", new_serial, score=3, comment="重录评语 R2")
    new_code = publish_snapshot(client, "P1", new_serial)["access_code"]

    snap = export_snapshot(client)
    wipe_instance()
    assert restore(client, snap).status_code == 200
    assert client.get(f"/feedback-snapshots/{new_code}").status_code == 200
    assert client.get(f"/feedback-snapshots/{old_code}").status_code == 404
    assert submit_objection(client, old_code, label=1, reason="旧码复活").status_code == 404
    assert submit_objection(client, new_code, label=1, reason="新码可提").status_code == 200
    # 论文已重录存在, 但删除时刻仍使旧版失效: 仅把旧版标记改回有效 -> 422
    bad = copy.deepcopy(snap)
    next(x for x in bad["data"]["feedback_snapshots"] if x["access_code"] == old_code)["invalidated"] = False
    resign(bad)
    wipe_instance()
    assert restore(client, bad).status_code == 422


def test_restore_v1_snapshot_uses_legacy_judgment_without_inferring_deletion(client):
    """旧格式 (v1) 快照未记载删除时, 沿用原有恢复判断, 不推断删除历史。

    构造旧软件导出的等价物: 删除未失效标记 (invalidated=false), 无 paper_deletions
    字段, format_version=1。该快照序号已落后当前发布序号 (旧码运行时按序号失效),
    v1 校验不因"论文已删除"而拒绝; 恢复后旧码仍按序号口径 404。
    """
    # 旧序号发布快照
    serial, _ = setup_published_paper(client)
    code = publish_snapshot(client, "P1", serial)["access_code"]
    # 删除 P1 (新软件会失效+写凭据), 再录入 P2 并重新发布 -> 序号推进
    client.delete("/papers/P1", headers=ORG)
    assert add_reviewer(client, "R3", institution="Inst-C").status_code == 201
    assert add_reviewer(client, "R4", institution="Inst-D").status_code == 201
    assert add_paper(client, "P2").status_code == 201
    new_serial = publish(client)["serial"]
    assert new_serial == serial + 1

    v2 = export_snapshot(client)
    # 等价改写为旧实例导出的 v1 快照: 去掉删除记载, 旧码标记保持有效
    v1 = copy.deepcopy(v2)
    v1["format_version"] = 1
    del v1["data"]["paper_deletions"]
    s = next(x for x in v1["data"]["feedback_snapshots"] if x["access_code"] == code)
    s["invalidated"] = False
    resign(v1)

    wipe_instance()
    r = restore(client, v1)
    assert r.status_code == 200, r.text
    assert r.json()["format_version"] == 1
    # 沿用原有判断: 旧序号快照在运行时按发布序号失效 (404), 无需删除历史
    assert client.get(f"/feedback-snapshots/{code}").status_code == 404
    # v2 对同一内容则按删除凭据核验: 标记有效但快照创建不晚于删除时刻 -> 矛盾快照
    bad_v2 = copy.deepcopy(v2)
    next(x for x in bad_v2["data"]["feedback_snapshots"] if x["access_code"] == code)["invalidated"] = False
    resign(bad_v2)
    wipe_instance()
    assert restore(client, bad_v2).status_code == 422


def test_restore_v1_envelope_must_not_carry_v2_table(client):
    """v1 信封携带未知的 paper_deletions 字段仍属结构非法 -> 422。"""
    snap = export_snapshot(client)
    snap["format_version"] = 1
    resign(snap)  # data 仍含 paper_deletions
    wipe_instance()
    assert restore(client, snap).status_code == 422
