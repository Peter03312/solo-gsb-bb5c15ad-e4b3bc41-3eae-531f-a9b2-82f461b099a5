#!/usr/bin/env bash
# 本轮用法端到端示例: 普通分配前的评审人锁定表
#   录入 -> 提交锁定 (携带所见修订号) -> 普通预演保留锁定槽位 (单人锁定专长由搭档满足) ->
#   相同锁定表重试幂等 (changed=false, 不推进修订号) -> 发布 ->
#   停用被锁定评审人 -> 预演说明具体冲突并标为不可完整分配 -> 发布拒绝且当前发布版不变 ->
#   清除锁定 -> 恢复可发布; 补位预演不读锁定表 (锁定仅约束普通分配)
# 建议对新库运行: ./examples/locks.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }

echo "== 1. 录入评审人与论文 (不同机构; R1/R4 容量 2) =="
for spec in 'R1 r1-secret Inst-A' 'R2 r2-secret Inst-B' 'R3 r3-secret Inst-C' 'R4 r4-secret Inst-D'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"$3\", \"capacity\": 2, \"avoid_papers\": []}"
done
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 匿名稿全文...", "topics": ["AI"], "institutions": ["Univ-X"]}'
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P2", "manuscript": "P2 匿名稿全文...", "topics": ["AI"], "institutions": ["Univ-Y"]}'

echo "== 2. 会务方查看当前锁定表 (初始为空) =="
curl -s "$BASE/assignment/locks" -H "$ORG" \
  | j "{'revision': d['revision'], 'locks': d['locks'], 'papers': d['locked_papers'], 'slots': d['locked_slots']}"

echo "== 3. 整表提交锁定: P1 锁定 R4, P2 不锁定 (携带所见资料修订号) =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/assignment/locks" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"locks\": {\"P1\": [\"R4\"]}}" \
  | j "{'changed': d['changed'], 'revision': d['revision'], 'locks': d['locks'], 'slots': d['locked_slots']}"

echo "== 4. 相同锁定表重试 -> changed=false, 不推进修订号; 过期修订号 -> 409 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/assignment/locks" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"locks\": {\"P1\": [\"R4\"]}}" \
  | j "{'changed': d['changed'], 'revision': d['revision']}"
curl -s -o /dev/null -w '过期修订号       -> HTTP %{http_code}\n' -X POST "$BASE/assignment/locks" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d '{"base_revision": 0, "locks": {"P1": ["R4"]}}'
curl -s -o /dev/null -w '未知论文         -> HTTP %{http_code}\n' -X POST "$BASE/assignment/locks" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"locks\": {\"GHOST\": [\"R1\"]}}"
curl -s -o /dev/null -w '未知评审人       -> HTTP %{http_code}\n' -X POST "$BASE/assignment/locks" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"locks\": {\"P1\": [\"R4\", \"RX\"]}}"
curl -s -o /dev/null -w '同篇重复锁定     -> HTTP %{http_code}\n' -X POST "$BASE/assignment/locks" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"locks\": {\"P1\": [\"R4\", \"R4\"]}}"
curl -s -o /dev/null -w '同篇超过两人     -> HTTP %{http_code}\n' -X POST "$BASE/assignment/locks" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"locks\": {\"P1\": [\"R1\", \"R2\", \"R3\"]}}"

echo "== 5. 普通预演: P1 保留锁定槽位 R4 (搭档在其余位置按既有约束/优化次序决定) =="
curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" \
  | j "{'feasible': d['feasible'], 'plan_P1': d['plan']['P1'], 'locks_P1': d['locks']['P1'], 'plan_P2': d['plan']['P2'], 'ratio': d['max_used_capacity_ratio']}"

echo "== 6. 单人锁定时专长可由另一人满足: 把 R4 改为不擅长 AI, 给 P1 再锁不擅长的 R4 仍可行 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -o /dev/null -X PUT "$BASE/reviewers/R4" -H "$ORG" -H 'Content-Type: application/json' -d "{
  \"credential\": \"r4-secret\", \"topics\": [\"DB\"], \"institution\": \"Inst-D\",
  \"capacity\": 2, \"avoid_papers\": []}"
curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" \
  | j "{'feasible': d['feasible'], 'plan_P1': d['plan']['P1']}   # 搭档必须擅长 AI"

echo "== 7. 发布 (携带预演所用修订号), 锁定槽位进入发布版 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}" | j "{'ok': d['ok'], 'serial': d['serial'], 'plan_P1': d['plan']['P1']}"

echo "== 8. 锁定人随后被停用: 预演说明 lock_reviewer_disabled, 标为不可完整分配 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -o /dev/null -X POST "$BASE/reviewers/R4/status" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"reviewer_id\": \"R4\", \"base_revision\": $REV, \"active\": false, \"reason\": \"长期休假\"}"
curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" \
  | j "{'feasible': d['feasible'], 'unassigned': d['unassigned'], 'P1_reasons': d['diagnostics']['P1']['reasons'], 'P1_lock_problems': d['diagnostics']['P1']['lock_problems']}"

echo "== 9. 发布拒绝 (422) 且不改变当前发布版; 锁定不被悄悄释放 (仍可查看) =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -o /dev/null -w '锁定失效时发布   -> HTTP %{http_code}\n' -X POST "$BASE/assignment/publish" \
  -H "$ORG" -H 'Content-Type: application/json' -d "{\"base_revision\": $REV}"
curl -s "$BASE/assignment" -H "$ORG" | j "{'serial': d['serial'], 'plan_P1': d['plan']['P1']}"
curl -s "$BASE/assignment/locks" -H "$ORG" | j "{'locks': d['locks']}   # 锁定仍在"

echo "== 10. 补位不读锁定表: 补位预演按既有确认槽位规则工作 (无 locks 字段) =="
curl -s -X POST "$BASE/assignment/backfill/dry-run" -H "$ORG" \
  | j "{'feasible': d['feasible'], 'has_locks_field': ('locks' in d), 'fixed': d['fixed']}"

echo "== 11. 会务方清除锁定 (空表), 普通分配恢复; 再发布成功 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/assignment/locks" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"locks\": {}}" | j "{'changed': d['changed'], 'locks': d['locks']}"
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -o /dev/null -w '清除锁定后发布   -> HTTP %{http_code}\n' -X POST "$BASE/assignment/publish" \
  -H "$ORG" -H 'Content-Type: application/json' -d "{\"base_revision\": $REV}"

echo "== 12. 查看最终锁定表 (为空) =="
curl -s "$BASE/assignment/locks" -H "$ORG"; echo
