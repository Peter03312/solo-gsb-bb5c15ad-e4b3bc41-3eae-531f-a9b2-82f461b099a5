"""本轮功能测试: 面向作者的匿名反馈快照。

覆盖需求:
- 会务方沿用 X-Organizer-Key 鉴权, POST 按论文发布快照, 请求须携带当前发布序号;
- 仅当该序号下两名评审人均已确认且各提交一份正式评语时才能发布;
  未发布/论文不在当前方案 404, 序号过期/超前 409, 资料不齐 422 且不产生新版本;
- 快照只向持码者展示论文编号与按固定顺序标号 1、2 的两份评分和评语,
  不暴露评审人编号、机构、收据及内部诊断;
- 发布返回该论文的随机访问码; 相同发布序号及两份评语收据的重复请求返回原快照原码
  (changed=false); 来源变化 (补位/重新发布) 后生成新版本, 旧码立即失效,
  旧版仅供会务方追溯;
- 持码者只能读取当前有效快照; 无效码统一 404, 不透露论文是否存在。
"""
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="review-snapshots-test-")
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


def setup_complete_snapshot(client, pid="P1", score_a=4, comment_a="甲的评语",
                            score_b=2, comment_b="乙的评语"):
    """建评审人/论文 -> 发布 -> 两人确认 -> 交齐两份评语 -> 发布快照。"""
    for rid in ("R1", "R2", "R3", "R4"):
        add_reviewer(client, rid, f"Inst-{rid}")
    add_paper(client, pid)
    pub = publish(client)
    serial = pub["serial"]
    rid_a, rid_b = pub["plan"][pid]
    decide(client, rid_a, pid, "confirm")
    decide(client, rid_b, pid, "confirm")
    submit_review(client, rid_a, pid, serial, score_a, comment_a)
    submit_review(client, rid_b, pid, serial, score_b, comment_b)
    r = client.post(
        f"/papers/{pid}/feedback-snapshot",
        headers=ORG,
        json={"serial": serial},
    )
    assert r.status_code == 200, r.text
    return pub, r.json(), (rid_a, rid_b)


def read_snapshot(client, code):
    return client.get(f"/feedback-snapshots/{code}")


# ------------------------------------------------------------ 发布与持码读取

def test_publish_snapshot_requires_organizer_key(client):
    for rid in ("R1", "R2", "R3", "R4"):
        add_reviewer(client, rid, f"Inst-{rid}")
    add_paper(client)
    pub = publish(client)
    assert client.post(
        "/papers/P1/feedback-snapshot", json={"serial": pub["serial"]}
    ).status_code == 401
    assert client.post(
        "/papers/P1/feedback-snapshot", headers=BAD_ORG, json={"serial": pub["serial"]}
    ).status_code == 401


def test_publish_succeeds_when_both_confirmed_and_submitted(client):
    pub, body, (rid_a, rid_b) = setup_complete_snapshot(client)
    assert body["changed"] is True
    assert body["version"] == 1
    assert body["serial"] == pub["serial"]
    assert body["access_code"].startswith("fbk-")
    snap = body["snapshot"]
    assert snap["paper_id"] == "P1"
    assert [x["label"] for x in snap["reviews"]] == [1, 2]
    assert snap["reviews"][0]["score"] == 4 and snap["reviews"][0]["comment"] == "甲的评语"
    assert snap["reviews"][1]["score"] == 2 and snap["reviews"][1]["comment"] == "乙的评语"


def test_code_holder_reads_only_anonymized_fields(client):
    _, body, (rid_a, rid_b) = setup_complete_snapshot(client)
    r = read_snapshot(client, body["access_code"])
    assert r.status_code == 200, r.text
    raw = r.text
    # 不含评审人编号/凭据、机构、收据、发布序号/版本、内部诊断
    for secret in (rid_a, rid_b, "Inst-", "rvw-", '"serial"', '"version"',
                   "receipt", "institution", "Author-P1"):
        assert secret not in raw, f"快照泄露内部字段: {secret}"
    # 无需任何凭据即可读取
    assert read_snapshot(client, body["access_code"]).status_code == 200


def test_invalid_codes_uniformly_rejected_no_existence_leak(client):
    setup_complete_snapshot(client)
    for bad in ("fbk-deadbeef", "nope", "", "fbk-" + "00" * 16):
        assert read_snapshot(client, bad).status_code == 404
    detail = read_snapshot(client, "fbk-deadbeef").json()["detail"]
    assert "P1" not in str(detail) and "不存在" not in str(detail)


# ------------------------------------------------------------ 发布前校验

def test_publish_without_any_publication_is_404(client):
    add_reviewer(client, "R1", "I1")
    add_paper(client)
    r = client.post("/papers/P1/feedback-snapshot", headers=ORG, json={"serial": 1})
    assert r.status_code == 404


def test_publish_paper_not_in_plan_is_404(client):
    for rid in ("R1", "R2", "R3", "R4"):
        add_reviewer(client, rid, f"Inst-{rid}")
    add_paper(client)
    pub = publish(client)
    r = client.post("/papers/NOPE/feedback-snapshot", headers=ORG,
                    json={"serial": pub["serial"]})
    assert r.status_code == 404


def test_stale_or_future_serial_rejected_409(client):
    for rid in ("R1", "R2", "R3", "R4"):
        add_reviewer(client, rid, f"Inst-{rid}")
    add_paper(client)
    pub = publish(client)
    # 超前序号 -> 409
    r = client.post("/papers/P1/feedback-snapshot", headers=ORG,
                    json={"serial": pub["serial"] + 1})
    assert r.status_code == 409
    # 推进发布序号后, 旧 (过期) 序号 -> 409
    out = backfill_publish(client)
    assert out["serial"] == pub["serial"] + 1
    r = client.post("/papers/P1/feedback-snapshot", headers=ORG,
                    json={"serial": pub["serial"]})
    assert r.status_code == 409


def test_incomplete_materials_rejected_422_and_no_version(client):
    for rid in ("R1", "R2", "R3", "R4"):
        add_reviewer(client, rid, f"Inst-{rid}")
    add_paper(client)
    pub = publish(client)
    serial = pub["serial"]
    rid_a, rid_b = pub["plan"]["P1"]

    # 全部 pending -> 422
    r = client.post("/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial})
    assert r.status_code == 422
    assert r.json()["detail"]["confirmed"] == 0 and r.json()["detail"]["submitted"] == 0

    # 一人确认但评语未交齐 -> 仍 422
    decide(client, rid_a, "P1", "confirm")
    submit_review(client, rid_a, "P1", serial, 4, "仅甲交了")
    r = client.post("/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["confirmed"] == 1 and detail["submitted"] == 1

    # 乙确认但未交评语 -> 仍拒绝
    decide(client, rid_b, "P1", "confirm")
    assert client.post(
        "/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial}
    ).status_code == 422

    # 拒绝不留下任何快照版本, 且任意"码"均无效
    submit_review(client, rid_b, "P1", serial, 2, "乙补齐")
    trace = client.get("/papers/P1/feedback-snapshots", headers=ORG).json()
    assert trace["snapshots"] == []
    assert read_snapshot(client, "fbk-anything").status_code == 404


def test_recused_slot_blocks_snapshot(client):
    for rid in ("R1", "R2", "R3", "R4"):
        add_reviewer(client, rid, f"Inst-{rid}")
    add_paper(client)
    pub = publish(client)
    serial = pub["serial"]
    rid_a, rid_b = pub["plan"]["P1"]
    decide(client, rid_a, "P1", "confirm")
    decide(client, rid_b, "P1", "recuse", reason="利益冲突")
    # 已回避槽位无法满足"两名评审人均确认" -> 422
    r = client.post("/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial})
    assert r.status_code == 422


# ------------------------------------------------------------ 幂等

def test_repeated_request_returns_same_snapshot_and_code(client):
    _, first, _ = setup_complete_snapshot(client)
    serial = first["serial"]
    again = client.post(
        "/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial}
    )
    assert again.status_code == 200
    j = again.json()
    assert j["changed"] is False
    assert j["access_code"] == first["access_code"]
    assert j["version"] == first["version"]
    assert j["snapshot"] == first["snapshot"]
    trace = client.get("/papers/P1/feedback-snapshots", headers=ORG).json()
    assert len(trace["snapshots"]) == 1


# ------------------------------------------------------------ 新版本 / 旧码失效

def test_backfill_creates_new_version_and_invalidates_old_code(client):
    pub, v1, _ = setup_complete_snapshot(client)
    old_code = v1["access_code"]
    assert read_snapshot(client, old_code).status_code == 200

    # 补位发布推进发布序号 (当前方案不变, 仅 serial+1)
    out = backfill_publish(client)
    assert out["serial"] == pub["serial"] + 1

    # 旧码立即失效 (仍找不到"论文是否存在"的任何线索)
    r = read_snapshot(client, old_code)
    assert r.status_code == 404

    # 同一 serial 的旧请求 -> 409, 不会返回旧快照
    r = client.post("/papers/P1/feedback-snapshot", headers=ORG,
                    json={"serial": pub["serial"]})
    assert r.status_code == 409

    # 新序号下两人确认状态已沿用, 且评语随槽位沿用 (同收据);
    # 两份评语收据相同但来源 (serial) 变化 -> 生成新版本与新码
    r = client.post("/papers/P1/feedback-snapshot", headers=ORG,
                    json={"serial": out["serial"]})
    assert r.status_code == 200, r.text
    v2 = r.json()
    assert v2["changed"] is True
    assert v2["version"] == 2
    assert v2["access_code"] != old_code
    assert v2["snapshot"] == v1["snapshot"]
    assert read_snapshot(client, old_code).status_code == 404
    assert read_snapshot(client, v2["access_code"]).status_code == 200


def test_normal_republish_new_reviews_make_new_version(client):
    _, v1, _ = setup_complete_snapshot(client)
    old_code = v1["access_code"]
    serial1 = v1["serial"]

    # 普通重新发布: 序号 +1, 评语不沿用, 旧码失效
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    repub = client.post("/assignment/publish", headers=ORG,
                        json={"base_revision": rev}).json()
    assert repub["serial"] == serial1 + 1
    assert read_snapshot(client, old_code).status_code == 404
    # 新序号槽位 pending, 资料不齐 -> 422 不产生版本
    assert client.post(
        "/papers/P1/feedback-snapshot", headers=ORG,
        json={"serial": repub["serial"]}
    ).status_code == 422

    rid_a, rid_b = repub["plan"]["P1"]
    decide(client, rid_a, "P1", "confirm")
    decide(client, rid_b, "P1", "confirm")
    submit_review(client, rid_a, "P1", repub["serial"], 5, "新版甲评语")
    submit_review(client, rid_b, "P1", repub["serial"], 3, "新版乙评语")
    r = client.post("/papers/P1/feedback-snapshot", headers=ORG,
                    json={"serial": repub["serial"]})
    assert r.status_code == 200
    v2 = r.json()
    assert v2["version"] == 2 and v2["access_code"] != old_code
    assert read_snapshot(client, v2["access_code"]).status_code == 200

    # 会务方可追溯全部旧版 (含已失效版本与收据), 且标出当前有效版
    trace = client.get("/papers/P1/feedback-snapshots", headers=ORG).json()
    assert [s["version"] for s in trace["snapshots"]] == [1, 2]
    assert [s["active"] for s in trace["snapshots"]] == [False, True]
    assert trace["snapshots"][0]["serial"] == serial1
    assert all(
        s["receipts"] and all(x.startswith("rvw-") for x in s["receipts"])
        for s in trace["snapshots"]
    )


def test_trace_requires_organizer_key(client):
    _, _, _ = setup_complete_snapshot(client)
    assert client.get("/papers/P1/feedback-snapshots").status_code == 401
    assert client.get("/papers/P1/feedback-snapshots", headers=BAD_ORG).status_code == 401


def test_trace_for_unknown_paper_is_empty_not_404(client):
    # 会务方追溯不存在的论文: 空版本列表即可, 无需区分
    r = client.get("/papers/NOPE/feedback-snapshots", headers=ORG)
    assert r.status_code == 200
    assert r.json()["snapshots"] == []


# ------------------------------------------------------------ 多论文 / 顺序

def test_two_papers_have_independent_snapshots(client):
    for rid in ("R1", "R2", "R3", "R4"):
        add_reviewer(client, rid, f"Inst-{rid}")
    add_paper(client, "P1")
    add_paper(client, "P2")
    pub = publish(client)
    serial = pub["serial"]
    plans = pub["plan"]
    # P1 交齐, P2 仅一人交
    for pid, want_complete in (("P1", True), ("P2", False)):
        a, b = plans[pid]
        decide(client, a, pid, "confirm")
        decide(client, b, pid, "confirm")
        submit_review(client, a, pid, serial, 4, f"{pid}-评语一")
        if want_complete:
            submit_review(client, b, pid, serial, 3, f"{pid}-评语二")
    r1 = client.post("/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial})
    r2 = client.post("/papers/P2/feedback-snapshot", headers=ORG, json={"serial": serial})
    assert r1.status_code == 200 and r2.status_code == 422
    snap = read_snapshot(client, r1.json()["access_code"]).json()
    assert snap["paper_id"] == "P1"
    comments = [x["comment"] for x in snap["reviews"]]
    assert comments == ["P1-评语一", "P1-评语二"]
