"""本轮功能测试: 论文评审保障等级 (高/中/普通)。

覆盖需求:
- 会务方凭密钥按论文设置等级 (high/medium/normal, 未设置视为普通), 提交论文编号、
  等级与当前资料修订号; 可查询各论文等级与修订号;
- 相同等级重试幂等 (changed=false, 不推进修订号); 有效变更推进修订号;
  版本不符 409, 未知或已撤回论文 404, 非法等级 422, 均拒绝且不留部分变更;
- 普通分配/补位无法完整覆盖目标论文时: 先使完整分配总数最大, 再依次使高、中等级
  完整分配数最大, 最后沿用既有字典序; 完整可行时仍按容量比例与字典序优化;
- 普通分配锁定与补位已确认槽位仍按既有约束处理, 失效锁定不被等级优先级释放;
- 预演返回各等级已分配/未分配数量 (level_summary) 及原有诊断;
  无法完整分配仍拒绝发布, 当前发布版不变;
- 删除或撤回论文后, 其等级随论文清除, 不再参与求解。
"""
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="review-levels-test-")
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
            "reviewer_status_changes", "institution_merges", "feedback_objections",
            "paper_withdrawals", "paper_guarantee_levels", "review_deadlines",
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


def set_level(client, pid, level, base_revision=None):
    if base_revision is None:
        base_revision = rev(client)
    return client.post(
        f"/papers/{pid}/guarantee-level",
        headers=ORG,
        json={"paper_id": pid, "level": level, "base_revision": base_revision},
    )


def get_levels(client):
    return client.get("/papers/guarantee-levels", headers=ORG).json()


def setup(client, reviewers=("R1", "R2", "R3", "R4"), papers=("P1", "P2"), capacity=3):
    for rid in reviewers:
        assert add_reviewer(client, rid, capacity=capacity).status_code == 201
    for pid in papers:
        assert add_paper(client, pid).status_code == 201


# ------------------------------------------------------------ 设置与查询

def test_organizer_key_required_for_levels(client):
    assert client.get("/papers/guarantee-levels").status_code == 401
    assert client.post(
        "/papers/P1/guarantee-level",
        json={"paper_id": "P1", "level": "high", "base_revision": 0},
    ).status_code == 401
    r = client.get("/papers/guarantee-levels", headers={"X-Organizer-Key": "wrong"})
    assert r.status_code == 401


def test_default_level_is_normal_and_query_reflects_settings(client):
    setup(client)
    g = get_levels(client)
    assert g["revision"] == rev(client)
    assert g["levels"] == {"P1": "normal", "P2": "normal"}
    assert g["summary"] == {"high": 0, "medium": 0, "normal": 2}

    r = set_level(client, "P1", "high")
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is True
    assert body["level"] == "high"
    assert body["revision"] == rev(client)
    g = get_levels(client)
    assert g["levels"] == {"P1": "high", "P2": "normal"}
    assert g["summary"] == {"high": 1, "medium": 0, "normal": 1}


def test_same_level_retry_is_idempotent(client):
    setup(client)
    r1 = set_level(client, "P1", "medium").json()
    assert r1["changed"] is True
    base = rev(client)
    r2 = set_level(client, "P1", "medium", base_revision=base)
    assert r2.status_code == 200
    assert r2.json()["changed"] is False
    assert r2.json()["revision"] == base
    assert rev(client) == base
    # 未设置即为普通: 对普通论文设置 normal 也是幂等 (不落行、不推进修订号)
    r3 = set_level(client, "P2", "normal", base_revision=base)
    assert r3.json()["changed"] is False
    assert rev(client) == base


def test_effective_changes_bump_revision_and_normal_clears_row(client):
    setup(client)
    assert set_level(client, "P1", "high").json()["changed"] is True
    assert set_level(client, "P1", "medium").json()["changed"] is True
    assert get_levels(client)["levels"]["P1"] == "medium"
    # 改回普通: 有效变更, 清除等级行
    r = set_level(client, "P1", "normal").json()
    assert r["changed"] is True
    assert get_levels(client)["levels"]["P1"] == "normal"
    # 再次设置为普通: 幂等
    assert set_level(client, "P1", "normal").json()["changed"] is False


def test_stale_revision_rejected_without_change(client):
    setup(client)
    set_level(client, "P1", "high")
    stale = rev(client)
    add_paper(client, "P3")  # 期间发生资料变更
    r = set_level(client, "P1", "medium", base_revision=stale)
    assert r.status_code == 409
    assert get_levels(client)["levels"]["P1"] == "high"


def test_unknown_paper_rejected_404(client):
    setup(client)
    r = set_level(client, "GHOST", "high")
    assert r.status_code == 404
    assert rev(client) == get_levels(client)["revision"]


def test_illegal_level_rejected_422_without_change(client):
    setup(client)
    before = rev(client)
    for bad in ("urgent", "HIGH", "低", ""):
        r = set_level(client, "P1", bad)
        assert r.status_code == 422, bad
    assert rev(client) == before
    assert get_levels(client)["levels"]["P1"] == "normal"


def test_path_body_mismatch_rejected_422(client):
    setup(client)
    r = client.post(
        "/papers/P1/guarantee-level",
        headers=ORG,
        json={"paper_id": "P2", "level": "high", "base_revision": rev(client)},
    )
    assert r.status_code == 422
    assert get_levels(client)["levels"]["P1"] == "normal"


def test_deleted_paper_level_is_cleared(client):
    setup(client)
    set_level(client, "P1", "high")
    client.delete("/papers/P1", headers=ORG)
    g = get_levels(client)
    assert "P1" not in g["levels"]
    assert g["summary"] == {"high": 0, "medium": 0, "normal": 1}
    # 同编号重新录入后等级不残留 (恢复默认普通)
    add_paper(client, "P1")
    assert get_levels(client)["levels"]["P1"] == "normal"


def test_withdrawn_paper_level_is_cleared_and_not_settable(client):
    setup(client)
    set_level(client, "P1", "high")
    r = client.post(
        "/papers/P1/withdrawal",
        headers=ORG,
        json={"paper_id": "P1", "base_revision": rev(client), "reason": "作者撤稿"},
    )
    assert r.status_code == 200
    g = get_levels(client)
    assert "P1" not in g["levels"]  # 撤回稿等级不再参与求解, 也不出现在查询中
    # 已撤回论文不可再设置等级 (404), 不留部分变更
    r = set_level(client, "P1", "medium")
    assert r.status_code == 404
    assert "P1" not in get_levels(client)["levels"]


# ------------------------------------------------------------ 求解优先级 (普通分配)

def test_high_level_prioritized_over_normal_when_partial(client):
    # 仅两名容量 1 的评审人: 只能完整覆盖一篇; 高等级论文优先于字典序
    add_reviewer(client, "R1", capacity=1)
    add_reviewer(client, "R2", capacity=1)
    add_paper(client, "P1")
    add_paper(client, "P2")
    dry = client.post("/assignment/dry-run", headers=ORG).json()
    assert dry["feasible"] is False
    assert dry["plan"] == {"P1": ["R1", "R2"]}  # 无等级时沿用字典序
    assert dry["unassigned"] == ["P2"]

    set_level(client, "P2", "high")
    dry = client.post("/assignment/dry-run", headers=ORG).json()
    assert dry["feasible"] is False
    assert dry["plan"] == {"P2": ["R1", "R2"]}  # 高等级优先
    assert dry["unassigned"] == ["P1"]
    assert dry["level_summary"] == {
        "high": {"assigned": 1, "unassigned": 0},
        "medium": {"assigned": 0, "unassigned": 0},
        "normal": {"assigned": 0, "unassigned": 1},
    }
    # 原有诊断仍在
    assert "P1" in dry["diagnostics"]


def test_total_count_dominates_level_priority(client):
    # P1(高) 只能与 R1,R2 配对; 放弃 P1 可完整覆盖两篇普通论文 -> 总数优先
    for rid in ("R1", "R2", "R3", "R4"):
        add_reviewer(client, rid, institution=f"Inst-{rid}", capacity=1)
    add_paper(client, "P1", institutions=["Inst-R3", "Inst-R4"])  # 仅 R1,R2 合格
    add_paper(client, "P2", institutions=["Inst-R2", "Inst-R4"])  # 仅 R1,R3 合格
    add_paper(client, "P3", institutions=["Inst-R1", "Inst-R3"])  # 仅 R2,R4 合格
    set_level(client, "P1", "high")
    dry = client.post("/assignment/dry-run", headers=ORG).json()
    assert dry["feasible"] is False
    assert dry["plan"] == {"P2": ["R1", "R3"], "P3": ["R2", "R4"]}
    assert dry["unassigned"] == ["P1"]
    assert dry["level_summary"]["high"] == {"assigned": 0, "unassigned": 1}
    assert dry["level_summary"]["normal"] == {"assigned": 2, "unassigned": 0}


def test_medium_prioritized_over_normal_and_high_over_medium(client):
    add_reviewer(client, "R1", capacity=1)
    add_reviewer(client, "R2", capacity=1)
    add_paper(client, "P1")
    add_paper(client, "P2")
    set_level(client, "P2", "medium")
    dry = client.post("/assignment/dry-run", headers=ORG).json()
    assert dry["plan"] == {"P2": ["R1", "R2"]}
    assert dry["level_summary"]["medium"] == {"assigned": 1, "unassigned": 0}
    assert dry["level_summary"]["normal"] == {"assigned": 0, "unassigned": 1}
    # 高 > 中: P1 改为高等级后翻转
    set_level(client, "P1", "high")
    dry = client.post("/assignment/dry-run", headers=ORG).json()
    assert dry["plan"] == {"P1": ["R1", "R2"]}
    assert dry["level_summary"]["high"] == {"assigned": 1, "unassigned": 0}
    assert dry["level_summary"]["medium"] == {"assigned": 0, "unassigned": 1}


def test_lexicographic_tiebreak_within_same_level(client):
    # 两篇同为高等级, 只能覆盖一篇 -> 沿用既有字典序 (编号小者优先)
    add_reviewer(client, "R1", capacity=1)
    add_reviewer(client, "R2", capacity=1)
    add_paper(client, "P1")
    add_paper(client, "P2")
    set_level(client, "P1", "high")
    set_level(client, "P2", "high")
    dry = client.post("/assignment/dry-run", headers=ORG).json()
    assert dry["plan"] == {"P1": ["R1", "R2"]}
    assert dry["level_summary"]["high"] == {"assigned": 1, "unassigned": 1}


def test_feasible_assignment_ignores_levels(client):
    # 完整可行时等级不影响容量比例与字典序优化
    setup(client, capacity=2)
    before = client.post("/assignment/dry-run", headers=ORG).json()
    assert before["feasible"] is True
    set_level(client, "P1", "high")
    set_level(client, "P2", "medium")
    after = client.post("/assignment/dry-run", headers=ORG).json()
    assert after["feasible"] is True
    assert after["plan"] == before["plan"]
    assert after["max_used_capacity_ratio"] == before["max_used_capacity_ratio"] == "1/2"
    assert after["level_summary"] == {
        "high": {"assigned": 1, "unassigned": 0},
        "medium": {"assigned": 1, "unassigned": 0},
        "normal": {"assigned": 0, "unassigned": 0},
    }


def test_publish_still_rejected_when_infeasible_with_levels(client):
    add_reviewer(client, "R1", capacity=1)
    add_reviewer(client, "R2", capacity=1)
    add_paper(client, "P1")
    add_paper(client, "P2")
    set_level(client, "P2", "high")
    pub = client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": rev(client)}
    )
    assert pub.status_code == 422
    assert client.get("/assignment", headers=ORG).status_code == 404  # 无发布版产生


# ------------------------------------------------------------ 与锁定表的交互

def test_invalid_lock_not_released_by_level_priority(client):
    # 高等级论文的锁定失效: 仍只报错不释放, 其他论文照常最大部分分配
    setup(client, papers=("P1", "P2"))
    client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": rev(client)}
    )
    set_level(client, "P1", "high")
    client.post(
        "/assignment/locks",
        headers=ORG,
        json={"base_revision": rev(client), "locks": {"P1": ["R4"]}},
    )
    client.delete("/reviewers/R4", headers=ORG)
    dry = client.post("/assignment/dry-run", headers=ORG).json()
    assert dry["feasible"] is False
    assert dry["unassigned"] == ["P1"]
    assert "lock_reviewer_not_found" in dry["diagnostics"]["P1"]["reasons"]
    assert dry["level_summary"]["high"] == {"assigned": 0, "unassigned": 1}
    assert dry["level_summary"]["normal"] == {"assigned": 1, "unassigned": 0}
    # 发布拒绝且当前发布版不变
    pub = client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": rev(client)}
    )
    assert pub.status_code == 422
    assert client.get("/assignment", headers=ORG).json()["serial"] == 1


def test_locked_feasible_assignment_unaffected_by_levels(client):
    setup(client, papers=("P1", "P2"))
    client.post(
        "/assignment/locks",
        headers=ORG,
        json={"base_revision": rev(client), "locks": {"P1": ["R4"]}},
    )
    set_level(client, "P2", "high")
    dry = client.post("/assignment/dry-run", headers=ORG).json()
    assert dry["feasible"] is True
    assert "R4" in dry["plan"]["P1"]  # 锁定槽位保留
    assert dry["level_summary"]["high"] == {"assigned": 1, "unassigned": 0}
    assert dry["level_summary"]["normal"] == {"assigned": 1, "unassigned": 0}


# ------------------------------------------------------------ 与补位的交互

def _publish(client):
    return client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": rev(client)}
    ).json()


def test_backfill_prioritizes_high_level_paper(client):
    setup(client, papers=("P1", "P2"), capacity=1)
    pub = _publish(client)
    assert pub["plan"] == {"P1": ["R1", "R2"], "P2": ["R3", "R4"]}
    # 删除 R3/R4 后只剩两个容量 1 的评审人: 补位只能完整覆盖一篇
    client.delete("/reviewers/R3", headers=ORG)
    client.delete("/reviewers/R4", headers=ORG)
    set_level(client, "P2", "high")
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"] is False
    assert dry["plan"] == {"P2": ["R1", "R2"]}  # 高等级优先于字典序
    assert dry["unassigned"] == ["P1"]
    assert dry["level_summary"]["high"] == {"assigned": 1, "unassigned": 0}
    assert dry["level_summary"]["normal"] == {"assigned": 0, "unassigned": 1}
    # 补不齐仍拒绝补位发布, 当前发布版不变
    bp = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": rev(client), "base_serial": dry["serial"]},
    )
    assert bp.status_code == 422
    assert client.get("/assignment", headers=ORG).json()["serial"] == 1


def test_backfill_feasible_ignores_levels(client):
    setup(client, papers=("P1", "P2"), capacity=1)
    _publish(client)
    set_level(client, "P2", "high")
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"] is True
    assert dry["plan"] == {"P1": ["R1", "R2"], "P2": ["R3", "R4"]}  # 与无等级一致
    assert dry["max_used_capacity_ratio"] == "1"
    assert dry["level_summary"]["high"] == {"assigned": 1, "unassigned": 0}
    assert dry["level_summary"]["normal"] == {"assigned": 1, "unassigned": 0}


def test_backfill_keeps_confirmed_slots_with_levels(client):
    # 已确认槽位仍按既有规则固定, 等级不改变确认槽位规则
    setup(client, papers=("P1", "P2"), capacity=2)
    pub = _publish(client)
    rid = pub["plan"]["P1"][0]
    client.post(
        "/reviewer/assignments/P1/decision",
        headers=reviewer_headers(rid),
        json={"paper_id": "P1", "decision": "confirm"},
    )
    set_level(client, "P2", "high")
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"] is True
    assert dry["fixed"]["P1"] == [rid]
    assert rid in dry["plan"]["P1"]


# ------------------------------------------------------------ 等级随资料生命周期

def test_level_change_bumps_revision_and_invalidates_publish_base(client):
    setup(client, capacity=2)
    dry = client.post("/assignment/dry-run", headers=ORG).json()
    assert dry["feasible"] is True
    set_level(client, "P1", "high")  # 有效变更推进修订号
    # 基于旧修订号的发布按既有规则 409
    pub = client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": dry["revision"]}
    )
    assert pub.status_code == 409
    # 按新修订号发布成功 (等级不影响完整可行方案)
    pub = client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": rev(client)}
    )
    assert pub.status_code == 200
    assert pub.json()["plan"] == dry["plan"]


def test_withdrawn_paper_level_not_in_solver_scope(client):
    # 撤回稿不参与求解: 其等级不再影响其他论文的部分分配
    add_reviewer(client, "R1", capacity=1)
    add_reviewer(client, "R2", capacity=1)
    add_paper(client, "P1")
    add_paper(client, "P2")
    set_level(client, "P1", "high")
    client.post(
        "/papers/P1/withdrawal",
        headers=ORG,
        json={"paper_id": "P1", "base_revision": rev(client), "reason": "重复投稿"},
    )
    dry = client.post("/assignment/dry-run", headers=ORG).json()
    # 撤回稿不出现在方案/未分配/诊断/等级统计中
    assert dry["feasible"] is True
    assert dry["plan"] == {"P2": ["R1", "R2"]}
    assert "P1" not in dry["unassigned"]
    assert dry["level_summary"] == {
        "high": {"assigned": 0, "unassigned": 0},
        "medium": {"assigned": 0, "unassigned": 0},
        "normal": {"assigned": 1, "unassigned": 0},
    }
