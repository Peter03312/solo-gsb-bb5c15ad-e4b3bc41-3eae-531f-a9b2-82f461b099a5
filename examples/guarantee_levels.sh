#!/usr/bin/env bash
# 本轮用法端到端示例: 论文评审保障等级 (高/中/普通)
#   录入 -> 按论文设置等级 (携带所见修订号) -> 查询各论文等级 ->
#   相同等级重试幂等 (changed=false, 不推进修订号) -> 非法等级/版本不符/未知论文拒绝 ->
#   容量不足时预演: 先总数最大, 再依次高/中等级完整分配数最大, 最后字典序
#   (level_summary 给出各等级已分配/未分配数量) -> 无法完整分配发布仍 422 ->
#   完整可行时等级不影响容量比例与字典序优化 -> 删除论文后其等级不再参与求解
# 建议对新库运行: ./examples/guarantee_levels.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }

echo "== 1. 录入评审人 (R1/R2 容量 1) 与论文 P1/P2 =="
for spec in 'R1 r1-secret Inst-A' 'R2 r2-secret Inst-B'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"$3\", \"capacity\": 1, \"avoid_papers\": []}"
done
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 匿名稿全文...", "topics": ["AI"], "institutions": ["Univ-X"]}'
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P2", "manuscript": "P2 匿名稿全文...", "topics": ["AI"], "institutions": ["Univ-Y"]}'

echo "== 2. 查询各论文等级 (未设置视为普通 normal) =="
curl -s "$BASE/papers/guarantee-levels" -H "$ORG" \
  | j "{'revision': d['revision'], 'levels': d['levels'], 'summary': d['summary']}"

echo "== 3. 会务方按论文设置等级: P2 设为高 (携带所见资料修订号) =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/papers/P2/guarantee-level" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P2\", \"level\": \"high\", \"base_revision\": $REV}" \
  | j "{'changed': d['changed'], 'level': d['level'], 'revision': d['revision']}"

echo "== 4. 相同等级重试 -> changed=false, 不推进修订号; 非法等级/版本不符/未知论文均拒绝 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/papers/P2/guarantee-level" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P2\", \"level\": \"high\", \"base_revision\": $REV}" \
  | j "{'changed': d['changed'], 'revision': d['revision']}"
curl -s -o /dev/null -w '非法等级           -> HTTP %{http_code}\n' -X POST "$BASE/papers/P2/guarantee-level" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P2\", \"level\": \"urgent\", \"base_revision\": $REV}"
curl -s -o /dev/null -w '过期修订号         -> HTTP %{http_code}\n' -X POST "$BASE/papers/P2/guarantee-level" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d '{"paper_id": "P2", "level": "medium", "base_revision": 0}'
curl -s -o /dev/null -w '未知论文           -> HTTP %{http_code}\n' -X POST "$BASE/papers/GHOST/guarantee-level" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"GHOST\", \"level\": \"high\", \"base_revision\": $REV}"

echo "== 5. 容量不足时的普通预演: 只能完整覆盖一篇, 高等级 P2 优先于字典序 =="
echo "    (仅 R1/R2 两名容量 1 的评审人 -> 两个槽位只能覆盖两篇论文之一)"
curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" \
  | j "{'feasible': d['feasible'], 'plan': d['plan'], 'unassigned': d['unassigned'], 'level_summary': d['level_summary']}"

echo "== 6. 无法完整分配仍拒绝发布 (422), 当前发布版不变 (尚无发布版 -> 404) =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -o /dev/null -w '不可完整分配时发布 -> HTTP %{http_code}\n' -X POST "$BASE/assignment/publish" \
  -H "$ORG" -H 'Content-Type: application/json' -d "{\"base_revision\": $REV}"
curl -s -o /dev/null -w '当前发布版         -> HTTP %{http_code}\n' "$BASE/assignment" -H "$ORG"

echo "== 7. 补录 R3/R4 后完整可行: 等级不影响既有容量比例与字典序优化 =="
for spec in 'R3 r3-secret Inst-C' 'R4 r4-secret Inst-D'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"$3\", \"capacity\": 2, \"avoid_papers\": []}"
done
curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" \
  | j "{'feasible': d['feasible'], 'plan': d['plan'], 'ratio': d['max_used_capacity_ratio'], 'level_summary': d['level_summary']}"
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}" | j "{'ok': d['ok'], 'serial': d['serial'], 'plan': d['plan']}"

echo "== 8. 删除论文后其等级不再参与求解 (等级行随论文清除) =="
curl -s -o /dev/null -X DELETE "$BASE/papers/P2" -H "$ORG"
curl -s "$BASE/papers/guarantee-levels" -H "$ORG" \
  | j "{'levels': d['levels'], 'summary': d['summary']}   # P2 及其等级已清除"
curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" \
  | j "{'feasible': d['feasible'], 'plan': d['plan'], 'level_summary': d['level_summary']}"

echo "== 9. 补位预演同样返回各等级统计 (完整可行时等级不影响结果) =="
curl -s -X POST "$BASE/assignment/backfill/dry-run" -H "$ORG" \
  | j "{'feasible': d['feasible'], 'plan': d['plan'], 'level_summary': d['level_summary']}"
