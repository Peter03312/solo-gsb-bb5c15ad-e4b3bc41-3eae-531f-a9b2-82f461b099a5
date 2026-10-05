"""本轮功能测试: 已交评语的更正 (会务方发起 -> 评审人更正 -> 快照重发)。

覆盖需求:
- 会务方凭密钥指定当前发布序号、论文、评审人及非空原因发起更正请求;
  只允许当前仍分配、已确认且已交评语的槽位: 旧序号 409, 未发布/论文不在方案/
  评审人已移出槽位 404, 槽位 pending/recused 或未交评语 409, 空原因 422;
  同槽位同原因重复发起幂等 (changed=false), 异原因 409;
- 更正请求发起即令该稿既有作者反馈访问码失效 (旧码立即 404),
  待更正期间发布快照 409; 更正请求不推进资料修订号;
- 评审人凭本人凭据查看自己的待更正任务 (不含他人), 以请求对应的发布序号和
  原评语收据提交新 1~5 整数评分与非空评语; 首份有效更正生成新收据并更新槽位评语,
  原评语冻结保留供会务方追溯; 同内容重试返回更正收据 (changed=false), 异内容 409;
- 发布序号变化后待更正请求失效 (不再列出, 提交 409/404);
- 补位保留槽位沿用更正后的评语 (同新收据), 移出槽位评语归档;
- 更正后会务方按现有完整性规则重新发布快照: 新版本 + 新访问码, 旧码保持失效;
- 既有正式评语首提交接口的一次提交及冲突语义不变; 评审人不得查看他人评语。
"""
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="review-corrections-test-")
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
            "paper_guarantee_levels", "review_deadlines",
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


def request_correction(client, pid, serial, rid, reason):
    return client.post(
        f"/papers/{pid}/review-corrections",
        headers=ORG,
        json={"paper_id": pid, "serial": serial, "reviewer_id": rid, "reason": reason},
    )


def submit_correction(client, rid, pid, serial, receipt, score, comment):
    return client.post(
        f"/reviewer/review-corrections/{pid}",
        headers=reviewer_headers(rid),
        json={
            "paper_id": pid,
            "serial": serial,
            "original_receipt": receipt,
            "score": score,
            "comment": comment,
        },
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


def setup_reviewed(client, pid="P1", score_a=4, comment_a="甲的原评语",
                   score_b=2, comment_b="乙的原评语"):
    """建评审人/论文 -> 发布 -> 两人确认 -> 各交一份正式评语。"""
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
    return pub, (rid_a, rid_b), (ra, rb)


def publish_snapshot(client, pid, serial):
    r = client.post(f"/papers/{pid}/feedback-snapshot", headers=ORG, json={"serial": serial})
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------ 发起更正请求 (会务方)

def test_request_correction_success_and_freezes_original(client):
    pub, (rid_a, _), (ra, _) = setup_reviewed(client)
    serial = pub["serial"]
    rev_before = client.get("/meta", headers=ORG).json()["revision"]

    r = request_correction(client, "P1", serial, rid_a, "评分与评语明显不符, 请更正")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["changed"] is True and j["state"] == "pending"
    assert j["serial"] == serial and j["reviewer_id"] == rid_a
    assert j["reason"] == "评分与评语明显不符, 请更正"
    assert j["original"] == {
        "score": 4, "comment": "甲的原评语",
        "receipt": ra["receipt"], "submitted_at": ra["submitted_at"],
    }
    assert j["corrected"] is None
    assert j["invalidated_snapshots"] == 0  # 本例尚未发布快照
    # 更正请求不推进资料修订号
    assert client.get("/meta", headers=ORG).json()["revision"] == rev_before


def test_request_correction_requires_organizer_key(client):
    pub, (rid_a, _), _ = setup_reviewed(client)
    serial = pub["serial"]
    body = {"paper_id": "P1", "serial": serial, "reviewer_id": rid_a, "reason": "x"}
    assert client.post("/papers/P1/review-corrections", json=body).status_code == 401
    assert client.post(
        "/papers/P1/review-corrections", headers=BAD_ORG, json=body
    ).status_code == 401


def test_request_correction_validation_errors(client):
    pub, (rid_a, rid_b), _ = setup_reviewed(client)
    serial = pub["serial"]
    # 序号超前 -> 409
    assert request_correction(client, "P1", serial + 1, rid_a, "r").status_code == 409
    # 论文不在当前发布版 -> 404
    assert request_correction(client, "NOPE", serial, rid_a, "r").status_code == 404
    # 评审人不在该论文槽位 -> 404
    assert request_correction(client, "P1", serial, "R4", "r").status_code == 404
    # 空原因 -> 422
    assert request_correction(client, "P1", serial, rid_a, "   ").status_code == 422
    # 路径与请求体论文不一致 -> 422
    r = client.post(
        "/papers/P1/review-corrections",
        headers=ORG,
        json={"paper_id": "P9", "serial": serial, "reviewer_id": rid_a, "reason": "r"},
    )
    assert r.status_code == 422


def test_request_correction_without_publication_is_404(client):
    add_reviewer(client, "R1", "I1")
    add_paper(client)
    assert request_correction(client, "P1", 1, "R1", "r").status_code == 404


def test_request_correction_requires_confirmed_and_submitted_slot(client):
    for rid in ("R1", "R2", "R3", "R4"):
        add_reviewer(client, rid, f"Inst-{rid}")
    add_paper(client)
    pub = publish(client)
    serial = pub["serial"]
    rid_a, rid_b = pub["plan"]["P1"]
    # pending 槽位 -> 409
    assert request_correction(client, "P1", serial, rid_a, "r").status_code == 409
    # 已确认但未交评语 -> 409
    decide(client, rid_a, "P1", "confirm")
    assert request_correction(client, "P1", serial, rid_a, "r").status_code == 409
    # 已回避槽位 -> 409
    decide(client, rid_b, "P1", "recuse", reason="利益冲突")
    assert request_correction(client, "P1", serial, rid_b, "r").status_code == 409


def test_request_correction_idempotent_same_reason_conflict_different(client):
    pub, (rid_a, _), _ = setup_reviewed(client)
    serial = pub["serial"]
    first = request_correction(client, "P1", serial, rid_a, "请更正评分").json()
    assert first["changed"] is True
    # 同原因重复发起 -> 幂等
    again = request_correction(client, "P1", serial, rid_a, "请更正评分")
    assert again.status_code == 200
    j = again.json()
    assert j["changed"] is False
    assert j["correction_id"] == first["correction_id"]
    # 异原因 -> 409
    assert request_correction(client, "P1", serial, rid_a, "另一个原因").status_code == 409
    # 会务方追溯只有一条记录
    view = client.get("/papers/P1/reviews", headers=ORG).json()
    assert len(view["corrections"]) == 1


def test_request_correction_invalidates_snapshot_codes_immediately(client):
    pub, (rid_a, _), _ = setup_reviewed(client)
    serial = pub["serial"]
    snap = publish_snapshot(client, "P1", serial)
    code = snap["access_code"]
    assert client.get(f"/feedback-snapshots/{code}").status_code == 200

    j = request_correction(client, "P1", serial, rid_a, "评语张冠李戴").json()
    assert j["invalidated_snapshots"] == 1
    # 既有访问码立即失效 (统一 404, 不透露论文是否存在)
    r = client.get(f"/feedback-snapshots/{code}")
    assert r.status_code == 404
    # 待更正期间按原序号重新发布快照 -> 409, 不产生新版本
    r = client.post("/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial})
    assert r.status_code == 409
    trace = client.get("/papers/P1/feedback-snapshots", headers=ORG).json()
    assert [s["version"] for s in trace["snapshots"]] == [1]
    assert trace["snapshots"][0]["active"] is False


# ------------------------------------------------------------ 评审人待更正任务列表

def test_reviewer_lists_only_own_pending_tasks(client):
    pub, (rid_a, rid_b), _ = setup_reviewed(client)
    serial = pub["serial"]
    request_correction(client, "P1", serial, rid_a, "甲的评语需更正")

    view_a = client.get("/reviewer/review-corrections", headers=reviewer_headers(rid_a))
    assert view_a.status_code == 200
    j = view_a.json()
    assert j["serial"] == serial
    assert len(j["corrections"]) == 1
    task = j["corrections"][0]
    assert task["paper_id"] == "P1" and task["state"] == "pending"
    assert task["reason"] == "甲的评语需更正"
    assert task["original"]["comment"] == "甲的原评语"
    # 另一评审人看不到该任务
    view_b = client.get("/reviewer/review-corrections", headers=reviewer_headers(rid_b)).json()
    assert view_b["corrections"] == []
    # 不泄露另一评审人信息
    assert rid_b not in str(view_a.json())


def test_reviewer_correction_tasks_empty_without_publication(client):
    add_reviewer(client, "R1", "I1")
    j = client.get("/reviewer/review-corrections", headers=reviewer_headers("R1")).json()
    assert j == {"reviewer_id": "R1", "serial": None, "corrections": []}


def test_reviewer_correction_endpoints_auth(client):
    pub, (rid_a, _), _ = setup_reviewed(client)
    serial = pub["serial"]
    request_correction(client, "P1", serial, rid_a, "r")
    # 错误凭据 -> 401
    assert client.get(
        "/reviewer/review-corrections",
        headers={"X-Reviewer-Id": rid_a, "X-Reviewer-Credential": "bad"},
    ).status_code == 401
    # 停用资格 -> 403
    rev = client.get("/meta", headers=ORG).json()["revision"]
    client.post(
        f"/reviewers/{rid_a}/status",
        headers=ORG,
        json={"reviewer_id": rid_a, "base_revision": rev, "active": False,
              "reason": "暂停评审"},
    )
    assert client.get(
        "/reviewer/review-corrections", headers=reviewer_headers(rid_a)
    ).status_code == 403
    r = submit_correction(client, rid_a, "P1", serial, "rvw-whatever", 5, "x")
    assert r.status_code == 403


# ------------------------------------------------------------ 评审人提交更正

def test_submit_correction_success_generates_new_receipt_and_keeps_original(client):
    pub, (rid_a, _), (ra, _) = setup_reviewed(client)
    serial = pub["serial"]
    request_correction(client, "P1", serial, rid_a, "请更正")
    rev_before = client.get("/meta", headers=ORG).json()["revision"]

    r = submit_correction(client, rid_a, "P1", serial, ra["receipt"], 5, "更正后的评语")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["changed"] is True
    assert j["receipt"].startswith("rvw-") and j["receipt"] != ra["receipt"]
    assert j["original_receipt"] == ra["receipt"]
    assert j["score"] == 5 and j["comment"] == "更正后的评语"
    # 更正不推进资料修订号
    assert client.get("/meta", headers=ORG).json()["revision"] == rev_before

    # 会务方视图: 槽位评语已更新为新收据; 原评语在 corrections 中冻结可追溯
    view = client.get("/papers/P1/reviews", headers=ORG).json()
    slot = next(s for s in view["slots"] if s["reviewer_id"] == rid_a)
    assert slot["review"]["score"] == 5 and slot["review"]["receipt"] == j["receipt"]
    corr = view["corrections"][0]
    assert corr["state"] == "completed"
    assert corr["original"]["comment"] == "甲的原评语"
    assert corr["original"]["receipt"] == ra["receipt"]
    assert corr["corrected"]["comment"] == "更正后的评语"
    assert corr["corrected"]["receipt"] == j["receipt"]

    # 评审人本人视图显示更正后的评语
    mine = client.get("/reviewer/reviews", headers=reviewer_headers(rid_a)).json()
    assert mine["reviews"][0]["receipt"] == j["receipt"]
    assert mine["reviews"][0]["comment"] == "更正后的评语"
    # 任务列表不再出现已完成任务
    tasks = client.get("/reviewer/review-corrections", headers=reviewer_headers(rid_a)).json()
    assert tasks["corrections"] == []


def test_submit_correction_idempotent_retry_and_conflict(client):
    pub, (rid_a, _), (ra, _) = setup_reviewed(client)
    serial = pub["serial"]
    request_correction(client, "P1", serial, rid_a, "请更正")
    first = submit_correction(client, rid_a, "P1", serial, ra["receipt"], 5, "更正后评语").json()

    # 完全相同的重试 (含首尾空白归一化) -> 返回更正收据, changed=false
    retry = submit_correction(client, rid_a, "P1", serial, ra["receipt"], 5, "  更正后评语 ")
    assert retry.status_code == 200
    j = retry.json()
    assert j["changed"] is False and j["receipt"] == first["receipt"]
    assert j["corrected_at"] == first["corrected_at"]

    # 内容不同 -> 409, 已存更正不变
    r = submit_correction(client, rid_a, "P1", serial, ra["receipt"], 3, "又想改")
    assert r.status_code == 409
    assert r.json()["detail"]["existing"]["receipt"] == first["receipt"]
    view = client.get("/papers/P1/reviews", headers=ORG).json()
    slot = next(s for s in view["slots"] if s["reviewer_id"] == rid_a)
    assert slot["review"]["score"] == 5


def test_submit_correction_validation_and_not_found(client):
    pub, (rid_a, rid_b), (ra, _) = setup_reviewed(client)
    serial = pub["serial"]
    request_correction(client, "P1", serial, rid_a, "请更正")
    # 非法评分 -> 422
    assert submit_correction(client, rid_a, "P1", serial, ra["receipt"], 6, "x").status_code == 422
    assert submit_correction(client, rid_a, "P1", serial, ra["receipt"], 2.5, "x").status_code == 422
    # 空白评语 -> 422
    assert submit_correction(client, rid_a, "P1", serial, ra["receipt"], 4, "  ").status_code == 422
    # 路径与请求体不一致 -> 422
    r = client.post(
        "/reviewer/review-corrections/P1",
        headers=reviewer_headers(rid_a),
        json={"paper_id": "P9", "serial": serial, "original_receipt": ra["receipt"],
              "score": 4, "comment": "x"},
    )
    assert r.status_code == 422
    # 序号超前 -> 409
    assert submit_correction(client, rid_a, "P1", serial + 1, ra["receipt"], 4, "x").status_code == 409
    # 收据不对应任何更正任务 -> 404
    assert submit_correction(client, rid_a, "P1", serial, "rvw-nope", 4, "x").status_code == 404
    # 他人 (无更正任务) -> 404
    rb_receipt = client.get("/papers/P1/reviews", headers=ORG).json()["slots"][1]["review"]["receipt"]
    assert submit_correction(client, rid_b, "P1", serial, rb_receipt, 4, "x").status_code == 404


def test_submit_correction_without_publication_is_404(client):
    add_reviewer(client, "R1", "I1")
    r = submit_correction(client, "R1", "P1", 1, "rvw-x", 4, "x")
    assert r.status_code == 404


def test_rejected_correction_does_not_persist_or_bump(client):
    pub, (rid_a, _), (ra, _) = setup_reviewed(client)
    serial = pub["serial"]
    request_correction(client, "P1", serial, rid_a, "请更正")
    rev_before = client.get("/meta", headers=ORG).json()["revision"]
    submit_correction(client, rid_a, "P1", serial, ra["receipt"], 9, "非法评分")
    submit_correction(client, rid_a, "P1", serial + 1, ra["receipt"], 4, "过期序号")
    submit_correction(client, rid_a, "P1", serial, "rvw-nope", 4, "无此任务")
    assert client.get("/meta", headers=ORG).json()["revision"] == rev_before
    view = client.get("/papers/P1/reviews", headers=ORG).json()
    assert view["corrections"][0]["state"] == "pending"
    slot = next(s for s in view["slots"] if s["reviewer_id"] == rid_a)
    assert slot["review"]["receipt"] == ra["receipt"]  # 原评语未被改动


# ------------------------------------------------------------ 更正后重新发布快照

def test_snapshot_republish_after_correction(client):
    pub, (rid_a, _), (ra, _) = setup_reviewed(client)
    serial = pub["serial"]
    v1 = publish_snapshot(client, "P1", serial)
    old_code = v1["access_code"]

    request_correction(client, "P1", serial, rid_a, "评分录入错误")
    assert client.get(f"/feedback-snapshots/{old_code}").status_code == 404
    corrected = submit_correction(client, rid_a, "P1", serial, ra["receipt"], 5, "更正后的甲评语").json()

    # 更正完成后, 会务方按现有完整性规则 (当前序号 + 两人确认 + 两份评语) 重新发布
    r = client.post("/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial})
    assert r.status_code == 200, r.text
    v2 = r.json()
    assert v2["changed"] is True and v2["version"] == 2
    assert v2["access_code"] != old_code
    # 新快照含更正后的评语
    comments = {x["comment"] for x in v2["snapshot"]["reviews"]}
    assert "更正后的甲评语" in comments and "甲的原评语" not in comments
    # 旧码保持失效, 新码可读
    assert client.get(f"/feedback-snapshots/{old_code}").status_code == 404
    assert client.get(f"/feedback-snapshots/{v2['access_code']}").status_code == 200
    # 重复发布 (同序号同收据) 幂等返回新码
    again = client.post("/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial})
    assert again.json()["changed"] is False
    assert again.json()["access_code"] == v2["access_code"]
    # 追溯: 旧版失效, 新版有效, 收据链清晰
    trace = client.get("/papers/P1/feedback-snapshots", headers=ORG).json()
    assert [(s["version"], s["active"]) for s in trace["snapshots"]] == [(1, False), (2, True)]
    assert ra["receipt"] in trace["snapshots"][0]["receipts"]
    assert corrected["receipt"] in trace["snapshots"][1]["receipts"]


# ------------------------------------------------------------ 发布序号变化使待更正失效

def test_serial_change_invalidates_pending_correction(client):
    pub, (rid_a, _), (ra, _) = setup_reviewed(client)
    serial = pub["serial"]
    request_correction(client, "P1", serial, rid_a, "请更正")

    # 补位发布推进发布序号 (方案不变, 确认与评语沿用)
    out = backfill_publish(client)
    assert out["serial"] == serial + 1

    # 待更正任务不再列出
    tasks = client.get("/reviewer/review-corrections", headers=reviewer_headers(rid_a)).json()
    assert tasks["serial"] == out["serial"] and tasks["corrections"] == []
    # 按旧序号提交 -> 409; 按新序号 + 旧收据 -> 404 (任务已失效)
    assert submit_correction(client, rid_a, "P1", serial, ra["receipt"], 5, "x").status_code == 409
    assert submit_correction(client, rid_a, "P1", out["serial"], ra["receipt"], 5, "x").status_code == 404
    # 会务方仍可在追溯中看到该请求停留于旧序号
    view = client.get("/papers/P1/reviews", headers=ORG).json()
    assert view["corrections"][0]["serial"] == serial
    assert view["corrections"][0]["state"] == "pending"


def test_backfill_carries_corrected_review_and_archives_removed(client):
    # R1/R2 初始确认并交评语; R2 的评语经更正后, 通过机构变更使 R2 被移出
    for rid in ("R1", "R2", "R3"):
        add_reviewer(client, rid, f"Inst-{rid}")
    add_paper(client, institutions=["Author-A"])
    pub = publish(client)
    serial = pub["serial"]
    rid_a, rid_b = pub["plan"]["P1"]
    decide(client, rid_a, "P1", "confirm")
    decide(client, rid_b, "P1", "confirm")
    submit_review(client, rid_a, "P1", serial, 4, "甲的评语")
    rb = submit_review(client, rid_b, "P1", serial, 2, "乙的原评语").json()

    # 会务方对乙发起更正, 乙提交更正评语 (新收据)
    request_correction(client, "P1", serial, rid_b, "评语需更正")
    corrected = submit_correction(client, rid_b, "P1", serial, rb["receipt"], 3, "乙更正后的评语").json()

    # 使 rid_b 的固定位置失效 (机构变得与 rid_a 相同) -> 补位移出 rid_b, R3 补入
    client.put(
        f"/reviewers/{rid_b}",
        headers=ORG,
        json={"credential": f"cred-{rid_b}", "topics": ["AI"],
              "institution": f"Inst-{rid_a}", "capacity": 3, "avoid_papers": []},
    )
    out = backfill_publish(client)
    assert set(out["plan"]["P1"]) == {rid_a, "R3"}
    # 保留槽位 (rid_a) 沿用原评语; 被移出的 rid_b 的更正后评语 (新收据) 进入归档
    view = client.get("/papers/P1/reviews", headers=ORG).json()
    assert view["serial"] == out["serial"]
    archived = {(a["serial"], a["reviewer_id"], a["receipt"]) for a in view["archived_reviews"]}
    assert (serial, rid_b, corrected["receipt"]) in archived
    # 更正记录完整可追溯 (原评语冻结在更正记录中)
    corr = view["corrections"][0]
    assert corr["state"] == "completed" and corr["corrected"]["receipt"] == corrected["receipt"]
    assert corr["original"]["receipt"] == rb["receipt"]


def test_backfill_retained_slot_keeps_corrected_review(client):
    pub, (rid_a, rid_b), (ra, _) = setup_reviewed(client)
    serial = pub["serial"]
    # 甲的评语更正后, 乙回避 -> 补位: 甲的确认槽位保留, 更正后的评语沿用
    request_correction(client, "P1", serial, rid_a, "请更正")
    corrected = submit_correction(client, rid_a, "P1", serial, ra["receipt"], 5, "甲更正后的评语").json()
    decide(client, rid_b, "P1", "recuse", reason="冲突")

    out = backfill_publish(client)
    assert out["carried_reviews"] == 1
    view = client.get("/reviewer/reviews", headers=reviewer_headers(rid_a)).json()
    assert view["serial"] == out["serial"]
    assert view["reviews"][0]["receipt"] == corrected["receipt"]
    assert view["reviews"][0]["comment"] == "甲更正后的评语"


# ------------------------------------------------------------ 与既有首提交接口的交互 (语义不变)

def test_first_submission_endpoint_semantics_unchanged_after_correction(client):
    pub, (rid_a, _), (ra, _) = setup_reviewed(client)
    serial = pub["serial"]
    request_correction(client, "P1", serial, rid_a, "请更正")
    corrected = submit_correction(client, rid_a, "P1", serial, ra["receipt"], 5, "更正后的评语").json()

    # 首提交接口对"更正后内容"的相同重试 -> 幂等返回更正收据
    retry = submit_review(client, rid_a, "P1", serial, 5, "更正后的评语")
    assert retry.status_code == 200
    assert retry.json()["changed"] is False
    assert retry.json()["receipt"] == corrected["receipt"]
    # 首提交接口对"原评语内容" -> 与现存 (更正后) 评语不同, 409 冲突
    assert submit_review(client, rid_a, "P1", serial, 4, "甲的原评语").status_code == 409


def test_correction_after_completion_can_be_requested_again(client):
    pub, (rid_a, _), (ra, _) = setup_reviewed(client)
    serial = pub["serial"]
    request_correction(client, "P1", serial, rid_a, "第一次更正")
    c1 = submit_correction(client, rid_a, "P1", serial, ra["receipt"], 5, "第一次更正后的评语").json()

    # 对已完成的槽位再次发起更正 (针对更正后的评语)
    r2 = request_correction(client, "P1", serial, rid_a, "第二次更正")
    assert r2.status_code == 200 and r2.json()["changed"] is True
    assert r2.json()["original"]["receipt"] == c1["receipt"]
    c2 = submit_correction(client, rid_a, "P1", serial, c1["receipt"], 3, "第二次更正后的评语").json()
    assert c2["receipt"] != c1["receipt"]
    # 第一次更正的同内容重试仍幂等返回第一次的收据
    replay = submit_correction(client, rid_a, "P1", serial, ra["receipt"], 5, "第一次更正后的评语")
    assert replay.status_code == 200 and replay.json()["receipt"] == c1["receipt"]
    # 会务方追溯两次更正
    view = client.get("/papers/P1/reviews", headers=ORG).json()
    assert [c["state"] for c in view["corrections"]] == ["completed", "completed"]
    slot = next(s for s in view["slots"] if s["reviewer_id"] == rid_a)
    assert slot["review"]["receipt"] == c2["receipt"]
