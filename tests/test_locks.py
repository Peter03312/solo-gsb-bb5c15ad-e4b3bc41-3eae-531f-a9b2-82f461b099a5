"""本轮功能测试: 普通分配前的评审人锁定表。

覆盖需求:
- 会务方整表提交锁定: 每篇论文 0~2 名评审人, 携带所见资料修订号; 空表清除锁定;
- 未知论文/评审人、重复锁定拒绝且不改表; 相同锁定表重试幂等 (不推进修订号);
- 过期修订号拒绝; 有效变更推进修订号; 写入返回修订号与是否变更;
- 普通预演与发布保留锁定槽位, 其余位置遵守机构冲突/专长/回避/资格/容量约束
  与既有优化次序; 单人锁定时专长可由另一人满足;
- 锁定人被删除/停用/回避或因资料及机构归并失格时: 预演说明具体冲突并标为
  不可完整分配, 发布拒绝且不改变当前发布版, 不悄悄释放锁定;
- 锁定累计超过容量 -> lock_assignments_exceed_capacity;
- 锁定仅约束普通分配: 补位预演/发布不读锁定表, 确认槽位规则不变;
- 删论文级联清除其锁定; 会务方可查看当前锁定表。
"""
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="review-locks-test-")
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
            "assignment_decisions", "reviewer_recusals", "assignment_locks",
            "submitted_reviews", "feedback_snapshots", "review_corrections",
            "reviewer_status_changes", "institution_merges", "paper_guarantee_levels", "review_deadlines",
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


def rev(client):
    return client.get("/meta", headers=ORG).json()["revision"]


def set_locks(client, locks, base_revision=None):
    if base_revision is None:
        base_revision = rev(client)
    return client.post(
        "/assignment/locks",
        headers=ORG,
        json={"base_revision": base_revision, "locks": locks},
    )


def setup(client, reviewers=("R1", "R2", "R3", "R4"), papers=("P1",)):
    for rid in reviewers:
        assert add_reviewer(client, rid).status_code == 201
    for pid in papers:
        assert add_paper(client, pid).status_code == 201


# ------------------------------------------------------------ 提交校验

def test_organizer_key_required_for_locks(client):
    assert client.get("/assignment/locks").status_code == 401
    assert client.post("/assignment/locks", json={"base_revision": 0, "locks": {}}).status_code == 401


def test_empty_lock_table_is_idempotent_and_returns_zero_counts(client):
    setup(client)
    r = set_locks(client, {})
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is False
    assert body["revision"] == rev(client)
    assert body["locks"] == {}
    assert body["locked_papers"] == 0
    assert body["locked_slots"] == 0


def test_lock_table_get_reflects_submission(client):
    setup(client)
    r = set_locks(client, {"P1": ["R2", "R1"]})
    assert r.status_code == 200
    assert r.json()["changed"] is True
    # 每篇内部按评审人编号字典序归一化
    assert r.json()["locks"] == {"P1": ["R1", "R2"]}
    g = client.get("/assignment/locks", headers=ORG).json()
    assert g["locks"] == {"P1": ["R1", "R2"]}
    assert g["locked_papers"] == 1
    assert g["locked_slots"] == 2


def test_identical_lock_table_retry_is_idempotent(client):
    setup(client)
    r1 = set_locks(client, {"P1": ["R1"]}).json()
    assert r1["changed"] is True
    base = rev(client)
    r2 = set_locks(client, {"P1": ["R1"]}, base_revision=base)
    assert r2.json()["changed"] is False
    assert r2.json()["revision"] == base
    # 首尾空白归一化后与当前表相同也视为幂等重试
    r3 = set_locks(client, {" P1 ": [" R1 "]}, base_revision=base)
    assert r3.status_code == 200
    assert r3.json()["changed"] is False
    assert rev(client) == base


def test_stale_revision_rejected_without_changing_table(client):
    setup(client)
    stale = rev(client)
    # 期间发生资料变更
    add_paper(client, "P2")
    r = set_locks(client, {"P1": ["R1"]}, base_revision=stale)
    assert r.status_code == 409
    g = client.get("/assignment/locks", headers=ORG).json()
    assert g["locks"] == {}


def test_unknown_paper_or_reviewer_rejected_without_changing_table(client):
    setup(client)
    set_locks(client, {"P1": ["R1"]})
    before = client.get("/assignment/locks", headers=ORG).json()["locks"]
    r = set_locks(client, {"P1": ["R1"], "GHOST": ["R2"]})
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["unknown_papers"] == ["GHOST"]
    assert client.get("/assignment/locks", headers=ORG).json()["locks"] == before

    r = set_locks(client, {"P1": ["R1", "RX"]})
    assert r.status_code == 404
    assert r.json()["detail"]["unknown_reviewers"] == ["RX"]
    assert client.get("/assignment/locks", headers=ORG).json()["locks"] == before


def test_duplicate_and_oversized_locks_rejected_422(client):
    setup(client, reviewers=("R1", "R2", "R3", "R4", "R5"))
    r = set_locks(client, {"P1": ["R1", "R1"]})
    assert r.status_code == 422
    assert r.json()["detail"]["duplicate_reviewer_locks"] == ["P1"]
    r = set_locks(client, {"P1": ["R1", "R2", "R3"]})
    assert r.status_code == 422
    assert r.json()["detail"]["more_than_two_locks"] == ["P1"]
    # 被拒不落表
    assert client.get("/assignment/locks", headers=ORG).json()["locks"] == {}


def test_blank_ids_rejected_422(client):
    setup(client)
    r = set_locks(client, {"P1": ["   "]})
    assert r.status_code == 422
    r = set_locks(client, {"  ": ["R1"]})
    assert r.status_code == 422


def test_effective_change_bumps_revision_and_full_replace_semantics(client):
    setup(client, papers=("P1", "P2"))
    r1 = set_locks(client, {"P1": ["R1"], "P2": ["R2"]}).json()
    assert r1["changed"] is True
    assert r1["revision"] == rev(client)
    # 整表替换: 新表不含 P2 -> P2 锁定被清除
    r2 = set_locks(client, {"P1": ["R1", "R2"]}).json()
    assert r2["changed"] is True
    assert set(r2["locks"]) == {"P1"}
    # 空表清除全部锁定 (有效变更, 推进修订号)
    r3 = set_locks(client, {}).json()
    assert r3["changed"] is True
    assert r3["locks"] == {}
    # 再次空表为幂等
    assert set_locks(client, {}).json()["changed"] is False


# ------------------------------------------------------------ 普通预演/发布保留槽位

def test_dry_run_and_publish_preserve_locked_slots(client):
    setup(client, papers=("P1",))
    set_locks(client, {"P1": ["R4"]})
    dry = client.post("/assignment/dry-run", headers=ORG).json()
    assert dry["feasible"] is True
    assert "R4" in dry["plan"]["P1"]  # 锁定者保留
    assert dry["locks"]["P1"] == ["R4"]
    assert dry["papers"]["P1"]["locks"] == ["R4"]

    r = client.post(
        "/assignment/publish",
        headers=ORG,
        json={"base_revision": rev(client)},
    )
    assert r.status_code == 200
    assert "R4" in r.json()["plan"]["P1"]


def test_two_locked_reviewers_are_exactly_assigned(client):
    setup(client, papers=("P1",))
    set_locks(client, {"P1": ["R4", "R2"]})
    r = client.post("/assignment/dry-run", headers=ORG).json()
    assert r["feasible"] is True
    assert r["plan"]["P1"] == ["R2", "R4"]  # 槽位仍按评审人编号字典序输出


def test_single_locked_nonexpert_expertise_satisfied_by_partner(client):
    # R1 不擅长, R2/R3 擅长; 锁定不擅长的 R1 -> 搭档必须是擅长者
    add_reviewer(client, "R1", topics=["DB"])
    add_reviewer(client, "R2", topics=["AI"])
    add_reviewer(client, "R3", topics=["AI"])
    add_reviewer(client, "R4", topics=["DB"])
    add_paper(client, "P1", topics=["AI"])
    set_locks(client, {"P1": ["R1"]})
    r = client.post("/assignment/dry-run", headers=ORG).json()
    assert r["feasible"] is True
    pair = r["plan"]["P1"]
    assert pair[0] == "R1"
    assert pair[1] in ("R2", "R3")  # 不会选不擅长的 R4


def test_locked_pair_both_nonexpert_is_infeasible(client):
    # 锁两人且都不擅长 -> 锁定槽位冲突, 不可完整分配, 不替换
    add_reviewer(client, "R1", topics=["DB"])
    add_reviewer(client, "R2", topics=["DB"])
    add_reviewer(client, "R3", topics=["AI"])
    add_paper(client, "P1", topics=["AI"])
    set_locks(client, {"P1": ["R1", "R2"]})
    r = client.post("/assignment/dry-run", headers=ORG).json()
    assert r["feasible"] is False
    assert r["unassigned"] == ["P1"]
    diag = r["diagnostics"]["P1"]
    assert "lock_pair_violates_expert_rule" in diag["reasons"]
    assert diag["lock_problems"][0]["reason"] == "lock_pair_violates_expert_rule"
    # 强行发布 -> 422 且不产生发布版
    pub = client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": rev(client)}
    )
    assert pub.status_code == 422
    assert client.get("/assignment", headers=ORG).status_code == 404


def test_lock_partner_must_obey_institution_rule(client):
    # R1 与 R2 同机构, 锁定 R1 时 R2 不得作为搭档 (剩余位置仍守机构冲突)
    add_reviewer(client, "R1", institution="Same-Inst")
    add_reviewer(client, "R2", institution="Same-Inst")
    add_reviewer(client, "R3", institution="Inst-C")
    add_paper(client, "P1")
    set_locks(client, {"P1": ["R1"]})
    r = client.post("/assignment/dry-run", headers=ORG).json()
    assert r["plan"]["P1"] == ["R1", "R3"]


def test_locked_reviewer_same_institution_as_author_blocks_paper(client):
    add_reviewer(client, "R1", institution="Univ-X")
    add_reviewer(client, "R2", institution="Inst-B")
    add_paper(client, "P1", institutions=["Univ-X"])
    set_locks(client, {"P1": ["R1"]})
    r = client.post("/assignment/dry-run", headers=ORG).json()
    assert r["feasible"] is False
    reasons = r["diagnostics"]["P1"]["reasons"]
    assert "lock_same_institution_as_author" in reasons


def test_locked_reviewer_on_avoid_list_blocks_paper(client):
    add_reviewer(client, "R1", avoid=["P1"])
    add_reviewer(client, "R2")
    add_paper(client, "P1")
    set_locks(client, {"P1": ["R1"]})
    r = client.post("/assignment/dry-run", headers=ORG).json()
    assert r["feasible"] is False
    assert "lock_on_reviewer_avoid_list" in r["diagnostics"]["P1"]["reasons"]


# ------------------------------------------------------------ 锁定后失格: 只报错不释放

def test_locked_reviewer_deleted_blocks_publish_without_releasing(client):
    setup(client, papers=("P1",))
    # 先发布一个正常版本, 验证锁定失效后发布会拒绝且不改变当前发布版
    client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": rev(client)}
    )
    set_locks(client, {"P1": ["R4"]})
    assert client.delete("/reviewers/R4", headers=ORG).status_code == 200
    dry = client.post("/assignment/dry-run", headers=ORG).json()
    assert dry["feasible"] is False
    problems = dry["diagnostics"]["P1"]["lock_problems"]
    assert any(p["reason"] == "lock_reviewer_not_found" for p in problems)
    # 发布拒绝 422, 当前发布版仍为旧版 (serial 1, 方案不含 R4)
    pub = client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": rev(client)}
    )
    assert pub.status_code == 422
    current = client.get("/assignment", headers=ORG).json()
    assert current["serial"] == 1
    assert "R4" not in current["plan"]["P1"]
    # 锁定表仍在 (未被悄悄释放)
    assert client.get("/assignment/locks", headers=ORG).json()["locks"] == {"P1": ["R4"]}
    # 清除失效锁定后恢复可发布
    set_locks(client, {})
    assert client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": rev(client)}
    ).status_code == 200


def test_locked_reviewer_disabled_blocks_paper(client):
    setup(client, papers=("P1",))
    set_locks(client, {"P1": ["R1"]})
    r0 = rev(client)
    client.post(
        "/reviewers/R1/status",
        headers=ORG,
        json={"reviewer_id": "R1", "base_revision": r0, "active": False, "reason": "休假"},
    )
    dry = client.post("/assignment/dry-run", headers=ORG).json()
    assert dry["feasible"] is False
    assert "lock_reviewer_disabled" in dry["diagnostics"]["P1"]["reasons"]
    assert client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": rev(client)}
    ).status_code == 422


def test_locked_reviewer_hard_recusal_after_publish_blocks_next_publish(client):
    # 先发布 (R1 在方案内) -> R1 对 P1 声明回避形成硬回避 -> 再锁定 R1 预演/发布
    setup(client, papers=("P1",))
    set_locks(client, {"P1": ["R1"]})
    client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": rev(client)}
    )
    d = client.post(
        "/reviewer/assignments/P1/decision",
        headers=reviewer_headers("R1"),
        json={"paper_id": "P1", "decision": "recuse", "reason": "利益冲突"},
    )
    assert d.status_code == 200
    dry = client.post("/assignment/dry-run", headers=ORG).json()
    assert dry["feasible"] is False
    assert "lock_hard_recusal_after_decline" in dry["diagnostics"]["P1"]["reasons"]
    assert client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": rev(client)}
    ).status_code == 422


def test_lock_invalidated_by_institution_merge(client):
    # 锁定 R1; R1 机构与作者机构归并后同组 -> 锁定槽位失效 (via_merge)
    add_reviewer(client, "R1", institution="Acme-U")
    add_reviewer(client, "R2", institution="Inst-B")
    add_paper(client, "P1", institutions=["Acme University"])
    set_locks(client, {"P1": ["R1"]})
    before = client.post("/assignment/dry-run", headers=ORG).json()
    assert before["feasible"] is True  # 归并前原名逐字不同, R1 合格
    r = client.post(
        "/institutions/merge-groups",
        headers=ORG,
        json={"name_a": "Acme-U", "name_b": "Acme University", "base_revision": rev(client)},
    )
    assert r.status_code == 200
    after = client.post("/assignment/dry-run", headers=ORG).json()
    assert after["feasible"] is False
    assert (
        "lock_same_institution_as_author_via_merge"
        in after["diagnostics"]["P1"]["reasons"]
    )


def test_locked_pair_invalidated_by_merge_of_reviewer_institutions(client):
    add_reviewer(client, "R1", institution="Acme-U")
    add_reviewer(client, "R2", institution="Acme University")
    add_paper(client, "P1", institutions=["Author-Inst"])
    set_locks(client, {"P1": ["R1", "R2"]})
    client.post(
        "/institutions/merge-groups",
        headers=ORG,
        json={"name_a": "Acme-U", "name_b": "Acme University", "base_revision": rev(client)},
    )
    r = client.post("/assignment/dry-run", headers=ORG).json()
    assert r["feasible"] is False
    reasons = r["diagnostics"]["P1"]["reasons"]
    assert "lock_pair_violates_institution_rule_via_merge" in reasons


def test_lock_assignments_exceed_reviewer_capacity(client):
    # R1 容量 1, 在两篇论文上都锁定 R1 -> 两篇均不可完整分配, 不释放任何锁定
    add_reviewer(client, "R1", capacity=1)
    add_reviewer(client, "R2")
    add_reviewer(client, "R3")
    add_paper(client, "P1")
    add_paper(client, "P2")
    set_locks(client, {"P1": ["R1"], "P2": ["R1"]})
    r = client.post("/assignment/dry-run", headers=ORG).json()
    assert r["feasible"] is False
    assert sorted(r["unassigned"]) == ["P1", "P2"]
    for pid in ("P1", "P2"):
        assert (
            "lock_assignments_exceed_capacity"
            in r["diagnostics"][pid]["reasons"]
        )
        assert r["diagnostics"][pid]["locks"] == ["R1"]


def test_capacity_within_lock_limit_still_blocks_when_slots_cannot_fit(client):
    # R2 容量 1, 锁定一篇; 另一篇无锁论文也需要人但容量受限 -> 部分方案诊断
    add_reviewer(client, "R1", capacity=1)
    add_reviewer(client, "R2", capacity=1)
    add_reviewer(client, "R3")
    add_paper(client, "P1")
    add_paper(client, "P2")
    set_locks(client, {"P1": ["R1"]})
    r = client.post("/assignment/dry-run", headers=ORG).json()
    # R1 锁 P1 (容量 1 已占满), P2 只能从 R2/R3 中选 -> 仍可行
    assert r["feasible"] is True
    assert set(r["plan"]["P1"]) == {"R1", "R3"}
    assert "R1" not in r["plan"]["P2"]


# ------------------------------------------------------------ 锁定不影响补位

def test_backfill_ignores_lock_table(client):
    setup(client, papers=("P1",))
    # 先在无锁定时发布, 再提交锁定: 补位基于当前发布版的确认槽位, 不读锁定表
    client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": rev(client)}
    )
    set_locks(client, {"P1": ["R4"]})
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    # 无确认关系 -> 全部重算, 方案不受 R4 锁定影响 (字典序最优不含 R4)
    assert "fixed" in dry
    assert "locks" not in dry
    plan_pair = dry["plan"]["P1"]
    assert "R4" not in plan_pair


def test_backfill_publish_ignores_locks_and_keeps_confirmation_rules(client):
    setup(client, papers=("P1",))
    pub = client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": rev(client)}
    ).json()
    rid = pub["plan"]["P1"][0]
    client.post(
        f"/reviewer/assignments/P1/decision",
        headers=reviewer_headers(rid),
        json={"paper_id": "P1", "decision": "confirm"},
    )
    # 提交一个与当前发布版无关的锁定表 (锁另一个人), 补位仍只固定已确认槽位
    other = next(r for r in ("R1", "R2", "R3", "R4") if r not in pub["plan"]["P1"])
    set_locks(client, {"P1": [other]})
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["fixed"]["P1"] == [rid]
    assert other not in dry["plan"]["P1"]
    bp = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": rev(client), "base_serial": dry["serial"]},
    )
    assert bp.status_code == 200
    assert bp.json()["carried_confirmations"] == 1


# ------------------------------------------------------------ 其他

def test_deleting_paper_clears_its_lock(client):
    setup(client, papers=("P1", "P2"))
    set_locks(client, {"P1": ["R1"], "P2": ["R2"]})
    client.delete("/papers/P1", headers=ORG)
    locks = client.get("/assignment/locks", headers=ORG).json()["locks"]
    assert locks == {"P2": ["R2"]}


def test_unlocked_papers_keep_ordinary_optimization_order(client):
    # 无锁定时锁定求解路径不启用, 响应形态与原有求解器一致 (无 locks/lock_problems)
    setup(client, papers=("P1",))
    r = client.post("/assignment/dry-run", headers=ORG).json()
    assert r["feasible"] is True
    assert "locks" not in r
    assert "lock_problems" not in r
    assert set(r["plan"]["P1"]) == {"R1", "R2"}


def test_locked_load_counts_in_capacity_ratio_optimization(client):
    # 锁定负载计入容量: R4 容量 2 锁 P1; 共 3 篇论文, 其余 3 人均摊后最高比例为 2/2=1,
    # R4 已占 1/2, 不应再被分到无锁论文 (否则其比例仍 1 但字典序更差的方案不会被选中);
    # 验证 R4 只出现在被锁定的 P1
    add_reviewer(client, "R1", capacity=2)
    add_reviewer(client, "R2", capacity=2)
    add_reviewer(client, "R3", capacity=2)
    add_reviewer(client, "R4", capacity=2)
    for pid in ("P1", "P2", "P3"):
        add_paper(client, pid)
    set_locks(client, {"P1": ["R4"]})
    r = client.post("/assignment/dry-run", headers=ORG).json()
    assert r["feasible"] is True
    assert "R4" in r["plan"]["P1"]
    assert "R4" not in r["plan"]["P2"]
    assert "R4" not in r["plan"]["P3"]
    # 优化次序不变: 先最低最高容量比例, 再全局字典序
    assert r["max_used_capacity_ratio"] == "1"


def test_lock_with_papers_having_no_topics_allows_nonexpert_partner(client):
    # 论文无主题时专长视为均满足: 锁定两人即使都无专长标签也合法
    add_reviewer(client, "R1", topics=[])
    add_reviewer(client, "R2", topics=[])
    add_paper(client, "P1", topics=[])
    set_locks(client, {"P1": ["R1", "R2"]})
    r = client.post("/assignment/dry-run", headers=ORG).json()
    assert r["feasible"] is True
    assert r["plan"]["P1"] == ["R1", "R2"]


def test_single_locked_nonexpert_without_expert_partner_is_infeasible(client):
    # 锁定不擅长的 R1, 而其他合格者也都不擅长 -> 无含锁定者的合法对
    add_reviewer(client, "R1", topics=["DB"])
    add_reviewer(client, "R2", topics=["DB"])
    add_paper(client, "P1", topics=["AI"])
    set_locks(client, {"P1": ["R1"]})
    r = client.post("/assignment/dry-run", headers=ORG).json()
    assert r["feasible"] is False
    assert "no_valid_pair_containing_lock_reviewer" in r["diagnostics"]["P1"]["reasons"]


def test_two_papers_one_lock_invalid_does_not_release_other_paper(client):
    # P1 锁定有效, P2 锁定失效 (先锁后删): P1 仍正常分配, P2 标为不可完整分配且锁不释放
    setup(client, papers=("P1", "P2"))
    assert set_locks(client, {"P1": ["R1"], "P2": ["R4"]}).status_code == 200
    client.delete("/reviewers/R4", headers=ORG)
    r = client.post("/assignment/dry-run", headers=ORG).json()
    assert r["feasible"] is False
    assert r["unassigned"] == ["P2"]
    assert r["plan"]["P1"] == ["R1", "R2"]
    assert "lock_reviewer_not_found" in r["diagnostics"]["P2"]["reasons"]


def test_publish_with_locked_partial_keeps_published_version_unchanged(client):
    # 多篇论文: 一篇锁定失效, 其他论文仍给出最大部分方案; 发布拒绝, 已发布版不变
    setup(client, reviewers=("R1", "R2", "R3", "R4"), papers=("P1", "P2"))
    client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": rev(client)}
    )
    set_locks(client, {"P1": ["R4"], "P2": ["R4"]})
    client.delete("/reviewers/R4", headers=ORG)
    dry = client.post("/assignment/dry-run", headers=ORG).json()
    assert dry["feasible"] is False
    assert dry["unassigned"] == ["P1", "P2"]
    for pid, diag in dry["diagnostics"].items():
        assert "lock_reviewer_not_found" in diag["reasons"]
    pub = client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": rev(client)}
    )
    assert pub.status_code == 422
    assert client.get("/assignment", headers=ORG).json()["serial"] == 1
