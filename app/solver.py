"""双盲评审分配求解器。

硬约束:
  1. 每篇论文恰好分配给 2 名评审人;
  2. 同一篇论文的两名评审人必须来自不同机构;
  3. 两名评审人中至少一人擅长该论文主题
     (评审人擅长主题与论文主题交集非空; 论文未标注主题时视为所有评审人均满足);
  4. 评审人不得与论文任一作者机构相同;
  5. 评审人不得评审自己声明回避的论文;
  6. 评审人已分配论文数不得超过其容量。

机构冲突一律按会务方维护的"归并组"判定 (inst_group: {机构原名 -> 组键}):
同一组键即视为同一机构, 即便原名不同 (同一机构的不同名称已归并)。
原名原本相同与由归并造成的同组在诊断原因中分别标注, 后者原因代码带
`_via_merge` 后缀, 以便预演解释标明冲突由归并造成。

优化目标 (按优先级):
  1. 最小化全体评审人中最高的"已用容量比例"(已分配数 / 容量);
  2. 在此前提下, 按 (论文编号升序, 每篇的评审人编号对升序) 展开的全局序列
     取字典序最小的方案。

评审保障等级 (levels: {论文编号: high/medium/normal}, 未设置视为普通):
  仅在无法完整覆盖全部目标论文时参与求解——部分方案的选取次序为
  1. 完整分配的论文总数最大;
  2. 在此前提下, 高等级 (high) 完整分配数最大;
  3. 再使中等级 (medium) 完整分配数最大;
  4. 最后沿用既有全局字典序择优。
  完整可行时等级不影响求解, 仍按上述容量比例与字典序优化。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations


@dataclass(frozen=True)
class Paper:
    paper_id: str
    topics: frozenset
    institutions: frozenset


@dataclass(frozen=True)
class Reviewer:
    reviewer_id: str
    topics: frozenset
    institution: str
    capacity: int
    avoid_papers: frozenset
    active: bool = True


def is_expert(reviewer: Reviewer, paper: Paper) -> bool:
    """评审人是否擅长该论文主题。论文无主题时视为任何评审人均满足。"""
    if not paper.topics:
        return True
    return not reviewer.topics.isdisjoint(paper.topics)


def analyze_paper(paper: Paper, reviewers, recused_pairs=(), inst_group=None, barred_pairs=()):
    """返回 (eligible, excluded, pairs)。

    eligible: 通过机构回避与回避名单过滤的评审人 (按编号升序);
    excluded: 被排除评审人及原因 (评审人通过决定接口声明的硬回避单独标注;
              由机构归并造成的作者同机构使用 same_institution_as_author_via_merge;
              统一评审截止后该稿逾期槽位的评审人仅对该稿标注 review_overdue);
    pairs:    忽略容量时该论文的全部合法评审人对 (元组内与元组间均按编号升序)。

    机构比较一律按归并组 inst_group 判定; 缺省 (空映射) 时按原名逐字比较。
    barred_pairs: {(reviewer_id, paper_id)} 仅对特定论文排除该评审人
                  (补位时逾期槽位的评审人不得在该稿补位中重新获稿;
                  对该评审人的其余论文资格无影响)。
    """
    def group_of(name):
        return inst_group.get(name, name) if inst_group else name

    author_groups = {group_of(name) for name in paper.institutions}
    eligible, excluded = [], []
    for r in reviewers:  # callers 保证 reviewers 已按 reviewer_id 升序
        if not r.active:
            excluded.append({"reviewer_id": r.reviewer_id, "reason": "reviewer_disabled"})
        elif group_of(r.institution) in author_groups:
            # 原名逐字相同是既有的同机构; 原名不同却同组, 说明冲突由机构归并造成
            if r.institution in paper.institutions:
                reason = "same_institution_as_author"
            else:
                reason = "same_institution_as_author_via_merge"
            excluded.append({"reviewer_id": r.reviewer_id, "reason": reason})
        elif paper.paper_id in r.avoid_papers:
            reason = (
                "hard_recusal_after_decline"
                if (r.reviewer_id, paper.paper_id) in recused_pairs
                else "on_reviewer_avoid_list"
            )
            excluded.append({"reviewer_id": r.reviewer_id, "reason": reason})
        elif (r.reviewer_id, paper.paper_id) in barred_pairs:
            # 该稿统一评审截止后仍未交评语的逾期槽位: 本次补位不得把该稿
            # 重新分给该评审人 (仅对该论文排除, 其余论文不受影响)
            excluded.append({"reviewer_id": r.reviewer_id, "reason": "review_overdue"})
        else:
            eligible.append(r)
    pairs = []
    for a, b in combinations(eligible, 2):
        if group_of(a.institution) == group_of(b.institution):
            continue
        if not (is_expert(a, paper) or is_expert(b, paper)):
            continue
        pairs.append((a.reviewer_id, b.reviewer_id))
    return eligible, excluded, pairs


class Solver:
    def __init__(self, papers, reviewers, recused_pairs=(), inst_group=None, barred_pairs=()):
        self.papers = sorted(papers, key=lambda p: p.paper_id)
        self.reviewers = sorted(reviewers, key=lambda r: r.reviewer_id)
        self.inst_group = inst_group or None
        self.barred_pairs = set(barred_pairs)
        self.info = {}
        for p in self.papers:
            eligible, excluded, pairs = analyze_paper(
                p, self.reviewers, recused_pairs, self.inst_group, self.barred_pairs
            )
            self.info[p.paper_id] = {"eligible": eligible, "excluded": excluded, "pairs": pairs}

    def group_of(self, name):
        """机构原名 -> 归并组键 (无归并时为原名自身)。"""
        if self.inst_group:
            return self.inst_group.get(name, name)
        return name

    # ---------- 完整方案 ----------

    def find_first(self, caps):
        """在容量上限 caps 下求字典序最小的完整方案; 不可行返回 None。

        论文按编号升序处理, 每篇的评审人对按字典序枚举, 第一个完整解即全局字典序最小。
        """
        order = [p.paper_id for p in self.papers]
        n = len(order)
        pairs_of = {pid: self.info[pid]["pairs"] for pid in order}
        loads = {rid: 0 for rid in caps}
        assignment = {}

        def rest_ok(i):
            remaining = sum(caps[rid] - loads[rid] for rid in caps)
            if remaining < 2 * (n - i):
                return False
            for pid in order[i:]:
                if not any(loads[a] < caps[a] and loads[b] < caps[b] for a, b in pairs_of[pid]):
                    return False
            return True

        def backtrack(i):
            if i == n:
                return True
            pid = order[i]
            for a, b in pairs_of[pid]:
                if loads[a] < caps[a] and loads[b] < caps[b]:
                    loads[a] += 1
                    loads[b] += 1
                    assignment[pid] = (a, b)
                    if rest_ok(i + 1) and backtrack(i + 1):
                        return True
                    del assignment[pid]
                    loads[a] -= 1
                    loads[b] -= 1
            return False

        if rest_ok(0) and backtrack(0):
            return assignment
        return None

    def minimal_ratio_caps(self):
        """返回使方案可行的、最小化最高已用容量比例所对应的容量上限; 完全不可行返回 None。"""
        if not self.papers:
            return {r.reviewer_id: 0 for r in self.reviewers}
        if not self.reviewers:
            return None
        candidates = sorted({
            Fraction(k, r.capacity)
            for r in self.reviewers
            for k in range(1, r.capacity + 1)
        })

        def caps_for(t: Fraction):
            return {
                r.reviewer_id: (t * r.capacity).numerator // (t * r.capacity).denominator
                for r in self.reviewers
            }

        if self.find_first(caps_for(candidates[-1])) is None:
            return None
        lo, hi = 0, len(candidates) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self.find_first(caps_for(candidates[mid])) is not None:
                hi = mid
            else:
                lo = mid + 1
        return caps_for(candidates[lo])

    # ---------- 不可行时的部分方案与诊断 ----------

    def max_partial(self, caps, levels=None):
        """求最多论文被完整分配的部分方案。

        选取次序: 完整分配总数最大 -> 高等级完整分配数最大 -> 中等级完整
        分配数最大 -> 全局序列字典序最小 (levels 缺省时全部视为普通,
        退化为原有的"总数 + 字典序")。返回 (assignment, loads)。
        """
        levels = levels or {}
        order = [p.paper_id for p in self.papers]
        n = len(order)
        pairs_of = {pid: self.info[pid]["pairs"] for pid in order}
        loads = {rid: 0 for rid in caps}
        assignment = {}
        best = {"rank": None, "key": None, "assignment": None, "loads": None}

        def key_of(assign):
            seq = []
            for pid in order:
                if pid in assign:
                    a, b = assign[pid]
                    seq.append((0, a, b))
                else:
                    seq.append((1, "", ""))
            return tuple(seq)

        def rank_of(assign, count):
            # (总数, 高等级数, 中等级数): 越大越优; 等级未知一律按普通计
            high = sum(1 for pid in assign if levels.get(pid) == "high")
            medium = sum(1 for pid in assign if levels.get(pid) == "medium")
            return (count, high, medium)

        def backtrack(i, count):
            if best["rank"] is not None and best["rank"][0] > count + (n - i):
                return  # 已不可能取得更多分配
            if i == n:
                rank = rank_of(assignment, count)
                k = key_of(assignment)
                if (
                    best["rank"] is None
                    or rank > best["rank"]
                    or (rank == best["rank"] and k < best["key"])
                ):
                    best["rank"] = rank
                    best["key"] = k
                    best["assignment"] = dict(assignment)
                    best["loads"] = dict(loads)
                return
            pid = order[i]
            for a, b in pairs_of[pid]:
                if loads[a] < caps[a] and loads[b] < caps[b]:
                    loads[a] += 1
                    loads[b] += 1
                    assignment[pid] = (a, b)
                    backtrack(i + 1, count + 1)
                    del assignment[pid]
                    loads[a] -= 1
                    loads[b] -= 1
            backtrack(i + 1, count)  # 跳过该论文

        backtrack(0, 0)
        return best["assignment"] or {}, best["loads"] or {rid: 0 for rid in caps}

    def diagnose(self, unassigned_ids, loads, caps):
        """对每篇未分配论文给出限制原因。"""
        paper_by_id = {p.paper_id: p for p in self.papers}
        out = {}
        for pid in unassigned_ids:
            paper = paper_by_id[pid]
            info = self.info[pid]
            eligible = info["eligible"]
            pairs = info["pairs"]
            reasons = []
            if len(eligible) < 2:
                reasons.append("fewer_than_two_eligible_reviewers")
            full = [r.reviewer_id for r in eligible if loads[r.reviewer_id] >= caps[r.reviewer_id]]
            if eligible and len(eligible) - len(full) < 2:
                reasons.append("eligible_reviewers_at_capacity")
            if not any(is_expert(r, paper) for r in eligible):
                reasons.append("no_expert_among_eligible_reviewers")
            if len(eligible) >= 2 and len({self.group_of(r.institution) for r in eligible}) < 2:
                # 合格者全部同属一个归并组; 原名互异才可能是归并造成
                if len({r.institution for r in eligible}) < 2:
                    reasons.append("eligible_reviewers_share_single_institution")
                else:
                    reasons.append("eligible_reviewers_share_single_institution_via_merge")
            if not pairs:
                reasons.append("no_valid_reviewer_pair")
            elif not any(loads[a] < caps[a] and loads[b] < caps[b] for a, b in pairs):
                reasons.append("all_valid_pairs_blocked_by_capacity")
            out[pid] = {
                "eligible_reviewers": [r.reviewer_id for r in eligible],
                "eligible_at_capacity": full,
                "excluded": info["excluded"],
                "reasons": reasons,
            }
        return out


def _loads_of(plan):
    loads = {}
    for a, b in plan.values():
        loads[a] = loads.get(a, 0) + 1
        loads[b] = loads.get(b, 0) + 1
    return loads


def _level_summary(paper_ids, assigned_ids, levels):
    """按评审保障等级统计已分配/未分配论文数 (未设置等级视为普通 normal)。

    paper_ids: 本次求解的目标论文 (已按编号排序); assigned_ids: 其中被完整
    分配的论文集合。等级未知的值一律按普通计, 保证响应键固定为
    high/medium/normal 三档。
    """
    levels = levels or {}
    assigned = set(assigned_ids)
    summary = {lv: {"assigned": 0, "unassigned": 0} for lv in ("high", "medium", "normal")}
    for pid in paper_ids:
        lv = levels.get(pid, "normal")
        if lv not in summary:
            lv = "normal"
        summary[lv]["assigned" if pid in assigned else "unassigned"] += 1
    return summary


def compute_assignment(papers, reviewers, recused_pairs=(), inst_group=None, levels=None, barred_pairs=()):
    """求解入口。返回可 JSON 序列化的结果字典。

    inst_group: 机构归并组 {原名: 组键}; 机构冲突一律按归并组判定。
    levels:     评审保障等级 {论文编号: high/medium/normal}; 仅在无法完整
                覆盖全部论文时参与求解 (先总数, 再依次高/中等级数, 最后字典序),
                完整可行时不影响容量比例与字典序优化。
    barred_pairs: {(reviewer_id, paper_id)} 仅对特定论文排除该评审人
                  (补位逾期槽位专用; 普通分配传空, 沿用原行为)。
    """
    solver = Solver(papers, reviewers, recused_pairs, inst_group, barred_pairs)
    reviewer_by_id = {r.reviewer_id: r for r in solver.reviewers}
    paper_ids = [p.paper_id for p in solver.papers]
    explanations = {
        pid: {
            "eligible_reviewers": [r.reviewer_id for r in info["eligible"]],
            "excluded": info["excluded"],
        }
        for pid, info in solver.info.items()
    }

    def paper_entries(plan):
        return {
            pid: {"assigned": list(plan[pid]), **explanations[pid]}
            for pid in sorted(plan)
        }

    caps = solver.minimal_ratio_caps()
    if caps is not None:
        plan = solver.find_first(caps)
        loads = _loads_of(plan)
        ratio = Fraction(0)
        for r in solver.reviewers:
            ratio = max(ratio, Fraction(loads.get(r.reviewer_id, 0), r.capacity))
        return {
            "feasible": True,
            "plan": {pid: list(pair) for pid, pair in sorted(plan.items())},
            "unassigned": [],
            "max_used_capacity_ratio": str(ratio),
            "max_used_capacity_ratio_value": float(ratio),
            "loads": {rid: loads.get(rid, 0) for rid in sorted(reviewer_by_id)},
            "papers": paper_entries(plan),
            "diagnostics": {},
            "level_summary": _level_summary(paper_ids, plan.keys(), levels),
        }

    # 无完整方案: 给出最大部分方案 (先总数, 再依次高/中等级完整分配数,
    # 最后全局字典序) 与每篇未分配论文的限制原因, 发布版不受影响
    full_caps = {r.reviewer_id: r.capacity for r in solver.reviewers}
    partial, loads = solver.max_partial(full_caps, levels)
    unassigned = [p.paper_id for p in solver.papers if p.paper_id not in partial]
    return {
        "feasible": False,
        "plan": {pid: list(pair) for pid, pair in sorted(partial.items())},
        "unassigned": unassigned,
        "max_used_capacity_ratio": None,
        "max_used_capacity_ratio_value": None,
        "loads": {rid: loads.get(rid, 0) for rid in sorted(reviewer_by_id)},
        "papers": paper_entries(partial),
        "diagnostics": solver.diagnose(unassigned, loads, full_caps),
        "level_summary": _level_summary(paper_ids, partial.keys(), levels),
    }


# ---------------------------------------------------------------- 固定槽位求解
#
# 补位 (strict=False) 与普通分配锁定 (strict=True) 共用同一套"固定槽位 + 其余位置
# 重算"引擎, 仅在固定槽位失效时策略不同:
#   - 补位: 失效的固定位置释放为可补位置 (fixed_* 诊断), 尽力补齐;
#   - 锁定: 失效的锁定槽位绝不悄悄释放, 该论文直接标记为不可完整分配
#           (lock_* 诊断), 发布因此拒绝。

def _compute_fixed_assignment(
    papers, reviewers, fixed_map, recused_pairs=(), inst_group=None, levels=None, *,
    strict=False, barred_pairs=(),
):
    """固定槽位求解。

    fixed_map: {paper_id: [reviewer_id, ...]} 需要固定的槽位 (至多 2 个)。
    strict=True 时为普通分配锁定: 固定槽位携带的评审人若已删除/停用/回避/
                 与作者同机构 (含归并), 或被锁两人同机构/均不擅长/累计超容量,
                 该论文不可完整分配, 锁定槽位不予释放;
    strict=False 时为补位: 失效固定位置按既有规则释放并标注 fixed_* 原因
                 (含统一评审截止后仍未交评语的逾期槽位 fixed_review_overdue)。
    barred_pairs: {(reviewer_id, paper_id)} 仅对特定论文排除该评审人
                 (补位: 逾期槽位的评审人不得在该稿重新获稿, 诊断 review_overdue)。
    levels: 评审保障等级 {论文编号: high/medium/normal}; 仅在无法完整覆盖
            目标论文时参与部分方案选取 (先总数, 再依次高/中等级数, 最后字典序);
            完整可行时不影响容量比例与字典序优化; 失效锁定 (strict) 的论文
            始终不可完整分配, 等级再高也不释放其锁定。

    其余位置一律遵守原有全部硬约束与优化次序 (先最小化最高容量比例、
    再全局字典序最小); 单人固定时仅枚举"包含该固定评审人"的合法对,
    故专长要求可由另一人满足。
    """
    recused_pairs = set(recused_pairs)
    barred_pairs = set(barred_pairs)
    levels = levels or {}
    solver = Solver(papers, reviewers, recused_pairs, inst_group, barred_pairs)
    reviewer_by_id = {r.reviewer_id: r for r in solver.reviewers}
    paper_by_id = {p.paper_id: p for p in solver.papers}
    paper_ids = [p.paper_id for p in solver.papers]

    label = "lock" if strict else "fixed"
    slot_key = "locks" if strict else "fixed"
    problems_key = "lock_problems" if strict else "fixed_problems"

    def excluded_reason_code(base):
        # 普通分配的排除原因加 lock_ 前缀, 标明冲突发生在被锁定的槽位上
        return f"lock_{base}" if strict else base

    fixed = {}             # pid -> [合法固定评审人 id]
    problems = {}          # pid -> [{reviewer_id, reason}]
    requested = {}         # pid -> 请求固定的评审人 id 去重序列 (供锁定诊断展示)
    infeasible = set()     # strict: 锁定槽位失效、不可完整分配的论文

    for pid in sorted(fixed_map):
        if pid not in paper_by_id:
            continue  # 论文已删除: 该关系随论文一并消失 (锁定行已在删除时级联清除)
        requested[pid] = list(dict.fromkeys(fixed_map[pid]))
        info = solver.info[pid]
        excluded_reason = {e["reviewer_id"]: e["reason"] for e in info["excluded"]}
        kept, pps = [], []
        seen = set()
        for rid in fixed_map[pid]:
            if rid in seen:
                continue
            seen.add(rid)
            if rid not in reviewer_by_id:
                pps.append({"reviewer_id": rid, "reason": f"{label}_reviewer_not_found"})
            elif not reviewer_by_id[rid].active:
                pps.append({"reviewer_id": rid, "reason": f"{label}_reviewer_disabled"})
            elif (not strict) and (rid, pid) in barred_pairs:
                # 补位: 该稿统一评审截止后仍未交评语的逾期槽位即使已确认也释放,
                # 且本次补位不得把该稿重新分给该评审人 (analyze_paper 已排除该对)。
                # 锁定严格模式不读取截止 (barred_pairs 恒空), 普通分配沿用原行为。
                pps.append({"reviewer_id": rid, "reason": f"{label}_review_overdue"})
            elif rid in excluded_reason:
                pps.append(
                    {"reviewer_id": rid, "reason": excluded_reason_code(excluded_reason[rid])}
                )
            else:
                kept.append(rid)
        if strict and pps:
            # 锁定的任一槽位已失格: 双人规则仍需基于保留的锁定者继续检查,
            # 最后统一标记为不可完整分配 (绝不释放、绝不替换)
            infeasible.add(pid)
        if len(kept) >= 2:
            a, b = kept[0], kept[1]
            ra, rb = reviewer_by_id[a], reviewer_by_id[b]
            if solver.group_of(ra.institution) == solver.group_of(rb.institution):
                # 原名不同却同归并组: 失效由机构归并造成
                reason = (
                    f"{label}_pair_violates_institution_rule"
                    + ("" if ra.institution == rb.institution else "_via_merge")
                )
                pps.append({"reviewer_id": b, "reason": reason})
                # 锁定严格模式: 整篇不可完整分配; 补位释放模式: 释放后一槽位重算
                if strict:
                    infeasible.add(pid)
                kept = [a]
            elif not (is_expert(ra, paper_by_id[pid]) or is_expert(rb, paper_by_id[pid])):
                pps.append({"reviewer_id": b, "reason": f"{label}_pair_violates_expert_rule"})
                if strict:
                    infeasible.add(pid)
                kept = [a]
        fixed[pid] = kept
        if pps:
            problems[pid] = pps

    if strict:
        # strict: 任何锁定失效的论文整体退出求解: 锁定清空但论文保留在 fixed 中
        # (空列表), 使其既不会作为完整锁定对、也不会被当成无锁自由论文重算;
        # 其锁定负载不计入容量
        for pid in infeasible:
            fixed[pid] = []

    # 固定位置带来的初始负载
    full_caps = {r.reviewer_id: r.capacity for r in solver.reviewers}
    loads = {r.reviewer_id: 0 for r in solver.reviewers}
    for pid in sorted(fixed):
        for rid in fixed[pid]:
            loads[rid] += 1

    if strict:
        # 锁定累计超过评审人实际容量: 涉及该评审人的全部锁定论文标为不可完整分配,
        # 绝不释放他人槽位。容量按评审人自身容量判定 (非最小化比例上限)。
        over = [rid for rid in sorted(loads) if loads[rid] > full_caps[rid]]
        for rid in over:
            for pid in sorted(p for p in fixed if rid in fixed[p]):
                problems.setdefault(pid, []).append(
                    {"reviewer_id": rid, "reason": f"{label}_assignments_exceed_capacity"}
                )
                infeasible.add(pid)
        for pid in infeasible:
            fixed[pid] = []
        loads = {r.reviewer_id: 0 for r in solver.reviewers}
        for p in fixed:
            for rid in fixed[p]:
                loads[rid] += 1
    else:
        # 补位: 超过容量时确定性地释放编号最大论文上的多余固定位置
        for rid in sorted(loads):
            cap = reviewer_by_id[rid].capacity
            if loads[rid] <= cap:
                continue
            for pid in sorted(
                (p for p in fixed if rid in fixed[p]), reverse=True
            ):
                if loads[rid] <= cap:
                    break
                fixed[pid].remove(rid)
                problems.setdefault(pid, []).append(
                    {"reviewer_id": rid, "reason": f"{label}_assignments_exceed_capacity"}
                )
                loads[rid] -= 1

    fixed_pairs = {pid: tuple(ids) for pid, ids in fixed.items() if len(ids) == 2}
    free_pids = [
        p.paper_id
        for p in solver.papers
        if p.paper_id not in fixed_pairs and p.paper_id not in infeasible
    ]

    def candidate_pairs(pid):
        required = set(fixed.get(pid, ()))
        # 单人固定: 仅枚举包含该固定评审人的合法对 (专长可由搭档满足)
        return [pair for pair in solver.info[pid]["pairs"] if required.issubset(pair)]

    pairs_of = {pid: candidate_pairs(pid) for pid in free_pids}

    def rest_ok(cur_loads, i, caps):
        # 剩余自由槽位需求: 每个自由论文 2 个位置, 单固定论文的固定成员已由预占负载覆盖
        needed_slots = sum(2 - len(fixed.get(pid, ())) for pid in free_pids[i:])
        remaining = sum(caps[r] - cur_loads[r] for r in caps)
        if remaining < needed_slots:
            return False
        for pid in free_pids[i:]:
            required = set(fixed.get(pid, ()))
            if required:
                # 固定成员已预占负载且校验过容量, 只需存在一名有余量的搭档
                (rid,) = tuple(required)
                partners = {a if b == rid else b for a, b in pairs_of[pid]}
                if not any(cur_loads[other] < caps[other] for other in partners):
                    return False
            elif not any(cur_loads[a] < caps[a] and cur_loads[b] < caps[b] for a, b in pairs_of[pid]):
                return False
        return True

    def search(caps):
        """固定位置满足容量上限时, 在剩余位置上求字典序最小完整方案。"""
        cur = dict(loads)
        if any(cur[r] > caps[r] for r in caps):
            return None
        chosen = {}

        def backtrack(i):
            if i == len(free_pids):
                return True
            pid = free_pids[i]
            required = set(fixed.get(pid, ()))
            for a, b in pairs_of[pid]:
                # 固定成员的占用已预载在 cur 中, 只需其当前负载不超上限;
                # 新占用容量的自由成员才检查余量
                free_a, free_b = a not in required, b not in required
                ok_a = (cur[a] < caps[a]) if free_a else (cur[a] <= caps[a])
                ok_b = (cur[b] < caps[b]) if free_b else (cur[b] <= caps[b])
                if ok_a and ok_b:
                    if free_a:
                        cur[a] += 1
                    if free_b:
                        cur[b] += 1
                    chosen[pid] = (a, b)
                    if rest_ok(cur, i + 1, caps) and backtrack(i + 1):
                        return True
                    del chosen[pid]
                    if free_a:
                        cur[a] -= 1
                    if free_b:
                        cur[b] -= 1
            return False

        if rest_ok(cur, 0, caps) and backtrack(0):
            return chosen
        return None

    plan = dict(fixed_pairs)

    # ---- 最小化最高已用容量比例 (含固定负载) ----
    if solver.reviewers:
        candidates = sorted({
            Fraction(k, r.capacity)
            for r in solver.reviewers
            for k in range(1, r.capacity + 1)
        })

        def caps_for(t):
            return {
                r.reviewer_id: (t * r.capacity).numerator // (t * r.capacity).denominator
                for r in solver.reviewers
            }

        chosen = search(caps_for(candidates[-1]))
        # 锁定槽位失效的论文不可完整分配: 即便其余自由论文能解, 也必须走
        # 不可完整分配分支 (绝不悄悄释放锁定), 发布据此拒绝
        if chosen is not None and not infeasible:
            lo, hi = 0, len(candidates) - 1
            while lo < hi:
                mid = (lo + hi) // 2
                if search(caps_for(candidates[mid])) is not None:
                    hi = mid
                else:
                    lo = mid + 1
            chosen = search(caps_for(candidates[lo]))
            plan.update(chosen)
            final_loads = {rid: 0 for rid in full_caps}
            for a, b in plan.values():
                final_loads[a] += 1
                final_loads[b] += 1
            ratio = max(
                (Fraction(final_loads[r.reviewer_id], r.capacity) for r in solver.reviewers),
                default=Fraction(0),
            )
            return {
                "feasible": True,
                "plan": {pid: list(plan[pid]) for pid in sorted(plan)},
                "unassigned": [],
                "max_used_capacity_ratio": str(ratio),
                "max_used_capacity_ratio_value": float(ratio),
                "loads": {rid: final_loads[rid] for rid in sorted(final_loads)},
                slot_key: {pid: list(fixed.get(pid, ())) for pid in sorted(plan)},
                problems_key: problems,
                "papers": _fixed_paper_entries(solver, plan, fixed, problems, slot_key, problems_key),
                "diagnostics": {},
                "level_summary": _level_summary(paper_ids, plan.keys(), levels),
            }

    # ---- 无完整方案: 最大部分方案 + 诊断 ----
    # 部分方案选取次序: 完整分配总数最大 -> 高等级数最大 -> 中等级数最大 ->
    # 全局字典序最小; 失效锁定 (strict) 的论文不参与求解, 等级再高也不释放
    cur = dict(loads)
    best = {"rank": None, "key": None, "chosen": None}
    n = len(free_pids)
    chosen_partial = {}

    def key_of(chosen):
        seq = []
        for pid in free_pids:
            if pid in chosen:
                seq.append((0,) + chosen[pid])
            else:
                seq.append((1, "", ""))
        return tuple(seq)

    def rank_of(chosen, count):
        # 固定对在所有候选中恒定分配, 不影响比较, 只需统计自由论文的等级
        high = sum(1 for pid in chosen if levels.get(pid) == "high")
        medium = sum(1 for pid in chosen if levels.get(pid) == "medium")
        return (count, high, medium)

    def backtrack_partial(i, count):
        if best["rank"] is not None and best["rank"][0] > count + (n - i):
            return
        if i == n:
            rank = rank_of(chosen_partial, count)
            k = key_of(chosen_partial)
            if (
                best["rank"] is None
                or rank > best["rank"]
                or (rank == best["rank"] and k < best["key"])
            ):
                best["rank"] = rank
                best["key"] = k
                best["chosen"] = dict(chosen_partial)
            return
        pid = free_pids[i]
        required = set(fixed.get(pid, ()))
        for a, b in pairs_of[pid]:
            free_a, free_b = a not in required, b not in required
            ok_a = (cur[a] < full_caps[a]) if free_a else (cur[a] <= full_caps[a])
            ok_b = (cur[b] < full_caps[b]) if free_b else (cur[b] <= full_caps[b])
            if ok_a and ok_b:
                if free_a:
                    cur[a] += 1
                if free_b:
                    cur[b] += 1
                chosen_partial[pid] = (a, b)
                backtrack_partial(i + 1, count + 1)
                del chosen_partial[pid]
                if free_a:
                    cur[a] -= 1
                if free_b:
                    cur[b] -= 1
        backtrack_partial(i + 1, count)

    backtrack_partial(0, 0)
    partial_plan = dict(fixed_pairs)
    if best["chosen"]:
        partial_plan.update(best["chosen"])
    final_loads = {rid: 0 for rid in full_caps}
    for a, b in partial_plan.values():
        final_loads[a] += 1
        final_loads[b] += 1
    unassigned = [p.paper_id for p in solver.papers if p.paper_id not in partial_plan]
    return {
        "feasible": False,
        "plan": {pid: list(partial_plan[pid]) for pid in sorted(partial_plan)},
        "unassigned": unassigned,
        "max_used_capacity_ratio": None,
        "max_used_capacity_ratio_value": None,
        "loads": {rid: final_loads[rid] for rid in sorted(final_loads)},
        slot_key: {pid: list(fixed.get(pid, ())) for pid in sorted(partial_plan)},
        problems_key: problems,
        "papers": _fixed_paper_entries(
            solver, partial_plan, fixed, problems, slot_key, problems_key
        ),
        "diagnostics": _fixed_diagnose(
            solver, unassigned, final_loads, full_caps, pairs_of, fixed, problems,
            requested, infeasible, slot_key, problems_key, label,
        ),
        "level_summary": _level_summary(paper_ids, partial_plan.keys(), levels),
    }


def compute_backfill(papers, reviewers, fixed_map, recused_pairs=(), inst_group=None,
                     levels=None, barred_pairs=()):
    """补位预演: 仅面向当前发布版内的论文 (papers 已按该范围过滤)。

    fixed_map:     {paper_id: [reviewer_id, ...]} 当前发布版中"已确认且未回避"
                   的关系, 这些位置固定不动; 其余位置在原有硬约束与优化次序下重算。
    recused_pairs: {(reviewer_id, paper_id)} 评审人通过决定接口声明的硬回避
                   (与会务方录入的 avoid_papers 等价, 但诊断原因单独标注)。
    inst_group:    机构归并组 {原名: 组键}; 机构冲突一律按归并组判定。
    levels:        评审保障等级 {论文编号: high/medium/normal}; 仅在无法完整
                   覆盖目标论文时参与部分方案选取 (先总数, 再依次高/中等级数,
                   最后字典序), 完整可行时不影响既有优化次序。
    barred_pairs:  {(reviewer_id, paper_id)} 统一评审截止后仍未交评语的逾期槽位:
                   即使此前已确认也从固定位置释放 (fixed_review_overdue),
                   且本次补位不得把该稿重新分给该评审人 (该对在求解中以
                   review_overdue 排除, 对该评审人的其余论文资格无影响)。

    数据变更导致固定关系失效时 (评审人被删除/与作者变为同机构/硬回避等),
    该固定位置释放为可补位置, 并在 papers[pid].fixed_problems 中说明。
    已确认槽位若因机构归并而不再满足"两名评审人不同机构", 同样释放,
    不得固定该槽位 (fixed_pair_violates_institution_rule_via_merge)。

    注意: 补位不读取普通分配锁定表——锁定仅约束普通分配, 不改变补位规则。
    """
    return _compute_fixed_assignment(
        papers, reviewers, fixed_map, recused_pairs, inst_group, levels,
        strict=False, barred_pairs=barred_pairs,
    )


def compute_locked_assignment(papers, reviewers, lock_map, recused_pairs=(), inst_group=None, levels=None):
    """带锁定槽位的普通分配。

    lock_map: {paper_id: [reviewer_id, ...]} 会务方在普通分配前提交的锁定表
              (每篇 0~2 名评审人; 空表/缺省 = 不锁定)。
    levels:   评审保障等级 {论文编号: high/medium/normal}; 仅在无法完整覆盖
              全部论文时参与部分方案选取 (先总数, 再依次高/中等级数, 最后字典序)。

    锁定槽位必须保留: 其余位置在机构冲突、主题专长、回避、资格、容量等全部
    既有硬约束与既有优化次序下重算; 单人锁定时专长可由另一人满足。
    锁定人随后被删除、停用、回避或因资料/机构归并而失格, 或锁定累计超容量时,
    锁定槽位绝不悄悄释放: 对应论文在 lock_problems/诊断中给出具体原因并标记为
    不可完整分配 (feasible=false), 发布据此拒绝且不改变当前发布版;
    保障等级再高也不释放失效锁定。
    """
    return _compute_fixed_assignment(
        papers, reviewers, lock_map, recused_pairs, inst_group, levels, strict=True
    )


def _fixed_paper_entries(solver, plan, fixed, problems, slot_key, problems_key):
    out = {}
    for pid in sorted(plan):
        out[pid] = {
            "assigned": list(plan[pid]),
            slot_key: list(fixed.get(pid, ())),
            "eligible_reviewers": [r.reviewer_id for r in solver.info[pid]["eligible"]],
            "excluded": [dict(e) for e in solver.info[pid]["excluded"]],
            problems_key: problems.get(pid, []),
        }
    return out


def _fixed_diagnose(solver, unassigned_ids, loads, caps, pairs_of, fixed, problems,
                    requested, infeasible, slot_key, problems_key, label):
    paper_by_id = {p.paper_id: p for p in solver.papers}
    out = {}
    for pid in unassigned_ids:
        paper = paper_by_id[pid]
        info = solver.info[pid]
        eligible = info["eligible"]
        pps = problems.get(pid, [])
        entry = {
            "eligible_reviewers": [r.reviewer_id for r in eligible],
            "eligible_at_capacity": [
                r.reviewer_id
                for r in eligible
                if loads[r.reviewer_id] >= caps[r.reviewer_id]
            ],
            "excluded": [dict(e) for e in info["excluded"]],
            slot_key: list(requested.get(pid, fixed.get(pid, ()))),
            problems_key: pps,
        }
        if pid in infeasible:
            # 锁定槽位失效: 具体冲突即限制原因, 不释放锁定、不做替换尝试
            entry["reasons"] = list(dict.fromkeys(p["reason"] for p in pps))
            out[pid] = entry
            continue
        pairs = pairs_of.get(pid, [])
        reasons = []
        if len(eligible) < 2:
            reasons.append("fewer_than_two_eligible_reviewers")
        full = entry["eligible_at_capacity"]
        if eligible and len(eligible) - len(full) < 2:
            reasons.append("eligible_reviewers_at_capacity")
        if not any(is_expert(r, paper) for r in eligible):
            reasons.append("no_expert_among_eligible_reviewers")
        if len(eligible) >= 2 and len({solver.group_of(r.institution) for r in eligible}) < 2:
            if len({r.institution for r in eligible}) < 2:
                reasons.append("eligible_reviewers_share_single_institution")
            else:
                reasons.append("eligible_reviewers_share_single_institution_via_merge")
        if fixed.get(pid):
            if not pairs:
                reasons.append(f"no_valid_pair_containing_{label}_reviewer")
            elif not any(loads[a] < caps[a] and loads[b] < caps[b] for a, b in pairs):
                reasons.append(f"all_{label}_compatible_pairs_blocked_by_capacity")
        else:
            if not info["pairs"]:
                reasons.append("no_valid_reviewer_pair")
            elif not any(loads[a] < caps[a] and loads[b] < caps[b] for a, b in info["pairs"]):
                reasons.append("all_valid_pairs_blocked_by_capacity")
        entry["reasons"] = reasons
        out[pid] = entry
    return out
