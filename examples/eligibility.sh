#!/usr/bin/env bash
# 本轮用法端到端示例: 评审资格停用/启用与变更记录
#   录入 -> 预演/发布 -> 确认 -> 会务方停用 (事务内核对修订号) ->
#   评审人凭据仍有效但立即 403, 既有分配/确认/评语不删, 已发布快照仍有效 ->
#   普通分配排除停用者 -> 补位释放其已确认槽位 (其他合格槽位固定) ->
#   重新启用仅恢复当前发布版中仍属于本人的任务; 查看变更记录
# 建议对新库运行: ./examples/eligibility.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }

echo "== 1. 录入 4 名不同机构评审人 + 1 篇论文 (注意先清空库或使用新库) =="
for spec in 'R1 r1-secret Inst-A' 'R2 r2-secret Inst-B' 'R3 r3-secret Inst-C' 'R4 r4-secret Inst-D'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"$3\", \"capacity\": 3, \"avoid_papers\": []}"
done
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 匿名稿全文...", "topics": ["AI"],
  "institutions": ["Univ-X"]}'

echo "== 2. 普通预演 + 发布 =="
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
PUB=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}")
RA=$(echo "$PUB" | j 'd["plan"]["P1"][0]')
RB=$(echo "$PUB" | j 'd["plan"]["P1"][1]')
echo "P1 分配: $RA, $RB"
CRED_A="r$(echo "$RA" | sed 's/R//')-secret"
CRED_B="r$(echo "$RB" | sed 's/R//')-secret"

echo "== 3. 两人均确认 (确认槽位在后续补位中作为固定位置的候选) =="
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CRED_A" \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CRED_B" \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'

echo "== 4. 会务方停用 $RA: 携带所见资料修订号 + 目标状态 false + 非空原因 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/reviewers/$RA/status" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"reviewer_id\": \"$RA\", \"base_revision\": $REV, \"active\": false, \"reason\": \"长期休假暂停本轮评审\"}"; echo

echo "== 4b. 匹配修订号重复提交同一状态和原因 -> 幂等 changed=false, 不推进修订号 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/reviewers/$RA/status" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"reviewer_id\": \"$RA\", \"base_revision\": $REV, \"active\": false, \"reason\": \"长期休假暂停本轮评审\"}"; echo
echo "-- 同状态异原因 -> 409; 空原因 -> 422; 过期修订号 -> 409; 未知评审人 -> 404 --"
curl -s -o /dev/null -w '同状态异原因     -> HTTP %{http_code}\n' -X POST "$BASE/reviewers/$RA/status" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"reviewer_id\": \"$RA\", \"base_revision\": $REV, \"active\": false, \"reason\": \"另一个原因\"}"
curl -s -o /dev/null -w '空原因           -> HTTP %{http_code}\n' -X POST "$BASE/reviewers/$RA/status" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"reviewer_id\": \"$RA\", \"base_revision\": $REV, \"active\": false, \"reason\": \"   \"}"
curl -s -o /dev/null -w '过期修订号       -> HTTP %{http_code}\n' -X POST "$BASE/reviewers/$RA/status" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"reviewer_id\": \"$RA\", \"base_revision\": 0, \"active\": false, \"reason\": \"x\"}"
curl -s -o /dev/null -w '未知评审人       -> HTTP %{http_code}\n' -X POST "$BASE/reviewers/GHOST/status" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"reviewer_id\": \"GHOST\", \"base_revision\": $REV, \"active\": false, \"reason\": \"x\"}"

echo "== 5. 停用即时生效: $RA 凭据仍有效 (错误密码仍 401), 但取稿/决定/评语一律 403 =="
curl -s -o /dev/null -w '凭据错误         -> HTTP %{http_code}\n' "$BASE/reviewer/assignments" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: wrong"
curl -s -o /dev/null -w '停用者取稿       -> HTTP %{http_code}\n' "$BASE/reviewer/assignments" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CRED_A"
curl -s -o /dev/null -w '停用者提交决定   -> HTTP %{http_code}\n' -X POST \
  "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CRED_A" \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'
SERIAL=$(curl -s "$BASE/assignment" -H "$ORG" | j 'd["serial"]')
curl -s -o /dev/null -w '停用者提交评语   -> HTTP %{http_code}\n' -X POST \
  "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CRED_A" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SERIAL, \"score\": 4, \"comment\": \"评语\"}"

echo "== 6. 既有发布分配/确认不删除: 当前发布版仍可核对, $RA 槽位仍为 confirmed =="
curl -s "$BASE/assignment" -H "$ORG" | j '{"serial": d["serial"], "P1": d["papers"]["P1"]["slots"]}'

echo "== 7. 停用者不进入后续普通分配 (dry-run 标注 reviewer_disabled) =="
curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" \
  | j "{'plan_P1': d['plan']['P1'], 'excluded': [e for e in d['papers']['P1']['excluded'] if e['reviewer_id']=='$RA']}"

echo "== 8. 补位预演: $RA 的已确认槽位释放 (fixed_reviewer_disabled), $RB 仍合格保持固定 =="
DRY=$(curl -s -X POST "$BASE/assignment/backfill/dry-run" -H "$ORG")
echo "$DRY" | j "{'feasible': d['feasible'], 'plan_P1': d['plan']['P1'], 'fixed_P1': d['fixed']['P1'], 'problems_P1': d['fixed_problems']['P1']}"
BREV=$(echo "$DRY" | j 'd["revision"]')
BSER=$(echo "$DRY" | j 'd["serial"]')

echo "== 9. 补位发布 (双版本复核); 被替换的 $RA 不再授权取稿 =="
curl -s -X POST "$BASE/assignment/backfill/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $BREV, \"base_serial\": $BSER}" | j "{'ok': d['ok'], 'serial': d['serial'], 'plan_P1': d['plan']['P1'], 'carried_confirmations': d['carried_confirmations']}"

echo "== 10. 重新启用 $RA: 仅恢复当前发布版中仍属于本人的任务; 已被补位替换的槽位不找回 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/reviewers/$RA/status" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"reviewer_id\": \"$RA\", \"base_revision\": $REV, \"active\": true, \"reason\": \"假期结束恢复评审\"}"; echo
curl -s "$BASE/reviewer/assignments" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CRED_A" \
  | j "{'assignments': d['assignments'], 'serial': d['serial']}"

echo "== 11. 会务方查看 $RA 的完整变更记录 (停用/启用, 含原因、修订号、时间) =="
curl -s "$BASE/reviewers/$RA/status" -H "$ORG"; echo
