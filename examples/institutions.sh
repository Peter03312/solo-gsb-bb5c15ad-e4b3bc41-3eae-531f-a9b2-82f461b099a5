#!/usr/bin/env bash
# 本轮用法端到端示例: 机构原名归并 (同一机构的不同名称归为同一冲突判定组)
#   录入 (评审人/作者使用同一机构的不同原名) -> 归并预校验: 原名不同不冲突 ->
#   会务方凭密钥提交两个原名 + 所见修订号归并 -> 同组重复提交幂等 (不推进修订号) ->
#   普通预演: 与作者同归并组的评审人被排除 (same_institution_as_author_via_merge) ->
#   发布 -> 两人确认 -> 再归并两名评审人的机构 ->
#   补位预演: 已确认槽位因归并失效, 不固定 (fixed_pair_..._via_merge) ->
#   双版本复核补位发布 -> 查看全部归并组
# 建议对新库运行: ./examples/institutions.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }

echo "== 1. 录入评审人与论文 (同一机构使用不同原名: Acme-U / Acme University) =="
for spec in 'R1 r1-secret Acme-U' 'R2 r2-secret Acme-Lab' 'R3 r3-secret Inst-C' 'R4 r4-secret Inst-D' 'R5 r5-secret Inst-E'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"$3\", \"capacity\": 3, \"avoid_papers\": []}"
done
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 匿名稿全文...", "topics": ["AI"],
  "institutions": ["Acme University"]}'

echo "== 2. 归并前预演: 原名逐字不同, R1 与作者机构不判为同一机构 =="
curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" \
  | j "{'plan_P1': d['plan']['P1'], 'excluded_R1': [e for e in d['papers']['P1']['excluded'] if e['reviewer_id']=='R1']}"

echo "== 3. 会务方归并两个机构原名 (携带所见资料修订号) =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/institutions/merge-groups" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"name_a\": \"Acme-U\", \"name_b\": \"Acme University\", \"base_revision\": $REV}"; echo

echo "== 3b. 同组重复提交 (含交换方向) 幂等 changed=false, 不推进修订号、不写边 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/institutions/merge-groups" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"name_a\": \"Acme University\", \"name_b\": \"Acme-U\", \"base_revision\": $REV}"; echo
echo "-- 过期修订号 -> 409; 空白名称 -> 422; 未知名称 -> 404 (均不留部分变更) --"
curl -s -o /dev/null -w '过期修订号       -> HTTP %{http_code}\n' -X POST "$BASE/institutions/merge-groups" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d '{"name_a": "Acme-U", "name_b": "Acme University", "base_revision": 0}'
curl -s -o /dev/null -w '空白名称         -> HTTP %{http_code}\n' -X POST "$BASE/institutions/merge-groups" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"name_a\": \"   \", \"name_b\": \"Acme University\", \"base_revision\": $REV}"
curl -s -o /dev/null -w '未知机构原名     -> HTTP %{http_code}\n' -X POST "$BASE/institutions/merge-groups" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"name_a\": \"Acme-U\", \"name_b\": \"Ghost-University\", \"base_revision\": $REV}"

echo "== 4. 传递归并: Acme-Lab 与 Acme-U 同组 => 与 Acme University 也同组 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/institutions/merge-groups" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"name_a\": \"Acme-Lab\", \"name_b\": \"Acme-U\", \"base_revision\": $REV}" \
  | j "{'changed': d['changed'], 'names': d['names'], 'revision': d['revision']}"
curl -s "$BASE/institutions/merge-groups" -H "$ORG" | j 'd["groups"]'

echo "== 5. 归并后普通预演: R1/R2 与作者同归并组被排除; 两名同组评审人不能配对 =="
curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" \
  | j "{'feasible': d['feasible'], 'plan_P1': d['plan']['P1'], 'excluded': d['papers']['P1']['excluded']}"

echo "== 6. 用当前修订号发布; 原始机构名称在资料中保持原样 =="
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
PUB=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}")
echo "$PUB" | j "{'serial': d['serial'], 'plan_P1': d['plan']['P1']}"
curl -s "$BASE/reviewers" -H "$ORG" | j "{r['reviewer_id']: r['institution'] for r in d['reviewers']}"

echo "== 7. 当前发布版两名评审人确认, 随后归并两人机构 -> 已确认槽位因归并失效 =="
RA=$(echo "$PUB" | j 'd["plan"]["P1"][0]')
RB=$(echo "$PUB" | j 'd["plan"]["P1"][1]')
CRED_A="$(echo "$RA" | tr 'A-Z' 'a-z')-secret"
CRED_B="$(echo "$RB" | tr 'A-Z' 'a-z')-secret"
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CRED_A" \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CRED_B" \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'
IA=$(curl -s "$BASE/reviewers" -H "$ORG" | j "next(r['institution'] for r in d['reviewers'] if r['reviewer_id']=='$RA')")
IB=$(curl -s "$BASE/reviewers" -H "$ORG" | j "next(r['institution'] for r in d['reviewers'] if r['reviewer_id']=='$RB')")
if [ "$IA" != "$IB" ]; then
  REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
  curl -s -X POST "$BASE/institutions/merge-groups" -H "$ORG" -H 'Content-Type: application/json' \
    -d "{\"name_a\": \"$IA\", \"name_b\": \"$IB\", \"base_revision\": $REV}" \
    | j "{'changed': d['changed'], 'names': d['names']}"
else
  echo "两人机构原名本就相同 ($IA), 无需归并即可复现同机构失效"
fi

echo "== 8. 补位预演: 失效槽位不固定 (fixed_pair_violates_institution_rule_via_merge) =="
DRY=$(curl -s -X POST "$BASE/assignment/backfill/dry-run" -H "$ORG")
echo "$DRY" | j "{'feasible': d['feasible'], 'plan_P1': d['plan']['P1'], 'fixed_P1': d['fixed']['P1'], 'problems_P1': d['fixed_problems'].get('P1', [])}"

echo "== 9. 补位发布: 双版本复核 (修订号 + 发布序号); 旧发布版/评语仍可追溯 =="
BREV=$(echo "$DRY" | j 'd["revision"]')
BSER=$(echo "$DRY" | j 'd["serial"]')
curl -s -X POST "$BASE/assignment/backfill/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $BREV, \"base_serial\": $BSER}" \
  | j "{'ok': d['ok'], 'serial': d['serial'], 'plan_P1': d['plan']['P1'], 'carried_confirmations': d['carried_confirmations'], 'carried_reviews': d['carried_reviews']}"

echo "== 10. 会务方查看全部归并组 (传递闭包; 归并不可拆分) =="
curl -s "$BASE/institutions/merge-groups" -H "$ORG"; echo
