"""本轮功能测试: 机构原名归并 (同一机构的不同名称归为同一冲突判定组)。

覆盖需求:
- 会务方凭密钥提交两个"当前论文作者或评审人资料中出现过"的机构原名及所见资料修订号;
- 归并关系传递 (A~B, B~C => A 与 C 同组) 且不可拆分 (只有归并, 没有拆组);
- 同组重复提交幂等 (changed=false), 不推进修订号、不写边;
  同一名称与自身提交亦为幂等;
- 过期/超前修订号 409 (事务内最先核对); 空白名称 422; 未知名称 404;
  被拒请求不留部分变更 (修订号与归并组均不变);
- 有效归并推进资料修订号 +1, 基于旧修订号的普通发布/补位发布按原规则 409;
- 普通分配与补位均按归并组判定:
    * 评审人与作者同机构 (原名不同但已归并) -> 排除,
      原因 same_institution_as_author_via_merge;
    * 两名评审人同归并组 -> 不能同篇配对, 无可行方案时诊断原因
      eligible_reviewers_share_single_institution_via_merge;
  原名原本相同的冲突仍使用既有原因代码;
- 预演解释标明由归并造成的冲突; 原始机构名称及既有响应字段保持原样;
- 已确认槽位因归并失效时, 补位不得固定该槽位
  (fixed_pair_violates_institution_rule_via_merge), 旧发布版与评语仍可追溯,
  补位发布继续遵守双版本复核及评语继承规则。
"""
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="review-merge-test-")
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
            "feedback_snapshots", "reviewer_status_changes", "institution_merges",
            "review_corrections", "assignment_locks", "paper_guarantee_levels", "review_deadlines",
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


def current_revision(client):
    return client.get("/meta", headers=ORG).json()["revision"]


def merge(client, name_a, name_b, revision=None):
    if revision is None:
        revision = current_revision(client)
    return client.post(
        "/institutions/merge-groups",
        headers=ORG,
        json={"name_a": name_a, "name_b": name_b, "base_revision": revision},
    )


def list_groups(client):
    return client.get("/institutions/merge-groups", headers=ORG).json()["groups"]


# ------------------------------------------------------------ 鉴权与入参

def test_merge_requires_organizer_key(client):
    add_reviewer(client, "R1", "Inst-A")
    add_paper(client, "P1", institutions=["Univ-X"])
    r = client.post(
        "/institutions/merge-groups",
        json={"name_a": "Inst-A", "name_b": "Univ-X", "base_revision": 0},
    )
    assert r.status_code == 401


def test_merge_blank_name_422_without_partial_change(client):
    add_reviewer(client, "R1", "Inst-A")
    add_paper(client, "P1", institutions=["Univ-X"])
    rev = current_revision(client)
    for a, b in (("   ", "Univ-X"), ("Inst-A", ""), ("", "")):
        r = merge(client, a, b, revision=rev)
        assert r.status_code == 422, (a, b, r.status_code)
    assert current_revision(client) == rev
    assert list_groups(client) == []


def test_merge_unknown_name_404_without_partial_change(client):
    add_reviewer(client, "R1", "Inst-A")
    add_paper(client, "P1", institutions=["Univ-X"])
    rev = current_revision(client)
    r = merge(client, "Inst-A", "Ghost-University", revision=rev)
    assert r.status_code == 404
    assert "Ghost-University" in r.json()["detail"]
    # 两个名称都未知时一并提示
    r = merge(client, "Ghost-A", "Ghost-B", revision=rev)
    assert r.status_code == 404
    assert current_revision(client) == rev
    assert list_groups(client) == []


def test_merge_stale_revision_rejected_first(client):
    add_reviewer(client, "R1", "Inst-A")
    add_paper(client, "P1", institutions=["Univ-X"])
    stale = current_revision(client)
    add_paper(client, "P2", institutions=["Univ-Y"])  # 修订号前进
    # 过期修订号 + 未知名称 + 空白名称 -> 一律先以 409 为准
    assert merge(client, "Ghost", "Inst-A", revision=stale).status_code == 409
    assert merge(client, "  ", "Inst-A", revision=stale).status_code == 409
    assert current_revision(client) == stale + 1
    assert list_groups(client) == []


def test_merge_future_revision_also_conflict(client):
    add_reviewer(client, "R1", "Inst-A")
    add_paper(client, "P1", institutions=["Univ-X"])
    r = merge(client, "Inst-A", "Univ-X", revision=current_revision(client) + 5)
    assert r.status_code == 409
    assert list_groups(client) == []


# ------------------------------------------------------------ 有效归并 / 幂等 / 传递

def test_effective_merge_bumps_revision_and_returns_group(client):
    add_reviewer(client, "R1", "Inst-A")
    add_paper(client, "P1", institutions=["Univ-X"])
    rev = current_revision(client)
    r = merge(client, "Inst-A", "Univ-X")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["changed"] is True
    assert j["revision"] == rev + 1
    assert j["names"] == ["Inst-A", "Univ-X"]
    assert j["submitted"] == ["Inst-A", "Univ-X"]
    assert j["group_key"] == "Inst-A"
    assert current_revision(client) == rev + 1
    groups = list_groups(client)
    assert groups == [{"group_key": "Inst-A", "names": ["Inst-A", "Univ-X"], "size": 2}]


def test_merge_trims_surrounding_whitespace(client):
    add_reviewer(client, "R1", "Inst-A")
    add_paper(client, "P1", institutions=["Univ-X"])
    r = merge(client, "  Inst-A  ", " Univ-X ")
    assert r.status_code == 200
    assert r.json()["names"] == ["Inst-A", "Univ-X"]
    # 资料中的原始机构名称保持原样
    assert client.get("/reviewers", headers=ORG).json()["reviewers"][0]["institution"] == "Inst-A"
    paper = client.get("/papers", headers=ORG).json()["papers"][0]
    assert paper["institutions"] == ["Univ-X"]


def test_same_group_resubmit_is_idempotent(client):
    add_reviewer(client, "R1", "Inst-A")
    add_paper(client, "P1", institutions=["Univ-X"])
    rev1 = merge(client, "Inst-A", "Univ-X").json()["revision"]
    # 同组重复提交 (含交换方向) -> changed=false, 不推进、不写边
    for a, b in (("Inst-A", "Univ-X"), ("Univ-X", "Inst-A")):
        r = merge(client, a, b, revision=rev1)
        assert r.status_code == 200
        j = r.json()
        assert j["changed"] is False and j["revision"] == rev1
        assert j["names"] == ["Inst-A", "Univ-X"]
    with db.read_txn() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM institution_merges").fetchone()["c"] == 1


def test_merge_name_with_itself_is_idempotent(client):
    add_reviewer(client, "R1", "Inst-A")
    rev = current_revision(client)
    r = merge(client, "Inst-A", "Inst-A")
    assert r.status_code == 200
    j = r.json()
    assert j["changed"] is False and j["revision"] == rev
    assert j["names"] == ["Inst-A"]
    with db.read_txn() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM institution_merges").fetchone()["c"] == 0
    # 单名称不形成归并组
    assert list_groups(client) == []


def test_merge_is_transitive(client):
    # Inst-A (评审人), Inst-Abbr (评审人), Univ-X (作者) 三个原名, 两两归并
    add_reviewer(client, "R1", "Inst-A")
    add_reviewer(client, "R2", "Inst-Abbr")
    add_paper(client, "P1", institutions=["Univ-X"])
    r1 = merge(client, "Inst-A", "Inst-Abbr").json()["revision"]
    r2 = merge(client, "Inst-Abbr", "Univ-X", revision=r1).json()["revision"]
    assert r2 == r1 + 1
    groups = list_groups(client)
    assert len(groups) == 1
    assert groups[0]["names"] == ["Inst-A", "Inst-Abbr", "Univ-X"]
    # 传递闭包: Inst-A 与 Univ-X 未直接提交过, 再提交幂等
    r = merge(client, "Inst-A", "Univ-X", revision=r2)
    assert r.json()["changed"] is False and r.json()["revision"] == r2


def test_merges_from_both_author_and_reviewer_sides(client):
    add_reviewer(client, "R1", "Acme-U")
    add_paper(client, "P1", institutions=["Acme University"])
    j = merge(client, "Acme-U", "Acme University").json()
    assert j["changed"] is True
    assert j["names"] == ["Acme University", "Acme-U"]


def test_merge_edges_persist_after_names_leave_current_data(client):
    add_reviewer(client, "R1", "Inst-A")
    add_paper(client, "P1", institutions=["Univ-X"])
    merge(client, "Inst-A", "Univ-X")
    # 删除评审人后, 归并边仍保留可追溯; 组视图不消失
    client.delete("/reviewers/R1", headers=ORG)
    groups = list_groups(client)
    assert groups == [{"group_key": "Inst-A", "names": ["Inst-A", "Univ-X"], "size": 2}]
    # 只剩一个当前名称时无法再用已删除名称做归并 (名称必须在当前资料中)
    rev = current_revision(client)
    r = merge(client, "Inst-A", "Univ-X", revision=rev)
    assert r.status_code == 404


# ------------------------------------------------------------ 普通分配按归并组判定

def test_merged_reviewer_same_institution_as_author_excluded(client):
    add_reviewer(client, "R1", "Inst-A")
    add_reviewer(client, "R2", "Inst-B")
    add_reviewer(client, "R3", "Inst-C")
    add_paper(client, "P1", institutions=["Univ-X"])
    # 归并前: R1 合格
    before = client.post("/assignment/dry-run", headers=ORG).json()
    assert "R1" in before["plan"]["P1"]
    rev = current_revision(client)
    assert merge(client, "Inst-A", "Univ-X").status_code == 200
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert "R1" not in body["plan"]["P1"]
    reasons = {e["reviewer_id"]: e["reason"] for e in body["papers"]["P1"]["excluded"]}
    assert reasons["R1"] == "same_institution_as_author_via_merge"
    # 归并推进修订号: 基于旧修订号的发布 409
    assert client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": rev}
    ).status_code == 409
    # 用新修订号可以发布, 原始机构字段保持原样
    rev2 = body["revision"]
    pub = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev2})
    assert pub.status_code == 200
    assert "R1" not in pub.json()["plan"]["P1"]
    listing = client.get("/reviewers", headers=ORG).json()["reviewers"]
    assert {r["reviewer_id"]: r["institution"] for r in listing}["R1"] == "Inst-A"


def test_exact_name_match_keeps_original_reason(client):
    add_reviewer(client, "R1", "Univ-X")
    add_reviewer(client, "R2", "Inst-B")
    add_reviewer(client, "R3", "Inst-C")
    add_paper(client, "P1", institutions=["Univ-X"])
    body = client.post("/assignment/dry-run", headers=ORG).json()
    reasons = {e["reviewer_id"]: e["reason"] for e in body["papers"]["P1"]["excluded"]}
    assert reasons["R1"] == "same_institution_as_author"


def test_two_reviewers_in_merged_group_cannot_pair(client):
    # R1/R2 的机构原名不同, 归并后同组; R3 第三机构; 论文只匹配该主题
    add_reviewer(client, "R1", "Inst-A")
    add_reviewer(client, "R2", "Inst-Abbr")
    add_reviewer(client, "R3", "Inst-C")
    add_paper(client, "P1", institutions=["Univ-X"])
    merge(client, "Inst-A", "Inst-Abbr")
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is True
    pair = set(body["plan"]["P1"])
    assert pair == {"R3", "R1"} or pair == {"R3", "R2"}
    # R1/R2 不会同篇
    assert not {"R1", "R2"} <= pair


def test_share_single_institution_diagnostic_marks_merge(client):
    # 三名合格评审人全部落在同一归并组 (原名互异) -> 无法凑出两人不同机构
    add_reviewer(client, "R1", "Inst-A")
    add_reviewer(client, "R2", "Inst-Abbr")
    add_reviewer(client, "R3", "Inst-Alias")
    add_paper(client, "P1", institutions=["Univ-X"])
    r1 = merge(client, "Inst-A", "Inst-Abbr").json()["revision"]
    merge(client, "Inst-Abbr", "Inst-Alias", revision=r1)
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is False and body["unassigned"] == ["P1"]
    reasons = body["diagnostics"]["P1"]["reasons"]
    assert "eligible_reviewers_share_single_institution_via_merge" in reasons
    assert "eligible_reviewers_share_single_institution" not in reasons


def test_share_single_institution_exact_name_reason_unchanged(client):
    add_reviewer(client, "R1", "Inst-A")
    add_reviewer(client, "R2", "Inst-A")
    add_paper(client, "P1", institutions=["Univ-X"])
    body = client.post("/assignment/dry-run", headers=ORG).json()
    reasons = body["diagnostics"]["P1"]["reasons"]
    assert "eligible_reviewers_share_single_institution" in reasons
    assert "eligible_reviewers_share_single_institution_via_merge" not in reasons


def test_merge_bumps_revision_blocks_publish_and_reflects_republish(client):
    add_reviewer(client, "R1", "Inst-A")
    add_reviewer(client, "R2", "Inst-B")
    add_reviewer(client, "R3", "Inst-C")
    add_paper(client, "P1", institutions=["Univ-X"])
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    pub = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert pub.status_code == 200
    assert "R1" in pub.json()["plan"]["P1"]
    # 发布后归并使 R1 与作者同机构: 旧发布版保持不变可供核对
    merge(client, "Inst-A", "Univ-X")
    pub_before = client.get("/assignment", headers=ORG).json()
    assert "R1" in pub_before["plan"]["P1"]
    assert pub_before["revision"] == rev
    # 重新发布后新方案遵守归并
    rev2 = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    pub2 = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev2})
    assert "R1" not in pub2.json()["plan"]["P1"]


# ------------------------------------------------------------ 补位按归并组判定

def test_backfill_confirmed_slot_invalidated_by_merge_is_not_fixed(client):
    # 4 名评审人: R1=Inst-A, R2=Inst-Abbr (与 R1 归并), R3/R4 其他机构
    add_reviewer(client, "R1", "Inst-A")
    add_reviewer(client, "R2", "Inst-Abbr")
    add_reviewer(client, "R3", "Inst-C")
    add_reviewer(client, "R4", "Inst-D")
    add_paper(client, "P1", institutions=["Univ-X"])
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    pub = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert pub.status_code == 200
    plan = pub.json()["plan"]["P1"]

    # 先归并, 再让当前发布版的两人都确认 (确认在归并前已存在, 归并使其失效)
    ra, rb = plan[0], plan[1]
    client.post(
        "/reviewer/assignments/P1/decision", headers=reviewer_headers(ra),
        json={"paper_id": "P1", "decision": "confirm"},
    )
    client.post(
        "/reviewer/assignments/P1/decision", headers=reviewer_headers(rb),
        json={"paper_id": "P1", "decision": "confirm"},
    )
    # 归并当前发布版两人的机构原名
    inst_of = {
        r["reviewer_id"]: r["institution"]
        for r in client.get("/reviewers", headers=ORG).json()["reviewers"]
    }
    r = merge(client, inst_of[ra], inst_of[rb])
    assert r.status_code == 200, r.text

    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"] is True
    # 失效槽位不得固定: 后一槽位释放并标注归并原因; 补位方案中两人不再同篇
    problems = {
        (p["reviewer_id"], p["reason"])
        for p in dry["fixed_problems"].get("P1", [])
    }
    assert any(
        rid == rb and reason == "fixed_pair_violates_institution_rule_via_merge"
        for rid, reason in problems
    )
    assert dry["fixed"].get("P1", []) == [ra]
    assert not {ra, rb} <= set(dry["plan"]["P1"])
    assert ra in dry["plan"]["P1"]

    # 补位发布: 双版本复核
    r = client.post(
        "/assignment/backfill/publish", headers=ORG,
        json={"base_revision": dry["revision"] - 1, "base_serial": dry["serial"]},
    )
    assert r.status_code == 409
    r = client.post(
        "/assignment/backfill/publish", headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert not {ra, rb} <= set(j["plan"]["P1"])
    # 保留的固定槽位确认状态沿用; 被释放者不再在方案中
    assert j["carried_confirmations"] == 1


def test_backfill_uses_merge_groups_for_new_pairs(client):
    # R1 固定 (已确认); 归并后 R2 与 R1 同组, 补位搭档不得是 R2。
    # 发布时 R2/R3 均回避 P1 以锁定初始方案 (R1, R4); 随后解除 R2 的回避并归并机构,
    # 使 R2 在补位时个人完全合格, 仅仅因为与固定者 R1 同归并组而不能配对。
    add_reviewer(client, "R1", "Inst-A")
    add_reviewer(client, "R2", "Inst-Abbr", avoid=["P1"])
    add_reviewer(client, "R3", "Inst-C", avoid=["P1"])
    add_reviewer(client, "R4", "Inst-D")
    add_reviewer(client, "R5", "Inst-E")
    add_paper(client, "P1", institutions=["Univ-X"])
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    pub = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    plan = pub.json()["plan"]["P1"]
    assert set(plan) == {"R1", "R4"}
    client.post(
        "/reviewer/assignments/P1/decision", headers=reviewer_headers("R1"),
        json={"paper_id": "P1", "decision": "confirm"},
    )
    client.post(
        "/reviewer/assignments/P1/decision", headers=reviewer_headers("R4"),
        json={"paper_id": "P1", "decision": "recuse", "reason": "冲突"},
    )
    # 解除 R2 的录入回避 (资料变更): 个人合格, 无任何个人排除原因
    r = client.put(
        "/reviewers/R2", headers=ORG,
        json={
            "credential": "cred-R2", "topics": ["AI"], "institution": "Inst-Abbr",
            "capacity": 3, "avoid_papers": [],
        },
    )
    assert r.status_code == 200
    merge(client, "Inst-A", "Inst-Abbr")
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"] is True
    assert "R1" in dry["plan"]["P1"]
    assert "R2" not in dry["plan"]["P1"]  # 与固定者 R1 同归并组, 不能配对
    # 论文级解释中 R2 并非个人排除项 (与作者不同组、未回避), 仅是不能与 R1 配对
    excluded = {e["reviewer_id"]: e["reason"] for e in dry["papers"]["P1"]["excluded"]}
    assert "R2" not in excluded
    # 搭档只能是 R5 (R3 录入回避, R4 硬回避, R2 同归并组)
    assert dry["plan"]["P1"] == ["R1", "R5"]


def test_backfill_author_merge_releases_confirmed_slot_with_excluded_reason(client):
    # 已确认槽位的评审人因归并与作者同机构 -> 槽位失效释放, 原因标注归并
    add_reviewer(client, "R1", "Inst-A")
    add_reviewer(client, "R2", "Inst-B")
    add_reviewer(client, "R3", "Inst-C")
    add_paper(client, "P1", institutions=["Univ-X"])
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    pub = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    plan = pub.json()["plan"]["P1"]
    rid_a = "R1"
    assert rid_a in plan
    rid_b = next(x for x in plan if x != rid_a)
    client.post(
        "/reviewer/assignments/P1/decision", headers=reviewer_headers(rid_a),
        json={"paper_id": "P1", "decision": "confirm"},
    )
    client.post(
        f"/reviewer/assignments/P1/decision", headers=reviewer_headers(rid_b),
        json={"paper_id": "P1", "decision": "confirm"},
    )
    merge(client, "Inst-A", "Univ-X")
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"] is True
    problems = {
        (p["reviewer_id"], p["reason"])
        for p in dry["fixed_problems"].get("P1", [])
    }
    assert (rid_a, "same_institution_as_author_via_merge") in problems
    assert rid_a not in dry["plan"]["P1"]
    # 普通排除解释也带归并后缀
    reasons = {e["reviewer_id"]: e["reason"] for e in dry["papers"]["P1"]["excluded"]}
    assert reasons[rid_a] == "same_institution_as_author_via_merge"


def test_backfill_publish_after_merge_inherits_reviews_and_keeps_old_traceable(client):
    # 归并仅使一个已确认槽位失效; 保留槽位的评语在补位发布后沿用 (同收据),
    # 被移出槽位无评语场景外, 旧发布版仍可在评语归档中追溯。
    add_reviewer(client, "R1", "Inst-A")
    add_reviewer(client, "R2", "Inst-B")
    add_reviewer(client, "R3", "Inst-C")
    add_reviewer(client, "R4", "Inst-D")
    add_paper(client, "P1", institutions=["Univ-X"])
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    pub = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev}).json()
    old_serial = pub["serial"]
    rid_keep = "R1"
    rid_drop = next(x for x in pub["plan"]["P1"] if x != rid_keep)
    # 两人确认并各交评语
    for rid in (rid_keep, rid_drop):
        client.post(
            "/reviewer/assignments/P1/decision", headers=reviewer_headers(rid),
            json={"paper_id": "P1", "decision": "confirm"},
        )
    for rid, score, comment in (
        (rid_keep, 5, "保留槽位的评语"),
        (rid_drop, 3, "将随归并失效槽位归档的评语"),
    ):
        r = client.post(
            "/reviewer/assignments/P1/review", headers=reviewer_headers(rid),
            json={"paper_id": "P1", "serial": old_serial, "score": score, "comment": comment},
        )
        assert r.status_code == 200, r.text
    # 归并使 rid_drop 与作者同机构
    inst_drop = next(
        r["institution"] for r in client.get("/reviewers", headers=ORG).json()["reviewers"]
        if r["reviewer_id"] == rid_drop
    )
    merge(client, inst_drop, "Univ-X")
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"] is True
    r = client.post(
        "/assignment/backfill/publish", headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert r.status_code == 200
    j = r.json()
    assert rid_drop not in j["plan"]["P1"] and rid_keep in j["plan"]["P1"]
    assert j["carried_confirmations"] == 1 and j["carried_reviews"] == 1
    # 会务方追溯: 保留槽位评语仍在当前 slots; 被移出槽位的旧评语进入归档 (带旧 serial)
    pr = client.get("/papers/P1/reviews", headers=ORG).json()
    slot_reviewers = {s["reviewer_id"] for s in pr["slots"]}
    assert rid_keep in slot_reviewers and rid_drop not in slot_reviewers
    kept = next(s for s in pr["slots"] if s["reviewer_id"] == rid_keep)
    assert kept["submitted"] is True and kept["review"]["comment"] == "保留槽位的评语"
    archived = pr["archived_reviews"]
    assert len(archived) == 1
    assert archived[0]["reviewer_id"] == rid_drop
    assert archived[0]["serial"] == old_serial
    assert archived[0]["comment"] == "将随归并失效槽位归档的评语"


# ------------------------------------------------------------ GET 组视图

def test_list_groups_empty_and_revision(client):
    r = client.get("/institutions/merge-groups", headers=ORG)
    assert r.status_code == 200
    assert r.json() == {"revision": 0, "groups": []}
    add_reviewer(client, "R1", "Inst-A")
    add_paper(client, "P1", institutions=["Univ-X"])
    merge(client, "Inst-A", "Univ-X")
    body = client.get("/institutions/merge-groups", headers=ORG).json()
    assert body["revision"] == current_revision(client)
    assert len(body["groups"]) == 1


def test_list_groups_requires_organizer_key(client):
    assert client.get("/institutions/merge-groups").status_code == 401
