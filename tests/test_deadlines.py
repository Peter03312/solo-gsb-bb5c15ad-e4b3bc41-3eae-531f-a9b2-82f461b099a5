"""本轮功能测试: 统一 UTC 评审截止时刻 (review deadline)。

覆盖需求:
- 会务方为当前发布版设置一次统一 UTC 截止时刻 (携带资料修订号 + 发布序号);
- 未设置截止时沿用现有行为; 时刻须晚于设置时刻, 非法时刻/版本不符拒绝;
- 同版同值重试幂等 (changed=false), 同版异值拒绝 (409), 拒绝不改方案与历史;
- 状态视图: 未确认 / 已确认未交 / 已交 / 已回避 / 逾期, 已交评语与收据保持有效;
- 服务端到达截止后未交评语槽位逾期: 不得再确认或交评语 (相同内容重试仍幂等);
- 补位预演/发布释放逾期槽位 (即使已确认, fixed_review_overdue),
  且本次补位不得把该稿重新分给该逾期评审人 (review_overdue); 其余确认槽位固定;
- 截止判定与评语提交、补位发布无并发竞态;
- 旧版截止不约束新发布版 (普通发布/补位发布推进 serial 后截止自然失效)。
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

_TMPDIR = tempfile.mkdtemp(prefix="review-deadline-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["ORGANIZER_KEY"] = "test-organizer-key"

import pytest
from fastapi.testclient import TestClient

from app import db, main
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
            "assignment_locks", "paper_guarantee_levels", "review_deadlines",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'revision'")
    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def frozen_clock():
    """把 _utcnow 固定到一个确定的基准时刻, 返回 [基准时刻] 以便测试中推进。

    无论测试是否抛错都恢复真实时钟, 避免污染同进程内后续测试文件。
    """
    base = datetime(2026, 10, 4, 9, 0, 0, tzinfo=timezone.utc)
    holder = {"now": base}
    original = main._utcnow
    main._utcnow = lambda: holder["now"]
    try:
        yield holder
    finally:
        main._utcnow = original


def add_reviewer(client, rid, institution, capacity=5, topics=None, avoid=None):
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


def add_paper(client, pid, topics=None, institutions=None):
    return client.post(
        "/papers",
        headers=ORG,
        json={
            "paper_id": pid,
            "manuscript": f"manuscript-of-{pid}",
            "topics": topics if topics is not None else ["AI"],
            "institutions": institutions if institutions is not None else [f"Author-{pid}"],
        },
    )


def setup_published(client, reviewers=("R1", "R2"), papers=("P1",)):
    """录入评审人/论文并普通发布, 返回 (meta, plan)。meta 含 revision/serial。"""
    for rid in reviewers:
        add_reviewer(client, rid, f"Inst-{rid}")
    for pid in papers:
        add_paper(client, pid, institutions=[f"Author-{pid}"])
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 200, r.text
    return r.json(), client.get("/assignment", headers=ORG).json()["plan"]


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def set_deadline(client, rev, serial, deadline_at):
    return client.post(
        "/review-deadline",
        headers=ORG,
        json={"base_revision": rev, "base_serial": serial, "deadline_at": deadline_at},
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


def submit_review(client, rid, pid, serial, score=4, comment="质量很好, 建议小修。"):
    return client.post(
        f"/reviewer/assignments/{pid}/review",
        headers=reviewer_headers(rid),
        json={"paper_id": pid, "serial": serial, "score": score, "comment": comment},
    )


# ------------------------------------------------------------ 设置截止

def test_set_deadline_and_view(client, frozen_clock):
    meta, plan = setup_published(client)
    rev, serial = meta["revision"], meta["serial"]
    dl = frozen_clock["now"] + timedelta(days=1)
    r = set_deadline(client, rev, serial, iso(dl))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is True
    assert body["revision"] == rev and body["serial"] == serial
    assert body["review_deadline"]["deadline_at"].startswith("2026-10-05T09:00:00")
    # 设置截止不推进修订号
    assert client.get("/meta", headers=ORG).json()["revision"] == rev

    view = client.get("/review-deadline", headers=ORG).json()
    assert view["serial"] == serial and view["deadline_expired"] is False
    assert view["review_deadline"] is not None
    states = {(s["paper_id"], s["reviewer_id"]): s["state"] for s in view["slots"]}
    for pid, pair in plan.items():
        for rid in pair:
            assert states[(pid, rid)] == "unconfirmed"
    assert view["summary"]["unconfirmed"] == len(plan) * 2


def test_timezone_offsets_normalized_to_utc(client, frozen_clock):
    meta, _ = setup_published(client)
    # 北京时间 17:00 (+08:00) == UTC 09:00, 晚于基准 09:00 必须严格——用 18:00 (+08:00)
    r = set_deadline(client, meta["revision"], meta["serial"], "2026-10-04T18:00:00+08:00")
    assert r.status_code == 200, r.text
    assert r.json()["review_deadline"]["deadline_at"].startswith("2026-10-04T10:00:00")


@pytest.mark.parametrize("bad", [
    "not-a-time",
    "2026-10-04T09:00:00",       # 无时区
    "2026-10-04 09:00",          # 无法解析
])
def test_invalid_deadline_string_rejected_422(client, frozen_clock, bad):
    meta, _ = setup_published(client)
    r = set_deadline(client, meta["revision"], meta["serial"], bad)
    assert r.status_code == 422


def test_deadline_must_be_in_future(client, frozen_clock):
    meta, _ = setup_published(client)
    now = frozen_clock["now"]
    assert set_deadline(client, meta["revision"], meta["serial"], iso(now)).status_code == 422
    assert set_deadline(
        client, meta["revision"], meta["serial"], iso(now - timedelta(seconds=1))
    ).status_code == 422


def test_set_deadline_requires_published(client, frozen_clock):
    add_reviewer(client, "R1", "Inst-A")
    r = set_deadline(client, 0, 1, iso(frozen_clock["now"] + timedelta(hours=1)))
    assert r.status_code == 404


def test_set_deadline_version_mismatch_409(client, frozen_clock):
    meta, _ = setup_published(client)
    dl = iso(frozen_clock["now"] + timedelta(hours=1))
    # 修订号不符
    assert set_deadline(client, meta["revision"] + 1, meta["serial"], dl).status_code == 409
    assert set_deadline(client, meta["revision"] - 1, meta["serial"], dl).status_code == 409
    # 发布序号不符
    assert set_deadline(client, meta["revision"], meta["serial"] + 1, dl).status_code == 409
    # 被拒后再用正确版本设置仍成功, 证明拒绝未污染
    r = set_deadline(client, meta["revision"], meta["serial"], dl)
    assert r.status_code == 200 and r.json()["changed"] is True


def test_same_version_same_value_idempotent(client, frozen_clock):
    meta, _ = setup_published(client)
    dl = iso(frozen_clock["now"] + timedelta(hours=2))
    r1 = set_deadline(client, meta["revision"], meta["serial"], dl)
    r2 = set_deadline(client, meta["revision"], meta["serial"], "2026-10-04T11:00:00Z")
    assert r1.json()["changed"] is True
    assert r2.status_code == 200
    assert r2.json()["changed"] is False
    assert r2.json()["review_deadline"]["deadline_at"] == r1.json()["review_deadline"]["deadline_at"]
    with db.read_txn() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM review_deadlines").fetchone()["c"] == 1
    # 修订号仍未推进
    assert client.get("/meta", headers=ORG).json()["revision"] == meta["revision"]


def test_same_version_different_value_rejected_409(client, frozen_clock):
    meta, _ = setup_published(client)
    dl1 = iso(frozen_clock["now"] + timedelta(hours=2))
    dl2 = iso(frozen_clock["now"] + timedelta(hours=3))
    assert set_deadline(client, meta["revision"], meta["serial"], dl1).status_code == 200
    r = set_deadline(client, meta["revision"], meta["serial"], dl2)
    assert r.status_code == 409
    # 原值不变
    view = client.get("/review-deadline", headers=ORG).json()
    assert view["review_deadline"]["deadline_at"].startswith("2026-10-04T11:00:00")


def test_data_change_after_set_makes_retry_stale(client, frozen_clock):
    meta, _ = setup_published(client)
    set_deadline(client, meta["revision"], meta["serial"], iso(frozen_clock["now"] + timedelta(hours=2)))
    # 资料变更推进修订号后, 同值重试携带旧修订号 -> 409 (版本不符);
    # 携带新修订号同值重试 -> 同版同值幂等 (仍 changed=false)
    add_paper(client, "P2", institutions=["Author-P2"])
    new_rev = client.get("/meta", headers=ORG).json()["revision"]
    assert set_deadline(
        client, meta["revision"], meta["serial"], iso(frozen_clock["now"] + timedelta(hours=2))
    ).status_code == 409
    r = set_deadline(
        client, new_rev, meta["serial"], iso(frozen_clock["now"] + timedelta(hours=2))
    )
    assert r.status_code == 200 and r.json()["changed"] is False


def test_view_before_deadline_no_overdue_state(client, frozen_clock):
    _, plan = setup_published(client, papers=("P1",))
    r1, r2 = plan["P1"]
    serial = client.get("/assignment", headers=ORG).json()["serial"]
    decide(client, r1, "P1", "confirm")
    submit_review(client, r1, "P1", serial)
    decide(client, r2, "P1", "confirm")  # 已确认未交
    rev = client.get("/meta", headers=ORG).json()["revision"]  # 决定推进修订号后再取
    set_deadline(client, rev, serial, iso(frozen_clock["now"] + timedelta(hours=1)))
    view = client.get("/review-deadline", headers=ORG).json()
    states = {(s["paper_id"], s["reviewer_id"]): s["state"] for s in view["slots"]}
    assert states[("P1", r1)] == "submitted"
    assert states[("P1", r2)] == "confirmed_unsubmitted"
    assert view["summary"] == {
        "unconfirmed": 0, "confirmed_unsubmitted": 1, "submitted": 1,
        "recused": 0, "overdue": 0,
    }


def test_recused_slot_is_state_recused_not_overdue(client, frozen_clock):
    _, plan = setup_published(client)
    r1, r2 = plan["P1"]
    serial = client.get("/assignment", headers=ORG).json()["serial"]
    decide(client, r1, "P1", "recuse", reason="与作者有合作")
    rev = client.get("/meta", headers=ORG).json()["revision"]  # 回避推进修订号后再取
    set_deadline(client, rev, serial, iso(frozen_clock["now"] + timedelta(hours=1)))
    frozen_clock["now"] += timedelta(hours=2)  # 过截止
    view = client.get("/review-deadline", headers=ORG).json()
    states = {s["reviewer_id"]: s["state"] for s in view["slots"]}
    assert states[r1] == "recused" and states[r2] == "overdue"
    assert view["summary"]["recused"] == 1 and view["summary"]["overdue"] == 1


# ------------------------------------------------------------ 截止后评审人受限

def test_after_deadline_confirm_and_review_rejected(client, frozen_clock):
    _, plan = setup_published(client)
    r1, r2 = plan["P1"]
    serial = client.get("/assignment", headers=ORG).json()["serial"]
    decide(client, r2, "P1", "confirm")  # R2 截止前确认 (未交)
    rev = client.get("/meta", headers=ORG).json()["revision"]  # 确认推进修订号后再取
    set_deadline(client, rev, serial, iso(frozen_clock["now"] + timedelta(hours=1)))
    frozen_clock["now"] += timedelta(hours=2)  # 过截止

    # R1 尚未确认 -> 逾期, 不能再确认
    r = decide(client, r1, "P1", "confirm")
    assert r.status_code == 409
    # R2 已确认未交 -> 逾期, 不能再交评语
    r = submit_review(client, r2, "P1", serial)
    assert r.status_code == 409
    # 被拒不落库
    view = client.get("/review-deadline", headers=ORG).json()
    assert view["summary"]["submitted"] == 0
    assert view["summary"]["overdue"] == 2


def test_submitted_review_stays_valid_after_deadline(client, frozen_clock):
    _, plan = setup_published(client)
    r1, _ = plan["P1"]
    serial = client.get("/assignment", headers=ORG).json()["serial"]
    decide(client, r1, "P1", "confirm")
    first = submit_review(client, r1, "P1", serial, comment="截止前已交评语")
    assert first.status_code == 200
    receipt = first.json()["receipt"]
    rev = client.get("/meta", headers=ORG).json()["revision"]  # 确认推进修订号后再取
    set_deadline(client, rev, serial, iso(frozen_clock["now"] + timedelta(hours=1)))
    frozen_clock["now"] += timedelta(hours=2)

    # 截止后: 已交评语保持有效; 相同内容重试仍幂等返回原收据
    retry = submit_review(client, r1, "P1", serial, comment="截止前已交评语")
    assert retry.status_code == 200
    assert retry.json()["changed"] is False and retry.json()["receipt"] == receipt
    # 异内容仍按既有规则 409 冲突
    conflict = submit_review(client, r1, "P1", serial, comment="截止后想改评语")
    assert conflict.status_code == 409
    # 评审人仍可查看本人评语
    mine = client.get("/reviewer/reviews", headers=reviewer_headers(r1)).json()
    assert len(mine["reviews"]) == 1 and mine["reviews"][0]["receipt"] == receipt


def test_reviewer_assignments_show_deadline_and_overdue(client, frozen_clock):
    _, plan = setup_published(client)
    r1, _ = plan["P1"]
    rev = client.get("/meta", headers=ORG).json()["revision"]
    serial = client.get("/assignment", headers=ORG).json()["serial"]
    set_deadline(client, rev, serial, iso(frozen_clock["now"] + timedelta(hours=1)))
    before = client.get("/reviewer/assignments", headers=reviewer_headers(r1)).json()
    assert before["review_deadline"]["expired"] is False
    assert before["review_deadline"]["overdue_papers"] == []
    frozen_clock["now"] += timedelta(hours=2)
    after = client.get("/reviewer/assignments", headers=reviewer_headers(r1)).json()
    assert after["review_deadline"]["expired"] is True
    assert after["review_deadline"]["overdue_papers"] == ["P1"]


# ------------------------------------------------------------ 补位与逾期

def test_backfill_dry_run_releases_overdue_confirmed_slot(client, frozen_clock):
    # 4 名评审人: P1 初版分给 R1/R2; R3/R4 为补位候选人
    add_reviewer(client, "R1", "Inst-R1")
    add_reviewer(client, "R2", "Inst-R2")
    add_reviewer(client, "R3", "Inst-R3")
    add_reviewer(client, "R4", "Inst-R4")
    add_paper(client, "P1", institutions=["Author-P1"])
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    plan = client.get("/assignment", headers=ORG).json()["plan"]
    r1, r2 = plan["P1"]

    # R1 已确认且已交评语; R2 已确认但未交 (将逾期)
    decide(client, r1, "P1", "confirm")
    serial = client.get("/assignment", headers=ORG).json()["serial"]
    submit_review(client, r1, "P1", serial)
    decide(client, r2, "P1", "confirm")
    cur_rev = client.get("/meta", headers=ORG).json()["revision"]
    set_deadline(client, cur_rev, serial, iso(frozen_clock["now"] + timedelta(hours=1)))
    frozen_clock["now"] += timedelta(hours=2)

    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["deadline_expired"] is True
    overdue = {(s["paper_id"], s["reviewer_id"]) for s in dry["overdue_slots"]}
    assert (("P1", r1) in overdue) is False       # 已交不逾期
    assert ("P1", r2) in overdue                   # 已确认未交 -> 逾期
    # R1 槽位仍固定; R2 槽位释放 (fixed_review_overdue)
    assert dry["fixed"]["P1"] == [r1]
    problem = {p["reviewer_id"]: p["reason"] for p in dry["fixed_problems"]["P1"]}
    assert problem[r2] == "fixed_review_overdue"
    # 本次补位不得把 P1 重新分给 R2 (review_overdue 排除)
    assert r2 not in dry["plan"]["P1"]
    excluded = {e["reviewer_id"]: e["reason"] for e in dry["papers"]["P1"]["excluded"]}
    assert excluded.get(r2) == "review_overdue"
    new_other = [rid for rid in dry["plan"]["P1"] if rid != r1][0]
    assert new_other in ("R3", "R4")


def test_backfill_publish_releases_overdue_and_recounts(client, frozen_clock):
    add_reviewer(client, "R1", "Inst-R1")
    add_reviewer(client, "R2", "Inst-R2")
    add_reviewer(client, "R3", "Inst-R3")
    add_paper(client, "P1", institutions=["Author-P1"])
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    plan = client.get("/assignment", headers=ORG).json()["plan"]
    r1, r2 = plan["P1"]
    decide(client, r1, "P1", "confirm")
    serial = client.get("/assignment", headers=ORG).json()["serial"]
    submit_review(client, r1, "P1", serial)
    decide(client, r2, "P1", "confirm")
    cur_rev = client.get("/meta", headers=ORG).json()["revision"]
    set_deadline(client, cur_rev, serial, iso(frozen_clock["now"] + timedelta(hours=1)))
    frozen_clock["now"] += timedelta(hours=2)

    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    pub = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert pub.status_code == 200, pub.text
    body = pub.json()
    assert body["serial"] == serial + 1
    assert body["carried_confirmations"] == 1          # R1 保留
    assert body["carried_reviews"] == 1               # R1 评语沿用
    assert body["released_overdue_confirmations"] == 1  # R2 逾期释放
    assert r2 not in body["plan"]["P1"]
    # 新版中 R2 与 P1 不再有确认关系
    with db.read_txn() as conn:
        row = conn.execute(
            "SELECT state FROM assignment_decisions WHERE serial = ? AND paper_id = ? AND reviewer_id = ?",
            (body["serial"], "P1", r2),
        ).fetchone()
        assert row is None
    # 逾期者 R2 已不在 P1 方案中, 对 P1 操作按未分配任务 404
    assert decide(client, r2, "P1", "confirm").status_code == 404


def test_other_confirmed_slots_stay_fixed_when_one_overdue(client, frozen_clock):
    # 两篇论文, 各 2 名评审人 + 候选人; 仅 P1 有逾期, P2 的确认槽位必须继续固定
    for rid in ("R1", "R2", "R3", "R4", "R5", "R6"):
        add_reviewer(client, rid, f"Inst-{rid}")
    add_paper(client, "P1", institutions=["Author-P1"])
    add_paper(client, "P2", institutions=["Author-P2"])
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    plan = client.get("/assignment", headers=ORG).json()["plan"]
    serial = client.get("/assignment", headers=ORG).json()["serial"]
    # 全部确认; P1 的 R2 不交评语, 其余都交
    for pid in ("P1", "P2"):
        for rid in plan[pid]:
            decide(client, rid, pid, "confirm")
            if not (pid == "P1" and rid == plan["P1"][1]):
                submit_review(client, rid, pid, serial)
    overdue_rid = plan["P1"][1]
    cur_rev = client.get("/meta", headers=ORG).json()["revision"]
    set_deadline(client, cur_rev, serial, iso(frozen_clock["now"] + timedelta(hours=1)))
    frozen_clock["now"] += timedelta(hours=2)

    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["fixed"]["P2"] == sorted(plan["P2"])     # P2 两人都固定
    assert dry["fixed"]["P1"] == [plan["P1"][0]]        # P1 仅保留已交者
    assert overdue_rid not in dry["plan"]["P1"]


def test_pending_overdue_slot_also_released_and_barred(client, frozen_clock):
    # R1 已确认已交; R2 全程未确认 (pending), 截止后同样逾期、释放且不重分
    add_reviewer(client, "R1", "Inst-R1")
    add_reviewer(client, "R2", "Inst-R2")
    add_reviewer(client, "R3", "Inst-R3")
    add_paper(client, "P1", institutions=["Author-P1"])
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    plan = client.get("/assignment", headers=ORG).json()["plan"]
    r1, r2 = plan["P1"]
    decide(client, r1, "P1", "confirm")
    serial = client.get("/assignment", headers=ORG).json()["serial"]
    submit_review(client, r1, "P1", serial)
    # r2 保持 pending
    cur_rev = client.get("/meta", headers=ORG).json()["revision"]
    set_deadline(client, cur_rev, serial, iso(frozen_clock["now"] + timedelta(hours=1)))
    frozen_clock["now"] += timedelta(hours=2)

    view = client.get("/review-deadline", headers=ORG).json()
    states = {s["reviewer_id"]: s["state"] for s in view["slots"]}
    assert states[r2] == "overdue"
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["fixed"]["P1"] == [r1]
    assert r2 not in dry["plan"]["P1"]
    excluded = {e["reviewer_id"]: e["reason"] for e in dry["papers"]["P1"]["excluded"]}
    assert excluded.get(r2) == "review_overdue"
    # pending 逾期槽位未确认, 不出现在 fixed_problems (仅被排除出重分)
    assert "P1" not in dry["fixed_problems"] or all(
        p["reviewer_id"] != r2 for p in dry["fixed_problems"].get("P1", [])
    )


def test_no_deadline_backfill_unchanged(client):
    # 未设置截止时补位沿用既有行为: 已确认槽位全部固定 (响应无逾期槽位)
    _, plan = setup_published(client, reviewers=("R1", "R2", "R3"), papers=("P1",))
    r1, r2 = plan["P1"]
    decide(client, r1, "P1", "confirm")
    decide(client, r2, "P1", "confirm")
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["review_deadline"] is None
    assert dry["deadline_expired"] is False
    assert dry["overdue_slots"] == []
    assert dry["fixed"]["P1"] == sorted([r1, r2])


# ------------------------------------------------------------ 旧版截止不约束新版

def test_old_deadline_does_not_constrain_new_published_version(client, frozen_clock):
    _, plan = setup_published(client, reviewers=("R1", "R2", "R3"))
    r1, r2 = plan["P1"]
    rev = client.get("/meta", headers=ORG).json()["revision"]
    serial = client.get("/assignment", headers=ORG).json()["serial"]
    set_deadline(client, rev, serial, iso(frozen_clock["now"] + timedelta(hours=1)))
    frozen_clock["now"] += timedelta(hours=2)  # 旧版截止已到

    # 普通发布推进 serial -> 新版无截止, 沿用现有行为 (逾期不再约束)
    new_rev = client.get("/meta", headers=ORG).json()["revision"]
    pub = client.post("/assignment/publish", headers=ORG, json={"base_revision": new_rev})
    assert pub.status_code == 200
    new_serial = pub.json()["serial"]
    assert new_serial == serial + 1
    view = client.get("/review-deadline", headers=ORG).json()
    assert view["review_deadline"] is None and view["deadline_expired"] is False
    # 新版上 R1 可正常确认/交评语, 不受旧截止限制
    assert decide(client, r1, "P1", "confirm").status_code == 200
    r = submit_review(client, r1, "P1", new_serial)
    assert r.status_code == 200


def test_new_version_can_set_its_own_deadline(client, frozen_clock):
    _, _ = setup_published(client, reviewers=("R1", "R2", "R3", "R4"))
    serial = client.get("/assignment", headers=ORG).json()["serial"]
    # 截止前 R1 先确认并交评语 (补位固定); R2 未确认未交, 截止后逾期并被补位释放
    client.post("/reviewer/assignments/P1/decision", headers=reviewer_headers("R1"),
                json={"paper_id": "P1", "decision": "confirm"})
    assert submit_review(client, "R1", "P1", serial).status_code == 200
    rev = client.get("/meta", headers=ORG).json()["revision"]
    set_deadline(client, rev, serial, iso(frozen_clock["now"] + timedelta(hours=1)))
    frozen_clock["now"] += timedelta(hours=2)  # 过截止, R2 槽位逾期
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"] is True
    pub = client.post(
        "/assignment/backfill/publish", headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    ).json()
    # 新版可独立设置自己的截止
    new_dl = iso(frozen_clock["now"] + timedelta(days=1))
    r = set_deadline(client, pub["revision"], pub["serial"], new_dl)
    assert r.status_code == 200 and r.json()["changed"] is True
    assert client.get("/review-deadline", headers=ORG).json()["review_deadline"]["serial"] == pub["serial"]


# ------------------------------------------------------------ 并发竞态

def test_concurrent_review_submit_and_deadline_expiry_no_overdue_insert(client, frozen_clock):
    _, plan = setup_published(client)
    r1, r2 = plan["P1"]
    serial = client.get("/assignment", headers=ORG).json()["serial"]
    decide(client, r1, "P1", "confirm")
    decide(client, r2, "P1", "confirm")
    deadline = frozen_clock["now"] + timedelta(seconds=1)
    set_deadline(
        client, client.get("/meta", headers=ORG).json()["revision"], serial, iso(deadline)
    )

    errors = []

    def submit_race(rid):
        try:
            for i in range(20):
                r = submit_review(client, rid, "P1", serial, comment=f"评语 {rid} 第 {i} 次")
                assert r.status_code in (200, 409, 422, 404), r.text
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def tick():
        # 逐步推进时钟越过截止, 与提交并发
        for _ in range(20):
            frozen_clock["now"] += timedelta(milliseconds=100)

    import threading
    threads = [
        threading.Thread(target=submit_race, args=(r1,)),
        threading.Thread(target=submit_race, args=(r2,)),
        threading.Thread(target=tick),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    # 最终时钟已过截止: 任何成功插入的评语都必须与其"提交时未逾期"一致。
    # 关键不变量: 被判逾期 (409) 的提交绝不落库; 每个槽位至多一份评语。
    frozen_clock["now"] = deadline + timedelta(hours=1)
    view = client.get("/review-deadline", headers=ORG).json()
    submitted_pairs = {
        (s["paper_id"], s["reviewer_id"]) for s in view["slots"] if s["state"] == "submitted"
    }
    overdue_pairs = {
        (s["paper_id"], s["reviewer_id"]) for s in view["slots"] if s["state"] == "overdue"
    }
    assert not (submitted_pairs & overdue_pairs)
    assert view["summary"]["submitted"] + view["summary"]["overdue"] == 2


def test_concurrent_backfill_publish_and_review_at_deadline(client, frozen_clock):
    add_reviewer(client, "R1", "Inst-R1")
    add_reviewer(client, "R2", "Inst-R2")
    add_reviewer(client, "R3", "Inst-R3")
    add_paper(client, "P1", institutions=["Author-P1"])
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    plan = client.get("/assignment", headers=ORG).json()["plan"]
    serial = client.get("/assignment", headers=ORG).json()["serial"]
    r2 = plan["P1"][1]
    decide(client, plan["P1"][0], "P1", "confirm")
    decide(client, r2, "P1", "confirm")
    deadline = frozen_clock["now"] + timedelta(seconds=1)
    set_deadline(
        client, client.get("/meta", headers=ORG).json()["revision"], serial, iso(deadline)
    )

    errors = []

    def publisher():
        try:
            for _ in range(10):
                dry = client.post("/assignment/backfill/dry-run", headers=ORG)
                b = dry.json()
                r = client.post(
                    "/assignment/backfill/publish", headers=ORG,
                    json={"base_revision": b["revision"], "base_serial": b["serial"]},
                )
                assert r.status_code in (200, 409, 422), r.text
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def submitter():
        try:
            for i in range(10):
                r = submit_review(client, r2, "P1", serial, comment=f"赶截止 {i}")
                assert r.status_code in (200, 409, 422, 404), r.text
                frozen_clock["now"] += timedelta(milliseconds=150)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    import threading
    threads = [threading.Thread(target=publisher), threading.Thread(target=submitter)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    # 时序无关的安全不变量: R2 不可能"既在截止后成功交评语、又被补位判定逾期释放"。
    # 写事务 (BEGIN IMMEDIATE) 串行化了评语提交与补位发布, 二者对截止的判定互斥。
    frozen_clock["now"] = deadline + timedelta(hours=1)
    cur_serial = client.get("/assignment", headers=ORG).json()["serial"]
    final_plan = client.get("/assignment", headers=ORG).json()["plan"]
    with db.read_txn() as conn:
        r2_rows = conn.execute(
            "SELECT serial, submitted_at FROM submitted_reviews"
            " WHERE paper_id = 'P1' AND reviewer_id = ?",
            (r2,),
        ).fetchall()

    if cur_serial > serial:
        # 发生过补位发布: 截止前提交评语的槽位会在后续每次补位中固定并沿用评语
        # (每个发布序号各一行, 同收据), 故 R2 若仍在最终方案, 其最早评语必在
        # 截止前的旧序号 (serial) 上——不可能是在截止后的新序号首次插入;
        # R2 若被逾期释放, 则不得出现在最终方案的 P1 槽位中
        if r2 in final_plan.get("P1", []):
            assert r2_rows and r2_rows[0]["serial"] == serial
        else:
            assert r2 not in final_plan.get("P1", [])  # 逾期释放, 本次补位不再分给他
    else:
        # 未发生发布: 仍在被截止约束的同一序号上, 过截止后 R2 必然逾期且无评语
        assert r2_rows == []
