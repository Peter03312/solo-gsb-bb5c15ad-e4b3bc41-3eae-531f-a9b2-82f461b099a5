"""SQLite 存储层。

- 单连接 + 进程内可重入锁, 写操作使用 BEGIN IMMEDIATE 串行化;
- meta 表维护资料修订号 (revision), 任何论文/评审人资料变更都会使其 +1;
- published 表保存当前已发布方案、发布序号 (serial, 每次发布/补位发布 +1)
  及其所依据的修订号;
- assignment_decisions 表保存评审人对当前发布版分配关系的确认/回避决定
  (按发布序号隔离);
- submitted_reviews 表保存评审人对"当前发布版中已确认且未回避"槽位的正式评分
  与评语 (按发布序号隔离; 每个有效槽位至多一份, 以收据 receipt 标识;
  补位发布时连续保留的确认槽位连同评语一起复制到新序号);
- reviewer_recusals 表保存评审人声明过的硬回避, 后续普通求解与补位都必须遵守;
- reviewers.active 标记评审资格 (1=启用, 既有评审人初始即启用; 0=停用),
  reviewer_status_changes 表保存会务方停用/启用的变更记录 (仅有效变更写入;
  匹配修订号下重复提交同一状态和原因为幂等请求, 不推进修订号、不写记录);
- feedback_snapshots 表保存会务方按论文发布的、面向作者的匿名反馈快照
  (仅当某发布序号下两名评审人均已确认且各提交一份正式评语时生成;
  以 (paper_id, serial, receipt1, receipt2) 幂等, 来源变化即新发布序号 -> version+1
  与新随机访问码, 旧码立即失效, 旧版行保留仅供会务方追溯);
- institution_merges 表保存会务方提交的机构归并边 (同一机构的不同原名);
  全表边以并查集解释为冲突判定组, 关系传递且不可拆分, 仅有效归并写入并推进修订号,
  普通分配与补位均按归并组判定"作者同机构"与"两名评审人同机构";
- review_corrections 表保存会务方针对已交评语发起的更正请求 (按发布序号 serial 隔离;
  发起时冻结原评语内容/收据供会务方追溯, 并令该稿当前序号的反馈快照访问码立即失效);
  评审人提交的首份有效更正生成新收据并更新该槽位评语 (state: pending -> completed),
  相同内容重试幂等返回更正收据, 内容不同冲突; 发布序号变化后待更正请求自然失效;
- assignment_locks 表保存会务方在普通分配前提交的锁定表 (每篇 0~2 名评审人;
  整表替换, 空表清除全部锁定); 锁定仅约束普通分配, 不改变补位规则;
  被锁评审人失格时锁定行不级联, 由预演/发布诊断冲突并拒绝发布, 不悄悄释放,
  论文删除时其锁定行随论文级联清除;
- feedback_objections 表保存作者凭"当前有效的快照访问码"对标号 1/2 评语提交的异议
  (同一快照同一标号仅一条: UNIQUE(snapshot_id, label); 相同理由重试返回原记录,
  不同理由冲突); 提交即冻结该标号在快照中的评分与评语并发放随机查询凭据
  (obj- 前缀, query_token 全局唯一), 持凭据可随时查看处理状态, 与访问码此后是否
  失效无关; state: pending(待处理) / rejected(驳回, 不影响快照) / accepted(受理,
  已走既有评语更正请求流程) / expired(发布序号变化或论文撤回后未处理异议过期,
  仅供会务方追溯); 每次发布 (含补位发布) 推进发布序号时, 旧序号下仍 pending 的
  异议在同一事务内原子标记 expired, 历史行永久保留;
- papers.withdrawn 标记论文是否已被会务方撤回 (1=已撤回): 撤回在同一个写事务内
  生效——立即停止评审人取稿/确认/回避/提交评语与更正、该稿全部反馈访问码失效、
  待处理异议原子过期、普通分配锁定行随撤回清除; 撤回稿不进入后续普通分配与补位,
  同编号不能重新录入, 也不能再更新/删除 (资料行与全部评审痕迹保留供会务方追溯)。
  paper_withdrawals 表每篇撤回稿至多一行, 保存非空撤回原因、推进后的资料修订号、
  撤回时间及撤回瞬间的发布序号与槽位 (供追溯); 匹配当前修订号的同原因重试幂等
  返回原记录 (不推进修订号), 异原因/版本不符拒绝且无部分改动。
- paper_deletions 表保存会务方删除论文的失效凭据 (与撤回不同: 删除不保留论文
  资料行, 同编号允许重新录入)。删除在同一个写事务内生效——该稿此前发布的全部
  反馈快照访问码立即失效 (feedback_snapshots.invalidated=1), 仍 pending 的作者
  异议原子标记 expired; 同编号重新录入后, 删除时刻 (deleted_at) 之前创建的旧
  快照继续保持失效, 旧访问码绝不恢复效力。同一编号可多次"删除 -> 重录",
  每次删除追加一行; 删除行还冻结删除瞬间的发布序号与槽位、本次失效快照数/过期
  异议数, 既是会务方追溯依据, 也是迁移恢复时跨记录重算访问码效力的凭据
  (仅靠快照自报 invalidated 标记无法防止被改回有效的伪造快照)。
  冻结的发布序号 (published_serial) 同时是评审人侧删除状态的判定依据: 删除后
  当前发布版不重写, 历史槽位仍留在 published.plan 中, 但该序号已被删除凭据冻结
  (paper_deleted_after_publish)——评审人的确认/回避/正式评语/评语更正一律拒绝,
  且不写新决定/硬回避/评语/更正、不推进资料修订号, 取稿列表/本人评语/待更正
  任务也不再呈现; 同编号重新录入但未重新发布时旧序号仍冻结, 只有重新发布
  (普通发布/补位发布推进序号) 后评审人才能按新发布序号重新确认并提交。
- paper_guarantee_levels 表保存会务方按论文设置的评审保障等级 (high/medium;
  未设置即普通 normal, 不落行, 设置为 normal 即清除该行): 仅在普通分配/补位
  无法完整覆盖各自目标论文时参与求解——先使完整分配的论文总数最大, 再依次使
  高、中等级完整分配数最大, 最后沿用既有字典序择优; 完整可行时等级不影响
  既有容量比例与字典序优化。相同等级重试幂等 (不推进修订号), 有效变更推进
  资料修订号 +1; 论文删除或撤回时其等级行随论文级联清除 (不再参与求解)。
- review_deadlines 表保存会务方为某个发布版设置的"一次统一 UTC 评审截止时刻"
  (按发布序号 serial 隔离, 每版至多一行): 设置须同时携带资料修订号与发布序号,
  时刻须晚于设置时刻且显式携带时区 (归一化为 UTC 存储); 同版同时刻重试幂等
  (不产生新变更), 同版异时刻拒绝, 版本不符/非法时刻拒绝且不留任何改动。
  设置截止不推进资料修订号与发布序号, 不改变当前方案及历史记录; 未设置截止时
  沿用既有行为。服务端时刻到达截止后, 该发布版中仍未交正式评语的槽位即逾期
  (已回避槽位不计逾期): 评审人不得再确认或交评语, 已交评语及收据保持有效
  (相同内容重试仍幂等返回原收据)。补位预演/发布在同一写事务内判定逾期:
  逾期槽位即使此前已确认也释放 (fixed_review_overdue), 且本次补位不得把该稿
  重新分给该逾期评审人 (求解时该 (评审人, 论文) 对以 review_overdue 排除);
  其余已确认槽位仍按既有规则固定。截止按发布序号隔离: 普通发布/补位发布推进
  序号后旧版截止不约束新发布版 (旧行保留仅供追溯)。
- migration_restores 表保存实例迁移的恢复标记 (会务方快照迁移): 每次成功恢复
  写入一行 (快照校验和/格式版本/恢复后的修订号与发布序号/时刻); 恢复仅接受
  尚无业务记录且从未恢复过的目标实例, 重复恢复拒绝。本表为实例本地元数据,
  不随快照导出。
- 发布 = 在同一个写事务内 "校验修订号 -> 重算 -> 写入",
  因此与资料修改并发时不会产生基于旧版资料的方案。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.getcwd(), "data", "app.db"))

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    paper_id     TEXT PRIMARY KEY,
    manuscript   TEXT NOT NULL,
    topics       TEXT NOT NULL,   -- JSON array
    institutions TEXT NOT NULL,   -- JSON array
    withdrawn    INTEGER NOT NULL DEFAULT 0  -- 1=已撤回 (资料行保留, 同编号不可再录入)
);
CREATE TABLE IF NOT EXISTS reviewers (
    reviewer_id  TEXT PRIMARY KEY,
    credential   TEXT NOT NULL,
    topics       TEXT NOT NULL,   -- JSON array
    institution  TEXT NOT NULL,
    capacity     INTEGER NOT NULL CHECK (capacity >= 1),
    avoid_papers TEXT NOT NULL,   -- JSON array
    active       INTEGER NOT NULL DEFAULT 1  -- 评审资格: 1=启用(既有评审人初始即启用), 0=停用
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS published (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    serial       INTEGER NOT NULL DEFAULT 0,
    revision     INTEGER NOT NULL,
    plan         TEXT NOT NULL,   -- JSON: {paper_id: [reviewer_id, reviewer_id]}
    explanations TEXT NOT NULL,   -- JSON: 每篇论文的分配与排除原因
    published_at TEXT NOT NULL
);
-- 评审人对当前发布版中分配关系的决定 (按发布序号 serial 隔离; 无行 = pending)
CREATE TABLE IF NOT EXISTS assignment_decisions (
    serial      INTEGER NOT NULL,
    paper_id    TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    state       TEXT NOT NULL CHECK (state IN ('confirmed', 'recused')),
    reason      TEXT,            -- 回避原因 (recused 时非空)
    decided_at  TEXT NOT NULL,
    PRIMARY KEY (serial, paper_id, reviewer_id)
);
-- 评审人对"已确认且未回避"槽位提交的正式评分与评语 (按发布序号 serial 隔离;
-- 每个有效任务槽位至多一份; receipt 为首次提交时发放的收据, 相同内容重试原样返回。
-- receipt 不设全局唯一: 补位发布连续保留的确认槽位沿用同一份评语与同一收据,
-- 旧序号行保留供会务方追溯, 因此同一收据可随不同 serial 各存一行)
CREATE TABLE IF NOT EXISTS submitted_reviews (
    serial       INTEGER NOT NULL,
    paper_id     TEXT NOT NULL,
    reviewer_id  TEXT NOT NULL,
    score        INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
    comment      TEXT NOT NULL,
    receipt      TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    PRIMARY KEY (serial, paper_id, reviewer_id)
);
-- 评审人一旦对某篇论文声明回避, 即成为后续所有分配 (含补位) 的硬回避
CREATE TABLE IF NOT EXISTS reviewer_recusals (
    reviewer_id      TEXT NOT NULL,
    paper_id         TEXT NOT NULL,
    reason           TEXT NOT NULL,
    created_serial   INTEGER NOT NULL,
    created_revision INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (reviewer_id, paper_id)
);
-- 评审资格 (启用/停用) 变更记录, 仅供会务方追溯。仅有效变更写入;
-- 匹配修订号下重复提交同一状态和原因属于幂等请求, 不产生新记录。
CREATE TABLE IF NOT EXISTS reviewer_status_changes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    reviewer_id   TEXT NOT NULL,
    active        INTEGER NOT NULL,           -- 变更后的目标状态: 1=启用, 0=停用
    reason        TEXT NOT NULL,              -- 会务方填写的非空原因
    revision      INTEGER NOT NULL,           -- 本次有效变更推进后的资料修订号
    changed_at    TEXT NOT NULL
);
-- 面向作者的匿名反馈快照 (会务方按论文发布; 仅当该发布序号下两名评审人均已确认
-- 且各提交一份正式评语时才允许生成)。
-- receipt1/receipt2 按方案槽位固定顺序 (评审人编号字典序) 记录两份评语的收据,
-- 与 (paper_id, serial) 共同构成幂等键: 相同发布序号及两份评语收据的重复请求
-- 返回原快照与原码; 来源 (发布序号) 变化即生成新版本 (version + 1) 与新随机码,
-- 旧版行保留仅供会务方追溯。快照仅冻结论文编号、两份评分与评语, 不保存评审人编号
-- /机构等内部信息; access_code 全局唯一, 仅最新且序号仍为当前发布序号的版本可读。
CREATE TABLE IF NOT EXISTS feedback_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id     TEXT NOT NULL,
    version      INTEGER NOT NULL,
    serial       INTEGER NOT NULL,
    receipt1     TEXT NOT NULL,
    receipt2     TEXT NOT NULL,
    access_code  TEXT NOT NULL UNIQUE,
    review1      TEXT NOT NULL,   -- 冻结 JSON: {"score":..,"comment":..}
    review2      TEXT NOT NULL,   -- 冻结 JSON: {"score":..,"comment":..}
    created_at   TEXT NOT NULL,
    invalidated  INTEGER NOT NULL DEFAULT 0,  -- 1=更正请求发起/撤回/删除论文后访问码立即失效
    UNIQUE (paper_id, serial, receipt1, receipt2)
);
-- 会务方提交的机构归并边 (同一机构的不同原名)。归并关系传递且不可拆分:
-- 全表边以并查集解释为冲突判定组, 任何提交只能增加边 (使组变大),
-- 不存在"拆组/移出"操作; 同组重复提交幂等, 不写边、不推进修订号。
-- 仅有效归并 (使两个原先不同的组合并) 写入一行并推进资料修订号。
CREATE TABLE IF NOT EXISTS institution_merges (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name_a       TEXT NOT NULL,   -- 请求中的机构原名 (首尾空白归一化后的原样名称)
    name_b       TEXT NOT NULL,
    revision     INTEGER NOT NULL,  -- 本次有效归并推进后的资料修订号
    merged_at    TEXT NOT NULL
);
-- 普通分配锁定表 (会务方在普通分配前提交, 仅约束普通分配, 不影响补位规则)。
-- 每篇论文至多一行, reviewers_json 为 0~2 个评审人编号的 JSON 数组
-- (空数组行不写入, 缺省 = 不锁定; 整表替换语义下空表 = 清除全部锁定)。
-- 锁定仅为普通求解器的固定槽位输入: 被锁评审人随后被删除/停用/回避或因
-- 资料及机构归并而失格时锁定行不自动级联, 由预演/发布诊断具体冲突
-- (lock_reviewer_not_found 等) 并拒绝发布, 绝不悄悄释放锁定;
-- 论文删除时其锁定行随论文级联清除 (论文本身已不参与分配)。
CREATE TABLE IF NOT EXISTS assignment_locks (
    paper_id       TEXT PRIMARY KEY,
    reviewers_json TEXT NOT NULL,   -- JSON array: 1~2 个评审人编号 (字典序)
    updated_at     TEXT NOT NULL
);
-- 会务方针对已交评语发起的更正请求 (按发布序号 serial 隔离)。
-- 发起时冻结原评语 (评分/评语/收据/提交时间) 供会务方追溯; 评审人凭
-- "请求对应的发布序号 + 原评语收据" 提交更正, 首份有效更正生成新收据并
-- 直接更新该槽位的正式评语 (state 置 completed, 记录更正后内容/新收据/时间),
-- 相同内容重试幂等返回更正收据, 内容不同按冲突拒绝;
-- 发布序号变化后, 旧序号下的待更正请求自然失效 (不再列出也不可提交)。
CREATE TABLE IF NOT EXISTS review_corrections (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    serial               INTEGER NOT NULL,   -- 更正请求针对的发布序号
    paper_id             TEXT NOT NULL,
    reviewer_id          TEXT NOT NULL,
    reason               TEXT NOT NULL,      -- 会务方填写的非空更正原因
    original_score       INTEGER NOT NULL,   -- 被更正评语的冻结内容 (供会务方追溯)
    original_comment     TEXT NOT NULL,
    original_receipt     TEXT NOT NULL,      -- 被更正评语的收据
    original_submitted_at TEXT NOT NULL,
    state                TEXT NOT NULL CHECK (state IN ('pending', 'completed')),
    new_score            INTEGER,            -- 更正后内容 (completed 时非空)
    new_comment          TEXT,
    new_receipt          TEXT,               -- 更正评语的新收据
    requested_at         TEXT NOT NULL,
    corrected_at         TEXT
);
-- 作者面向反馈快照中标号 1/2 评语提交的异议。
-- 作者身份即"持当前有效快照访问码者": 提交时核对访问码存在且当前仍有效
-- (快照序号为当前发布序号且未被更正请求失效); 同一快照同一标号至多一条
-- (UNIQUE 约束兜底并发, 写事务 BEGIN IMMEDIATE 串行化保证并发提交不生成两条)。
-- 提交时冻结该标号在快照中的评分/评语 (frozen_review_json) 与目标评语收据
-- (target_receipt, 仅会务方视图使用), 发放随机查询凭据 query_token:
-- 凭据查询不依赖访问码此后是否有效, 即使快照码随发布序号变化/更正请求失效,
-- 持凭据仍可查看处理状态; 已失效访问码不得再提交新异议。
-- pending: 待会务方处理; rejected: 会务方驳回 (不影响快照, 记录驳回说明);
-- accepted: 会务方受理 (受理时原子核对发布序号仍为当前序号、目标评语收据未变、
--   无既有待更正请求, 按匿名标号定位评审人并走既有更正请求流程, 快照访问码
--   随之立即失效, 关联更正请求 id);
-- expired: 发布序号变化 (普通发布/补位发布) 或论文撤回/删除时仍未处理的异议
--   在同一事务内原子过期, 历史永久保留供会务方追溯。
CREATE TABLE IF NOT EXISTS feedback_objections (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id        INTEGER NOT NULL,    -- 异议所针对的快照版本
    paper_id           TEXT NOT NULL,
    serial             INTEGER NOT NULL,    -- 快照发布序号 (异议提交时的当前序号)
    label              INTEGER NOT NULL CHECK (label IN (1, 2)),
    reason             TEXT NOT NULL,       -- 作者填写的非空异议理由
    frozen_review_json TEXT NOT NULL,       -- 冻结 JSON: {"score":..,"comment":..}
    target_receipt     TEXT NOT NULL,       -- 目标评语收据 (提交时冻结, 受理时核对)
    state              TEXT NOT NULL CHECK (state IN ('pending', 'rejected', 'accepted', 'expired')),
    query_token        TEXT NOT NULL UNIQUE, -- 随机查询凭据 (obj- 前缀, 全局唯一)
    resolution_note    TEXT,                -- 驳回说明 (会务方可填) / 受理时的更正原因
    correction_id      INTEGER,             -- 受理后关联的既有更正请求 id
    created_at         TEXT NOT NULL,
    decided_at         TEXT,                -- 驳回/受理时间; 序号变化过期时同事务写入
    UNIQUE (snapshot_id, label)
);
-- 论文撤回记录 (会务方凭密钥撤回, 每篇撤回稿至多一行)。
-- 撤回在同一个写事务内生效: papers.withdrawn 置 1, 立即停止评审人取稿/确认/回避/
-- 评语与更正, 该稿全部反馈快照访问码失效 (feedback_snapshots.invalidated=1),
-- 仍 pending 的作者异议原子标记 expired, 普通分配锁定行随撤回清除
-- (撤回稿不参与后续普通分配与补位, 其锁定不得阻塞其他论文);
-- 资料行、已处理异议与评语、快照、分配/确认/更正历史均不删除, 冻结撤回瞬间的
-- 发布序号与槽位 (published_serial / published_plan_json) 供会务方追溯。
-- 撤回推进资料修订号 +1; 匹配当前修订号的同原因重试幂等返回原记录 (不推进修订号),
-- 异原因或修订号不符一律拒绝且无部分改动。
CREATE TABLE IF NOT EXISTS paper_withdrawals (
    paper_id            TEXT PRIMARY KEY,
    reason              TEXT NOT NULL,       -- 会务方填写的非空撤回原因
    revision            INTEGER NOT NULL,    -- 撤回推进后的资料修订号
    published_serial    INTEGER,             -- 撤回瞬间的发布序号 (从未发布为 NULL)
    published_plan_json TEXT,                -- 撤回瞬间该稿的槽位 JSON: [评审人编号 ...]
    withdrawn_at        TEXT NOT NULL
);
-- 论文删除凭据 (会务方 DELETE /papers/{id}; 与撤回不同: 资料行删除, 同编号可重录)。
-- 每次删除在同一个写事务内追加一行, 同时:
--   * 该稿此前的全部反馈快照访问码置 invalidated=1 (行保留供会务方追溯);
--   * 仍 pending 的作者异议原子标记 expired (查询凭据仍可查状态);
--   * 普通分配锁定行/保障等级行随论文删除级联清除。
-- 同编号重新录入后, deleted_at 之前创建的旧快照继续失效 (旧码不复活); 重录后
-- 若再次删除则再追加一行。冻结删除瞬间的发布序号与槽位、失效快照数/过期异议数
-- 供会务方追溯; 迁移恢复时删除行是跨记录重算访问码效力的凭据
-- (伪造快照仅把 invalidated 改回 0 无法通过恢复核验)。
CREATE TABLE IF NOT EXISTS paper_deletions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id             TEXT NOT NULL,
    revision             INTEGER NOT NULL,    -- 删除推进后的资料修订号
    published_serial     INTEGER,             -- 删除瞬间的发布序号 (从未发布为 NULL)
    published_plan_json  TEXT,                -- 删除瞬间该稿的槽位 JSON: [评审人编号 ...]
    invalidated_snapshots INTEGER NOT NULL,   -- 本次失效的快照版数
    expired_objections   INTEGER NOT NULL,    -- 本次过期的待处理异议数
    deleted_at           TEXT NOT NULL
);
-- 论文评审保障等级 (会务方按论文设置, 仅约束"无法完整覆盖"时的求解优先次序)。
-- 每篇论文至多一行, 仅保存 high/medium; 未设置即普通 normal (不落行,
-- 设置为 normal 即清除该行)。等级仅在普通分配/补位无法完整覆盖各自目标论文时
-- 参与求解: 先使完整分配的论文总数最大, 再依次使高、中等级完整分配数最大,
-- 最后沿用既有字典序择优; 完整可行时等级不影响既有容量比例与字典序优化。
-- 相同等级重试幂等 (不推进修订号), 有效变更推进资料修订号 +1;
-- 论文删除或撤回时其等级行随论文级联清除 (删除/撤回稿不再参与求解)。
CREATE TABLE IF NOT EXISTS paper_guarantee_levels (
    paper_id   TEXT PRIMARY KEY,
    level      TEXT NOT NULL CHECK (level IN ('high', 'medium')),
    revision   INTEGER NOT NULL,   -- 本次设置推进后的资料修订号
    updated_at TEXT NOT NULL
);
-- 某发布版的统一 UTC 评审截止时刻 (会务方设置, 每版至多一行, 按发布序号隔离)。
-- deadline_at 为归一化后的 UTC ISO 8601 字符串; 设置时核对资料修订号与发布序号,
-- 要求时刻晚于设置时的服务端 UTC 时刻; 同版同时刻重试幂等 (不产生新变更),
-- 同版异时刻拒绝, 版本不符/非法时刻拒绝且不留改动; 设置不推进修订号/发布序号。
-- 截止判定在各写事务内惰性完成 (截止与评语提交/确认/补位发布同事务判定,
-- 避免并发竞态): 服务端时刻到达 deadline_at 后, 该序号下仍未交正式评语且
-- 未回避的槽位即逾期——评审人不得再确认或交评语; 补位时逾期槽位 (含已确认)
-- 释放且该 (评审人, 论文) 不参与该稿重算 (review_overdue), 其余已确认槽位固定。
-- 普通发布/补位发布推进 serial 后旧版截止行保留但不约束新版 (仅供会务方追溯)。
CREATE TABLE IF NOT EXISTS review_deadlines (
    serial      INTEGER PRIMARY KEY,   -- 截止所约束的发布版序号
    revision    INTEGER NOT NULL,      -- 设置时核对通过的资料修订号
    deadline_at TEXT NOT NULL,         -- 归一化 UTC ISO 8601 截止时刻 (须晚于设置时刻)
    set_at      TEXT NOT NULL          -- 设置时刻 (归一化 UTC)
);
-- 实例迁移恢复标记 (会务方快照迁移): 每次成功恢复写入一行。
-- 恢复只接受"尚无业务记录"的目标实例; 本表使"恢复过空快照的实例"也能被识别,
-- 重复恢复一律拒绝 (409), 与业务表非空检查共同保证恢复不叠加、不留部分数据。
-- 本表是实例本地元数据, 不属于业务数据, 不随快照导出。
CREATE TABLE IF NOT EXISTS migration_restores (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    checksum       TEXT NOT NULL,      -- 已恢复快照的内容校验和 (sha256:...)
    format_version INTEGER NOT NULL,   -- 已恢复快照的格式版本
    revision       INTEGER NOT NULL,   -- 恢复后实例的资料修订号
    serial         INTEGER NOT NULL,   -- 恢复后实例的发布序号 (无发布版为 0)
    restored_at    TEXT NOT NULL       -- 恢复完成时刻 (UTC)
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """为旧版数据库补齐新增列 (SQLite 不支持 IF NOT EXISTS 的 ADD COLUMN)。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(published)").fetchall()}
    if "serial" not in cols:
        conn.execute("ALTER TABLE published ADD COLUMN serial INTEGER NOT NULL DEFAULT 0")
    reviewer_cols = {r["name"] for r in conn.execute("PRAGMA table_info(reviewers)").fetchall()}
    if "active" not in reviewer_cols:
        # 既有评审人初始视为启用
        conn.execute("ALTER TABLE reviewers ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    snapshot_cols = {r["name"] for r in conn.execute("PRAGMA table_info(feedback_snapshots)").fetchall()}
    if "invalidated" not in snapshot_cols:
        # 既有快照默认未被更正请求失效
        conn.execute(
            "ALTER TABLE feedback_snapshots ADD COLUMN invalidated INTEGER NOT NULL DEFAULT 0"
        )
    paper_cols = {r["name"] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
    if "withdrawn" not in paper_cols:
        # 既有论文均未撤回
        conn.execute(
            "ALTER TABLE papers ADD COLUMN withdrawn INTEGER NOT NULL DEFAULT 0"
        )


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = os.path.abspath(DB_PATH)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
        _migrate(_conn)
        _conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('revision', '0')")
    return _conn


def get_revision(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()
    return int(row["value"])


def bump_revision(conn: sqlite3.Connection) -> int:
    rev = get_revision(conn) + 1
    conn.execute("UPDATE meta SET value = ? WHERE key = 'revision'", (str(rev),))
    return rev


# ------------------------------------------------------------ 机构归并组 (并查集)

def institution_merge_edges(conn: sqlite3.Connection):
    """全部机构归并边, 按提交顺序 (id) 返回 [(name_a, name_b), ...]。"""
    return [
        (r["name_a"], r["name_b"])
        for r in conn.execute(
            "SELECT name_a, name_b FROM institution_merges ORDER BY id"
        ).fetchall()
    ]


def institution_groups(conn: sqlite3.Connection, known=None):
    """按并查集解释归并边, 返回 {机构原名: 归并组键}。

    归并关系传递且不可拆分: 全表边构成无向图, 同一连通分量内的所有名称
    映射到同一组键 (组内按名称排序后的首个名称, 随归并扩组可能变化,
    仅用于冲突判定的内部键)。

    known: 可选的当前资料机构原名集合; 给出时结果只包含这些名称
    (已被删除的资料名称仍参与并查集, 不影响其余名称的归组),
    未归并名称映射到自身。
    """
    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            # 确定性地以较小名称为根, 组键不依赖边的插入方向
            small, large = (ra, rb) if ra < rb else (rb, ra)
            parent[large] = small

    for a, b in institution_merge_edges(conn):
        union(a, b)

    names = set(parent)
    if known is not None:
        names |= set(known)
    return {name: find(name) for name in names}


def current_institution_names(conn: sqlite3.Connection):
    """当前论文作者或评审人资料中出现过的非空机构原名集合 (原样, 不做空白归一化)。"""
    names = set()
    for r in conn.execute("SELECT institution FROM reviewers").fetchall():
        if r["institution"]:
            names.add(r["institution"])
    for r in conn.execute("SELECT institutions FROM papers").fetchall():
        for name in json.loads(r["institutions"]):
            if name:
                names.add(name)
    return names


def load_assignment_locks(conn: sqlite3.Connection):
    """读取普通分配锁定表, 返回 {paper_id: [reviewer_id, ...]} (按论文编号排序)。"""
    return {
        r["paper_id"]: json.loads(r["reviewers_json"])
        for r in conn.execute(
            "SELECT paper_id, reviewers_json FROM assignment_locks ORDER BY paper_id"
        ).fetchall()
    }


def load_guarantee_levels(conn: sqlite3.Connection):
    """读取论文评审保障等级表, 返回 {paper_id: 'high'|'medium'} (按论文编号排序)。

    未设置等级的论文视为普通 (normal), 不落行也不出现在结果中;
    删除/撤回论文的等级行已随论文级联清除, 不会出现在结果中。
    """
    return {
        r["paper_id"]: r["level"]
        for r in conn.execute(
            "SELECT paper_id, level FROM paper_guarantee_levels ORDER BY paper_id"
        ).fetchall()
    }


def load_review_deadline(conn: sqlite3.Connection, serial: int):
    """读取某发布序号的统一评审截止行; 该版未设置截止时返回 None。"""
    return conn.execute(
        "SELECT serial, revision, deadline_at, set_at"
        " FROM review_deadlines WHERE serial = ?",
        (serial,),
    ).fetchone()


def expire_pending_objections(conn: sqlite3.Connection, new_serial: int, now: str) -> int:
    """发布序号推进为 new_serial 时, 在同一写事务内把旧序号下仍 pending 的异议标记过期。

    未处理异议随发布序号变化过期 (普通发布/补位发布均须调用);
    已驳回/已受理的异议为终态, 不改动; 过期行永久保留供会务方追溯。
    返回本次标记过期的异议条数。
    """
    return conn.execute(
        "UPDATE feedback_objections SET state = 'expired', decided_at = ?"
        " WHERE state = 'pending' AND serial < ?",
        (now, new_serial),
    ).rowcount


def expire_pending_objections_for_paper(
    conn: sqlite3.Connection, paper_id: str, now: str
) -> int:
    """论文撤回时, 在同一写事务内把该稿仍 pending 的作者异议标记过期。

    撤回立即生效: 待处理异议不再可驳回/受理 (终态化), 已处理 (rejected/accepted)
    与既已过期的异议保持不变; 全部历史保留供会务方追溯。返回过期条数。
    """
    return conn.execute(
        "UPDATE feedback_objections SET state = 'expired', decided_at = ?"
        " WHERE state = 'pending' AND paper_id = ?",
        (now, paper_id),
    ).rowcount


def latest_paper_deletion_at(conn: sqlite3.Connection, paper_id: str) -> str | None:
    """某编号最近一次删除的时刻 (UTC); 从未删除过返回 None。

    同编号重新录入后, 删除时刻 (含同一事务内失效的快照) 之前创建的旧快照
    继续失效; 重录之后发布的新快照创建时刻严格晚于该时刻, 不受影响。
    """
    row = conn.execute(
        "SELECT MAX(deleted_at) AS at FROM paper_deletions WHERE paper_id = ?",
        (paper_id,),
    ).fetchone()
    return row["at"] if row is not None else None


def paper_deleted_after_publish(
    conn: sqlite3.Connection, paper_id: str, serial: int
) -> bool:
    """该论文的当前发布序号槽位是否已随删除失效 (删除凭据冻结的序号 == serial)。

    会务方删除论文时当前发布版不重写: 历史槽位仍留在 published.plan 中, 但该槽位
    冻结进删除凭据 (paper_deletions.published_serial 记录删除瞬间的发布序号)。
    评审人侧一切写操作 (确认/回避/正式评语/评语更正) 与取稿视图必须据此拒绝:
      - 删除后、同编号重新录入但尚未重新发布时, 当前发布序号仍是删除时冻结的旧
        序号 -> 返回 True, 旧槽位继续失效;
      - 重新录入并重新发布 (普通发布/补位发布推进序号) 后, 删除凭据冻结的旧序号
        严格小于新序号 -> 返回 False, 评审人按新序号重新确认并提交;
      - 从未发布方案或删除瞬间该稿不在方案中时冻结序号为 NULL, 不会命中。
    同一编号可多次"删除 -> 重录": 只要存在一行冻结序号等于当前序号即视为已删除。
    """
    row = conn.execute(
        "SELECT 1 FROM paper_deletions"
        " WHERE paper_id = ? AND published_serial = ? LIMIT 1",
        (paper_id, serial),
    ).fetchone()
    return row is not None


def invalidate_snapshots_for_paper_deletion(
    conn: sqlite3.Connection, paper_id: str
) -> int:
    """删除论文时, 在同一写事务内把该稿此前全部未失效快照访问码置失效。

    旧版/当前版统一失效, 快照行保留供会务方追溯; 返回本次失效的版数。
    持码读取与异议提交沿用无效码/已失效码同形的 404, 不透露论文是否存在。
    """
    return conn.execute(
        "UPDATE feedback_snapshots SET invalidated = 1"
        " WHERE paper_id = ? AND invalidated = 0",
        (paper_id,),
    ).rowcount


def load_paper_deletions(conn: sqlite3.Connection, paper_id: str):
    """按删除顺序读取某编号的全部删除凭据 (会务方追溯)。"""
    return conn.execute(
        "SELECT id, paper_id, revision, published_serial, published_plan_json,"
        " invalidated_snapshots, expired_objections, deleted_at"
        " FROM paper_deletions WHERE paper_id = ? ORDER BY id",
        (paper_id,),
    ).fetchall()


@contextmanager
def read_txn():
    with _lock:
        conn = get_conn()
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


@contextmanager
def write_txn():
    with _lock:
        conn = get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def reset_for_tests() -> None:
    """关闭并丢弃当前连接 (仅测试使用, 配合重新设置 DB_PATH)。"""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
