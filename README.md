# 双盲论文评审分配服务

基于 **Python + FastAPI + SQLite** 的双盲评审分配服务。会务方凭本地密钥维护论文与评审人资料，
求解器在硬约束下自动计算全局最优分配方案，支持预演、版本化发布与评审人匿名稿分发。

## 功能与规则

**分配硬约束**（每篇论文恰好 2 名评审人）：

1. 两名评审人必须来自**不同机构**；
2. 两人中**至少一人擅长该论文主题**（擅长主题与论文主题交集非空；论文未标注主题时视为均满足）；
3. 评审人**不得与任一作者同机构**；
4. 评审人**不得评审自己声明回避的论文**；
5. 评审人已分配论文数**不得超过其容量**。

> 约束 1、3 中的"机构"一律按会务方维护的**机构归并组**判定：同一机构的不同原名归并后
> 即视为同一机构（原名逐字相同与由归并造成的同组在预演诊断中分别标注，详见下文
> "机构归并组"）。

**优化目标**（多解时按优先级）：

1. **最小化最高已用容量比例**（全体评审人中 `已分配数 / 容量` 的最大值）；
2. 在此前提下，按 **(论文编号, 评审人编号) 展开的全局序列取字典序最小**的方案。

**无完整方案时**：返回最大部分分配、未分配论文清单及每篇的限制原因（合格评审人不足、
容量占满、无擅长者、机构冲突等），**已发布版本保持不变**。

**普通分配评审人锁定表（会务方）**：会务方可在普通分配前对
`POST /assignment/locks` 整表提交锁定，请求体为
`{"base_revision": N, "locks": {"P1": ["R1", "R2"], "P2": ["R3"]}}`：

- **整表替换**：每篇论文可锁定 **0～2 名评审人**；未列出的论文视为不锁定；
  **空表（`{}`）表示清除全部锁定**；每篇内部按评审人编号字典序归一化（提交顺序无关）；
- 请求在**同一个写事务内先核对修订号**：过期/超前 `409`，拒绝且不改表；
  **未知论文或未知评审人 `404`**（整表拒绝、不留部分变更）；同篇重复锁定或超过两名 `422`，
  论文/评审人编号去首尾空白后为空白 `422`；
- **相同锁定表重试幂等**（规范化后一致即 `changed=false`，不推进修订号、不写表）；
  **有效变更推进资料修订号 +1**；响应返回**当前修订号与是否变更**、锁定论文/槽位计数；
  `GET /assignment/locks` 可随时查看当前锁定表；
- **普通预演与普通发布必须保留锁定槽位**，其余位置在机构冲突、主题专长、回避、资格、
  容量等**全部既有硬约束**与**既有优化次序**下重算；**单人锁定时专长要求可由另一人满足**
  （搭档必须擅长且与锁定者不同机构）；
- **锁定人随后被删除、停用、回避，或因资料变更/机构归并而失格，或锁定累计超过其容量时，
  绝不悄悄释放锁定**：普通预演对该论文给出具体冲突（`lock_*` 原因代码）并标记为
  **不可完整分配**（`feasible=false`）；普通发布 **422 拒绝且不改变当前发布版**；
  会务方须显式调整/清除锁定后才能再次发布；
- **锁定仅约束普通分配**：补位预演/发布不读取锁定表，仍只固定当前发布版中已确认且未回避的
  槽位，补位的确认槽位规则完全不变；
- 论文删除时其锁定行随论文级联清除（论文已不参与分配）；评审人删除不级联——锁定保留并在
  下次预演/发布时以 `lock_reviewer_not_found` 暴露，由会务方显式处理。

**论文评审保障等级（会务方）**：会务方可凭密钥按论文设置评审保障等级
（`POST /papers/{paper_id}/guarantee-level`，请求体
`{"paper_id": "P1", "level": "high", "base_revision": N}`；等级取值
`high`（高）/ `medium`（中）/ `normal`（普通），**未设置即普通**）：

- 请求在**同一个写事务内先核对修订号**：过期/超前 `409`；**未知或已撤回论文 `404`**；
  **非法等级 `422`**；路径与请求体论文编号不一致 `422`；被拒请求不留任何部分变更；
- **相同等级重试幂等**（`changed=false`，不推进修订号、不写记录）；
  **有效变更推进资料修订号 +1**（设为 `normal` 即恢复默认、清除等级行）；
  `GET /papers/guarantee-levels` 可随时查询各论文有效等级与当前资料修订号；
- 等级**仅在普通分配或补位预演无法完整覆盖各自目标论文时**参与求解：
  先使完整分配的论文**总数**最大，再依次使**高、中等级**的完整分配数最大，
  最后沿用既有全局字典序择优；**完整可行时等级不影响**既有容量比例与字典序优化；
- 普通分配锁定与补位已确认槽位仍按既有约束处理；**失效锁定不被等级优先级释放**
  （锁定失效的论文即使为高等级也仍不可完整分配）；
- 普通预演与补位预演响应新增 `level_summary`（各等级已分配/未分配数量），
  原有诊断不变；无法完整分配时发布仍 `422` 拒绝，当前发布版不变；
- **删除或撤回论文后，其等级行随论文级联清除**，不再参与求解，也不再出现在等级查询中。

**版本化发布**：每次资料变更（论文/评审人增删改）使资料修订号 +1；预演返回所依据的修订号，
发布必须携带该修订号，不一致则 `409` 拒绝。校验修订号、重算方案、写入发布版在**同一个
SQLite 写事务**（`BEGIN IMMEDIATE`）内完成，与资料修改并发不会产生基于旧版资料的方案。

每次发布（含补位发布）还会使**发布序号 `serial`** +1，用于标识槽位决定所依附的发布版。

**评审人确认/回避（发布分配后）**：评审人凭现有本人凭据（`X-Reviewer-Id` +
`X-Reviewer-Credential`）对**当前发布版中分配给自己**的论文提交决定：

- `confirm` 确认接受，或 `recuse` 并附**非空回避原因**；
- 同一决定重复提交**幂等**（`changed=false`，不推进修订号）；**已回避任务不能再确认**（`409`）；
  未分配给本人的任务、不存在的论文、尚未发布时一律拒绝（`404`）；空回避原因 `422`；
- **论文被会务方删除后**，即使当前发布方案仍保留该稿删除前的历史槽位，确认与回避也一律
  `404` 拒绝——包括既有决定的幂等重试与"确认后改回避"，**不写决定、不写硬回避、不推进
  资料修订号**；同编号重新录入但**尚未重新发布**（当前发布序号仍是删除时冻结的旧序号）时
  同样拒绝；**重新发布（普通/补位，发布序号 +1）后**评审人按**新发布序号的新槽位**重新确认
  （详见下文"论文删除的反馈生命周期"）；该稿同时立即从评审人取稿列表与逾期任务视图移除；
- **有效决定**（新确认、首次回避、确认后改回避）使资料修订号 +1；
- 回避**立即**停止该评审人读取该匿名稿（取稿列表不再包含该论文），并写入**硬回避表**——
  后续普通求解与补位都不得再把该论文分给该评审人（诊断原因
  `hard_recusal_after_decline`）；
- 回避后**当前发布方案不变**，仍可供会务方核对（每个槽位带 `confirmed/recused/pending`
  状态），其他评审人取稿不受影响。

**正式评语收集（已确认任务）**：评审人凭现有本人凭据（`X-Reviewer-Id` +
`X-Reviewer-Credential`）对**当前发布版中已确认且未回避**的槽位提交
**1～5 的整数评分**与**非空评语**，请求体须携带所针对的**发布序号 `serial`**：

- 提交在**同一个写事务**内核对发布序号、（论文, 评审人）分配关系与决定状态：
  序号过期/超前 `409`；尚未发布、论文不存在、未分配给本人或已被移出当前方案 `404`；
  槽位为 `pending` 或 `recused` `409`；评分非 1～5 整数或评语为空白 `422`；
  被拒请求**不落库**，也不推进资料修订号；
- **论文被会务方删除后**，历史槽位上的正式评语提交一律 `404` 拒绝（删除状态校验优先于
  槽位状态、既有评语与截止判定）：**既有评语的同内容重试也不再幂等返回原收据**，异内容
  同样拒绝，**不写新评语/收据、不推进资料修订号**；同编号重录但未重新发布（旧序号）同样
  拒绝；重新发布后属新序号新槽位，**须重新确认并重新提交，旧评语不复用**；该稿立即从
  评审人本人评语视图移除（旧评语仅供会务方追溯）；
- **同一有效任务只收一份**：评分与评语（首尾空白归一化后）**完全相同的重试**幂等返回
  **原收据 `receipt`**（`changed=false`）；**内容不同**返回 `409` 冲突，已存评语不变；
- 评语收集不影响资料修订号、发布序号与补位求解；
- **会务方可按论文**查看当前两名评审人的提交进度（已确认数/已交数/是否齐全）与评语全文；
  补位发布时**连续保留的确认槽位沿用原评语（同收据）**，**被移出槽位的评语仅供会务方追溯**
  （响应中的 `archived_reviews`），**不计入新方案进度**；同一评审人**后来重新获得该稿**时
  属于新序号下的新槽位，**不得复用旧评语**，须重新确认并重新提交；
- 评审人仅可查看**本人当前有效任务**上的评语（`GET /reviewer/reviews`），
  看不到另一评审人的评语，响应也不含作者机构。

**补位预演与补位发布**：会务方以**当前发布版 + 当前资料修订号**预演补位
（`POST /assignment/backfill/dry-run`）：

- 当前发布版中**已确认且未回避**的关系固定不动；其余位置（pending、已回避释放、
  或因资料变更而失效的固定位置）遵守原有全部硬约束与优化次序（先最小化最高容量比例、
  再全局字典序最小）重算；
- 无完整补位方案时返回未补齐论文及限制原因（`feasible=false`），**不改变发布版**；
- 补位发布（`POST /assignment/backfill/publish`）必须**同时复核两种版本**：资料修订号
  `base_revision` 与发布序号 `base_serial`，任一过期（期间有资料变更或任何新发布）即 `409`；
- 补位发布成功后发布序号 +1，**仅按新发布版授权取稿**；仍在新方案中的
  （论文, 评审人）槽位**保留其确认状态**，被移出方案的关系不再授权取稿；
- **删除稿例外**：某论文在当前发布序号下已被会务方删除（含同编号重新录入但未重新发布）时，
  其删除前的历史槽位**不作为固定位置**（重录稿在补位中作为全新论文重算两名评审人），
  这些槽位也**不产生逾期重分**；补位发布推进序号后，新方案中该稿的槽位**一律为新槽位**：
  删除前的**旧确认/旧评语不沿用**（响应计数 `dropped_deleted_confirmations`），
  评审人须在新序号下重新确认、重新提交；其他论文连续保留槽位的确认/评语沿用规则不变。

**统一 UTC 评审截止时刻（会务方）**：会务方可为**当前发布版**设置**一次**统一的
UTC 评审截止时刻（`POST /review-deadline`，请求体
`{"base_revision": N, "base_serial": M, "deadline_at": "2026-11-01T12:00:00Z"}`）；
**未设置截止时一切沿用既有行为**：

- 设置在**同一个写事务（`BEGIN IMMEDIATE`）内同时核对资料修订号与发布序号**：
  尚未发布 `404`；修订号或序号过期/超前 `409`，**拒绝不改变当前方案、截止设置及历史记录**；
- 截止时刻必须可解析为 **ISO 8601、显式携带时区**（`Z`/`+00:00`/`+08:00` 等，
  服务端**归一化为 UTC**），且**严格晚于设置时的服务端当前时刻**，否则 `422`；
- **同一发布版只能设置一次**：归一化 UTC 后**同版同时刻重试幂等**（`changed=false`，
  不产生新变更、不写新行、不推进修订号/序号）；**同版异时刻 `409` 拒绝**
  （截止不修改、不覆盖）；设置截止本身**不推进资料修订号与发布序号**；
- `GET /review-deadline` 可随时查看当前发布版的截止设置、是否已到截止、服务端当前
  UTC 时刻，以及各槽位状态与汇总：`unconfirmed`（未确认）、`confirmed_unsubmitted`
  （已确认未交）、`submitted`（已交）、`recused`（已回避，不计逾期）、`overdue`
  （已到截止仍未交评语且未回避，含已确认未交）；已撤回稿槽位单独标记、不计入汇总；
  **已删除（含同编号重录但未重新发布）的历史槽位同样带 `deleted` 标记、计入
  `deleted_slots` 但不列入汇总、不计逾期（删除后不再接受任何评审操作）**；
- **服务端到达截止时刻**，该发布版中**仍未交正式评语且未回避**的槽位即**逾期**：
  评审人**不得再确认或交评语**（`409`）；**已交评语及收据保持有效**
  （截止后相同内容重试仍幂等返回原收据，异内容仍按既有规则 `409`），
  已交评语的更正流程不受影响；评审人取稿视图附 `review_deadline` 与本人
  `overdue_papers`；`GET /meta` 亦返回当前发布版的截止与是否已到截止；
- **补位预演与补位发布在同一写事务内判定逾期**：逾期槽位**即使此前已确认也释放**
  （`fixed_review_overdue`），且**本次补位不得把该稿重新分给该逾期评审人**
  （该（评审人, 论文）对在求解中以 `review_overdue` 排除，仅对该论文排除，
  不影响该评审人的其余论文）；**其余已确认槽位仍按既有规则固定**，已交评语随
  固定槽位沿用（同收据）；预演响应附 `review_deadline`、`deadline_expired` 与
  `overdue_slots`，补位发布响应附 `released_overdue_confirmations`；
- 截止判定与**评语提交、确认、补位发布共用同一把写事务锁**，截止到点与提交/发布
  并发时由事务串行化，绝不出现"既在截止后交评语、又被判逾期释放"的竞态；
- **旧版截止不约束新发布版**：普通发布/补位发布推进 `serial` 后，旧序号的截止行
  保留但不再生效，新版本初始无截止、可独立设置自己的截止。

**面向作者的匿名反馈快照**：会务方按论文发布（`POST /papers/{paper_id}/feedback-snapshot`，
请求体携带**当前发布序号 `serial`**）：

- 仅当该版**两名评审人均已确认且各提交一份正式评语**时才能发布；
  尚未发布/论文不在当前发布版 `404`，发布序号过期或超前 `409`，
  确认或评语资料不齐 `422`，**被拒请求不留下新版本**；
- 快照按方案槽位的**固定顺序**（评审人编号字典序）将两份评语标为 **1、2**；
  持码者视图**只含论文编号、两份评分与评语**，不暴露评审人编号、机构、评语收据及任何内部诊断；
- 发布返回该论文的**随机访问码**（`fbk-` 前缀），持码者凭码读取
  （`GET /feedback-snapshots/{code}`，**无需任何凭据**），**只能读取当前有效快照**：
  补位或重新发布导致发布序号变化时**旧码立即失效**；会务方对该稿已交评语
  **发起更正请求时既有访问码同样立即失效**，待更正完成后由会务方按现有规则重新发布；
- **相同发布序号及两份评语收据**的重复请求幂等返回**原快照与原码**（`changed=false`）；
  来源变化（新发布序号下的新槽位/新收据）后**生成新版本**（`version+1`、新访问码），
  旧版保留但仅供会务方追溯（`GET /papers/{paper_id}/feedback-snapshots`，
  含每版序号、访问码、评语收据及是否当前有效）；
- **无效码/已失效码统一 `404`**，响应体相同，**不透露论文是否存在**。

**评审资格停用/启用（会务方）**：既有评审人初始视为**启用**。会务方凭密钥对
`POST /reviewers/{reviewer_id}/status` 提交**评审人编号、所见资料修订号 `base_revision`、
目标状态 `active`（`false` 停用 / `true` 启用）与非空原因**：

- 状态变更在**同一个写事务内先核对修订号**：修订号过期/超前 `409`、未知评审人 `404`、
  空原因 `422`，被拒即不改状态、不推进修订号；
- **匹配修订号下重复提交同一状态和原因**幂等返回（`changed=false`），不推进修订号、不写记录；
  **同状态异原因**（含对初始即为启用者再次启用）`409`；**有效变更**推进资料修订号，
  基于旧修订号的后续普通发布/补位发布按原规则拒绝；
- 停用后该评审人**现有凭据仍然有效但立即 403**：不能取稿、提交决定或评语；
  **既有发布分配、确认和评语不删除**，当前发布版仍可供会务方核对，
  **已发布的作者反馈快照仍按原有规则有效**；
- 停用者**不进入后续普通分配和补位**：普通预演的排除原因标注 `reviewer_disabled`；
  补位时其**已确认槽位被释放**（诊断原因 `fixed_reviewer_disabled`），
  其他仍合格的已确认槽位保持固定；
- **启用后只恢复当前发布版中仍分配给本人的任务访问**；不找回已被补位替换的槽位，
  被归档的评语也不恢复（同一人后来重新获稿仍属新槽位，须重新确认、重新提交评语）；
- `GET /reviewers/{reviewer_id}/status` 可查看当前状态与**全部变更记录**
  （每次有效变更的目标状态、原因、推进后的修订号与时间）；评审人列表与详情同步包含 `active`。

**机构归并组（同一机构的不同原名，会务方）**：会务方凭密钥对
`POST /institutions/merge-groups` 提交**两个当前论文作者或评审人资料中出现过的
机构原名**与**所见资料修订号 `base_revision`**，把它们归为同一个冲突判定组：

- 归并关系**传递且不可拆分**：A 与 B 同组、B 与 C 同组，则 A 与 C 同组；
  系统只提供归并，不存在拆组/移出操作（归并边永久保留）；
- **同组重复提交幂等**（含交换两个名称的顺序、名称与自身归并）：`changed=false`，
  不写归并边、不推进修订号；**有效归并**（两个原先不同的组首次合并）推进资料修订号 +1；
- 请求在**同一个写事务内先核对修订号**：修订号过期/超前 `409`、名称首尾空白归一化后
  为空白 `422`、任一名称未出现在当前资料中 `404`，被拒请求不写入任何边、不留下部分变更；
- 成功响应返回**归并后的名称组**（`names`，按名称字典序）、**当前修订号** `revision`
  与**是否变更** `changed`；会务方可 `GET /institutions/merge-groups`
  查看全部归并组（含已删除资料名称的历史归并，仅供追溯）；
- **普通分配与补位都按归并组判定机构冲突**——"评审人与作者同机构"与
  "两名评审人是否同机构"一律比较归并组而非原名；预演解释须**标明由归并造成的冲突**
  （原因代码带 `_via_merge` 后缀），原名逐字相同的冲突仍用原有原因代码；
  **原始机构名称及既有响应字段保持原样**（资料、方案、评语、快照中均不改名）；
- **已确认槽位因归并而失效时，补位不得固定该槽位**（两名已确认评审人被归并为同机构时
  诊断 `fixed_pair_violates_institution_rule_via_merge`，释放后一槽位重算）；
  旧发布版与评语仍可在会务方视图中追溯，补位发布继续遵守**双版本复核**
  （`base_revision` + `base_serial`）及**评语继承**（保留槽位沿用同收据、
  移出槽位评语进入 `archived_reviews`）规则。

**已交评语更正（会务方发起 → 评审人更正 → 快照重发）**：会务方发现已交评语需要更正时，
凭密钥对 `POST /papers/{paper_id}/review-corrections` 指定**当前发布序号 `serial`、论文、
评审人及非空原因**发起更正请求：- 只允许**当前仍分配、已确认且已交评语**的槽位：尚未发布/论文不在当前发布版 `404`，
  评审人不在该论文当前槽位（旧序号或已移出方案的槽位）`404`，发布序号过期/超前 `409`，
  槽位 `pending`/`recused` 或尚未交评语 `409`，原因为空白 `422`；被拒请求不落库；
  **论文已删除（含同编号重录但未重新发布）的历史槽位同样拒绝发起（`409`）：旧评语
  仅供会务方追溯，重新发布后新槽位须重新确认、重新提交，再按新序号的新评语发起更正**；
- 同一槽位已有待完成更正请求时，**相同原因的重复发起幂等**（`changed=false`），
  **不同原因 `409`**；更正请求不推进资料修订号与发布序号；
- **发起即在同一个写事务内令该稿既有作者反馈访问码失效**（响应含失效版数
  `invalidated_snapshots`），旧码立即 `404`；待更正期间按原序号发布快照 `409`；
- 评审人凭现有本人凭据 `GET /reviewer/review-corrections` 查看**自己的待更正任务**
  （含更正原因与被更正评语的冻结内容，不含他人任务），并对
  `POST /reviewer/review-corrections/{paper_id}` 以**请求对应的发布序号和原评语收据**
  提交**新 1～5 整数评分与非空评语**；
- **首份有效更正生成新收据**并直接更新该槽位的正式评语，**原评语在更正记录中冻结保留**
  供会务方追溯（`GET /papers/{paper_id}/reviews` 响应新增 `corrections`）；
  **同内容重试幂等返回更正收据**（`changed=false`），**异内容 `409` 冲突**，已存更正不变；
- **发布序号变化（补位/重新发布）后，旧序号下的待更正请求失效**：不再列出，
  按旧序号提交 `409`、按新序号配旧收据提交 `404`；
- **补位保留槽位时沿用更正后的评语**（同新收据），**移出槽位的评语照常归档**
  （`archived_reviews`）；更正完成后须由会务方**按现有完整性规则重新发布快照**
  （当前序号 + 两人确认 + 两份评语），生成新版本与新访问码，旧码保持失效；
- 既有正式评语首提交接口的一次提交及冲突语义不变（更正后该接口对更正后内容的
  相同重试幂等返回更正收据，对原评语内容 `409`）；评审人不得查看他人评语。

**作者反馈异议（作者提交 → 会务方驳回/受理）**：作者凭**当前有效的反馈快照访问码**，
对快照中**标号 1 或 2** 的评语提交非空异议理由
（`POST /feedback-objections`，请求体 `{"access_code","label","reason"}`，无需任何请求头）：

- **同一快照同一标号仅留一条**：相同理由（首尾空白归一化后）的重试幂等返回**原记录与原查询凭据**
  （`changed=false`）；理由不同返回 `409` 冲突，已存异议不变；
  数据库唯一约束 `UNIQUE(snapshot_id, label)` 与 `BEGIN IMMEDIATE` 写事务共同保证
  **并发提交也绝不生成两条**；
- **仅当前有效快照可提交**：无效码、已失效码（发布序号变化、更正请求或异议受理导致失效）
  统一 `404`，响应体相同，**不透露论文是否存在**，已失效访问码不得再提交新异议；
  空白理由 `422`，标号非 1/2 `422`；
- 提交时**冻结该标号在快照中的评分与评语及目标评语收据**，并返回**随机查询凭据**
  （`obj-` 前缀）；持凭据可随时 `GET /feedback-objections/{token}` 查看处理状态
  （`pending`/`rejected`/`accepted`/`expired`），**即使快照访问码后来失效**；
  作者侧任何响应都**不暴露评审人编号、机构或评语收据**；
- **未处理异议随发布序号变化过期**：普通发布或补位发布推进 `serial` 时，旧序号下仍
  `pending` 的异议在**同一写事务内原子标记 `expired`**（含过期时间），驳回/受理为终态不改；
  过期与全部历史异议保留供会务方追溯；
- 会务方凭密钥查看异议：`GET /papers/{paper_id}/objections`（可选 `?state=` 过滤）
  与 `GET /objections`（全部论文），视图含异议理由、**冻结的原反馈**、按匿名标号定位的
  评审人编号、目标评语收据、快照访问码及受理后关联的更正请求；
- 会务方对 `POST /objections/{id}/decision` 选择**驳回或受理**，仅 `pending` 异议可处理
  （重复处理 `409`，异议不存在 `404`）：
  - **驳回 `{"decision":"reject","note"?:...}`**：不影响快照，访问码仍可读取，
    可记录驳回说明（作者凭据可见）；
  - **受理 `{"decision":"accept","reason": 非空更正原因}`**：受理时在**同一个写事务内
    原子核对**：① 快照发布序号仍为当前发布序号；② 该标号目标评语收据未变（未被更正/替换）；
    ③ 按匿名标号能定位到当前槽位评审人。核对通过即**走既有评语更正请求流程**
    （插入待更正请求、冻结原评语、**该稿原访问码立即失效**），异议置 `accepted` 并关联
    更正请求，评审人随后按既有更正接口提交更正、会务方再重新发布快照；
  - **发布版或目标评语已变化、该槽位已有待更正请求时拒绝受理 `409`
    且不改变异议状态**；
    受理更正原因为空白 `422`。

**论文撤回（会务方）**：会务方凭密钥对 `POST /papers/{paper_id}/withdrawal`
提交**论文编号、所见资料修订号 `base_revision` 与非空撤回原因**，撤回一篇现存论文：

- 请求在**同一个写事务内先核对修订号**：过期/超前 `409`、未知论文 `404`、
  路径与请求体编号不一致或空原因 `422`，被拒请求不留任何部分改动；
  **匹配当前修订号的同原因重试幂等返回原撤回记录**（`changed=false`，
  不推进修订号、不重复失效快照/异议）；**已撤回稿异原因 `409`**；
- **撤回在同一事务内生效**：该稿**立即停止**评审人取稿、确认、回避、提交评语
  与更正（取稿列表不再包含该论文；决定/评语/更正提交统一按未分配任务 `404`），
  该稿**全部反馈快照访问码立即失效**（持码者读快照、提交新异议统一 `404`，
  不透露论文是否存在），仍 `pending` 的作者异议**原子标记 `expired`**
  （作者原查询凭据仍可查状态；已处理异议终态不变），普通分配锁定行随撤回清除；
- **当前发布版与发布序号保持不变**，仍可供会务方逐槽位核对（每篇带 `withdrawn`
  标记与撤回原因）；撤回**推进资料修订号 +1**，基于旧修订号的普通发布/补位发布
  沿用既有规则 `409`；撤回记录冻结撤回瞬间的发布序号与槽位；
- **已处理异议、评语与更正记录、快照版本、决定/回避与分配历史全部保留**，
  供会务方在 `GET /papers/{paper_id}/withdrawal`、`GET /papers/{paper_id}/reviews`、
  `GET /papers/{paper_id}/feedback-snapshots`、异议列表与 `/assignment` 中追溯；
- **后续普通分配与补位均跳过撤回稿**（不出现在方案、未分配清单或诊断中，
  补位不为其补人），**撤回稿的锁定不得阻塞其他论文**；撤回稿**同编号不能重新录入**
  （`409`）、不可再更新/删除（`409`）、不可再发起更正或发布快照（`409`），
  也不可再对其提交锁定表（整表 `404`）；**其他论文的确认与评语沿用既有规则**。

**论文删除的反馈生命周期（会务方）**：会务方删除现存论文（`DELETE /papers/{paper_id}`）
与撤回不同——删除移除资料行且**同编号允许重新录入**，但删除在**同一个写事务
（`BEGIN IMMEDIATE`）内原子完成反馈生命周期收尾**：

- 该稿此前发布的**全部反馈快照访问码立即失效**（快照行保留供会务方追溯）：持码读取评语
  （`GET /feedback-snapshots/{code}`）与持码提交新异议（`POST /feedback-objections`）
  对旧码一律返回**与无效码同形的 404**（响应体相同，不透露论文是否存在）；
- 该稿仍 `pending` 的作者异议在**同一事务内原子标记 `expired`**（驳回/受理/既过期终态不变）；
  作者的随机查询凭据（`obj-`）**继续可查**为 `expired`；
- 删除响应沿用既有约定推进**资料修订号 +1**（当前发布版与发布序号不变），返回本次
  **失效快照数/过期异议数**及冻结删除瞬间发布序号与槽位的**删除凭据**；
  普通分配锁定行与保障等级行仍随论文级联清除；未知论文 `404`、已撤回论文 `409`；
- **删除后评审人操作立即停止（删除状态校验）**：当前发布方案虽仍保留该稿删除前的
  **历史槽位**供会务方核对，但评审人对这些槽位的**确认、回避、正式评语与评语更正提交**
  一律按"未分配任务"同形返回 `404`——**既有决定/评语的幂等重试、确认后改回避同样拒绝**，
  **不写新的决定、硬回避、评语、更正或收据，不推进资料修订号**；取稿列表、本人评语视图、
  本人待更正任务与逾期任务视图立即不再包含该稿；
- 历史快照、异议、评语/决定/更正记录均不删除，仍可由会务方按编号追溯：
  `GET /papers/{paper_id}/feedback-snapshots`、`/papers/{paper_id}/objections` 等既有追溯视图
  对已删除（含已重录）的编号照常工作；删除凭据见
  `GET /papers/{paper_id}/deletions`（某编号的全部删除事件，从未删除 `404`）
  与 `GET /papers/deletions`（全部删除事件，可选 `?paper_id=` 过滤）；
- **同编号重新录入不恢复旧码效力、也不恢复评审操作**：重录后但**尚未重新发布分配**时，
  当前发布序号仍是删除凭据冻结的旧序号，历史槽位依旧，评审人确认/回避/评语仍 `404`；
  须**重新发布分配**（普通发布或补位发布，推进发布序号）后，评审人才按**新发布序号的
  新槽位**重新确认、重新交评语（补位发布同样**不沿用**删除前的旧确认/旧评语，
  响应计数 `dropped_deleted_confirmations`）；重新发布快照时生成新版本（`version+1`）与
  **新访问码**；删除时刻（`deleted_at`）及之前创建的旧版本继续失效，旧码持续 `404`；
  未重新发布分配时按旧序号重发相同评语收据的快照返回 `409`（不返回旧码、不覆盖历史版本行）。

**会务方实例迁移（一致业务数据快照导出 → 空实例恢复）**：会务方凭密钥可把当前实例的
全部业务数据导出为一份一致快照（`GET /migration/export`），并在另一个**尚无业务记录**
的空实例上恢复（`POST /migration/restore`），用于实例迁移：

- **导出与并发写入隔离**：导出在 `BEGIN IMMEDIATE` 写事务内读取全部业务表，
  与并发写入串行化，得到一致快照；导出为只读操作，不推进任何版本号；
- **快照覆盖全部现存业务记录**：论文（含已撤回）与评审人资料（**含评审人凭据**）、
  资格变更记录、硬回避、资料修订号与当前发布版（发布序号/方案/解释）、普通分配锁定表、
  机构归并边、评审保障等级、统一评审截止、撤回记录、**论文删除凭据
 （`paper_deletions`：每次删除一行，含同编号重录后的再次删除）**、评审人决定、
  正式评语、评语更正记录、匿名反馈快照（**含访问码**）与作者异议（**含查询凭据**）；
  **不包含会务方密钥**（密钥是实例本地配置，不属业务数据）；
  快照含评审人凭据与各类访问码，属敏感数据，应妥善保管与传输；
- **格式版本与内容校验**：快照携带格式标识 `format`、格式版本 `format_version`（当前 **v2**；
  v1 无删除凭据，仍可恢复）与内容校验和 `checksum`（对 `data` 的规范化 JSON 取 SHA-256），
  恢复方据此核对；
- **恢复前校验**：先核对格式标识/格式版本与内容校验和，再校验记录结构与引用符合
  历史保留规则（锁定/保障等级必须指向现存未撤回论文、撤回记录与 `withdrawn` 标记
  一一对应、异议必须指向现存快照版本且论文/序号一致、按发布序号隔离的记录不得越过
  当前发布序号、当前序号的决定/评语必须落在当前发布方案槽位上、各表唯一约束不冲突等；
  决定/评语/更正/快照/异议等历史记录按保留规则可指向已删除的论文或评审人）；
  任一不符 `422` 拒绝且不写入任何数据；
- **访问码效力的跨记录核验（复活攻击防护）**：快照中的 `invalidated` 失效标记**不可自证**——
  把旧快照标记改回有效并重算校验和的伪造快照不会被接受。恢复前另依据**快照版本、
  评语更正请求、论文撤回记录与（v2）论文删除凭据**重算每个访问码的应有效力并逐行比对：
  同一（论文，发布序号）内仅**最高版本**可能有效；快照创建不晚于**更正请求发起时刻**
  （`requested_at`）的版本必已随该请求失效；该组存在**待完成更正请求**时全部失效；
  **撤回稿**的全部快照失效；**快照创建不晚于论文删除时刻（`deleted_at`）的版本必已随删除
  失效——同编号重新录入也不恢复旧码**；待处理异议不得指向已撤回/已删除失效的快照。
  对当前发布序号上标记有效的快照，其两份评语收据还必须等于
  **当前发布方案槽位上的现行评语收据**（更正会原地更换收据，故只改标记/时间戳无法让
  旧快照复活）。标记与重算结果任一方向不一致均为"矛盾快照"，`422` 拒绝且目标实例
  **不留部分数据**；已因更正、撤回或删除失效的快照**不能因仍属当前发布序号而复活**，
  历史内容（旧版本、旧访问码、更正记录、删除凭据）仍随快照迁移、供会务方追溯；
  合法的**连续更正、同序号再次发布、跨发布序号历史快照与删除后同编号重录**均可迁移，
  恢复后**仅当前有效码**可读取快照并提交异议；
- **旧格式快照沿用原有恢复判断**：v1 快照未记载删除时，不依据当前数据推断删除历史
  （不要求 `paper_deletions`、不按删除时刻重算），其余结构/校验和/更正/撤回核验不变；
  v1 信封不得携带 v2 字段（否则按未知字段 `422`）；
- **仅接受空实例**：目标实例全部业务表为空且资料修订号为 0，且从未执行过恢复；
  目标非空或重复恢复 `409` 拒绝；全部写入在同一个 `BEGIN IMMEDIATE` 写事务内完成，
  任一失败整体回滚，**绝不留下部分数据**；
- **恢复后按原规则工作**：资料修订号、发布序号、历史记录、评审人凭据、反馈访问码与
  异议查询凭据继续生效（恢复本身不推进修订号与发布序号）；既有 HTTP API 契约与
  启动方式不变；仅会务方可导出或恢复。

**权限模型**：

| 角色 | 凭据 | 权限 |
|---|---|---|
| 会务方 | 本地密钥（请求头 `X-Organizer-Key`） | 维护资料、预演、发布、查询分配与排除原因、按论文查看评审评语与提交进度、发布与追溯面向作者的匿名反馈快照、停用/启用评审资格并查看变更记录、发起评语更正请求并追溯更正记录、提交并查看普通分配评审人锁定表、查看作者反馈异议与冻结的原反馈并选择驳回或受理、撤回现存论文并追溯撤回记录与全部评审痕迹、删除论文（同事务原子失效该稿快照访问码、过期待处理异议，删除凭据与全部历史可追溯；同编号可重录但旧码不复活）、按论文设置评审保障等级并查询各论文等级、为当前发布版设置一次统一 UTC 评审截止时刻并查看各槽位未确认/已确认未交/已交/逾期状态、导出一致业务数据快照并在空实例恢复（实例迁移） |
| 评审人 | 自身凭据（`X-Reviewer-Id` + `X-Reviewer-Credential`） | 资格启用时：对已发布方案中分配给自己的任务确认/回避、读取匿名稿（不含作者机构）、提交并查看本人正式评语、查看本人待更正任务并提交更正评语；资格停用后凭据仍有效但上述操作一律 `403`；论文撤回后该稿立即从取稿列表移除，对其确认/回避/评语/更正提交一律 `404`（其他论文任务不受影响） |
| 持码者 | 快照随机访问码（路径参数，无需请求头） | 仅可读取对应论文**当前有效**的匿名反馈快照（论文编号 + 标号 1、2 的评分与评语）；凭当前有效访问码对标号 1、2 评语提交非空异议并持随机查询凭据查看处理状态（凭据在快照码失效后仍可查询）；论文撤回或**被会务方删除（含同编号重新录入）**后该稿删除前的访问码立即失效（读取/提交统一 `404`，与无效码同形，不透露论文是否存在），待处理异议变为 `expired` 但凭据仍可查状态 |

## 一键启动（Docker Compose）

```bash
docker compose up --build -d
```

服务地址：**http://localhost:8000**（交互式 API 文档：http://localhost:8000/docs）

SQLite 数据库文件在首次启动时自动初始化，持久化于名为 `review_data` 的卷（容器内 `/data/app.db`）。

### 仅 Docker

```bash
docker build -t review-assignment .
docker run -d -p 8000:8000 \
  -e ORGANIZER_KEY=please-change-me \
  -v review_data:/data \
  review-assignment
```

### 本地开发（无 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
ORGANIZER_KEY=dev-organizer-key python -m app.main
```

## 配置

均通过环境变量配置：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ORGANIZER_KEY` | `dev-organizer-key` | 会务方本地密钥，**生产环境必须修改** |
| `DB_PATH` | `./data/app.db`（容器内 `/data/app.db`） | SQLite 数据库文件路径 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8000` | 监听端口 |

Compose 下可用 `ORGANIZER_KEY=xxx PORT=9000 docker compose up -d` 覆盖。

## API 一览

会务方接口均需请求头 `X-Organizer-Key`；评审人接口需 `X-Reviewer-Id` 与 `X-Reviewer-Credential`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查（无需凭据） |
| GET | `/meta` | 当前资料修订号与发布状态（含当前发布版的统一评审截止 `review_deadline` 与是否已到截止） |
| POST | `/papers` | 录入论文（编号/匿名稿/主题/作者机构），重复编号 `409`；**已撤回编号 `409`（同编号不能重新录入）** |
| PUT / DELETE | `/papers/{paper_id}` | 更新 / 删除论文，不存在 `404`；**论文已撤回时更新/删除均 `409`（资料与评审痕迹保留供追溯）**；删除在同一写事务内原子失效该稿全部快照访问码、把待处理异议标记 `expired`（旧码持码读取/提交新异议统一 `404`，异议查询凭据仍可查），推进修订号 +1，当前发布版/发布序号不变；响应含失效快照数、过期异议数与删除凭据；**同编号可重新录入，删除时刻之前的旧快照继续失效** |
| GET | `/papers/deletions` | 会务方查看全部论文删除凭据（删除时刻、推进后的修订号、冻结发布序号/槽位、失效快照数/过期异议数；可选 `?paper_id=` 过滤） |
| GET | `/papers/{paper_id}/deletions` | 会务方按编号查看全部删除事件（同编号可多次"删除→重录"，按删除顺序）；该编号从未删除 `404` |
| GET | `/papers` | 论文列表（含每篇 `withdrawn` 状态与撤回记录 `withdrawal`） |
| POST | `/papers/{paper_id}/withdrawal` | 撤回现存论文：`{"paper_id","base_revision","reason"}`；事务内先核对修订号，过期/超前 `409`，未知论文 `404`，空原因/编号不一致 `422`，异原因重试 `409`，同原因匹配当前修订号重试返回原记录（`changed=false`）；撤回同事务停止评审人取稿/确认/回避/评语/更正、失效该稿全部快照访问码、待处理异议变 `expired`、清除该稿锁定与保障等级并推进修订号；返回撤回状态、修订号、撤回记录（冻结撤回时的发布序号/槽位）与失效快照数/过期异议数 |
| GET | `/papers/{paper_id}/withdrawal` | 会务方查看撤回记录（撤回状态、原因、推进后的修订号、撤回时间、冻结的发布序号与槽位）；论文未知或未撤回 `404` |
| POST | `/papers/{paper_id}/guarantee-level` | 按论文设置评审保障等级：`{"paper_id","level","base_revision"}`，等级 `high`/`medium`/`normal`（未设置即普通，设为 `normal` 即恢复默认）；事务内先核对修订号，过期/超前 `409`，未知或已撤回论文 `404`，非法等级或编号不一致 `422`，均不留部分变更；相同等级重试幂等（`changed=false`，不推进修订号），有效变更推进修订号 |
| GET | `/papers/guarantee-levels` | 会务方查询各现存论文的有效保障等级（未设置为 `normal`）、各等级论文数与当前资料修订号；已删除/已撤回论文的等级已清除，不再列出也不再参与求解 |
| POST | `/reviewers` | 录入评审人（编号/凭据/擅长主题/机构/容量/回避论文），重复编号 `409`，非法容量 `422`；既有评审人初始为启用状态（`active=true`） |
| PUT / DELETE | `/reviewers/{reviewer_id}` | 更新 / 删除评审人 |
| GET | `/reviewers` | 评审人列表（含每人 `active` 资格状态） |
| POST | `/reviewers/{reviewer_id}/status` | 停用/启用评审资格：`{"reviewer_id","base_revision","active","reason"}`；事务内先核对修订号，过期/超前 `409`，未知评审人 `404`，空原因 `422`，同状态异原因 `409`，同状态同原因重复提交幂等（`changed=false`），有效变更推进修订号 |
| GET | `/reviewers/{reviewer_id}/status` | 查看评审人当前资格状态与全部变更记录（含每次原因、修订号、时间），未知评审人 `404` |
| POST | `/institutions/merge-groups` | 机构归并：`{"name_a","name_b","base_revision"}`；两名称须为当前资料中出现的机构原名；过期/超前修订号 `409`，空白名称 `422`，未知名称 `404`，同组重复提交幂等（`changed=false`，不推进修订号），有效归并推进修订号；返回归并后的名称组 `names`、当前修订号与 `changed` |
| GET | `/institutions/merge-groups` | 查看全部机构归并组（传递闭包，仅列 ≥2 个原名的组；归并不可拆分） |
| POST | `/assignment/locks` | 普通分配锁定表（整表替换）：`{"base_revision":N,"locks":{"P1":["R1","R2"]}}`，每篇 0~2 名，空表清除全部锁定；未知论文/评审人或**含已撤回论文**（`withdrawn_papers`）`404`，同篇重复或超两人/空白编号 `422`，过期修订号 `409`，均不改表；相同表重试幂等（`changed=false`），有效变更推进修订号；返回修订号、`changed` 与锁定计数 |
| GET | `/assignment/locks` | 会务方查看当前锁定表（`locks`、资料修订号、锁定论文/槽位计数） |
| POST | `/assignment/dry-run` | 预演：返回方案、未分配论文与限制原因、各等级已分配/未分配数量（`level_summary`）、资料修订号；有锁定时保留锁定槽位，锁定失效以 `lock_*` 诊断并标为不可完整分配；无法完整覆盖时按 总数→高/中等级数→字典序 选取部分方案；不影响已发布版 |
| POST | `/assignment/publish` | 发布：携带 `{"base_revision": N}`，修订号过期 `409`，无可行方案 `422` |
| POST | `/assignment/backfill/dry-run` | 补位预演：固定已确认关系，其余位置按原约束/优化次序重算；返回 `revision` 与 `serial` 及各等级已分配/未分配数量（`level_summary`）；无法完整覆盖时同样按 总数→高/中等级数→字典序 选取部分方案 |
| POST | `/assignment/backfill/publish` | 补位发布：`{"base_revision": N, "base_serial": M}` 双版本复核，过期 `409`，补不齐 `422`；响应附 `carried_confirmations`/`carried_reviews`（连续保留槽位沿用的确认/评语）与 **`dropped_deleted_confirmations`（删除稿含同编号重录后，删除前旧确认不沿用到新序号槽位的数量；旧评语同样不沿用，须重新确认、重新提交）** |
| POST | `/review-deadline` | 为当前发布版设置一次统一 UTC 评审截止：`{"base_revision":N,"base_serial":M,"deadline_at":"2026-11-01T12:00:00Z"}`；时刻须显式携带时区（归一化 UTC）且晚于当前时刻；尚未发布 `404`，修订号/序号不符 `409`，非法/过去时刻 `422`；同版同时刻重试幂等（`changed=false`），同版异时刻 `409`；设置不推进修订号/序号、不改方案与历史 |
| GET | `/review-deadline` | 会务方查看当前发布版截止设置、是否已到截止、服务端 UTC 时刻，以及各槽位状态（`unconfirmed`/`confirmed_unsubmitted`/`submitted`/`recused`/`overdue`）与汇总计数；已撤回槽位单独标记、不计入汇总 |
| GET | `/assignment` | 查询已发布方案（含 `serial`）及每篇论文的分配/排除原因、每槽位确认/回避状态；撤回稿仍在当前发布版视图中但带 `withdrawn=true` 与 `withdrawal_reason`（另返回 `withdrawn_papers` 清单），评审人侧则立即不可见；**被删除论文的历史槽位同样保留供会务方核对，带 `deleted=true`（另返回 `deleted_papers` 清单；同编号重录但未重新发布时仍标注，重新发布推进序号后自动消失），评审人侧立即不可见且不可操作** |
| GET | `/papers/{paper_id}/reviews` | 会务方按论文查看当前两名评审人的评语提交进度、评语全文，及被移出槽位的追溯评语（`archived_reviews`）与全部更正记录（`corrections`）；撤回稿返回 `withdrawn=true` 与撤回记录（撤回后又重新发布过普通方案时 `slots` 为空、原评语全部在 `archived_reviews`） |
| POST | `/papers/{paper_id}/review-corrections` | 会务方发起评语更正请求：`{"paper_id","serial","reviewer_id","reason"}`；仅当前仍分配、已确认且已交评语的槽位；旧序号 `409`、已移出槽位 `404`、未确认/未交评语 `409`、空原因 `422`、**论文已撤回 `409`**；同槽位同原因重复发起幂等（`changed=false`），异原因 `409`；发起即令该稿既有反馈访问码失效 |
| POST | `/papers/{paper_id}/feedback-snapshot` | 会务方发布面向作者的匿名反馈快照：`{"serial": N}`；资料不齐 `422` 不留新版，序号过期/超前 `409`，未发布/论文不在方案 `404`，**论文已撤回 `409`**；成功返回随机访问码与快照，同序号同两份评语收据重复请求返回原码（`changed=false`） |
| GET | `/feedback-snapshots/{access_code}` | 持码者读取当前有效快照（**无需凭据**）：仅论文编号与标号 1、2 的两份评分和评语；无效码/已失效旧码统一 `404` |
| GET | `/papers/{paper_id}/feedback-snapshots` | 会务方追溯某论文的全部快照版本（含已失效旧版、访问码、评语收据与当前有效标记） |
| POST | `/feedback-objections` | 作者凭当前有效快照访问码对标号 1/2 评语提交非空异议：`{"access_code","label","reason"}`（**无需凭据**）；同快照同标号仅一条，同理由重试返回原记录（`changed=false`），异理由 `409`，并发不生成两条；返回随机查询凭据（`obj-` 前缀）；无效/已失效访问码统一 `404`，空白理由/非法标号 `422` |
| GET | `/feedback-objections/{query_token}` | 持随机查询凭据查看异议处理状态（**无需凭据**）：`pending`/`rejected`/`accepted`/`expired`；快照码失效后仍可查询；无效凭据统一 `404`；响应不含评审人编号、机构、收据 |
| GET | `/papers/{paper_id}/objections` | 会务方按论文查看全部异议与冻结的原反馈（含按标号定位的评审人、目标收据、快照码、关联更正；可选 `?state=pending\|rejected\|accepted\|expired` 过滤） |
| GET | `/objections` | 会务方查看全部论文异议（含已过期历史；可选 `?state=` 过滤，按提交顺序） |
| POST | `/objections/{objection_id}/decision` | 会务方处理异议：`{"decision":"reject","note"?}`（驳回不影响快照）或 `{"decision":"accept","reason"}`（受理须非空更正原因，原子核对发布序号仍为当前序号且目标评语收据未变，按标号走既有更正请求流程并使原访问码立即失效）；异议不存在 `404`，非 pending `409`，发布版/目标评语已变化或该槽位已有待更正请求 `409` 且不改异议状态，受理空原因 `422` |
| GET | `/reviewer/assignments` | 评审人读取分配给自己的匿名稿（凭据失效 `401`；**资格停用 `403`**；已回避论文不返稿，仅列于 `recused`；**已撤回论文立即不返稿**）；响应附当前发布版统一评审截止 `review_deadline` 与本人逾期论文 `overdue_papers`（未设置截止为 `null`） |
| POST | `/reviewer/assignments/{paper_id}/decision` | 评审人提交 `{"paper_id","decision":"confirm"|"recuse","reason"?}`；幂等；未分配 `404`，已回避再确认 `409`，空回避原因 `422`，**资格停用 `403`**，**论文已撤回 `404`**，**论文已被会务方删除（含同编号重录但未重新发布；历史槽位仍在当前方案中）时确认/回避一律 `404`，既有决定的幂等重试与确认后改回避同样拒绝，不写决定/硬回避、不推进修订号**，**该版统一评审截止到达后未确认槽位再确认 `409`（已逾期）** |
| POST | `/reviewer/assignments/{paper_id}/review` | 评审人提交 `{"paper_id","serial","score":1~5整数,"comment"}`；同内容重试返原收据，内容不同 `409`，序号过期/超前 `409`，未分配/已移出/已撤回 `404`，未确认/已回避 `409`，非法评分/空评语 `422`，**资格停用 `403`**，**论文已被会务方删除（含同编号重录但未重新发布）时历史槽位一律 `404`——既有评语的同内容重试也不再幂等返原收据，不写评语/收据、不推进修订号；重新发布（新序号）后须重新确认并重新提交，旧评语不复用**，**该版统一评审截止到达后仍未交评语的槽位提交 `409`（已逾期；已交评语相同重试仍幂等返原收据）** |
| GET | `/reviewer/reviews` | 评审人查看本人当前有效任务上已提交的评语（不含他人评语与作者机构；**资格停用 `403`**；不含已撤回论文） |
| GET | `/reviewer/review-corrections` | 评审人查看本人在当前发布序号下的待更正任务（含更正原因与原评语冻结内容；不含他人任务；**资格停用 `403`**；**已撤回论文的待更正任务不再列出；论文已删除（含同编号重录但未重新发布）的旧序号更正任务同样不再列出**） |
| POST | `/reviewer/review-corrections/{paper_id}` | 评审人提交更正评语：`{"paper_id","serial","original_receipt","score","comment"}`；首份有效更正生成新收据并更新槽位评语（原评语冻结保留）；同内容重试返更正收据（`changed=false`），异内容 `409`，序号过期/超前 `409`，无对应任务/论文已撤回/论文已删除（含同编号重录但未重新发布）`404`，非法评分/空评语 `422`，**资格停用 `403`** |
| GET | `/migration/export` | 会务方导出一致业务数据快照（`BEGIN IMMEDIATE` 事务内读取，与并发写入隔离）：覆盖论文/评审人资料（含评审人凭据）、资格变更、硬回避、修订号与发布版、锁定、归并、保障等级、截止、撤回记录、**论文删除凭据 `paper_deletions`**、决定、评语、更正、反馈快照（含访问码）与异议（含查询凭据）；**不含会务方密钥**；响应附格式版本（v2）与内容校验和 `checksum`（SHA-256）；只读，不推进版本号 |
| POST | `/migration/restore` | 会务方把快照恢复到**尚无业务记录的空实例**：先核对格式标识/格式版本（接受 v1/v2）/内容校验和与记录引用（符合历史保留规则），失败 `422`；**访问码效力另按快照版本/更正请求/撤回记录/删除凭据跨记录重算（失效标记被改回有效、旧收据冒充现行、同编号重录后旧码复活等矛盾快照 `422`，已失效快照不得因仍属当前发布序号而复活；v1 旧格式未记载删除时沿用原有恢复判断，不推断删除历史）**；目标非空或重复恢复 `409`；全部写入在同一写事务内完成，任一失败整体回滚、不留部分数据；恢复后仅当前有效访问码可读快照/提异议，历史版本与删除凭据供会务方追溯，修订号、发布序号、凭据按原规则工作 |

## 调用示例

完整可运行脚本见 [`examples/demo.sh`](examples/demo.sh)（服务启动后执行 `./examples/demo.sh`）；
确认/回避 + 补位流程见 [`examples/backfill.sh`](examples/backfill.sh)
（`./examples/backfill.sh`，建议对新库运行）；
正式评语收集流程见 [`examples/reviews.sh`](examples/reviews.sh)
（`./examples/reviews.sh`，建议对新库运行）；
匿名反馈快照流程见 [`examples/snapshots.sh`](examples/snapshots.sh)
（`./examples/snapshots.sh`，建议对新库运行）；
本轮评审资格停用/启用流程见 [`examples/eligibility.sh`](examples/eligibility.sh)
（`./examples/eligibility.sh`，建议对新库运行）；
本轮机构归并流程见 [`examples/institutions.sh`](examples/institutions.sh)
（`./examples/institutions.sh`，建议对新库运行）；
本轮评语更正流程见 [`examples/corrections.sh`](examples/corrections.sh)
（`./examples/corrections.sh`，建议对新库运行）；
本轮普通分配锁定表流程见 [`examples/locks.sh`](examples/locks.sh)
（`./examples/locks.sh`，建议对新库运行）；
本轮作者反馈异议流程见 [`examples/objections.sh`](examples/objections.sh)
（`./examples/objections.sh`，建议对新库运行）；
本轮论文撤回流程见 [`examples/withdrawals.sh`](examples/withdrawals.sh)
（`./examples/withdrawals.sh`，建议对新库运行）；
本轮评审保障等级流程见 [`examples/guarantee_levels.sh`](examples/guarantee_levels.sh)
（`./examples/guarantee_levels.sh`，建议对新库运行）；
本轮统一 UTC 评审截止流程见 [`examples/deadlines.sh`](examples/deadlines.sh)
（`./examples/deadlines.sh`，建议对新库运行）；
本轮会务方实例迁移（导出 → 空实例恢复）流程见 [`examples/migration.sh`](examples/migration.sh)
（`./examples/migration.sh`，需要一个已有数据的源实例与一个空库目标实例）；
本轮论文删除反馈生命周期（删除同事务失效旧码/过期异议、历史与凭据可追溯、同编号重录旧码不复活、
删除凭据随迁移核验）流程见 [`examples/paper_deletions.sh`](examples/paper_deletions.sh)
（`./examples/paper_deletions.sh`，需要源实例与一个空库目标实例）。

```bash
BASE=http://localhost:8000
ORG='X-Organizer-Key: dev-organizer-key'

# 1. 录入评审人
curl -X POST $BASE/reviewers -H "$ORG" -H 'Content-Type: application/json' -d '{
  "reviewer_id": "R1", "credential": "r1-secret", "topics": ["AI"],
  "institution": "Inst-A", "capacity": 2, "avoid_papers": []
}'

# 2. 录入论文
curl -X POST $BASE/papers -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "匿名稿全文...", "topics": ["AI"],
  "institutions": ["Univ-X"]
}'

# 3. 预演（记下返回的 revision）
curl -X POST $BASE/assignment/dry-run -H "$ORG"

# 4. 发布（携带预演所用修订号；资料已变动则返回 409，需重新预演）
curl -X POST $BASE/assignment/publish -H "$ORG" -H 'Content-Type: application/json' \
  -d '{"base_revision": 2}'

# 5. 会务方查询分配与排除原因
curl $BASE/assignment -H "$ORG"

# 6. 评审人凭自身凭据读取已分配匿名稿
curl $BASE/reviewer/assignments -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret'
```

### 本轮用法：评审人确认/回避与会务方补位

```bash
# 7. 评审人确认当前分配给自己的论文（有效决定推进资料修订号；重复提交幂等）
curl -X POST $BASE/reviewer/assignments/P1/decision \
  -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret' \
  -H 'Content-Type: application/json' \
  -d '{"paper_id": "P1", "decision": "confirm"}'
# -> {"state":"confirmed","changed":true,"revision":N,"serial":S,...}

# 8. 另一评审人声明回避（原因非空；提交后立即不能再读取该稿，且成为硬回避）
curl -X POST $BASE/reviewer/assignments/P1/decision \
  -H 'X-Reviewer-Id: R2' -H 'X-Reviewer-Credential: r2-secret' \
  -H 'Content-Type: application/json' \
  -d '{"paper_id": "P1", "decision": "recuse", "reason": "近三年与第二作者有合作"}'

# 9. 会务方补位预演：已确认关系固定，其余位置重算；记下 revision 与 serial
curl -X POST $BASE/assignment/backfill/dry-run -H "$ORG"
# -> {"feasible":true,"revision":N,"serial":S,"plan":{...},"fixed":{"P1":["R1"]},...}

# 10. 补位发布：双版本复核；期间资料变更或有过任何发布都会 409，需重新预演
curl -X POST $BASE/assignment/backfill/publish -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d '{"base_revision": N, "base_serial": S}'
# -> {"ok":true,"serial":S+1,"revision":N,"carried_confirmations":1,"plan":{...}}
```

评审人取稿响应新增 `states`（本人在当前发布版各槽位的 `confirmed/recused/pending`）
与 `recused`（已回避论文编号、本人填写的原因、时间）；`assignments` 中不再出现已回避论文。

### 本轮用法：正式评分与评语收集

```bash
# 11. 评审人先确认任务 (仅 confirmed 槽位可交正式评语), 再提交 1~5 整数评分与非空评语
curl -X POST $BASE/reviewer/assignments/P1/review \
  -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret' \
  -H 'Content-Type: application/json' \
  -d '{"paper_id": "P1", "serial": S, "score": 4, "comment": "选题重要, 建议补充消融实验。"}'
# -> {"ok":true,"score":4,"comment":"...","receipt":"rvw-xxxx","changed":true,"serial":S,...}

# 12. 完全相同的重试 (评分+评语一致, 首尾空白归一化) -> 原收据, changed=false
#     内容不同 -> 409 冲突, 已存评语不变; 过期/超前 serial -> 409
# 13. 评审人查看本人当前有效任务的评语 (看不到另一评审人, 也无作者机构)
curl $BASE/reviewer/reviews -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret'
# -> {"reviewer_id":"R1","serial":S,"reviews":[{"paper_id":"P1","score":4,"comment":"...","receipt":"rvw-xxxx",...}]}

# 14. 会务方按论文查看两名评审人的提交进度与评语全文
curl $BASE/papers/P1/reviews -H "$ORG"
# -> {"paper_id":"P1","serial":S,
#     "progress":{"slots":2,"confirmed":2,"submitted":1,"complete":false},
#     "slots":[{"reviewer_id":"R1","state":"confirmed","submitted":true,"review":{...}},
#              {"reviewer_id":"R2","state":"confirmed","submitted":false,"review":null}],
#     "archived_reviews":[]}
```

补位发布成功后，响应新增 `carried_reviews`（连续保留确认槽位所沿用的评语份数）：
沿用的评语在新序号下**收据与内容不变**；被移出槽位的评语不再出现在任何人的当前视图，
仅由会务方在该论文的 `archived_reviews`（带原 `serial`）中追溯；同一人后来重新获稿时
须重新确认并提交新评语（新收据）。

### 本轮用法：面向作者的匿名反馈快照

```bash
# 15. 两名评审人均已确认并各交一份正式评语后, 会务方携带当前发布序号发布快照
curl -X POST $BASE/papers/P1/feedback-snapshot -H "$ORG" \
  -H 'Content-Type: application/json' -d '{"serial": S}'
# -> {"ok":true,"paper_id":"P1","serial":S,"version":1,
#     "access_code":"fbk-xxxx","changed":true,
#     "snapshot":{"paper_id":"P1","reviews":[
#       {"label":1,"score":4,"comment":"..."},{"label":2,"score":2,"comment":"..."}]}}

# 16. 相同发布序号及两份评语收据重复请求 -> 原快照原码, changed=false
#     序号过期/超前 -> 409; 确认/评语未齐 -> 422 (不产生新版本); 论文不在方案 -> 404

# 17. 作者(持码者)读取: 无需任何凭据, 只见论文编号与标号 1、2 的评分/评语
curl $BASE/feedback-snapshots/fbk-xxxx
# -> {"paper_id":"P1","reviews":[{"label":1,"score":4,"comment":"..."},
#                                {"label":2,"score":2,"comment":"..."}]}

# 18. 无效码或补位/重新发布导致序号前进后的旧码 -> 统一 404 (不透露论文是否存在)
#     在新序号上重新发布成功则生成 version+1 与新访问码

# 19. 会务方追溯该论文的全部快照版本 (含已失效旧版)
curl $BASE/papers/P1/feedback-snapshots -H "$ORG"
# -> {"paper_id":"P1","current_serial":S2,"snapshots":[
#     {"version":1,"serial":S1,"active":false,"access_code":"fbk-....","receipts":[...],"reviews":[...]},
#     {"version":2,"serial":S2,"active":true, "access_code":"fbk-....","receipts":[...],"reviews":[...]}]}
```

快照响应不含评审人编号、评审人机构、作者机构、评语收据、发布序号/版本及任何内部诊断字段；
标号 1、2 的固定顺序为方案槽位顺序（评审人编号字典序）。

### 本轮用法：评审资格停用/启用与变更记录

```bash
# 20. 先取当前资料修订号, 再停用评审人 R1 (原因必须非空)
REV=$(curl -s $BASE/meta -H "$ORG" | python3 -c 'import sys,json;print(json.load(sys.stdin)["revision"])')
curl -X POST $BASE/reviewers/R1/status -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"reviewer_id\": \"R1\", \"base_revision\": $REV, \"active\": false, \"reason\": \"长期休假暂停本轮评审\"}"
# -> {"ok":true,"reviewer_id":"R1","active":false,"changed":true,"revision":N+1,"reason":"..."}

# 21. 匹配修订号重复提交同一状态和原因 -> changed=false (不推进修订号、不写记录)
#     同状态异原因 -> 409; 过期/超前修订号 -> 409; 未知评审人 -> 404; 空原因 -> 422
# 22. R1 凭据仍有效, 但取稿/提交决定/提交评语/查看评语立即全部 403 (错误凭据仍为 401)
curl -s -o /dev/null -w '%{http_code}\n' $BASE/reviewer/assignments \
  -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret'   # -> 403
# 23. 既有发布分配/确认/评语不删除; 已发布的作者反馈快照仍有效;
#     后续普通预演 R1 被标注 reviewer_disabled; 补位预演释放其已确认槽位
#     (fixed_reviewer_disabled), 其他合格的已确认槽位保持固定
# 24. 重新启用: 只恢复当前发布版中仍分配给 R1 的任务; 已被补位替换的槽位不找回
curl -X POST $BASE/reviewers/R1/status -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"reviewer_id\": \"R1\", \"base_revision\": $REV2, \"active\": true, \"reason\": \"假期结束恢复评审\"}"
# -> {"ok":true,"active":true,"changed":true,"revision":N2,...}

# 25. 会务方查看当前状态与全部变更记录 (每次有效变更的目标状态/原因/修订号/时间)
curl $BASE/reviewers/R1/status -H "$ORG"
# -> {"reviewer_id":"R1","active":true,"history":[
#     {"active":false,"reason":"长期休假暂停本轮评审","revision":8,"changed_at":"..."},
#     {"active":true,"reason":"假期结束恢复评审","revision":12,"changed_at":"..."}]}
```

### 本轮用法：机构归并组（同一机构的不同原名）

```bash
# 26. 先取当前资料修订号, 再提交两个"当前资料中出现过"的机构原名
REV=$(curl -s $BASE/meta -H "$ORG" | python3 -c 'import sys,json;print(json.load(sys.stdin)["revision"])')
curl -X POST $BASE/institutions/merge-groups -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"name_a\": \"Acme-U\", \"name_b\": \"Acme University\", \"base_revision\": $REV}"
# -> {"ok":true,"changed":true,"revision":N+1,
#     "names":["Acme University","Acme-U"],"group_key":"Acme University",
#     "submitted":["Acme-U","Acme University"]}

# 27. 同组重复提交 (含交换顺序) 幂等 changed=false, 不推进修订号、不写归并边;
#     名称与自身归并同样幂等; 归并关系传递: A~B、B~C => A 与 C 同组, 再提交 A、C 幂等
#     过期/超前修订号 -> 409; 空白名称 -> 422; 名称未出现在当前论文/评审人资料 -> 404
#     (被拒请求不写入任何边, 不留下部分变更)
curl $BASE/institutions/merge-groups -H "$ORG"
# -> {"revision":N+1,"groups":[
#     {"group_key":"Acme University","names":["Acme University","Acme-U"],"size":2}]}

# 28. 普通预演按归并组判定冲突, 解释标明"由归并造成":
curl -X POST $BASE/assignment/dry-run -H "$ORG"
# 评审人机构 "Acme-U" 与作者机构 "Acme University" 已归并 ->
#   papers.P1.excluded: [{"reviewer_id":"R1","reason":"same_institution_as_author_via_merge"}]
# 两名评审人原名不同但同归并组 -> 不能同篇配对; 全部合格者同组且无可行方案时,
#   诊断原因 eligible_reviewers_share_single_institution_via_merge
# (原名逐字相同的冲突仍使用 same_institution_as_author 等既有原因代码;
#  资料与响应中的原始机构名称保持原样, 不做替换)

# 29. 已确认槽位因归并失效 (两名已确认评审人被归并为同机构) 时,
#     补位预演不固定该槽位:
curl -X POST $BASE/assignment/backfill/dry-run -H "$ORG"
# -> fixed_problems.P1: [{"reviewer_id":"R4",
#     "reason":"fixed_pair_violates_institution_rule_via_merge"}],
#    fixed.P1: ["R3"], plan.P1: ["R3","R5"]
# 30. 补位发布仍需双版本复核; 保留槽位的确认与评语沿用 (同收据),
#     被移出槽位的评语仅供会务方追溯 (archived_reviews)
curl -X POST $BASE/assignment/backfill/publish -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d '{"base_revision": N, "base_serial": S}'
```

### 本轮用法：已交评语更正（会务方发起 → 评审人更正 → 快照重发）

```bash
# 31. 会务方发现 R1 的已交评语需要更正: 指定当前发布序号 + 论文 + 评审人 + 非空原因
curl -X POST $BASE/papers/P1/review-corrections -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d '{"paper_id": "P1", "serial": S, "reviewer_id": "R1", "reason": "评分与评语内容明显不符, 请更正"}'
# -> {"ok":true,"changed":true,"correction_id":1,"state":"pending","serial":S,
#     "original":{"score":4,"comment":"...","receipt":"rvw-....",...},
#     "invalidated_snapshots":1,...}   # 该稿既有作者反馈访问码立即失效 (旧码 404)
#    仅当前仍分配、已确认且已交评语的槽位可发起: 旧序号 409, 已移出槽位 404,
#    pending/recused 或未交评语 409, 空原因 422; 同槽位同原因重复发起幂等, 异原因 409

# 32. 待更正期间按原序号发布快照 -> 409 (须待更正完成后重新发布)

# 33. 评审人凭本人凭据查看自己的待更正任务 (含更正原因与原评语收据, 不含他人任务)
curl $BASE/reviewer/review-corrections -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret'
# -> {"reviewer_id":"R1","serial":S,"corrections":[{"correction_id":1,"reason":"...",
#     "original":{"score":4,"comment":"...","receipt":"rvw-...."},"state":"pending",...}]}

# 34. 评审人以请求对应的发布序号 + 原评语收据提交新 1~5 整数评分与非空评语
curl -X POST $BASE/reviewer/review-corrections/P1 \
  -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret' \
  -H 'Content-Type: application/json' \
  -d '{"paper_id": "P1", "serial": S, "original_receipt": "rvw-....",
       "score": 5, "comment": "更正: 选题重要, 方法扎实, 建议接收。"}'
# -> {"ok":true,"receipt":"rvw-<新收据>","original_receipt":"rvw-....","changed":true,...}
#    首份有效更正生成新收据并更新槽位评语, 原评语冻结保留供会务方追溯;
#    同内容重试 -> 返更正收据 changed=false; 异内容 -> 409; 收据无对应任务 -> 404

# 35. 会务方按论文追溯更正记录 (原评语与更正后内容对照)
curl $BASE/papers/P1/reviews -H "$ORG"
# -> {"paper_id":"P1","serial":S,...,"slots":[...],
#     "corrections":[{"state":"completed","original":{...},"corrected":{...},...}]}

# 36. 更正完成后, 会务方按现有完整性规则重新发布快照 -> version+1 与新访问码, 旧码保持失效
curl -X POST $BASE/papers/P1/feedback-snapshot -H "$ORG" \
  -H 'Content-Type: application/json' -d '{"serial": S}'

# 37. 补位发布: 保留槽位沿用更正后的评语 (同新收据);
#     发布序号变化后, 旧序号下的待更正请求失效 (不再列出, 按旧序号提交 409)
```

### 本轮用法：普通分配评审人锁定表

```bash
# 38. 会务方先取资料修订号, 再整表提交锁定: P1 锁 R4 一人, P2 锁 R1+R2 两人;
#     未列出的论文不锁定, 空表 {} 清除全部锁定
REV=$(curl -s $BASE/meta -H "$ORG" | python3 -c 'import sys,json;print(json.load(sys.stdin)["revision"])')
curl -X POST $BASE/assignment/locks -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"locks\": {\"P1\": [\"R4\"], \"P2\": [\"R1\", \"R2\"]}}"
# -> {"ok":true,"changed":true,"revision":N+1,
#     "locks":{"P1":["R4"],"P2":["R1","R2"]},"locked_papers":2,"locked_slots":3}

# 39. 相同锁定表重试 -> changed=false (不推进修订号); 每篇按评审人编号字典序归一化
#     过期/超前修订号 -> 409; 未知论文/评审人 -> 404 (整表拒绝、不改表);
#     同篇重复锁定或超过两名 -> 422; 空白编号 -> 422
# 40. 会务方查看当前锁定表
curl $BASE/assignment/locks -H "$ORG"
# -> {"revision":N+1,"locks":{"P1":["R4"],...},"locked_papers":2,"locked_slots":3}

# 41. 普通预演: 锁定槽位保留; 单人锁定时搭档必须与锁定者不同机构且可补足专长
curl -X POST $BASE/assignment/dry-run -H "$ORG"
# -> {"feasible":true,"plan":{"P1":["R1","R4"],...},
#     "locks":{"P1":["R4"],"P2":["R1","R2"]},"lock_problems":{},...}

# 42. 锁定人随后被停用/删除/回避, 或资料及机构归并使其失格, 或锁定累计超容量:
#     预演不释放槽位, 而是说明具体冲突并标为不可完整分配; 发布 422 且当前发布版不变
# -> {"feasible":false,"unassigned":["P1"],"diagnostics":{"P1":{
#       "locks":["R4"],"lock_problems":[{"reviewer_id":"R4","reason":"lock_reviewer_disabled"}],
#       "reasons":["lock_reviewer_disabled"]}}}

# 43. 补位预演/发布不读取锁定表 (响应无 locks 字段), 仍只固定已确认且未回避的槽位:
curl -X POST $BASE/assignment/backfill/dry-run -H "$ORG"

# 44. 会务方调整或清除锁定后普通分配恢复:
curl -X POST $BASE/assignment/locks -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV2, \"locks\": {}}"   # 空表清除全部锁定
```

### 本轮用法：作者反馈异议（提交 → 查询凭据 → 会务方驳回/受理）

```bash
# 45. 会务方已发布快照后, 作者凭当前有效访问码对标号 1 的评语提交非空异议 (无需任何请求头)
curl -X POST $BASE/feedback-objections -H 'Content-Type: application/json' -d '{
  "access_code": "fbk-....", "label": 1,
  "reason": "评语一引用的对比实验并非本文方法, 存在事实错误, 请核查"}'
# -> {"ok":true,"changed":true,"query_token":"obj-....",
#     "objection":{"paper_id":"P1","label":1,"state":"pending",
#                  "reason":"...","submitted_at":"...","resolved_at":null,"resolution":null}}
#    响应不含评审人编号、机构、评语收据、快照码/序号/版本

# 46. 同快照同标号: 相同理由重试 -> 原记录原凭据 changed=false (首尾空白归一化);
#     理由不同 -> 409 冲突; 空白理由/标号非 1|2 -> 422;
#     无效或已失效访问码 -> 统一 404 (不透露论文是否存在); 并发提交也只有一条

# 47. 持随机查询凭据查看处理状态 (无需凭据; 快照码后来失效仍可查询)
curl $BASE/feedback-objections/obj-....
# -> {"paper_id":"P1","label":1,"state":"pending",...}
#    驳回后: state="rejected", resolution={"decided_at":"...","note":"驳回说明"}
#    受理后: state="accepted", resolution={"decided_at":"...","reason":"更正原因"}
#    序号变化: state="expired", resolved_at=过期时间

# 48. 会务方查看某论文异议与冻结的原反馈 (按匿名标号定位评审人, 含目标收据)
curl "$BASE/papers/P1/objections" -H "$ORG"
# -> {"paper_id":"P1","current_serial":S,"objections":[{"objection_id":1,"label":1,
#     "reviewer_id":"R1","state":"pending","reason":"...",
#     "frozen_feedback":{"label":1,"score":4,"comment":"..."},
#     "target_receipt":"rvw-....","snapshot_version":1,"snapshot_access_code":"fbk-....",...}]}
curl "$BASE/objections?state=pending" -H "$ORG"          # 全部论文, 可按状态过滤

# 49. 驳回: 不影响快照, 访问码仍可读 (可附驳回说明)
curl -X POST $BASE/objections/1/decision -H "$ORG" -H 'Content-Type: application/json' \
  -d '{"decision":"reject","note":"经复核原评语引用准确, 异议不成立"}'

# 50. 受理: 非空更正原因; 同一写事务原子核对"快照序号仍为当前序号 + 目标评语收据未变",
#     按匿名标号定位评审人并走既有更正请求流程, 原访问码立即失效
curl -X POST $BASE/objections/2/decision -H "$ORG" -H 'Content-Type: application/json' \
  -d '{"decision":"accept","reason":"作者异议成立: 请核对修订稿第 4 节并更正评分与评语"}'
# -> {"ok":true,"changed":true,"invalidated_snapshots":1,
#     "objection":{"state":"accepted","reviewer_id":"R4","target_receipt":"rvw-....",
#                  "correction":{"state":"pending",...},...}}
#    发布版/目标评语已变化或该槽位已有待更正请求 -> 409 且异议状态不变;
#    受理空更正原因 -> 422; 非 pending 异议重复处理 -> 409

# 51. 受理后沿用既有更正流程: 评审人查看待更正任务并提交更正, 会务方再重新发布快照
curl $BASE/reviewer/review-corrections -H 'X-Reviewer-Id: R4' -H 'X-Reviewer-Credential: r4-secret'
curl -X POST $BASE/reviewer/review-corrections/P2 \
  -H 'X-Reviewer-Id: R4' -H 'X-Reviewer-Credential: r4-secret' \
  -H 'Content-Type: application/json' \
  -d '{"paper_id":"P2","serial":S,"original_receipt":"rvw-....",
       "score":4,"comment":"复核修订稿第 4 节: 缺陷已有回应, 调整评价。"}'
curl -X POST $BASE/papers/P2/feedback-snapshot -H "$ORG" \
  -H 'Content-Type: application/json' -d '{"serial": S}'   # 新版本 + 新访问码

# 52. 普通/补位发布推进发布序号时, 旧序号下仍 pending 的异议在同一事务内标记 expired,
#     驳回/受理终态不变; 全部历史 (含过期) 供会务方追溯, 作者凭据仍可查为 expired
```

### 本轮用法：会务方撤回现存论文

```bash
# 53. 会务方先取当前资料修订号, 再提交 论文编号 + 修订号 + 非空撤回原因
REV=$(curl -s $BASE/meta -H "$ORG" | python3 -c 'import sys,json;print(json.load(sys.stdin)["revision"])')
curl -X POST $BASE/papers/P1/withdrawal -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"base_revision\": $REV, \"reason\": \"作者声明一稿多投, 经编委会确认撤稿\"}"
# -> {"ok":true,"state":"withdrawn","changed":true,"revision":N+1,
#     "invalidated_snapshots":1,"expired_objections":1,
#     "withdrawal":{"paper_id":"P1","state":"withdrawn","reason":"...","revision":N+1,
#                   "withdrawn_at":"...","published_serial":S,
#                   "published_plan":["R1","R2"]}}   # 冻结撤回瞬间的发布序号与槽位

# 54. 匹配当前修订号的同原因重试 -> 原记录 changed=false (不推进修订号、不重复失效/过期)
#     异原因 -> 409; 过期/超前修订号 -> 409; 未知论文 -> 404; 空原因/编号不一致 -> 422
REV=$(curl -s $BASE/meta -H "$ORG" | python3 -c 'import sys,json;print(json.load(sys.stdin)["revision"])')
curl -X POST $BASE/papers/P1/withdrawal -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"base_revision\": $REV, \"reason\": \"作者声明一稿多投, 经编委会确认撤稿\"}"
# -> {"changed":false,"revision":N+1,"invalidated_snapshots":0,"expired_objections":0,...}

# 55. 撤回在同一事务内立即生效: 评审人取稿不再包含该论文; 确认/回避/评语/更正提交统一 404
curl -s -o /dev/null -w '%{http_code}\n' $BASE/reviewer/assignments \
  -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret'   # 200, assignments 不含 P1
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  $BASE/reviewer/assignments/P1/decision \
  -H 'X-Reviewer-Id: R2' -H 'X-Reviewer-Credential: r2-secret' \
  -H 'Content-Type: application/json' \
  -d '{"paper_id":"P1","decision":"confirm"}'                   # -> 404

# 56. 该稿反馈访问码立即失效 (持码者统一 404, 不透露论文是否存在),
#     待处理异议变为 expired; 作者原查询凭据仍可查状态
curl -s -o /dev/null -w '%{http_code}\n' $BASE/feedback-snapshots/fbk-xxxx   # -> 404
curl $BASE/feedback-objections/obj-xxxx
# -> {"paper_id":"P1","label":1,"state":"expired","resolved_at":"...",...}

# 57. 当前发布版与发布序号不变, 仍可供会务方逐槽位核对 (P1 带 withdrawn 标记与原因)
curl $BASE/assignment -H "$ORG"
# -> {"serial":S,...,"withdrawn_papers":["P1"],
#     "papers":{"P1":{"withdrawn":true,"withdrawal_reason":"...","slots":[...]},...}}

# 58. 全部评审痕迹保留供会务方追溯: 撤回记录 / 评语进度与归档 / 快照版本 / 异议 / 决定
curl $BASE/papers/P1/withdrawal -H "$ORG"
curl $BASE/papers/P1/reviews -H "$ORG"          # 含 withdrawn 标记、slots、archived_reviews、corrections
curl $BASE/papers/P1/feedback-snapshots -H "$ORG"   # 旧版保留, active=false
curl "$BASE/papers/P1/objections" -H "$ORG"

# 59. 同编号不能重新录入 (409); 撤回稿不可更新/删除 (409)、不可发起更正/发布快照 (409);
#     对其提交锁定表整表 404 (撤回不阻塞其他论文)
curl -s -o /dev/null -w '%{http_code}\n' -X POST $BASE/papers -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d '{"paper_id":"P1","manuscript":"重投","topics":["AI"],"institutions":["Univ-Z"]}'  # -> 409

# 60. 后续普通分配与补位均跳过撤回稿 (不进 plan/unassigned/diagnostics, 补位不补人);
#     基于旧修订号的普通发布/补位发布沿用既有规则 409, 需按新修订号重新预演;
#     其他论文的确认与评语规则完全不变
curl -X POST $BASE/assignment/dry-run -H "$ORG"   # plan 中不再有 P1
```

### 本轮用法：论文评审保障等级（高/中/普通）

```bash
# 61. 会务方先取当前资料修订号, 再按论文设置等级 (high/medium/normal; 未设置即普通)
REV=$(curl -s $BASE/meta -H "$ORG" | python3 -c 'import sys,json;print(json.load(sys.stdin)["revision"])')
curl -X POST $BASE/papers/P2/guarantee-level -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P2\", \"level\": \"high\", \"base_revision\": $REV}"
# -> {"ok":true,"paper_id":"P2","level":"high","changed":true,"revision":N+1}

# 62. 相同等级重试 -> changed=false, 不推进修订号; 有效变更推进修订号
#     (设为 normal 即恢复默认); 过期/超前修订号 -> 409; 未知或已撤回论文 -> 404;
#     非法等级 (非 high/medium/normal) 或路径与请求体编号不一致 -> 422;
#     被拒请求不留任何部分变更
curl -X POST $BASE/papers/P2/guarantee-level -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P2\", \"level\": \"high\", \"base_revision\": $REV2}"
# -> {"ok":true,"changed":false,"revision":N+1,...}

# 63. 会务方查询各论文有效等级与当前修订号 (未设置为 normal; 已删除/撤回论文不列出)
curl $BASE/papers/guarantee-levels -H "$ORG"
# -> {"revision":N+1,"levels":{"P1":"normal","P2":"high"},
#     "summary":{"high":1,"medium":0,"normal":1}}

# 64. 容量不足时的普通预演: 先使完整分配总数最大, 再依次使高、中等级完整分配数
#     最大, 最后沿用既有字典序; level_summary 给出各等级已分配/未分配数量
curl -X POST $BASE/assignment/dry-run -H "$ORG"
# -> {"feasible":false,"plan":{"P2":["R1","R2"]},"unassigned":["P1"],
#     "level_summary":{"high":{"assigned":1,"unassigned":0},
#                      "medium":{"assigned":0,"unassigned":0},
#                      "normal":{"assigned":0,"unassigned":1}},
#     "diagnostics":{"P1":{...}}}        # 原有诊断不变; 发布仍 422, 当前发布版不变

# 65. 完整可行时等级不影响既有容量比例与字典序优化; 补位预演同样返回 level_summary;
#     失效锁定不被等级优先级释放 (高等级论文锁定失效仍不可完整分配)
curl -X POST $BASE/assignment/backfill/dry-run -H "$ORG"
# -> {"feasible":true,...,"level_summary":{"high":{"assigned":1,"unassigned":0},...}}

# 66. 删除或撤回论文后, 其等级行随论文清除, 不再参与求解也不再出现在等级查询中
curl -X DELETE $BASE/papers/P2 -H "$ORG"
curl $BASE/papers/guarantee-levels -H "$ORG"   # levels 中不再有 P2
```

### 本轮用法：当前发布版的统一 UTC 评审截止时刻

```bash
# 67. 先发布分配, 让 R1 截止前确认并交评语、R2 仅确认未交; 确认会推进资料修订号
curl -X POST $BASE/reviewer/assignments/P1/decision \
  -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret' \
  -H 'Content-Type: application/json' -d '{"paper_id":"P1","decision":"confirm"}'
curl -X POST $BASE/reviewer/assignments/P1/review \
  -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret' \
  -H 'Content-Type: application/json' \
  -d '{"paper_id":"P1","serial":S,"score":4,"comment":"R1 的正式评语"}'
curl -X POST $BASE/reviewer/assignments/P1/decision \
  -H 'X-Reviewer-Id: R2' -H 'X-Reviewer-Credential: r2-secret' \
  -H 'Content-Type: application/json' -d '{"paper_id":"P1","decision":"confirm"}'

# 68. 会务方取最新 revision + 当前 serial, 为当前发布版设置一次统一 UTC 截止
#     时刻须显式携带时区 (Z / +00:00 / +08:00), 归一化为 UTC, 且晚于设置时刻
REV=$(curl -s $BASE/meta -H "$ORG" | python3 -c 'import sys,json;print(json.load(sys.stdin)["revision"])')
SER=$(curl -s $BASE/assignment -H "$ORG" | python3 -c 'import sys,json;print(json.load(sys.stdin)["serial"])')
curl -X POST $BASE/review-deadline -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"base_serial\": $SER, \"deadline_at\": \"2026-11-01T12:00:00Z\"}"
# -> {"ok":true,"changed":true,"revision":N,"serial":S,
#     "review_deadline":{"serial":S,"revision":N,
#                        "deadline_at":"2026-11-01T12:00:00+00:00","set_at":"..."}}

# 69. 同版同时刻重试 -> changed=false (不产生新变更, 不推进修订号/序号);
#     同版异时刻 -> 409; 修订号/序号不符 -> 409; 无时区/过去时刻 -> 422; 尚未发布 -> 404
#     (被拒请求不改变当前方案、截止设置及历史记录)
curl -X POST $BASE/review-deadline -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"base_serial\": $SER, \"deadline_at\": \"2026-11-01T20:00:00+08:00\"}"
# -> {"changed":false,...}   (+08:00 归一化后等于 12:00:00Z, 同值幂等)

# 70. 会务方查看各槽位状态: 未确认/已确认未交/已交/已回避/逾期 + 汇总
curl $BASE/review-deadline -H "$ORG"
# -> {"revision":N,"serial":S,
#     "review_deadline":{"deadline_at":"2026-11-01T12:00:00+00:00",...},
#     "deadline_expired":false,"server_time":"...","summary":{
#       "unconfirmed":0,"confirmed_unsubmitted":1,"submitted":1,"recused":0,"overdue":0},
#     "slots":[{"paper_id":"P1","reviewer_id":"R1","state":"submitted","submitted":true,...},
#              {"paper_id":"P1","reviewer_id":"R2","state":"confirmed_unsubmitted",...}]}

# 71. 服务端到达截止时刻后: 仍未交评语且未回避的槽位即逾期
#     R2 (已确认未交) 再交评语 -> 409; 未确认者再确认 -> 409;
#     R1 已交评语及收据保持有效 (相同内容重试仍 200 changed=false 返原收据)
curl -X POST $BASE/reviewer/assignments/P1/review \
  -H 'X-Reviewer-Id: R2' -H 'X-Reviewer-Credential: r2-secret' \
  -H 'Content-Type: application/json' \
  -d '{"paper_id":"P1","serial":S,"score":5,"comment":"截止后补交"}'   # -> 409
curl $BASE/review-deadline -H "$ORG" | python3 -m json.tool
#     summary: {"submitted":1,"overdue":1,...}; 逾期槽位 state="overdue"

# 72. 补位预演: R1 (已交) 槽位固定; R2 逾期槽位即使已确认也释放
#     (fixed_review_overdue), 且本次补位不得再把 P1 分给 R2 (review_overdue)
curl -X POST $BASE/assignment/backfill/dry-run -H "$ORG"
# -> {"feasible":true,"deadline_expired":true,
#     "fixed":{"P1":["R1"]},
#     "fixed_problems":{"P1":[{"reviewer_id":"R2","reason":"fixed_review_overdue"}]},
#     "plan":{"P1":["R1","R3"]},
#     "papers":{"P1":{"excluded":[{"reviewer_id":"R2","reason":"review_overdue"},...]}},
#     "overdue_slots":[{"paper_id":"P1","reviewer_id":"R2"}],
#     "review_deadline":{...}}

# 73. 补位发布仍走双版本复核: 沿用 R1 确认与评语 (carried_reviews),
#     R2 逾期确认释放 (released_overdue_confirmations), 新方案不含 R2
curl -X POST $BASE/assignment/backfill/publish -H "$ORG" -H 'Content-Type: application/json' \
  -d '{"base_revision": N, "base_serial": S}'
# -> {"ok":true,"serial":S+1,"carried_confirmations":1,"carried_reviews":1,
#     "released_overdue_confirmations":1,
#     "overdue_slots":[{"paper_id":"P1","reviewer_id":"R2"}],
#     "plan":{"P1":["R1","R3"]}}

# 74. 旧版截止不约束新发布版: 新版本初始无截止, 评审/确认恢复既有规则,
#     会务方可为新版本独立设置自己的截止
curl $BASE/review-deadline -H "$ORG"
# -> {"review_deadline":null,"deadline_expired":false,...}
```

可运行脚本见 [`examples/deadlines.sh`](examples/deadlines.sh)
（`./examples/deadlines.sh`，建议对新库运行；脚本会把截止设为约 2 分钟后并等待到点演示）。

### 本轮用法：删除已发布反馈的论文（反馈生命周期 + 同编号重录 + 迁移）

```bash
# 81. 前置: 发布分配 -> 两名评审人确认并交评语 -> 会务方发布快照, 作者持码提交异议
#     (沿用既有步骤 15、45; 此处得到访问码 FBK 与查询凭据 OBJ)
FBK=$(curl -s -X POST $BASE/papers/P1/feedback-snapshot -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SER}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_code"])')
OBJ=$(curl -s -X POST $BASE/feedback-objections -H 'Content-Type: application/json' \
  -d "{\"access_code\":\"$FBK\",\"label\":1,\"reason\":\"评语一存在事实错误\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["query_token"])')
curl -s -o /dev/null -w '%{http_code}\n' $BASE/feedback-snapshots/$FBK        # -> 200

# 82. 会务方删除论文: 同一写事务原子失效该稿全部快照访问码、过期待处理异议,
#     推进修订号 (当前发布版/发布序号不变); 响应含失效快照数/过期异议数/删除凭据
curl -s -X DELETE $BASE/papers/P1 -H "$ORG"
# -> {"ok":true,"paper_id":"P1","revision":N,"invalidated_snapshots":1,
#     "expired_objections":1,"deletion":{"id":..,"state":"deleted",
#       "revision":N,"published_serial":S,"published_plan":["R1","R2"],
#       "invalidated_snapshots":1,"expired_objections":1,"deleted_at":"..."}}

# 83. 旧码持码读取、提交新异议: 与无效码同形 404 (响应体相同, 不透露论文是否存在)
curl -s -w ' [%{http_code}]\n' $BASE/feedback-snapshots/$FBK
# -> {"detail":"无效或已失效的访问码 (invalid or expired access code)"} [404]
curl -s -w ' [%{http_code}]\n' -X POST $BASE/feedback-objections \
  -H 'Content-Type: application/json' \
  -d "{\"access_code\":\"$FBK\",\"label\":2,\"reason\":\"删除后提交\"}"   # -> [404]

# 83b. 删除后历史槽位仍冻结在当前发布方案 (serial 不变, 会务方可核对),
#      但评审人的确认/回避/正式评语一律 404: 不写决定/硬回避/评语、不推进修订号;
#      既有决定/评语的幂等重试与"确认后改回避"同样拒绝。
curl -s "$BASE/assignment" -H "$ORG" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["serial"], d["plan"]["P1"], d["deleted_papers"])'
# -> S ["R1","R2"] ["P1"]
RV1=(-H "X-Reviewer-Id: R1" -H "X-Reviewer-Credential: r1-secret" -H 'Content-Type: application/json')
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE/reviewer/assignments/P1/decision" \
  "${RV1[@]}" -d '{"paper_id":"P1","decision":"confirm"}'                # -> 404 (删除前已确认, 幂等重试也拒绝)
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE/reviewer/assignments/P1/decision" \
  "${RV1[@]}" -d '{"paper_id":"P1","decision":"recuse","reason":"删除后回避"}'  # -> 404
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE/reviewer/assignments/P1/review" \
  "${RV1[@]}" -d "{\"paper_id\":\"P1\",\"serial\":$SER,\"score\":1,\"comment\":\"删除后评语\"}"  # -> 404
curl -s "$BASE/reviewer/assignments" "${RV1[@]}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["assignments"])'  # -> []

# 84. 作者的异议查询凭据继续可查: state=expired
curl $BASE/feedback-objections/$OBJ
# -> {"paper_id":"P1","label":1,"state":"expired","resolved_at":"...",...}

# 85. 历史快照/异议与删除凭据仍由会务方追溯
curl $BASE/papers/P1/feedback-snapshots -H "$ORG"    # 旧版保留, active=false
curl "$BASE/papers/P1/objections" -H "$ORG"          # 含 expired 历史异议
curl $BASE/papers/P1/deletions -H "$ORG"             # 该编号的全部删除事件
curl "$BASE/papers/deletions?paper_id=P1" -H "$ORG"  # 或全量列表按编号过滤

# 86. 同编号重新录入: 未重新发布前 (序号仍为冻结旧序号 S) 评审操作仍 404
curl -s -o /dev/null -X POST $BASE/papers -H "$ORG" -H 'Content-Type: application/json' \
  -d '{"paper_id":"P1","manuscript":"同编号重录稿","topics":["AI"],"institutions":["Univ-Y"]}'
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE/reviewer/assignments/P1/decision" \
  "${RV1[@]}" -d '{"paper_id":"P1","decision":"confirm"}'                # -> 404
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE/reviewer/assignments/P1/review" \
  "${RV1[@]}" -d "{\"paper_id\":\"P1\",\"serial\":$SER,\"score\":2,\"comment\":\"重录未重发\"}"  # -> 404
#     重新发布分配 (普通或补位, 发布序号 +1) -> 按新序号新槽位重新确认/重新交评语,
#     旧码仍 404; 补位发布响应 dropped_deleted_confirmations 计数不沿用的旧确认:
REV=$(curl -s -X POST $BASE/assignment/dry-run -H "$ORG" | j 'd["revision"]')
SER2=$(curl -s -X POST $BASE/assignment/publish -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"base_revision\":$REV}" | j 'd["serial"]')
for rv in R1 R2; do
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
    -H "X-Reviewer-Id: $rv" -H "X-Reviewer-Credential: ${rv,,}-secret" \
    -H 'Content-Type: application/json' -d '{"paper_id":"P1","decision":"confirm"}'
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/review" \
    -H "X-Reviewer-Id: $rv" -H "X-Reviewer-Credential: ${rv,,}-secret" \
    -H 'Content-Type: application/json' \
    -d "{\"paper_id\":\"P1\",\"serial\":$SER2,\"score\":4,\"comment\":\"新序号新评语\"}"
done
curl -s -o /dev/null -w '%{http_code}\n' $BASE/feedback-snapshots/$FBK        # -> 404
#     重新发布快照: version+1 与新访问码 (旧码继续 404)
#     若未重新发布分配就按旧序号重发相同收据快照 -> 409 (不返回旧码、不覆盖历史版本)

# 87. 迁移导出 (v2) 携带 paper_deletions 删除凭据; 恢复后旧码仍 404、新码可读
curl -s $BASE/migration/export -H "$ORG" > snapshot.json
python3 - <<'PY'
import json
s = json.load(open("snapshot.json"))
assert s["format_version"] == 2
assert s["data"]["paper_deletions"][0]["invalidated_snapshots"] >= 1
print("export ok:", len(s["data"]["paper_deletions"]), "deletion record(s)")
PY
#     在空目标实例恢复后: 仅把失效标记改回 0 的伪造快照在恢复时 422 (矛盾快照、
#     不留部分数据); v1 旧格式快照未记载删除时沿用原有恢复判断, 不推断删除历史
```

可运行脚本见 [`examples/paper_deletions.sh`](examples/paper_deletions.sh)
（`./examples/paper_deletions.sh [BASE_URL] [BASE2_URL]`，目标实例可用空库启动）。

### 本轮用法：会务方实例迁移（导出一致快照 → 空实例恢复）

```bash
# 75. 会务方在源实例导出一致业务数据快照 (BEGIN IMMEDIATE 事务内读取, 与并发写入隔离);
#     快照覆盖全部现存业务记录 (含评审人凭据/反馈访问码/异议查询凭据), 不含会务方密钥
curl -s $BASE/migration/export -H "$ORG" > snapshot.json
# -> {"format":"review-assignment-migration","format_version":1,
#     "exported_at":"...","checksum":"sha256:<hex>",
#     "data":{"revision":N,"published":{"serial":S,...},
#             "papers":[...],"reviewers":[...],"assignment_decisions":[...],
#             "submitted_reviews":[...],"reviewer_recusals":[...],
#             "reviewer_status_changes":[...],"feedback_snapshots":[...],
#             "institution_merges":[...],"assignment_locks":[...],
#             "review_corrections":[...],"feedback_objections":[...],
#             "paper_withdrawals":[...],"paper_deletions":[...],
#             "paper_guarantee_levels":[...],"review_deadlines":[...]}}

# 76. (可选) 本地复算内容校验和, 核对快照完整后再迁移
python3 - <<'PY'
import hashlib, json
snap = json.load(open("snapshot.json"))
canon = json.dumps(snap["data"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
assert "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest() == snap["checksum"]
print("checksum ok")
PY

# 77. 在空目标实例恢复 (目标实例须为尚无业务记录的空库, 例如以新 DB_PATH 启动):
#     恢复前核对格式版本/内容校验和/记录引用 (符合历史保留规则);
#     校验失败 422、目标非空或重复恢复 409, 均不留下部分数据
curl -X POST $BASE2/migration/restore -H "$ORG" -H 'Content-Type: application/json' \
  --data-binary @snapshot.json
# -> {"ok":true,"restored":true,"format_version":1,"checksum":"sha256:<hex>",
#     "revision":N,"serial":S,"restored_at":"...",
#     "counts":{"papers":2,"reviewers":4,...}}

# 78. 恢复成功后一切按原规则工作: 修订号/发布序号续用, 历史记录可追溯,
#     评审人凭据可取稿/提交, 反馈访问码与异议查询凭据保持有效
curl $BASE2/meta -H "$ORG"                       # revision=N, published.serial=S
curl $BASE2/assignment -H "$ORG"                 # 当前发布版与槽位状态一致
curl $BASE2/reviewer/assignments -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret'
curl $BASE2/feedback-snapshots/fbk-xxxx          # 当前有效快照仍可读
curl $BASE2/feedback-objections/obj-xxxx         # 异议处理状态仍可查

# 79. 拒绝路径: 重复恢复 -> 409; 目标已有业务记录 -> 409;
#     篡改内容或校验和不符 -> 422; 无/错会务方密钥 -> 401
curl -s -o /dev/null -w '%{http_code}\n' -X POST $BASE2/migration/restore \
  -H "$ORG" -H 'Content-Type: application/json' --data-binary @snapshot.json   # -> 409

# 80. 复活攻击防护: 旧快照已因评语更正/撤回失效, 仅把 invalidated 改回 true 之外的
#     "有效" (false) 并重算校验和 -> 恢复前跨记录核验仍判定矛盾快照, 422 拒绝,
#     目标实例不留部分数据 (旧访问码在目标实例仍 404, 无法读取已更正反馈或提异议)
python3 - <<'PY'
import hashlib, json
snap = json.load(open("snapshot.json"))
old = next(s for s in snap["data"]["feedback_snapshots"] if s["invalidated"])
old["invalidated"] = False                       # 攻击者尝试复活旧访问码
snap["checksum"] = "sha256:" + hashlib.sha256(
    json.dumps(snap["data"], ensure_ascii=False, sort_keys=True,
               separators=(",", ":")).encode("utf-8")).hexdigest()
json.dump(snap, open("snapshot-forged.json", "w"), ensure_ascii=False)
PY
curl -s -X POST $BASE2/migration/restore -H "$ORG" -H 'Content-Type: application/json' \
  --data-binary @snapshot-forged.json
# -> 422 {"detail":"快照内容非法 (...): 快照版本 1 (访问码 'fbk-...') 的失效标记
#    被置为有效, 但依据业务记录它应当已失效 (同发布序号内已有更新版本;
#    快照创建不晚于评语更正请求发起时刻 ...)——已因更正或撤回失效的快照不能因
#    仍属当前发布序号而复活"}
#    合法的连续更正、同序号再次发布与跨发布序号历史快照则正常迁移 (见步骤 77-78):
#    恢复后仅当前有效码可读快照/提异议, 全部历史版本仍供会务方追溯
#    (论文删除后同编号重录的旧码同样不得复活: v2 快照按 paper_deletions 的删除时刻
#    重算效力, 仅改标记 -> 422; v1 旧格式未记载删除时沿用原有恢复判断, 不推断删除历史)
```

可运行脚本见 [`examples/migration.sh`](examples/migration.sh)
（`./examples/migration.sh [BASE_URL] [BASE2_URL]`；目标实例可用空库启动，例如
`DB_PATH=./data/app-target.db PORT=8001 python -m app.main`）。

预演响应示例（可行）：

```json
{
  "feasible": true,
  "revision": 7,
  "plan": {"P1": ["R1", "R2"]},
  "unassigned": [],
  "max_used_capacity_ratio": "3/4",
  "papers": {"P1": {"assigned": ["R1", "R2"],
    "eligible_reviewers": ["R1", "R2", "R3"],
    "excluded": [{"reviewer_id": "R4", "reason": "same_institution_as_author"}]}},
  "diagnostics": {},
  "level_summary": {"high": {"assigned": 0, "unassigned": 0},
    "medium": {"assigned": 0, "unassigned": 0},
    "normal": {"assigned": 1, "unassigned": 0}}
}
```

预演响应示例（无完整方案，`papers` 为最大部分分配，`diagnostics` 给出每篇未分配论文的限制原因；
部分方案按 完整分配总数 → 高等级数 → 中等级数 → 全局字典序 选取）：

```json
{
  "feasible": false,
  "unassigned": ["P2"],
  "diagnostics": {"P2": {
    "eligible_reviewers": ["R3"],
    "eligible_at_capacity": ["R3"],
    "excluded": [{"reviewer_id": "R1", "reason": "same_institution_as_author"}],
    "reasons": ["fewer_than_two_eligible_reviewers", "eligible_reviewers_at_capacity"]
  }},
  "level_summary": {"high": {"assigned": 0, "unassigned": 1},
    "medium": {"assigned": 0, "unassigned": 0},
    "normal": {"assigned": 1, "unassigned": 0}}
}
```

## 错误码

| 状态码 | 场景 |
|---|---|
| 401 | 会务方密钥缺失/错误；评审人凭据缺失/失效 |
| 403 | 评审资格已停用：凭据本身仍有效，但取稿、提交决定、提交评语、查看本人评语一律立即拒绝 |
| 404 | 更新/删除不存在的论文或评审人；调整资格或查看变更记录时评审人未知；尚未发布方案；评审人对**未分配给自己/已被移出当前方案/已撤回**的论文提交决定或评语；**论文已被会务方删除后，即使当前发布方案仍保留其历史槽位，评审人的确认/回避/正式评语/更正提交也一律拒绝（含同编号重录但未重新发布、既有决定/评语的幂等重试与确认后改回避；不写决定/硬回避/评语/更正、不推进修订号；重新发布推进发布序号后按新序号新槽位恢复可操作）**；评审人对已撤回论文提交评语更正；会务方查询当前发布版中不存在的论文评语；尚无发布版时补位预演/发布；反馈快照的访问码无效或已失效（旧码或已被更正请求/异议受理/**论文撤回/论文删除**失效的码，**含同编号重录后仍持有的旧码**，不区分论文是否存在）；未发布或论文不在当前发布版时发布快照；机构归并时任一名称未出现在当前论文作者或评审人资料中；更正请求针对未分配给该评审人的槽位（含已移出方案的槽位）；评审人提交更正时收据无对应（待）更正任务；锁定表包含未知论文、**已撤回论文**或未知评审人（整表拒绝、不改动当前锁定）；作者提交异议的快照访问码无效或已失效（旧码、被更正请求或异议受理或论文撤回/删除失效，不区分论文是否存在）；异议查询凭据无效（不区分论文是否存在）；会务方处理的异议编号不存在；撤回未知论文；查询未知或尚未撤回论文的撤回记录；**查询从未删除过的编号的删除凭据；对未知或已撤回论文设置评审保障等级；尚未发布任何方案时设置或查看统一评审截止** |
| 409 | 论文/评审人编号重复（**含以已撤回的论文编号重新录入；删除后的同编号可重新录入**）；发布携带的修订号已过期（资料已变更，**含撤回/删除推进修订号后的旧号发布/补位发布**）；补位发布携带的资料修订号或发布序号过期；已回避任务再次确认；评语提交携带的发布序号过期/超前；对 `pending`/`recused` 槽位提交评语；同一有效任务已有评语而提交内容不同（冲突）；**该发布版统一评审截止到达后，仍未交评语的槽位再确认或交评语（已逾期；已交评语相同内容重试仍幂等返原收据）**；**评审截止设置携带的资料修订号或发布序号不符（过期/超前）；同一发布版已设置过截止而提交不同时刻（同版异时刻，不改原截止）**；反馈快照请求携带的发布序号过期/超前；资格调整携带的修订号过期/超前；同状态异原因的资格调整（含对初始启用者再次启用）；机构归并携带的修订号过期/超前；更正请求携带的发布序号过期/超前；对未确认/未交评语的槽位发起更正；同槽位异原因重复发起更正；论文存在待完成更正请求时发布快照；更正提交携带的发布序号过期/超前；同一更正任务已有更正评语而提交内容不同；锁定表携带的资料修订号过期/超前；同一快照同一标号已有异议而理由不同；对非 `pending`（已驳回/已受理/已过期）异议重复处理；受理异议时快照发布序号已不是当前序号或目标评语收据已变化；受理异议时该槽位已有待完成更正请求（均不改变异议状态）；**论文撤回携带的修订号过期/超前；已撤回论文以不同原因重复撤回；更新/删除已撤回论文；对已撤回论文发起评语更正或发布反馈快照；同编号重录后未重新发布分配（发布序号未推进）即按旧序号重发相同评语收据的快照（旧版已随删除永久失效，不返回旧码）；评审保障等级设置携带的修订号过期/超前**；**迁移恢复的目标实例已存在业务记录（非空实例），或该实例此前已执行过恢复（重复恢复），均拒绝且不留下部分数据** |
| 422 | 非法容量（非整数、小于 1）、缺字段等参数校验失败；无可行方案时强行发布；回避原因为空；补位预演无法补齐为完整方案时强行补位发布；评分不是 1～5 的整数或评语为空白；路径论文编号与请求体不一致；路径评审人编号与请求体不一致；资格调整原因为空；发布反馈快照时两名评审人未全部确认或两份正式评语未交齐（不留下新版本）；机构归并的名称去首尾空白后为空白；更正请求原因为空；更正评分不是 1～5 的整数或更正评语为空白；锁定表中同一篇论文重复锁定同一评审人或每篇锁定超过两名；锁定表中的论文/评审人编号去首尾空白后为空白；存在锁定槽位失效（删除/停用/回避、资料或机构归并失格、锁定超容量）导致不可完整分配时强行普通发布（不改变当前发布版）；异议理由去首尾空白后为空白或标号不是 1/2；受理异议时更正原因为空白（不改异议状态）；**评审截止时刻无法解析、未显式携带时区或不晚于设置时服务端当前时刻**；**论文撤回原因去首尾空白后为空白或撤回路径论文编号与请求体不一致；评审保障等级非法（非 high/medium/normal）或等级设置路径与请求体论文编号不一致**；**迁移快照的格式标识/格式版本不符（接受 v1/v2）、内容校验和不符（已损坏或被篡改）、记录结构或引用不符合历史保留规则（如锁定/保障等级指向未知或已撤回论文、撤回记录与 withdrawn 标记不一一对应、异议指向未知快照版本或已撤回/已删除失效快照上仍有待处理异议、记录引用越过当前发布序号、当前序号决定/评语不在当前方案槽位上、唯一约束冲突），或访问码效力跨记录核验发现矛盾快照（v2：失效标记被改回有效/标记失效却无撤回/删除/更正/更高版本依据、删除时刻前创建的快照未标记失效、当前序号有效快照的评语收据与现行槽位评语不一致；已因更正/撤回/删除失效的快照不得复活；v1 旧格式未记载删除时沿用原有判断不推断删除历史），恢复拒绝且不写入任何数据** |

排除/限制原因代码：`same_institution_as_author`（与作者同机构，原名逐字相同）、
`same_institution_as_author_via_merge`（评审人与作者机构原名不同但已归并为同一冲突判定组）、
`on_reviewer_avoid_list`
（在回避名单）、`hard_recusal_after_decline`（评审人发布后声明回避形成的硬回避）、
`reviewer_disabled`（评审资格已停用，不进入普通分配）、
`fewer_than_two_eligible_reviewers`（合格者不足两人）、
`eligible_reviewers_at_capacity`（合格者容量占满）、`no_expert_among_eligible_reviewers`
（合格者无人擅长主题）、`eligible_reviewers_share_single_institution`（合格者同属一个机构）、
`eligible_reviewers_share_single_institution_via_merge`（合格者机构原名互异但同属一个归并组）、
`no_valid_reviewer_pair`（不存在合法评审人对）、`all_valid_pairs_blocked_by_capacity`
（所有合法评审人对均被容量阻塞）。补位专用：`fixed_reviewer_not_found`（固定关系中的评审人
已被删除）、`fixed_reviewer_disabled`（固定关系中的评审人资格已停用，槽位释放）、
`fixed_pair_violates_institution_rule` / `fixed_pair_violates_expert_rule`
（数据变更后固定两人不再满足不同机构/至少一名擅长）、
`fixed_pair_violates_institution_rule_via_merge`（固定两人原名不同，但机构归并后同组，
已确认槽位因此失效、不予固定）、`fixed_assignments_exceed_capacity`
（固定关系累计超过评审人容量）、`no_valid_pair_containing_fixed_reviewer`
（含固定评审人的合法对不存在）、`all_fixed_compatible_pairs_blocked_by_capacity`
（与固定评审人相容的评审人对均被容量阻塞）、`fixed_review_overdue`
（该槽位在统一评审截止后仍未交评语即逾期，即使此前已确认也从补位固定位置释放）；
补位排除原因另有 `review_overdue`（该（评审人, 论文）槽位已逾期，本次补位不得
把该稿重新分给该评审人；仅对该论文排除，不影响该评审人的其余论文）。普通分配锁定专用（预演 `lock_problems`/诊断中；
锁定槽位失效只报错不释放，普通发布据此 422 拒绝）：`lock_reviewer_not_found`
（被锁评审人已被删除）、`lock_reviewer_disabled`（被锁评审人资格已停用）、
`lock_same_institution_as_author` / `lock_same_institution_as_author_via_merge`
（被锁评审人与作者同机构，后者由机构归并造成）、`lock_on_reviewer_avoid_list`
（被锁评审人录入了对该论文的回避）、`lock_hard_recusal_after_decline`
（被锁评审人发布后声明过回避，形成硬回避）、
`lock_pair_violates_institution_rule` / `lock_pair_violates_institution_rule_via_merge`
（两名被锁评审人同机构，后者由机构归并造成）、`lock_pair_violates_expert_rule`
（两名被锁评审人均不擅长该论文主题）、`lock_assignments_exceed_capacity`
（该评审人被锁定的论文数累计超过其容量）、`no_valid_pair_containing_lock_reviewer`
（单人锁定时含锁定者的合法对不存在）、`all_lock_compatible_pairs_blocked_by_capacity`
（与锁定者相容的评审人对均被容量阻塞）。

## 项目结构

```
app/
  main.py     # FastAPI 路由、鉴权、发布/补位发布事务、评审人决定与正式评语、匿名反馈快照、评审资格停用/启用、机构归并组、已交评语更正、普通分配评审人锁定表、作者反馈异议与会务方驳回/受理、论文撤回（同事务停稿/失效快照码/过期异议/清锁定/推进修订号）、论文删除反馈生命周期（同一写事务失效该稿全部快照访问码/待处理异议过期、删除凭据 paper_deletions、历史与异议凭据可追溯、同编号重录旧码不复活）、论文评审保障等级设置与查询（同事务核对修订号/幂等/推进修订号）、当前发布版统一 UTC 评审截止（双版本核对/同值幂等/异值拒绝、事务内逾期判定、截止后禁止确认交评语、补位释放逾期槽位且不重分给逾期评审人、旧版截止不约束新版）、实例迁移快照导出/恢复端点（接受 v1/v2）
  migration.py # 会务方实例迁移：一致业务数据快照的导出（BEGIN IMMEDIATE 事务内读取，与并发写入隔离；当前格式 v2，含格式版本与 SHA-256 内容校验和；覆盖全部业务记录含评审人凭据/访问码/查询凭据/论文删除凭据 paper_deletions，不含会务方密钥）、恢复前校验（格式/校验和/记录引用符合历史保留规则；**访问码效力跨记录核验：按快照版本链、更正请求发起时刻/待完成状态、撤回记录与（v2）删除时刻重算每个快照的应失效状态并与 invalidated 标记逐行比对，当前序号有效快照的评语收据还须等于现行槽位评语收据，矛盾快照 422，已失效快照（含同编号重录旧码）不得复活；v1 旧格式无删除记载时沿用原有判断、不推断删除历史**）、空实例核对（业务表全空且修订号为 0、无恢复标记）与单事务全量写入（失败整体回滚，不留部分数据）
  solver.py   # 分配求解器 + 固定槽位求解引擎（补位=失效释放 / 普通锁定=失效只报错不释放；容量比例、字典序、部分方案与诊断；部分方案按 总数→高/中等级数→字典序 选取并返回 level_summary；停用者直接排除；撤回稿不进入求解器输入；机构冲突一律按归并组判定；barred_pairs 支持补位把逾期槽位评审人对该稿以 review_overdue 排除）
  db.py       # SQLite 连接、schema 初始化/迁移、修订号、发布序号、读写事务、评审资格状态与变更记录、机构归并边与并查集分组、评语更正记录、普通分配锁定表、作者异议表与发布序号变化/论文撤回/论文删除时的异议过期、papers.withdrawn 标记与 paper_withdrawals 撤回记录、paper_deletions 删除凭据（同事务失效快照码/过期异议，同编号多次删除重录逐行追加）、paper_guarantee_levels 评审保障等级（删除/撤回随论文级联清除）、review_deadlines 统一评审截止（按发布序号隔离，每版至多一行，旧版截止不约束新版）、migration_restores 迁移恢复标记（实例本地元数据，重复恢复拒绝）
  schemas.py  # 请求体模型与校验
tests/test_api.py                # 原有接口与求解器测试（含并发发布测试）
tests/test_backfill.py           # 确认/回避/硬回避/补位预演与发布测试
tests/test_reviews.py            # 正式评分/评语、收据幂等与冲突、进度与归档、补位沿用测试
tests/test_snapshots.py          # 匿名反馈快照：发布门槛、幂等原码、新版本旧码失效、统一 404、追溯
tests/test_eligibility.py        # 评审资格停用/启用：修订号核对、幂等/冲突、即时 403、槽位释放与恢复、变更记录
tests/test_institution_merges.py # 本轮机构归并：修订号核对、传递不可拆分、幂等、归并组冲突判定、预演归并标注、补位失效不固定、评语继承
tests/test_corrections.py        # 本轮评语更正：发起校验与幂等、访问码即时失效、待更正任务、更正提交幂等/冲突、快照重发、序号变化失效、补位沿用/归档
tests/test_locks.py              # 本轮普通分配锁定表：整表替换/修订号/幂等/404/422、槽位保留与专长由搭档满足、失格只报错不释放、归并/停用/删除/硬回避/容量诊断、发布拒绝不改当前版、补位不读锁定表、删论文级联
tests/test_objections.py         # 本轮作者反馈异议：有效码提交、同理由幂等/异理由冲突、并发仅一条、随机凭据在快照码失效后可查、已失效码 404、序号变化同事务过期、会务方视图、驳回不影响快照、受理原子核对序号/收据/待更正请求并走更正流程
tests/test_withdrawals.py        # 本轮论文撤回：修订号核对优先级/幂等原记录/异原因冲突/404/422、同编号不可重录、评审人侧即时停稿、快照码失效、异议同事务过期且凭据可查、锁定清除不阻塞他稿、普通分配/补位跳过、评语/快照/决定/更正追溯
tests/test_guarantee_levels.py   # 本轮评审保障等级：设置/查询/幂等/版本不符/未知或已撤回/非法等级、部分方案按 总数→高/中等级数→字典序 选取、level_summary、完整可行时等级不影响优化、失效锁定不被等级释放、补位交互、删除/撤回后等级清除
tests/test_deadlines.py          # 本轮统一 UTC 评审截止：设置双版本核对/时区归一化/未来时刻/幂等/异值拒绝、槽位五态汇总、截止后禁止确认交评语且已交评语与收据有效、补位释放逾期确认且不重分给逾期评审人、其余确认槽位固定、提交与补位发布并发无竞态、旧版截止不约束新版
tests/test_migration.py          # 本轮会务方实例迁移：导出覆盖全部业务记录且不含会务方密钥、格式版本与校验和、导出与并发写入隔离、空实例全量回环（修订号/发布序号/凭据/访问码/历史记录按原规则工作）、非空目标与重复恢复 409、格式/校验和/记录引用校验失败 422 且不留部分数据；**访问码效力跨记录核验：同序号连续更正/再次发布/跨序号历史快照/待更正/撤回等合法状态可迁移，改回失效标记（含伪造时间戳/旧收据冒充现行）等复活攻击一律 422，恢复后仅当前有效码可读可提异议**
tests/test_paper_deletions.py    # 本轮论文删除反馈生命周期：删除同事务失效旧快照访问码/过期待处理异议（持码读取与提交同形 404）、查询凭据继续可查、历史快照与异议/删除凭据可追溯、推进修订号但发布版/序号不变、同编号重录旧码不复活（重新发布生成新码）、多次删除重录追加凭据；迁移 v2 删除凭据回环、改回失效标记/pending 矛盾快照 422 且不留部分数据、v1 旧格式沿用原有恢复判断不推断删除历史
examples/demo.sh          # 端到端 curl 示例（录入 -> 预演 -> 发布 -> 取稿）
examples/backfill.sh      # 确认/回避 -> 补位预演 -> 双版本复核发布示例
examples/reviews.sh       # 正式评语收集端到端示例（确认 -> 提交评语 -> 进度查看 -> 补位沿用/归档）
examples/snapshots.sh     # 匿名反馈快照端到端示例（发布快照 -> 持码读取 -> 幂等 -> 序号变化旧码失效 -> 追溯）
examples/eligibility.sh   # 评审资格停用/启用端到端示例（停用 -> 即时断稿/排除 -> 补位释放 -> 重新启用 -> 变更记录）
examples/institutions.sh  # 机构归并端到端示例（机构归并 -> 普通分配按组冲突 -> 已确认槽位失效 -> 补位双版本发布 -> 归并组查看）
examples/corrections.sh   # 本轮端到端示例（发起更正 -> 访问码失效 -> 评审人更正 -> 快照重发 -> 补位沿用/待更正失效）
examples/locks.sh         # 本轮端到端示例（锁定整表提交/幂等 -> 普通预演保留槽位 -> 失格诊断与发布拒绝 -> 补位不读锁定 -> 清除锁定）
examples/objections.sh    # 本轮端到端示例（作者凭快照码提异议/凭据查状态/幂等冲突 -> 会务方驳回 -> 受理走更正流程并失效旧码 -> 序号变化异议过期）
examples/withdrawals.sh   # 本轮端到端示例（确认/评语/快照/异议 -> 会务方撤回同事务生效 -> 幂等/冲突/版本不符 -> 评审人停稿、快照码失效、异议过期 -> 追溯 -> 后续分配跳过、他稿规则不变）
examples/guarantee_levels.sh # 本轮端到端示例（设置/查询等级 -> 幂等/非法等级/版本不符 -> 容量不足时高等级优先与 level_summary -> 发布仍拒绝 -> 完整可行时等级不影响优化 -> 删除后等级清除）
examples/deadlines.sh     # 本轮端到端示例（双版本设置 UTC 截止 -> 同值幂等/异值拒绝 -> 槽位状态 -> 到点逾期禁止确认交评语且已交收据有效 -> 补位释放逾期确认且不重分 -> 旧版截止不约束新版）
examples/migration.sh     # 本轮端到端示例（源实例导出一致快照 -> 本地复算校验和 -> 空目标实例恢复 -> 修订号/发布序号/凭据/访问码按原规则工作；重复恢复 409；**复活攻击演示：把已失效快照标记改回有效并重算校验和 -> 422 矛盾快照、不留部分数据，合法快照恢复后仅当前有效码可读/可提异议**）
examples/paper_deletions.sh # 本轮端到端示例（快照/异议 -> 删除同事务失效旧码、过期异议、凭据可查、删除凭据追溯 -> 同编号重录重新发布新码旧码仍 404 -> v2 导出含 paper_deletions -> 空实例恢复旧码死/新码活；改回失效标记 422 不留部分数据；v1 旧格式沿用原有恢复判断）
Dockerfile / docker-compose.yml
```

## 测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```
