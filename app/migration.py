"""会务方实例迁移: 一致业务数据快照的导出与恢复。

导出 (GET /migration/export):
- 在 BEGIN IMMEDIATE 写事务内读取全部业务表, 与并发写入隔离, 得到一致快照;
- 快照覆盖全部现存业务记录: 论文 (含已撤回) 与评审人资料 (含评审人凭据)、
  资格变更记录、硬回避、资料修订号与当前发布版 (发布序号/方案/解释)、
  普通分配锁定表、机构归并边、评审保障等级、统一评审截止、论文撤回记录、
  论文删除凭据 (paper_deletions: 每次删除一行, 含同编号重录后的再次删除)、
  评审人决定、正式评语、评语更正记录、匿名反馈快照 (含访问码) 与作者异议
  (含查询凭据); 不包含会务方密钥 (密钥是实例本地配置, 不属于业务数据);
- 当前导出格式版本为 v2 (v1 无 paper_deletions, 仍可恢复, 恢复沿用原有判断);
- 快照附格式版本 (format/format_version) 与内容校验和 (checksum,
  对 data 的规范化 JSON 取 SHA-256), 供恢复方核对格式与内容完整性。

恢复 (POST /migration/restore):
- 先核对格式标识/格式版本与内容校验和, 再校验记录结构与引用符合历史保留规则
  (如: 锁定与保障等级只指向现存未撤回论文、撤回记录与 withdrawn 标记一一对应、
  异议必须指向现存快照版本且序号一致、按发布序号隔离的记录不得越过当前发布序号、
  当前序号的决定/评语必须落在当前发布方案槽位上、各表主键/唯一约束不冲突);
  历史记录 (决定/评语/回避/资格变更/更正/快照/异议) 按保留规则可指向已删除的
  论文或评审人, 不作存在性要求;
- **访问码效力的跨记录核验** (防"旧快照失效标记被改回有效并重算校验和"的伪造快照):
  不只看 feedback_snapshots.invalidated 标记本身, 而是依据快照版本 (同论文同发布
  序号内仅最高版本可能有效)、评语更正请求 (不晚于更正请求发起时刻创建的快照必已
  随请求失效; 组内存在待完成更正请求时全部失效)、论文撤回记录 (撤回稿全部快照
  失效) 与 **v2 新增的论文删除凭据** (删除时刻及之前创建的快照必已随删除失效,
  同编号重新录入不恢复旧码) 重算每个访问码的应有效力, 并与快照标记逐行比对;
  对当前发布序号上仍标记有效的快照, 还要其两份评语收据与当前发布方案槽位上的
  现行评语收据一致 (更正会原地更换收据, 故仅凭改标记/改时间无法让旧快照复活)。
  **v1 旧格式没有删除记载, 恢复完全沿用原有判断, 不推断删除历史。**
  任何不一致即"矛盾快照" 422 拒绝; 合法的连续更正、同序号再次发布、跨发布序号
  历史快照与"删除后同编号重录"均可迁移, 恢复后仅当前有效码可读快照并提交异议,
  历史内容仍供会务方追溯;
- 仅接受尚无业务记录 (全部业务表为空且资料修订号为 0) 且从未恢复过的目标实例;
  校验失败 (422)、目标非空或重复恢复 (409) 一律拒绝且不留下部分数据——
  全部写入在同一个 BEGIN IMMEDIATE 写事务内完成, 任一失败整体回滚;
- 恢复成功后资料修订号、发布序号、历史记录、评审人凭据、反馈访问码与
  异议查询凭据按原规则继续工作; 恢复本身不推进修订号与发布序号。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from . import db

# 快照格式标识与当前支持的格式版本
#
# 版本历史:
#   v1: 初始格式 (撤回记录 paper_withdrawals; 删除论文不保留任何凭据——
#       已删除论文的失效快照在跨记录核验中没有失效依据);
#   v2: 新增 data.paper_deletions 删除凭据: 会务方删除论文在同一事务内失效该稿
#       全部旧快照访问码、过期待处理异议, 并为每次删除 (含同编号多次"删除->重录")
#       追加可核验的凭据行。恢复时删除记录参与访问码效力重算——旧格式快照没有
#       删除记载, 恢复仍完全沿用 v1 的判断, 不推断删除历史。
FORMAT = "review-assignment-migration"
FORMAT_VERSION = 2
SUPPORTED_FORMAT_VERSIONS = (1, 2)

# 业务表 (恢复目标空性检查 = 这些表全部为空且资料修订号为 0;
# migration_restores 是实例本地元数据, 不属于业务数据)
BUSINESS_TABLES = (
    "papers",
    "reviewers",
    "published",
    "assignment_decisions",
    "submitted_reviews",
    "reviewer_recusals",
    "reviewer_status_changes",
    "feedback_snapshots",
    "institution_merges",
    "assignment_locks",
    "review_corrections",
    "feedback_objections",
    "paper_withdrawals",
    "paper_guarantee_levels",
    "review_deadlines",
    "paper_deletions",
)

# data 中以行数组形式出现的表
_LIST_TABLES_V1 = (
    "papers",
    "reviewers",
    "assignment_decisions",
    "submitted_reviews",
    "reviewer_recusals",
    "reviewer_status_changes",
    "feedback_snapshots",
    "institution_merges",
    "assignment_locks",
    "review_corrections",
    "feedback_objections",
    "paper_withdrawals",
    "paper_guarantee_levels",
    "review_deadlines",
)
_LIST_TABLES_V2 = _LIST_TABLES_V1 + ("paper_deletions",)


def list_tables_for(format_version: int) -> tuple:
    return _LIST_TABLES_V2 if format_version >= 2 else _LIST_TABLES_V1


def data_keys_for(format_version: int) -> tuple:
    return ("revision", "published") + list_tables_for(format_version)


# 当前格式版本导出的行表 (兼容别名; 旧代码/测试引用)
_LIST_TABLES = _LIST_TABLES_V2
_DATA_KEYS = ("revision", "published") + _LIST_TABLES


class SnapshotValidationError(ValueError):
    """快照格式、内容完整性或记录引用校验失败 (对应 HTTP 422)。"""


class RestoreConflictError(RuntimeError):
    """目标实例非空或已执行过恢复 (对应 HTTP 409)。"""


# ---------------------------------------------------------------- 校验和

def _canonical_json(obj) -> str:
    """规范化 JSON 序列化 (键排序、紧凑分隔符、保留非 ASCII), 校验和的输入。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def snapshot_checksum(data) -> str:
    """对快照 data 对象计算内容校验和 (sha256:<hex>)。"""
    digest = hashlib.sha256(_canonical_json(data).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# ---------------------------------------------------------------- 导出

def collect_snapshot(conn: sqlite3.Connection) -> dict:
    """在调用方事务内读取全部业务表, 返回快照 data 对象 (行序确定)。

    JSON 列反序列化为原生 JSON 值, 布尔标记导出为 true/false;
    评审人凭据、反馈访问码与异议查询凭据原样保留; 不含会务方密钥。
    """
    published_row = conn.execute(
        "SELECT serial, revision, plan, explanations, published_at"
        " FROM published WHERE id = 1"
    ).fetchone()
    data = {
        "revision": db.get_revision(conn),
        "published": (
            None
            if published_row is None
            else {
                "serial": published_row["serial"],
                "revision": published_row["revision"],
                "plan": json.loads(published_row["plan"]),
                "explanations": json.loads(published_row["explanations"]),
                "published_at": published_row["published_at"],
            }
        ),
    }
    data["papers"] = [
        {
            "paper_id": r["paper_id"],
            "manuscript": r["manuscript"],
            "topics": json.loads(r["topics"]),
            "institutions": json.loads(r["institutions"]),
            "withdrawn": bool(r["withdrawn"]),
        }
        for r in conn.execute(
            "SELECT paper_id, manuscript, topics, institutions, withdrawn"
            " FROM papers ORDER BY paper_id"
        ).fetchall()
    ]
    data["reviewers"] = [
        {
            "reviewer_id": r["reviewer_id"],
            "credential": r["credential"],
            "topics": json.loads(r["topics"]),
            "institution": r["institution"],
            "capacity": r["capacity"],
            "avoid_papers": json.loads(r["avoid_papers"]),
            "active": bool(r["active"]),
        }
        for r in conn.execute(
            "SELECT reviewer_id, credential, topics, institution, capacity,"
            " avoid_papers, active FROM reviewers ORDER BY reviewer_id"
        ).fetchall()
    ]
    data["assignment_decisions"] = [
        {
            "serial": r["serial"],
            "paper_id": r["paper_id"],
            "reviewer_id": r["reviewer_id"],
            "state": r["state"],
            "reason": r["reason"],
            "decided_at": r["decided_at"],
        }
        for r in conn.execute(
            "SELECT serial, paper_id, reviewer_id, state, reason, decided_at"
            " FROM assignment_decisions ORDER BY serial, paper_id, reviewer_id"
        ).fetchall()
    ]
    data["submitted_reviews"] = [
        {
            "serial": r["serial"],
            "paper_id": r["paper_id"],
            "reviewer_id": r["reviewer_id"],
            "score": r["score"],
            "comment": r["comment"],
            "receipt": r["receipt"],
            "submitted_at": r["submitted_at"],
        }
        for r in conn.execute(
            "SELECT serial, paper_id, reviewer_id, score, comment, receipt, submitted_at"
            " FROM submitted_reviews ORDER BY serial, paper_id, reviewer_id"
        ).fetchall()
    ]
    data["reviewer_recusals"] = [
        {
            "reviewer_id": r["reviewer_id"],
            "paper_id": r["paper_id"],
            "reason": r["reason"],
            "created_serial": r["created_serial"],
            "created_revision": r["created_revision"],
            "created_at": r["created_at"],
        }
        for r in conn.execute(
            "SELECT reviewer_id, paper_id, reason, created_serial, created_revision,"
            " created_at FROM reviewer_recusals ORDER BY reviewer_id, paper_id"
        ).fetchall()
    ]
    data["reviewer_status_changes"] = [
        {
            "id": r["id"],
            "reviewer_id": r["reviewer_id"],
            "active": bool(r["active"]),
            "reason": r["reason"],
            "revision": r["revision"],
            "changed_at": r["changed_at"],
        }
        for r in conn.execute(
            "SELECT id, reviewer_id, active, reason, revision, changed_at"
            " FROM reviewer_status_changes ORDER BY id"
        ).fetchall()
    ]
    data["feedback_snapshots"] = [
        {
            "id": r["id"],
            "paper_id": r["paper_id"],
            "version": r["version"],
            "serial": r["serial"],
            "receipt1": r["receipt1"],
            "receipt2": r["receipt2"],
            "access_code": r["access_code"],
            "review1": json.loads(r["review1"]),
            "review2": json.loads(r["review2"]),
            "created_at": r["created_at"],
            "invalidated": bool(r["invalidated"]),
        }
        for r in conn.execute(
            "SELECT id, paper_id, version, serial, receipt1, receipt2, access_code,"
            " review1, review2, created_at, invalidated"
            " FROM feedback_snapshots ORDER BY id"
        ).fetchall()
    ]
    data["institution_merges"] = [
        {
            "id": r["id"],
            "name_a": r["name_a"],
            "name_b": r["name_b"],
            "revision": r["revision"],
            "merged_at": r["merged_at"],
        }
        for r in conn.execute(
            "SELECT id, name_a, name_b, revision, merged_at"
            " FROM institution_merges ORDER BY id"
        ).fetchall()
    ]
    data["assignment_locks"] = [
        {
            "paper_id": r["paper_id"],
            "reviewers": json.loads(r["reviewers_json"]),
            "updated_at": r["updated_at"],
        }
        for r in conn.execute(
            "SELECT paper_id, reviewers_json, updated_at"
            " FROM assignment_locks ORDER BY paper_id"
        ).fetchall()
    ]
    data["review_corrections"] = [
        {
            "id": r["id"],
            "serial": r["serial"],
            "paper_id": r["paper_id"],
            "reviewer_id": r["reviewer_id"],
            "reason": r["reason"],
            "original_score": r["original_score"],
            "original_comment": r["original_comment"],
            "original_receipt": r["original_receipt"],
            "original_submitted_at": r["original_submitted_at"],
            "state": r["state"],
            "new_score": r["new_score"],
            "new_comment": r["new_comment"],
            "new_receipt": r["new_receipt"],
            "requested_at": r["requested_at"],
            "corrected_at": r["corrected_at"],
        }
        for r in conn.execute(
            "SELECT id, serial, paper_id, reviewer_id, reason, original_score,"
            " original_comment, original_receipt, original_submitted_at, state,"
            " new_score, new_comment, new_receipt, requested_at, corrected_at"
            " FROM review_corrections ORDER BY id"
        ).fetchall()
    ]
    data["feedback_objections"] = [
        {
            "id": r["id"],
            "snapshot_id": r["snapshot_id"],
            "paper_id": r["paper_id"],
            "serial": r["serial"],
            "label": r["label"],
            "reason": r["reason"],
            "frozen_review": json.loads(r["frozen_review_json"]),
            "target_receipt": r["target_receipt"],
            "state": r["state"],
            "query_token": r["query_token"],
            "resolution_note": r["resolution_note"],
            "correction_id": r["correction_id"],
            "created_at": r["created_at"],
            "decided_at": r["decided_at"],
        }
        for r in conn.execute(
            "SELECT id, snapshot_id, paper_id, serial, label, reason,"
            " frozen_review_json, target_receipt, state, query_token,"
            " resolution_note, correction_id, created_at, decided_at"
            " FROM feedback_objections ORDER BY id"
        ).fetchall()
    ]
    data["paper_withdrawals"] = [
        {
            "paper_id": r["paper_id"],
            "reason": r["reason"],
            "revision": r["revision"],
            "published_serial": r["published_serial"],
            "published_plan": (
                json.loads(r["published_plan_json"])
                if r["published_plan_json"] is not None
                else None
            ),
            "withdrawn_at": r["withdrawn_at"],
        }
        for r in conn.execute(
            "SELECT paper_id, reason, revision, published_serial,"
            " published_plan_json, withdrawn_at"
            " FROM paper_withdrawals ORDER BY paper_id"
        ).fetchall()
    ]
    data["paper_deletions"] = [
        {
            "id": r["id"],
            "paper_id": r["paper_id"],
            "revision": r["revision"],
            "published_serial": r["published_serial"],
            "published_plan": (
                json.loads(r["published_plan_json"])
                if r["published_plan_json"] is not None
                else None
            ),
            "invalidated_snapshots": r["invalidated_snapshots"],
            "expired_objections": r["expired_objections"],
            "deleted_at": r["deleted_at"],
        }
        for r in conn.execute(
            "SELECT id, paper_id, revision, published_serial, published_plan_json,"
            " invalidated_snapshots, expired_objections, deleted_at"
            " FROM paper_deletions ORDER BY id"
        ).fetchall()
    ]
    data["paper_guarantee_levels"] = [
        {
            "paper_id": r["paper_id"],
            "level": r["level"],
            "revision": r["revision"],
            "updated_at": r["updated_at"],
        }
        for r in conn.execute(
            "SELECT paper_id, level, revision, updated_at"
            " FROM paper_guarantee_levels ORDER BY paper_id"
        ).fetchall()
    ]
    data["review_deadlines"] = [
        {
            "serial": r["serial"],
            "revision": r["revision"],
            "deadline_at": r["deadline_at"],
            "set_at": r["set_at"],
        }
        for r in conn.execute(
            "SELECT serial, revision, deadline_at, set_at"
            " FROM review_deadlines ORDER BY serial"
        ).fetchall()
    ]
    return data


# ---------------------------------------------------------------- 校验

def _fail(where: str, msg: str):
    raise SnapshotValidationError(f"快照内容非法 ({where}): {msg}")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_str(v) -> bool:
    return isinstance(v, str)


def _is_nonempty_str(v) -> bool:
    return isinstance(v, str) and v != ""


def _check_fields(row, spec: dict, where: str):
    """按 {字段: 校验谓词} 核对一行; 字段必须恰好为 spec 的键集合。"""
    if not isinstance(row, dict):
        _fail(where, "行必须是对象")
    unknown = sorted(set(row) - set(spec))
    if unknown:
        _fail(where, f"含未知字段 {unknown}")
    for name, ok in spec.items():
        if name not in row:
            _fail(where, f"缺少字段 {name}")
        if not ok(row[name]):
            _fail(where, f"字段 {name} 取值非法: {row[name]!r}")


def _check_unique(seen: set, key, where: str, what: str):
    if key in seen:
        _fail(where, f"{what} 重复: {key!r}")
    seen.add(key)


def _check_frozen_review(value, where: str):
    _check_fields(
        value,
        {
            "score": lambda v: _is_int(v) and 1 <= v <= 5,
            "comment": _is_nonempty_str,
        },
        where,
    )


def validate_envelope(body) -> int:
    """核对恢复请求信封: 格式标识、格式版本、内容校验和 (不核对 data 内部结构)。

    返回快照携带的格式版本; 支持 v1 (旧格式, 无删除凭据) 与 v2 (含 paper_deletions)。
    """
    if not isinstance(body, dict):
        raise SnapshotValidationError("恢复请求体必须是 JSON 对象 (导出的快照文件)")
    if body.get("format") != FORMAT:
        raise SnapshotValidationError(
            f"格式标识不符: 期望 {FORMAT!r}, 实际 {body.get('format')!r}"
        )
    version = body.get("format_version")
    if version not in SUPPORTED_FORMAT_VERSIONS:
        raise SnapshotValidationError(
            f"不支持的格式版本: {version!r}"
            f" (本实例支持 {sorted(SUPPORTED_FORMAT_VERSIONS)})"
        )
    checksum = body.get("checksum")
    if not _is_nonempty_str(checksum) or not checksum.startswith("sha256:"):
        raise SnapshotValidationError("缺少内容校验和 checksum (sha256:<hex>)")
    data = body.get("data")
    if not isinstance(data, dict):
        raise SnapshotValidationError("缺少快照内容 data (必须是对象)")
    if snapshot_checksum(data) != checksum:
        raise SnapshotValidationError(
            "内容校验和不符: 快照已损坏或被篡改, 拒绝恢复"
        )
    return version


def validate_snapshot(data, *, format_version: int = FORMAT_VERSION) -> None:
    """校验快照 data 的结构与记录引用 (符合历史保留规则)。

    format_version:
      - v1: 旧格式, data 不含 paper_deletions; 访问码效力只按 快照版本链、
        评语更正请求、撤回记录重算, **不因缺少删除记载而推断删除历史**
        (沿用原有恢复判断);
      - v2: data 必须含 paper_deletions; 删除凭据参与重算——删除时刻
        (deleted_at) 之前创建的快照必已随删除失效, 同编号重录后旧码不复活;
        且 pending 异议不得指向已撤回/已删除失效的快照。

    历史保留规则: 决定/评语/硬回避/资格变更/更正/快照/异议等历史记录永不删除,
    可指向已被删除的论文或评审人, 因此不对这些记录做论文/评审人存在性要求;
    但锁定表与保障等级随论文删除/撤回级联清除、撤回记录与 withdrawn 标记一一对应、
    异议必须指向现存快照版本、按发布序号隔离的记录不得越过当前发布序号、
    当前序号的决定/评语必须落在当前发布方案槽位上——这些不变量必须成立。

    访问码效力不接受快照自报的 invalidated 标记: 另由 快照版本链、评语更正请求的
    发起时刻/待完成状态、论文撤回记录与 (v2) 论文删除凭据重算每个快照的应失效状态
    并逐行比对; 当前发布序号上标记有效的快照, 其两份评语收据还必须等于当前方案槽位
    的现行评语收据。任一不一致即矛盾快照 422, 防止"改回失效标记并重算校验和"使旧
    访问码复活、再次读取已更正/已删除反馈或提交异议。
    """
    if not isinstance(data, dict):
        _fail("data", "必须是对象")
    data_keys = data_keys_for(format_version)
    unknown = sorted(set(data) - set(data_keys))
    if unknown:
        _fail("data", f"含未知字段 {unknown}")
    for key in data_keys:
        if key not in data:
            _fail("data", f"缺少字段 {key}")

    revision = data["revision"]
    if not _is_int(revision) or revision < 0:
        _fail("data.revision", "资料修订号必须是 >= 0 的整数")
    for key in list_tables_for(format_version):
        if not isinstance(data[key], list):
            _fail(f"data.{key}", "必须是数组")

    # ---- 当前发布版 (可无) 与发布序号上限
    published = data["published"]
    if published is not None:
        _check_fields(
            published,
            {
                "serial": lambda v: _is_int(v) and v >= 1,
                "revision": lambda v: _is_int(v) and v >= 0,
                "plan": lambda v: isinstance(v, dict),
                "explanations": lambda v: isinstance(v, dict),
                "published_at": _is_nonempty_str,
            },
            "data.published",
        )
        for pid, pair in published["plan"].items():
            if (
                not _is_nonempty_str(pid)
                or not isinstance(pair, list)
                or len(pair) != 2
                or not all(_is_nonempty_str(r) for r in pair)
                or len(set(pair)) != 2
            ):
                _fail("data.published.plan", f"论文 {pid!r} 的槽位必须是 2 名不同评审人")
        if published["revision"] > revision:
            _fail(
                "data.published",
                f"发布版所依据的修订号 {published['revision']} 超过当前修订号 {revision}",
            )
    max_serial = published["serial"] if published is not None else 0
    current_slots = set()
    if published is not None:
        for pid, pair in published["plan"].items():
            for rid in pair:
                current_slots.add((pid, rid))

    def _check_serial(serial, where):
        if not _is_int(serial) or serial < 1:
            _fail(where, f"发布序号必须是 >= 1 的整数: {serial!r}")
        if serial > max_serial:
            _fail(
                where,
                f"记录引用的发布序号 {serial} 超过当前发布序号 {max_serial}"
                " (按发布序号隔离的记录不得越过当前发布版)",
            )

    def _check_stamped_revision(rev, where):
        # 记录携带的修订号不得越过当前资料修订号; 下界为 0
        # (空资料发布空方案后, 其截止行的核对修订号可为 0)
        if not _is_int(rev) or rev < 0 or rev > revision:
            _fail(
                where,
                f"记录携带的修订号 {rev!r} 非法 (须为 0..{revision},"
                " 不得越过当前资料修订号)",
            )

    # ---- 论文与评审人资料
    paper_ids, withdrawn_ids = set(), set()
    for p in data["papers"]:
        where = f"data.papers[{p.get('paper_id')!r}]" if isinstance(p, dict) else "data.papers"
        _check_fields(
            p,
            {
                "paper_id": _is_nonempty_str,
                "manuscript": _is_str,
                "topics": lambda v: isinstance(v, list) and all(_is_str(t) for t in v),
                "institutions": lambda v: isinstance(v, list) and all(_is_str(t) for t in v),
                "withdrawn": lambda v: isinstance(v, bool),
            },
            where,
        )
        _check_unique(paper_ids, p["paper_id"], where, "论文编号")
        if p["withdrawn"]:
            withdrawn_ids.add(p["paper_id"])

    reviewer_ids = set()
    for r in data["reviewers"]:
        where = (
            f"data.reviewers[{r.get('reviewer_id')!r}]" if isinstance(r, dict) else "data.reviewers"
        )
        _check_fields(
            r,
            {
                "reviewer_id": _is_nonempty_str,
                "credential": _is_nonempty_str,
                "topics": lambda v: isinstance(v, list) and all(_is_str(t) for t in v),
                "institution": _is_nonempty_str,
                "capacity": lambda v: _is_int(v) and v >= 1,
                "avoid_papers": lambda v: isinstance(v, list) and all(_is_str(t) for t in v),
                "active": lambda v: isinstance(v, bool),
            },
            where,
        )
        _check_unique(reviewer_ids, r["reviewer_id"], where, "评审人编号")

    # ---- 决定与正式评语 (历史记录: 可指向已删除的论文/评审人, 不查存在性)
    seen = set()
    for d in data["assignment_decisions"]:
        where = "data.assignment_decisions"
        _check_fields(
            d,
            {
                "serial": _is_int,
                "paper_id": _is_nonempty_str,
                "reviewer_id": _is_nonempty_str,
                "state": lambda v: v in ("confirmed", "recused"),
                "reason": lambda v: v is None or _is_nonempty_str(v),
                "decided_at": _is_nonempty_str,
            },
            where,
        )
        _check_serial(d["serial"], where)
        _check_unique(seen, (d["serial"], d["paper_id"], d["reviewer_id"]), where, "决定槽位")
        if d["state"] == "recused" and not _is_nonempty_str(d["reason"]):
            _fail(where, "回避决定必须携带非空回避原因")
        if d["state"] == "confirmed" and d["reason"] is not None:
            _fail(where, "确认决定不得携带回避原因")
        if d["serial"] == max_serial and (d["paper_id"], d["reviewer_id"]) not in current_slots:
            _fail(
                where,
                f"当前发布序号的决定 ({d['paper_id']}, {d['reviewer_id']})"
                " 不在当前发布方案槽位上",
            )

    seen = set()
    for r in data["submitted_reviews"]:
        where = "data.submitted_reviews"
        _check_fields(
            r,
            {
                "serial": _is_int,
                "paper_id": _is_nonempty_str,
                "reviewer_id": _is_nonempty_str,
                "score": lambda v: _is_int(v) and 1 <= v <= 5,
                "comment": _is_nonempty_str,
                "receipt": _is_nonempty_str,
                "submitted_at": _is_nonempty_str,
            },
            where,
        )
        _check_serial(r["serial"], where)
        _check_unique(seen, (r["serial"], r["paper_id"], r["reviewer_id"]), where, "评语槽位")
        if r["serial"] == max_serial and (r["paper_id"], r["reviewer_id"]) not in current_slots:
            _fail(
                where,
                f"当前发布序号的评语 ({r['paper_id']}, {r['reviewer_id']})"
                " 不在当前发布方案槽位上",
            )

    # ---- 硬回避 / 资格变更记录 (历史保留, 不查论文/评审人存在性)
    seen = set()
    for r in data["reviewer_recusals"]:
        where = "data.reviewer_recusals"
        _check_fields(
            r,
            {
                "reviewer_id": _is_nonempty_str,
                "paper_id": _is_nonempty_str,
                "reason": _is_nonempty_str,
                "created_serial": _is_int,
                "created_revision": _is_int,
                "created_at": _is_nonempty_str,
            },
            where,
        )
        _check_serial(r["created_serial"], where)
        _check_stamped_revision(r["created_revision"], where)
        _check_unique(seen, (r["reviewer_id"], r["paper_id"]), where, "硬回避")

    seen = set()
    for s in data["reviewer_status_changes"]:
        where = "data.reviewer_status_changes"
        _check_fields(
            s,
            {
                "id": lambda v: _is_int(v) and v >= 1,
                "reviewer_id": _is_nonempty_str,
                "active": lambda v: isinstance(v, bool),
                "reason": _is_nonempty_str,
                "revision": _is_int,
                "changed_at": _is_nonempty_str,
            },
            where,
        )
        _check_unique(seen, s["id"], where, "资格变更记录 id")
        _check_stamped_revision(s["revision"], where)

    # ---- 匿名反馈快照 (历史保留: 可指向已删除论文; 访问码全局唯一)
    snapshot_ids, access_codes, seen = set(), set(), set()
    snapshot_by_id = {}
    for s in data["feedback_snapshots"]:
        where = "data.feedback_snapshots"
        _check_fields(
            s,
            {
                "id": lambda v: _is_int(v) and v >= 1,
                "paper_id": _is_nonempty_str,
                "version": lambda v: _is_int(v) and v >= 1,
                "serial": _is_int,
                "receipt1": _is_nonempty_str,
                "receipt2": _is_nonempty_str,
                "access_code": _is_nonempty_str,
                "review1": lambda v: isinstance(v, dict),
                "review2": lambda v: isinstance(v, dict),
                "created_at": _is_nonempty_str,
                "invalidated": lambda v: isinstance(v, bool),
            },
            where,
        )
        _check_serial(s["serial"], where)
        _check_unique(snapshot_ids, s["id"], where, "快照 id")
        _check_unique(access_codes, s["access_code"], where, "快照访问码")
        _check_unique(
            seen,
            (s["paper_id"], s["serial"], s["receipt1"], s["receipt2"]),
            where,
            "快照幂等键 (paper_id, serial, receipt1, receipt2)",
        )
        _check_frozen_review(s["review1"], f"{where}.review1")
        _check_frozen_review(s["review2"], f"{where}.review2")
        snapshot_by_id[s["id"]] = s

    # ---- 机构归并边 (历史保留: 名称可为已删除资料中的原名, 不查存在性)
    seen = set()
    for m in data["institution_merges"]:
        where = "data.institution_merges"
        _check_fields(
            m,
            {
                "id": lambda v: _is_int(v) and v >= 1,
                "name_a": _is_nonempty_str,
                "name_b": _is_nonempty_str,
                "revision": _is_int,
                "merged_at": _is_nonempty_str,
            },
            where,
        )
        _check_unique(seen, m["id"], where, "归并边 id")
        _check_stamped_revision(m["revision"], where)

    # ---- 普通分配锁定表 (论文删除/撤回时级联清除: 必须指向现存未撤回论文;
    #      被锁评审人删除不级联, 不查评审人存在性)
    seen = set()
    for lock in data["assignment_locks"]:
        where = "data.assignment_locks"
        _check_fields(
            lock,
            {
                "paper_id": _is_nonempty_str,
                "reviewers": lambda v: isinstance(v, list)
                and 1 <= len(v) <= 2
                and all(_is_nonempty_str(r) for r in v)
                and len(set(v)) == len(v),
                "updated_at": _is_nonempty_str,
            },
            where,
        )
        _check_unique(seen, lock["paper_id"], where, "锁定行")
        if lock["paper_id"] not in paper_ids:
            _fail(where, f"锁定指向未知论文 {lock['paper_id']!r} (锁定随论文删除级联清除)")
        if lock["paper_id"] in withdrawn_ids:
            _fail(where, f"锁定指向已撤回论文 {lock['paper_id']!r} (锁定随撤回级联清除)")

    # ---- 评语更正记录 (历史保留, 不查论文/评审人存在性)
    correction_ids = set()
    for c in data["review_corrections"]:
        where = "data.review_corrections"
        _check_fields(
            c,
            {
                "id": lambda v: _is_int(v) and v >= 1,
                "serial": _is_int,
                "paper_id": _is_nonempty_str,
                "reviewer_id": _is_nonempty_str,
                "reason": _is_nonempty_str,
                "original_score": lambda v: _is_int(v) and 1 <= v <= 5,
                "original_comment": _is_nonempty_str,
                "original_receipt": _is_nonempty_str,
                "original_submitted_at": _is_nonempty_str,
                "state": lambda v: v in ("pending", "completed"),
                "new_score": lambda v: v is None or (_is_int(v) and 1 <= v <= 5),
                "new_comment": lambda v: v is None or _is_nonempty_str(v),
                "new_receipt": lambda v: v is None or _is_nonempty_str(v),
                "requested_at": _is_nonempty_str,
                "corrected_at": lambda v: v is None or _is_nonempty_str(v),
            },
            where,
        )
        _check_serial(c["serial"], where)
        _check_unique(correction_ids, c["id"], where, "更正记录 id")
        if c["state"] == "completed":
            if c["new_score"] is None or c["new_comment"] is None or c["new_receipt"] is None:
                _fail(where, "已完成更正必须携带更正后评分/评语/新收据")
            if c["corrected_at"] is None:
                _fail(where, "已完成更正必须携带更正时间")
        else:
            if (
                c["new_score"] is not None
                or c["new_comment"] is not None
                or c["new_receipt"] is not None
                or c["corrected_at"] is not None
            ):
                _fail(where, "待更正记录不得携带更正后内容")

    # ---- 作者异议 (必须指向现存快照版本; 受理必须关联现存更正记录)
    # v2 删除凭据按编号取最近删除时刻: pending 异议不得指向已随删除失效的快照
    latest_deletion_by_paper = {}
    if format_version >= 2:
        for _d in data["paper_deletions"]:
            prev = latest_deletion_by_paper.get(_d["paper_id"])
            if prev is None or _d["deleted_at"] > prev:
                latest_deletion_by_paper[_d["paper_id"]] = _d["deleted_at"]
    objection_ids, query_tokens, seen = set(), set(), set()
    for o in data["feedback_objections"]:
        where = "data.feedback_objections"
        _check_fields(
            o,
            {
                "id": lambda v: _is_int(v) and v >= 1,
                "snapshot_id": _is_int,
                "paper_id": _is_nonempty_str,
                "serial": _is_int,
                "label": lambda v: _is_int(v) and v in (1, 2),
                "reason": _is_nonempty_str,
                "frozen_review": lambda v: isinstance(v, dict),
                "target_receipt": _is_nonempty_str,
                "state": lambda v: v in ("pending", "rejected", "accepted", "expired"),
                "query_token": _is_nonempty_str,
                "resolution_note": lambda v: v is None or _is_str(v),
                "correction_id": lambda v: v is None or _is_int(v),
                "created_at": _is_nonempty_str,
                "decided_at": lambda v: v is None or _is_nonempty_str(v),
            },
            where,
        )
        _check_unique(objection_ids, o["id"], where, "异议 id")
        _check_unique(query_tokens, o["query_token"], where, "异议查询凭据")
        _check_serial(o["serial"], where)
        if o["snapshot_id"] not in snapshot_by_id:
            _fail(where, f"异议指向未知快照版本 snapshot_id={o['snapshot_id']!r}")
        snap = snapshot_by_id[o["snapshot_id"]]
        if o["paper_id"] != snap["paper_id"] or o["serial"] != snap["serial"]:
            _fail(where, "异议的论文/发布序号与所指快照版本不一致")
        _check_unique(seen, (o["snapshot_id"], o["label"]), where, "同快照同标号异议")
        _check_frozen_review(o["frozen_review"], f"{where}.frozen_review")
        if o["state"] == "pending":
            # 未处理异议随发布序号变化/撤回/删除原子过期: pending 必在当前发布序号上
            if o["serial"] != max_serial:
                _fail(where, "待处理异议的发布序号必须是当前发布序号 (否则应已过期)")
            if o["decided_at"] is not None:
                _fail(where, "待处理异议不得携带处理时间")
            # v2: 撤回/删除在同一事务原子过期待处理异议, 故 pending 异议不得指向
            # 已撤回论文或已因删除失效的快照 (删除/撤回凭据可核验, 非推断)。
            if format_version >= 2:
                if o["paper_id"] in withdrawn_ids:
                    _fail(where, "待处理异议指向已撤回论文的快照 (撤回同事务应已过期)")
                if bool(snap["invalidated"]) and (
                    latest_deleted_at := latest_deletion_by_paper.get(o["paper_id"])
                ) is not None and snap["created_at"] <= latest_deleted_at:
                    _fail(where, "待处理异议指向已随论文删除失效的快照 (删除同事务应已过期)")
        else:
            if o["decided_at"] is None:
                _fail(where, "已处理/已过期异议必须携带处理时间")
        if o["state"] == "accepted":
            if o["correction_id"] is None:
                _fail(where, "已受理异议必须关联评语更正记录")
            if o["correction_id"] not in correction_ids:
                _fail(where, f"已受理异议指向未知更正记录 {o['correction_id']!r}")
        elif o["correction_id"] is not None:
            _fail(where, "未受理的异议不得关联更正记录")

    # ---- 论文撤回记录 (与 papers.withdrawn 一一对应)
    withdrawal_ids = set()
    for w in data["paper_withdrawals"]:
        where = "data.paper_withdrawals"
        _check_fields(
            w,
            {
                "paper_id": _is_nonempty_str,
                "reason": _is_nonempty_str,
                "revision": _is_int,
                "published_serial": lambda v: v is None or (_is_int(v) and v >= 1),
                "published_plan": lambda v: v is None
                or (
                    isinstance(v, list)
                    and len(v) == 2
                    and all(_is_nonempty_str(r) for r in v)
                ),
                "withdrawn_at": _is_nonempty_str,
            },
            where,
        )
        _check_unique(withdrawal_ids, w["paper_id"], where, "撤回记录")
        _check_stamped_revision(w["revision"], where)
        if w["paper_id"] not in paper_ids:
            _fail(where, f"撤回记录指向未知论文 {w['paper_id']!r} (撤回稿资料行保留)")
        if w["paper_id"] not in withdrawn_ids:
            _fail(where, f"撤回记录指向的论文 {w['paper_id']!r} 未标记 withdrawn")
        if w["published_serial"] is not None and w["published_serial"] > max_serial:
            _fail(where, "撤回冻结的发布序号超过当前发布序号")
    missing = withdrawn_ids - withdrawal_ids
    if missing:
        _fail(
            "data.paper_withdrawals",
            f"已撤回论文缺少撤回记录: {sorted(missing)} (撤回与记录同事务写入)",
        )

    # ---- 论文删除凭据 (v2; 同编号可多次"删除 -> 重录", 按删除顺序逐行追加)
    deletion_ids = set()
    deletions_by_paper = {}
    if format_version >= 2:
        for d in data["paper_deletions"]:
            where = "data.paper_deletions"
            _check_fields(
                d,
                {
                    "id": lambda v: _is_int(v) and v >= 1,
                    "paper_id": _is_nonempty_str,
                    "revision": _is_int,
                    "published_serial": lambda v: v is None or (_is_int(v) and v >= 1),
                    "published_plan": lambda v: v is None
                    or (
                        isinstance(v, list)
                        and len(v) == 2
                        and all(_is_nonempty_str(r) for r in v)
                    ),
                    "invalidated_snapshots": lambda v: _is_int(v) and v >= 0,
                    "expired_objections": lambda v: _is_int(v) and v >= 0,
                    "deleted_at": _is_nonempty_str,
                },
                where,
            )
            _check_unique(deletion_ids, d["id"], where, "删除凭据 id")
            _check_stamped_revision(d["revision"], where)
            if d["published_serial"] is not None and d["published_serial"] > max_serial:
                _fail(where, "删除冻结的发布序号超过当前发布序号")
            deletions_by_paper.setdefault(d["paper_id"], []).append(d)
        # 同一编号多次删除 (中间重录): id/时刻按删除顺序单调, 冻结计数不得为负
        for pid, dels in deletions_by_paper.items():
            ordered = sorted(dels, key=lambda d: d["id"])
            prev_at = None
            for d in ordered:
                if prev_at is not None and d["deleted_at"] < prev_at:
                    _fail(
                        "data.paper_deletions",
                        f"论文 {pid!r} 的删除时刻不按删除顺序递增",
                    )
                prev_at = d["deleted_at"]

    # ---- 评审保障等级 (随论文删除/撤回级联清除: 必须指向现存未撤回论文)
    seen = set()
    for g in data["paper_guarantee_levels"]:
        where = "data.paper_guarantee_levels"
        _check_fields(
            g,
            {
                "paper_id": _is_nonempty_str,
                "level": lambda v: v in ("high", "medium"),
                "revision": _is_int,
                "updated_at": _is_nonempty_str,
            },
            where,
        )
        _check_unique(seen, g["paper_id"], where, "保障等级行")
        _check_stamped_revision(g["revision"], where)
        if g["paper_id"] not in paper_ids:
            _fail(where, f"保障等级指向未知论文 {g['paper_id']!r} (等级随论文删除级联清除)")
        if g["paper_id"] in withdrawn_ids:
            _fail(where, f"保障等级指向已撤回论文 {g['paper_id']!r} (等级随撤回级联清除)")

    # ---- 统一评审截止 (按发布序号隔离, 每版至多一行)
    seen = set()
    for dline in data["review_deadlines"]:
        where = "data.review_deadlines"
        _check_fields(
            dline,
            {
                "serial": _is_int,
                "revision": _is_int,
                "deadline_at": _is_nonempty_str,
                "set_at": _is_nonempty_str,
            },
            where,
        )
        _check_serial(dline["serial"], where)
        _check_stamped_revision(dline["revision"], where)
        _check_unique(seen, dline["serial"], where, "截止行")

    # ---- 跨记录核验: 访问码效力必须可由 快照版本/更正请求/撤回记录/删除凭据 重算
    #
    # 背景: 旧快照的失效 (更正请求发起、异议受理走更正流程、论文撤回、会务方删除论文)
    # 在库中只体现为 feedback_snapshots.invalidated=1。若有人导出后把该标记改回 0
    # 并重算校验和, 仅核对标记本身会接受伪造快照, 旧访问码在目标实例将再次可读
    # 已更正/已删除的反馈、甚至再次提交异议。因此恢复前必须依据其它业务记录重算每个
    # 访问码的应有效力, 标记与重算结果不一致即为"矛盾快照"。
    #
    # 效力规则 (与会话内运行时口径一致, 历史版本仅供会务方追溯):
    #   1. 已撤回论文: 该稿全部快照失效 (撤回同事务失效所有访问码);
    #   2. (v2) 论文删除: 删除时刻 (deleted_at) 及之前创建的快照必已随删除失效;
    #      同编号重新录入不恢复旧码效力——重录后新建快照的创建时刻严格晚于删除时刻;
    #   3. 同 (论文, 发布序号) 内按版本仅最高一版可能有效: 同序号再次发布 version+1,
    #      旧版在更正请求发起时已被置失效;
    #   4. 更正请求 (不论 pending/completed): 快照创建不晚于该请求发起时刻
    #      (requested_at) 的版本, 已在请求发起的同一事务失效; 待更正期间不允许发布,
    #      故 pending 请求时组内全部失效; completed 请求之后的重发版本创建时间必晚于
    #      requested_at, 才可能有效;
    # v1 旧格式没有删除记载, 删除规则不参与重算 (沿用原有恢复判断, 不推断删除历史)。
    # 时间戳只能辅助判定; 对当前发布序号上"标记有效"的快照, 还以现行评语收据核对
    # (更正会原地更换 submitted_reviews 的收据): 改标记/改时间而无对应收据, 复活不成立。
    snapshots = sorted(data["feedback_snapshots"], key=lambda s: s["id"])
    version_by_paper = {}
    groups = {}
    for s in snapshots:
        _check_unique(version_by_paper.setdefault(s["paper_id"], set()), s["version"],
                      "data.feedback_snapshots", "同论文快照 version")
        groups.setdefault((s["paper_id"], s["serial"]), []).append(s)

    # 版本号按论文连续分配 (MAX(version)+1), 不允许缺号: 缺号意味着有版本行被删除,
    # 而快照版本永不删除 (仅失效并保留供会务方追溯)
    for pid, versions in version_by_paper.items():
        if sorted(versions) != list(range(1, len(versions) + 1)):
            _fail(
                "data.feedback_snapshots",
                f"论文 {pid!r} 的快照版本号不连续: {sorted(versions)}"
                " (快照版本永不删除, 缺失版本与历史保留规则矛盾)",
            )

    # 更正记录按 (论文, 序号) 汇总; 只用于重算该组快照的应有效力
    corrections_by_group = {}
    for c in data["review_corrections"]:
        corrections_by_group.setdefault((c["paper_id"], c["serial"]), []).append(c)

    # v2: 每个编号最近一次删除时刻 (同编号多次"删除->重录"取最晚一次;
    # 删除同事务失效该稿此前全部快照, 重录后旧码继续失效)
    latest_deleted_at = {}
    if format_version >= 2:
        for d in data["paper_deletions"]:
            prev = latest_deleted_at.get(d["paper_id"])
            if prev is None or d["deleted_at"] > prev:
                latest_deleted_at[d["paper_id"]] = d["deleted_at"]

    # 当前发布方案槽位 -> 现行评语收据: 恢复后"当前有效"码必须读到的仍是现行评语
    current_review_receipts = {}
    for r in data["submitted_reviews"]:
        if r["serial"] == max_serial:
            current_review_receipts[(r["paper_id"], r["reviewer_id"])] = r["receipt"]

    for (pid, serial), group_snaps in groups.items():
        where = f"data.feedback_snapshots[paper_id={pid!r}, serial={serial}]"
        ordered = sorted(group_snaps, key=lambda s: s["version"])
        latest_version = ordered[-1]["version"]
        corrections = corrections_by_group.get((pid, serial), [])
        has_pending = any(c["state"] == "pending" for c in corrections)
        # 最晚的更正请求发起时刻: 不晚于该时刻创建的版本已随请求发起而失效
        latest_requested_at = max(c["requested_at"] for c in corrections) if corrections else None
        deleted_at = latest_deleted_at.get(pid) if format_version >= 2 else None

        for s in ordered:
            expect_invalidated = False
            reasons = []
            if pid in withdrawn_ids:
                expect_invalidated = True
                reasons.append("论文已撤回")
            if deleted_at is not None and s["created_at"] <= deleted_at:
                expect_invalidated = True
                reasons.append("快照创建不晚于论文删除时刻 (删除同事务已失效; 同编号重录不恢复旧码)")
            if s["version"] != latest_version:
                expect_invalidated = True
                reasons.append("同发布序号内已有更新版本")
            if has_pending:
                expect_invalidated = True
                reasons.append("该论文在该发布序号上存在待完成的评语更正请求")
            if latest_requested_at is not None and s["created_at"] <= latest_requested_at:
                expect_invalidated = True
                reasons.append("快照创建不晚于评语更正请求发起时刻 (请求发起时已失效)")
            if bool(s["invalidated"]) != expect_invalidated:
                if expect_invalidated:
                    _fail(
                        where,
                        f"快照版本 {s['version']} (访问码 {s['access_code']!r}) 的失效标记"
                        " 被置为有效, 但依据业务记录它应当已失效 ("
                        + "; ".join(reasons)
                        + ")——已因更正、撤回或删除失效的快照不能因仍属当前发布序号而复活",
                    )
                _fail(
                    where,
                    f"快照版本 {s['version']} (访问码 {s['access_code']!r}) 标记为已失效,"
                    " 但无对应的失效记录 (撤回记录/删除凭据/更高版本/更正请求);"
                    " 失效标记必须与快照版本、更正请求、撤回与删除记录一致",
                )

            # 当前发布序号上标记有效的快照: 恢复后其访问码将立即可读、可提异议,
            # 两份评语收据必须就是当前方案槽位上的现行评语收据 (更正会换收据,
            # 旧收据快照在更正链上必已失效), 否则快照内容与现行评语矛盾。
            if s["serial"] == max_serial and not s["invalidated"] and published is not None:
                pair = published["plan"].get(pid)
                if pair is None:
                    _fail(
                        where,
                        f"快照版本 {s['version']} 在当前发布序号上标记有效, 但论文 {pid!r}"
                        " 不在当前发布方案中 (该码恢复后无对应槽位却可读, 快照与方案矛盾)",
                    )
                else:
                    current_receipt_set = {
                        current_review_receipts.get((pid, rid)) for rid in pair
                    }
                    if (
                        None in current_receipt_set
                        or {s["receipt1"], s["receipt2"]} != current_receipt_set
                    ):
                        _fail(
                            where,
                            f"快照版本 {s['version']} (访问码 {s['access_code']!r})"
                            " 在当前发布序号上标记有效, 但其评语收据与当前发布方案槽位上的"
                            "现行评语收据不一致 (评语已被更正或替换却仍标记有效,"
                            " 旧访问码不得再次读取已更正的反馈)",
                        )


# ---------------------------------------------------------------- 恢复
# ---------------------------------------------------------------- 恢复

def assert_restorable_target(conn: sqlite3.Connection) -> None:
    """核对目标实例可恢复: 尚无业务记录且从未执行过恢复。

    必须在恢复写事务 (BEGIN IMMEDIATE) 内调用, 与并发写入串行化。
    """
    if conn.execute("SELECT 1 FROM migration_restores LIMIT 1").fetchone():
        raise RestoreConflictError(
            "该实例此前已执行过快照恢复, 重复恢复拒绝 (一个实例至多恢复一次)"
        )
    rev = db.get_revision(conn)
    non_empty = [
        table
        for table in BUSINESS_TABLES
        if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
    ]
    if rev != 0 or non_empty:
        detail = []
        if rev != 0:
            detail.append(f"资料修订号为 {rev} (空实例应为 0)")
        if non_empty:
            detail.append(f"业务表已有记录: {', '.join(non_empty)}")
        raise RestoreConflictError(
            "目标实例已存在业务记录, 仅接受尚无业务记录的空实例恢复 (" + "; ".join(detail) + ")"
        )


def load_snapshot(conn: sqlite3.Connection, data: dict) -> dict:
    """把校验通过的快照 data 全量写入空实例 (调用方事务内), 返回各表写入行数。

    自增 id 表按原 id 写入 (sqlite_sequence 随之推进), 引用关系保持不变;
    资料修订号与发布序号按快照原样恢复, 恢复本身不推进任何版本号。
    """
    counts = {}
    conn.execute("UPDATE meta SET value = ? WHERE key = 'revision'", (str(data["revision"]),))
    published = data["published"]
    if published is not None:
        conn.execute(
            "INSERT INTO published(id, serial, revision, plan, explanations, published_at)"
            " VALUES (1, ?, ?, ?, ?, ?)",
            (
                published["serial"],
                published["revision"],
                json.dumps(published["plan"], ensure_ascii=False),
                json.dumps(published["explanations"], ensure_ascii=False),
                published["published_at"],
            ),
        )
    for p in data["papers"]:
        conn.execute(
            "INSERT INTO papers(paper_id, manuscript, topics, institutions, withdrawn)"
            " VALUES (?,?,?,?,?)",
            (
                p["paper_id"],
                p["manuscript"],
                json.dumps(p["topics"], ensure_ascii=False),
                json.dumps(p["institutions"], ensure_ascii=False),
                1 if p["withdrawn"] else 0,
            ),
        )
    counts["papers"] = len(data["papers"])
    for r in data["reviewers"]:
        conn.execute(
            "INSERT INTO reviewers(reviewer_id, credential, topics, institution,"
            " capacity, avoid_papers, active) VALUES (?,?,?,?,?,?,?)",
            (
                r["reviewer_id"],
                r["credential"],
                json.dumps(r["topics"], ensure_ascii=False),
                r["institution"],
                r["capacity"],
                json.dumps(r["avoid_papers"], ensure_ascii=False),
                1 if r["active"] else 0,
            ),
        )
    counts["reviewers"] = len(data["reviewers"])
    for d in data["assignment_decisions"]:
        conn.execute(
            "INSERT INTO assignment_decisions"
            "(serial, paper_id, reviewer_id, state, reason, decided_at)"
            " VALUES (?,?,?,?,?,?)",
            (d["serial"], d["paper_id"], d["reviewer_id"], d["state"], d["reason"], d["decided_at"]),
        )
    counts["assignment_decisions"] = len(data["assignment_decisions"])
    for r in data["submitted_reviews"]:
        conn.execute(
            "INSERT INTO submitted_reviews"
            "(serial, paper_id, reviewer_id, score, comment, receipt, submitted_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                r["serial"],
                r["paper_id"],
                r["reviewer_id"],
                r["score"],
                r["comment"],
                r["receipt"],
                r["submitted_at"],
            ),
        )
    counts["submitted_reviews"] = len(data["submitted_reviews"])
    for r in data["reviewer_recusals"]:
        conn.execute(
            "INSERT INTO reviewer_recusals"
            "(reviewer_id, paper_id, reason, created_serial, created_revision, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (
                r["reviewer_id"],
                r["paper_id"],
                r["reason"],
                r["created_serial"],
                r["created_revision"],
                r["created_at"],
            ),
        )
    counts["reviewer_recusals"] = len(data["reviewer_recusals"])
    for s in data["reviewer_status_changes"]:
        conn.execute(
            "INSERT INTO reviewer_status_changes"
            "(id, reviewer_id, active, reason, revision, changed_at)"
            " VALUES (?,?,?,?,?,?)",
            (
                s["id"],
                s["reviewer_id"],
                1 if s["active"] else 0,
                s["reason"],
                s["revision"],
                s["changed_at"],
            ),
        )
    counts["reviewer_status_changes"] = len(data["reviewer_status_changes"])
    for s in data["feedback_snapshots"]:
        conn.execute(
            "INSERT INTO feedback_snapshots"
            "(id, paper_id, version, serial, receipt1, receipt2, access_code,"
            " review1, review2, created_at, invalidated)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                s["id"],
                s["paper_id"],
                s["version"],
                s["serial"],
                s["receipt1"],
                s["receipt2"],
                s["access_code"],
                json.dumps(s["review1"], ensure_ascii=False),
                json.dumps(s["review2"], ensure_ascii=False),
                s["created_at"],
                1 if s["invalidated"] else 0,
            ),
        )
    counts["feedback_snapshots"] = len(data["feedback_snapshots"])
    for m in data["institution_merges"]:
        conn.execute(
            "INSERT INTO institution_merges(id, name_a, name_b, revision, merged_at)"
            " VALUES (?,?,?,?,?)",
            (m["id"], m["name_a"], m["name_b"], m["revision"], m["merged_at"]),
        )
    counts["institution_merges"] = len(data["institution_merges"])
    for lock in data["assignment_locks"]:
        conn.execute(
            "INSERT INTO assignment_locks(paper_id, reviewers_json, updated_at)"
            " VALUES (?,?,?)",
            (lock["paper_id"], json.dumps(lock["reviewers"], ensure_ascii=False), lock["updated_at"]),
        )
    counts["assignment_locks"] = len(data["assignment_locks"])
    for c in data["review_corrections"]:
        conn.execute(
            "INSERT INTO review_corrections"
            "(id, serial, paper_id, reviewer_id, reason, original_score,"
            " original_comment, original_receipt, original_submitted_at, state,"
            " new_score, new_comment, new_receipt, requested_at, corrected_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                c["id"],
                c["serial"],
                c["paper_id"],
                c["reviewer_id"],
                c["reason"],
                c["original_score"],
                c["original_comment"],
                c["original_receipt"],
                c["original_submitted_at"],
                c["state"],
                c["new_score"],
                c["new_comment"],
                c["new_receipt"],
                c["requested_at"],
                c["corrected_at"],
            ),
        )
    counts["review_corrections"] = len(data["review_corrections"])
    for o in data["feedback_objections"]:
        conn.execute(
            "INSERT INTO feedback_objections"
            "(id, snapshot_id, paper_id, serial, label, reason, frozen_review_json,"
            " target_receipt, state, query_token, resolution_note, correction_id,"
            " created_at, decided_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                o["id"],
                o["snapshot_id"],
                o["paper_id"],
                o["serial"],
                o["label"],
                o["reason"],
                json.dumps(o["frozen_review"], ensure_ascii=False),
                o["target_receipt"],
                o["state"],
                o["query_token"],
                o["resolution_note"],
                o["correction_id"],
                o["created_at"],
                o["decided_at"],
            ),
        )
    counts["feedback_objections"] = len(data["feedback_objections"])
    for w in data["paper_withdrawals"]:
        conn.execute(
            "INSERT INTO paper_withdrawals"
            "(paper_id, reason, revision, published_serial, published_plan_json, withdrawn_at)"
            " VALUES (?,?,?,?,?,?)",
            (
                w["paper_id"],
                w["reason"],
                w["revision"],
                w["published_serial"],
                (
                    json.dumps(w["published_plan"], ensure_ascii=False)
                    if w["published_plan"] is not None
                    else None
                ),
                w["withdrawn_at"],
            ),
        )
    counts["paper_withdrawals"] = len(data["paper_withdrawals"])
    if "paper_deletions" in data:
        for d in data["paper_deletions"]:
            conn.execute(
                "INSERT INTO paper_deletions"
                "(id, paper_id, revision, published_serial, published_plan_json,"
                " invalidated_snapshots, expired_objections, deleted_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    d["id"],
                    d["paper_id"],
                    d["revision"],
                    d["published_serial"],
                    (
                        json.dumps(d["published_plan"], ensure_ascii=False)
                        if d["published_plan"] is not None
                        else None
                    ),
                    d["invalidated_snapshots"],
                    d["expired_objections"],
                    d["deleted_at"],
                ),
            )
        counts["paper_deletions"] = len(data["paper_deletions"])
    else:
        counts["paper_deletions"] = 0
    for g in data["paper_guarantee_levels"]:
        conn.execute(
            "INSERT INTO paper_guarantee_levels(paper_id, level, revision, updated_at)"
            " VALUES (?,?,?,?)",
            (g["paper_id"], g["level"], g["revision"], g["updated_at"]),
        )
    counts["paper_guarantee_levels"] = len(data["paper_guarantee_levels"])
    for dline in data["review_deadlines"]:
        conn.execute(
            "INSERT INTO review_deadlines(serial, revision, deadline_at, set_at)"
            " VALUES (?,?,?,?)",
            (dline["serial"], dline["revision"], dline["deadline_at"], dline["set_at"]),
        )
    counts["review_deadlines"] = len(data["review_deadlines"])
    return counts


def record_restore(
    conn: sqlite3.Connection,
    checksum: str,
    data: dict,
    restored_at: str,
    format_version: int = FORMAT_VERSION,
) -> None:
    """写入恢复标记 (同一恢复事务内): 使重复恢复被识别并拒绝。"""
    published = data["published"]
    conn.execute(
        "INSERT INTO migration_restores"
        "(checksum, format_version, revision, serial, restored_at)"
        " VALUES (?,?,?,?,?)",
        (
            checksum,
            format_version,
            data["revision"],
            published["serial"] if published is not None else 0,
            restored_at,
        ),
    )
