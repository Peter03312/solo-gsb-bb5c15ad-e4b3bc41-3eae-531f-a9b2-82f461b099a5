#!/usr/bin/env bash
# 本轮用法端到端示例: 已交评语的更正
#   发布 -> 两人确认并各交一份正式评语 -> 会务方发布反馈快照 ->
#   会务方发起更正请求 (既有访问码立即失效) -> 评审人查看待更正任务 ->
#   评审人凭发布序号 + 原评语收据提交更正 (新收据) -> 同内容重试幂等 / 异内容冲突 ->
#   会务方按现有完整性规则重新发布快照 (新版本 + 新访问码) ->
#   补位发布: 保留槽位沿用更正后的评语, 待更正请求随序号变化失效
# 前置: 建议对新库运行 (脚本会自行建数据; 若编号已存在可忽略 409)。
# 用法: ./examples/corrections.sh [BASE_URL]
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
RVA=$(curl -s -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"score\": 4, \"comment\": \"选题重要, 建议补充消融实验。\"}")
RA_RCT=$(echo "$RVA" | j 'd["receipt"]')
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"score\": 2, \"comment\": \"实验设计有漏洞, 结论偏强。\"}"

echo "== 3. 会务方发布反馈快照, 持码者可读 =="
SNAP=$(curl -s -X POST "$BASE/papers/P1/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SER}")
CODE=$(echo "$SNAP" | j 'd["access_code"]')
echo "version=$(echo "$SNAP" | j 'd["version"]') access_code=$CODE"
curl -s "$BASE/feedback-snapshots/$CODE" | j 'd["paper_id"]' > /dev/null && echo "持码读取: OK"

echo "== 4. 会务方发现 $RA 的已交评语需要更正, 发起更正请求 (当前序号 + 论文 + 评审人 + 非空原因) =="
REQ=$(curl -s -X POST "$BASE/papers/P1/review-corrections" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"reviewer_id\": \"$RA\", \"reason\": \"评分与评语内容明显不符, 请更正\"}")
echo "$REQ"
echo "-> 发起即令既有访问码失效 (invalidated_snapshots=$(echo "$REQ" | j 'd["invalidated_snapshots"]')):"
curl -s -o /dev/null -w "   旧访问码读取 -> HTTP %{http_code} (404, 不透露论文是否存在)\n" \
  "$BASE/feedback-snapshots/$CODE"

echo "== 4b. 同原因重复发起 -> 幂等 changed=false; 异原因 -> 409; 旧序号/已移出槽位 -> 拒绝 =="
curl -s -X POST "$BASE/papers/P1/review-corrections" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"reviewer_id\": \"$RA\", \"reason\": \"评分与评语内容明显不符, 请更正\"}" \
  | j 'd["changed"]'
curl -s -o /dev/null -w '异原因重复发起  -> HTTP %{http_code}\n' -X POST \
  "$BASE/papers/P1/review-corrections" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"reviewer_id\": \"$RA\", \"reason\": \"另一个原因\"}"
curl -s -o /dev/null -w '过期发布序号    -> HTTP %{http_code}\n' -X POST \
  "$BASE/papers/P1/review-corrections" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $((SER + 1)), \"reviewer_id\": \"$RA\", \"reason\": \"x\"}"
curl -s -o /dev/null -w '未分配评审人    -> HTTP %{http_code}\n' -X POST \
  "$BASE/papers/P1/review-corrections" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"reviewer_id\": \"R4\", \"reason\": \"x\"}"

echo "== 5. 待更正期间按原序号重新发布快照 -> 409 (须待更正完成) =="
curl -s -o /dev/null -w '待更正期间发快照 -> HTTP %{http_code}\n' -X POST \
  "$BASE/papers/P1/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SER}"

echo "== 6. $RA 凭本人凭据查看待更正任务 (含更正原因与原评语收据) =="
curl -s "$BASE/reviewer/review-corrections" -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA"; echo
echo "   ($RB 的待更正列表为空, 看不到他人任务:)"
curl -s "$BASE/reviewer/review-corrections" -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB" \
  | j 'd["corrections"]'

echo "== 7. $RA 以请求对应的发布序号 + 原评语收据提交更正 (新 1~5 整数评分 + 非空评语) =="
COR=$(curl -s -X POST "$BASE/reviewer/review-corrections/P1" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"original_receipt\": \"$RA_RCT\", \"score\": 5, \"comment\": \"更正: 选题重要, 方法扎实, 建议接收。\"}")
echo "$COR"
NEW_RCT=$(echo "$COR" | j 'd["receipt"]')
echo "-> 新收据 $NEW_RCT (原收据 $RA_RCT 的评语冻结保留供会务方追溯)"

echo "== 7b. 同内容重试 -> 返回更正收据 changed=false; 异内容 -> 409 冲突 =="
curl -s -X POST "$BASE/reviewer/review-corrections/P1" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"original_receipt\": \"$RA_RCT\", \"score\": 5, \"comment\": \"更正: 选题重要, 方法扎实, 建议接收。\"}" \
  | j '(d["changed"], d["receipt"])'
curl -s -o /dev/null -w '异内容更正重试  -> HTTP %{http_code}\n' -X POST \
  "$BASE/reviewer/review-corrections/P1" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"original_receipt\": \"$RA_RCT\", \"score\": 3, \"comment\": \"又想改\"}"

echo "== 8. 会务方按论文追溯: 槽位评语已更新, 原评语冻结在 corrections 中 =="
curl -s "$BASE/papers/P1/reviews" -H "$ORG" | j 'd["corrections"]'

echo "== 9. 更正完成后, 会务方按现有完整性规则重新发布快照 -> 新版本 + 新访问码 =="
SNAP2=$(curl -s -X POST "$BASE/papers/P1/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SER}")
echo "$SNAP2"
CODE2=$(echo "$SNAP2" | j 'd["access_code"]')
echo "-> 持新码读取 (含更正后的评语):"
curl -s "$BASE/feedback-snapshots/$CODE2"; echo
curl -s -o /dev/null -w "-> 旧码仍失效 -> HTTP %{http_code}\n" "$BASE/feedback-snapshots/$CODE"
echo "-> 会务方追溯全部版本:"
curl -s "$BASE/papers/P1/feedback-snapshots" -H "$ORG" | j '[(s["version"], s["active"]) for s in d["snapshots"]]'

echo "== 10. 会务方对 $RB 也发起更正请求 (保持待更正, 不提交) =="
curl -s -X POST "$BASE/papers/P1/review-corrections" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"reviewer_id\": \"$RB\", \"reason\": \"结论措辞请斟酌\"}" \
  | j '(d["state"], d["reviewer_id"])'
curl -s "$BASE/reviewer/review-corrections" -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB" \
  | j 'len(d["corrections"])'

echo "== 11. $RB 声明回避 -> 补位发布: 保留槽位沿用更正后的评语 (同新收据) =="
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB" \
  -H 'Content-Type: application/json' \
  -d '{"paper_id": "P1", "decision": "recuse", "reason": "近三年与第二作者合作"}'
DRY=$(curl -s -X POST "$BASE/assignment/backfill/dry-run" -H "$ORG")
BP=$(curl -s -X POST "$BASE/assignment/backfill/publish" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $(echo "$DRY" | j 'd["revision"]'), \"base_serial\": $(echo "$DRY" | j 'd["serial"]')}")
NSER=$(echo "$BP" | j 'd["serial"]')
echo "补位发布: serial=$NSER carried_reviews=$(echo "$BP" | j 'd["carried_reviews"]')"
echo "-> $RA 的更正后评语在新序号下沿用 (收据不变):"
curl -s "$BASE/reviewer/reviews" -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  | j '[(r["receipt"], r["comment"]) for r in d["reviews"]]'

echo "== 12. 发布序号已变化: 旧序号下的待更正请求失效 =="
echo "-> $RB 的待更正任务列表已为空:"
curl -s "$BASE/reviewer/review-corrections" -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB" \
  | j 'd["corrections"]'
RB_RCT=$(curl -s "$BASE/papers/P1/reviews" -H "$ORG" \
  | j '[c["original"]["receipt"] for c in d["corrections"] if c["reviewer_id"] == "'"$RB"'"][0]')
curl -s -o /dev/null -w '-> 按旧序号提交更正 -> HTTP %{http_code} (409, 待更正请求已失效)\n' -X POST \
  "$BASE/reviewer/review-corrections/P1" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"original_receipt\": \"$RB_RCT\", \"score\": 3, \"comment\": \"迟到的更正\"}"
echo "-> 会务方仍可在 corrections 中追溯该待更正请求 (停留于旧序号):"
curl -s "$BASE/papers/P1/reviews" -H "$ORG" \
  | j '[(c["serial"], c["reviewer_id"], c["state"]) for c in d["corrections"]]'
echo "完成。"
