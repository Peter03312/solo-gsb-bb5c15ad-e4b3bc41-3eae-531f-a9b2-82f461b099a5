#!/usr/bin/env bash
# 本轮用法端到端示例: 面向作者的匿名反馈快照
#   发布 -> 两人确认并各交一份正式评语 -> 会务方携带当前发布序号发布快照 ->
#   持码者凭随机访问码只读论文编号 + 标号 1、2 的两份评分与评语 (无需凭据) ->
#   相同请求幂等返回原码 -> 补位发布使序号前进, 旧码立即失效 ->
#   重新发布快照生成新版本与新码 -> 会务方追溯全部历史版本
# 前置: 建议对新库运行 (脚本会自行建数据; 若编号已存在可忽略 409)。
# 用法: ./examples/snapshots.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }

echo "== 1. 录入 4 名不同机构评审人 + 1 篇论文 =="
for spec in 'R1 r1-secret Inst-A' 'R2 r2-secret Inst-B' 'R3 r3-secret Inst-C' 'R4 r4-secret Inst-D'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"$3\", \"capacity\": 3, \"avoid_papers\": []}"
done
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 匿名稿全文...", "topics": ["AI"],
  "institutions": ["Univ-X"]}'

echo "== 2. 预演 + 发布, 两人确认并各交一份正式评语 =="
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
PUB=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}")
SER=$(echo "$PUB" | j 'd["serial"]')
RA=$(echo "$PUB" | j 'd["plan"]["P1"][0]')
RB=$(echo "$PUB" | j 'd["plan"]["P1"][1]')
CA="r$(echo "$RA" | sed 's/R//')-secret"
CB="r$(echo "$RB" | sed 's/R//')-secret"
echo "P1 (serial=$SER) 分配: $RA, $RB"
for H in "$RA $CA" "$RB $CB"; do
  set -- $H
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
    -H "X-Reviewer-Id: $1" -H "X-Reviewer-Credential: $2" \
    -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'
done
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"score\": 4, \"comment\": \"选题重要, 建议补充消融实验。\"}"
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"score\": 2, \"comment\": \"实验设计有漏洞, 结论偏强。\"}"

echo "== 3. 资料不齐时拒绝发布 (不会留下新版本): 此处两人已齐, 直接发布成功 =="
curl -s -o /dev/null -w '序号过期/超前    -> HTTP %{http_code}\n' -X POST \
  "$BASE/papers/P1/feedback-snapshot" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"serial\": $((SER + 1))}"

echo "== 4. 会务方携带当前发布序号发布匿名反馈快照, 得到随机访问码 =="
SNAP=$(curl -s -X POST "$BASE/papers/P1/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SER}")
echo "$SNAP"
CODE=$(echo "$SNAP" | j 'd["access_code"]')
VER=$(echo "$SNAP" | j 'd["version"]')
echo "访问码: $CODE (version=$VER)"

echo "== 5. 相同发布序号及两份评语收据重复请求 -> 原快照原码, changed=false =="
curl -s -X POST "$BASE/papers/P1/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SER}" \
  | j '{"changed": d["changed"], "same_code": d["access_code"] == "'"$CODE"'"}'

echo "== 6. 持码者读取: 无需任何凭据, 只见论文编号与标号 1、2 的评分评语 =="
curl -s "$BASE/feedback-snapshots/$CODE"; echo
echo "-- 响应中不含评审人编号/机构/收据/内部诊断:"
BODY=$(curl -s "$BASE/feedback-snapshots/$CODE")
for secret in "$RA" "$RB" "Inst-" "rvw-" "receipt" "institution" "Univ-X"; do
  if echo "$BODY" | grep -qF "$secret"; then echo "  泄露: $secret"; else echo "  未泄露: $secret"; fi
done

echo "== 7. 无效码统一 404, 不透露论文是否存在 =="
curl -s -w '  -> HTTP %{http_code}\n' "$BASE/feedback-snapshots/fbk-does-not-exist"

echo "== 8. 补位发布使发布序号 +1, 旧码立即失效 (仍是 404) =="
DRY=$(curl -s -X POST "$BASE/assignment/backfill/dry-run" -H "$ORG")
curl -s -o /dev/null -X POST "$BASE/assignment/backfill/publish" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $(echo "$DRY" | j 'd["revision"]'), \"base_serial\": $(echo "$DRY" | j 'd["serial"]')}"
NSER=$((SER + 1))
curl -s -o /dev/null -w '旧码读取          -> HTTP %{http_code}\n' "$BASE/feedback-snapshots/$CODE"
curl -s -o /dev/null -w '用旧序号再发布    -> HTTP %{http_code}\n' -X POST \
  "$BASE/papers/P1/feedback-snapshot" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"serial\": $SER}"

echo "== 9. 新序号下重新发布快照 (确认与评语随保留槽位沿用) -> 新版本与新码 =="
SNAP2=$(curl -s -X POST "$BASE/papers/P1/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $NSER}")
CODE2=$(echo "$SNAP2" | j 'd["access_code"]')
echo "新版本: version=$(echo "$SNAP2" | j 'd["version"]'), 新码=$CODE2"
curl -s -o /dev/null -w '旧码读取          -> HTTP %{http_code}\n' "$BASE/feedback-snapshots/$CODE"
curl -s -o /dev/null -w '新码读取          -> HTTP %{http_code}\n' "$BASE/feedback-snapshots/$CODE2"

echo "== 10. 会务方追溯该论文全部快照版本 (旧版仅供会务方, active 标出当前有效) =="
curl -s "$BASE/papers/P1/feedback-snapshots" -H "$ORG" \
  | j '[(s["version"], s["serial"], s["active"], s["access_code"]) for s in d["snapshots"]]'
