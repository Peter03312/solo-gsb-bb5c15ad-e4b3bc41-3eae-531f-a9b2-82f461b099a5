#!/usr/bin/env bash
# 本轮用法端到端示例: 删除状态对评审人操作的校验
#
#   发布 -> 两评审人确认/交评语 (会务方另对 R1 发起一条待更正请求) ->
#   会务方删除 PDL (当前发布版不重写, 历史槽位仍在 plan 中):
#     * 评审人确认/回避/正式评语/评语更正统一 404, 不落任何新决定/硬回避/评语/更正;
#     * 资料修订号保持删除后的值不被被拒请求推进;
#     * 取稿列表/本人评语/待更正任务不再呈现该槽位; 会务方也不能再发起更正;
#   同编号重新录入但"不"重新发布 -> 旧序号仍冻结, 继续 404;
#   重新录入 + 重新发布 (发布序号推进) -> 评审人按新序号重新确认并提交;
#   再次删除立即冻结新序号槽位; 删除凭据/历史评语全程可供会务方追溯。
#
# 前置: 建议对新库运行 (脚本自行建数据; 编号已存在可忽略 409/200)。
#   DB_PATH=./data/app.db ORGANIZER_KEY=dev-organizer-key python -m app.main
# 用法: ./examples/reviewer_deleted_paper.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
auth() { echo "X-Reviewer-Id: $1"; echo "X-Reviewer-Credential: $2"; }

PID=PDL
R1=RD1; C1=rd1-secret
R2=RD2; C2=rd2-secret

echo "== 1. 录入 2 名不同机构评审人 + 论文 $PID, 预演并发布 (serial=1) =="
for spec in "$R1 $C1 Inst-A" "$R2 $C2 Inst-B"; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"$3\", \"capacity\": 3, \"avoid_papers\": []}"
done
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d "{
  \"paper_id\": \"$PID\", \"manuscript\": \"$PID 匿名稿全文...\",
  \"topics\": [\"AI\"], \"institutions\": [\"Univ-X\"]}"
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
SER=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}" | j 'd["serial"]')
echo "    已发布: revision=$REV serial=$SER"

echo "== 2. 两评审人确认并交正式评语 =="
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/$PID/decision" \
  -H "X-Reviewer-Id: $R1" -H "X-Reviewer-Credential: $C1" \
  -H 'Content-Type: application/json' -d "{\"paper_id\":\"$PID\",\"decision\":\"confirm\"}"
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/$PID/decision" \
  -H "X-Reviewer-Id: $R2" -H "X-Reviewer-Credential: $C2" \
  -H 'Content-Type: application/json' -d "{\"paper_id\":\"$PID\",\"decision\":\"confirm\"}"
RECEIPT1=$(curl -s -X POST "$BASE/reviewer/assignments/$PID/review" \
  -H "X-Reviewer-Id: $R1" -H "X-Reviewer-Credential: $C1" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\":\"$PID\",\"serial\":$SER,\"score\":4,\"comment\":\"初版评语 R1\"}" \
  | j 'd["receipt"]')
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/$PID/review" \
  -H "X-Reviewer-Id: $R2" -H "X-Reviewer-Credential: $C2" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\":\"$PID\",\"serial\":$SER,\"score\":5,\"comment\":\"初版评语 R2\"}"
echo "    R1 评语收据: $RECEIPT1"

echo "== 3. 会务方对 R1 的评语发起一条待更正请求 (state=pending) =="
curl -s -X POST "$BASE/papers/$PID/review-corrections" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\":\"$PID\",\"serial\":$SER,\"reviewer_id\":\"$R1\",\"reason\":\"评语依据需核对\"}" \
  | j '{"correction_id": d["correction_id"], "state": d["state"]}'
echo "    评审人待更正任务数:"
curl -s "$BASE/reviewer/review-corrections" -H "X-Reviewer-Id: $R1" -H "X-Reviewer-Credential: $C1" \
  | j 'len(d["corrections"])'

echo "== 4. 会务方删除 $PID: 当前发布版与发布序号不变, 修订号 +1 =="
curl -s -X DELETE "$BASE/papers/$PID" -H "$ORG" \
  | j '{"revision": d["revision"], "frozen_serial": d["deletion"]["published_serial"], "frozen_plan": d["deletion"]["published_plan"]}'
REV_AFTER_DELETE=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
echo "    删除后资料修订号: $REV_AFTER_DELETE; 当前发布序号仍为: $(curl -s "$BASE/assignment" -H "$ORG" | j 'd["serial"]')"

echo "== 5. 删除后评审人写操作全部 404 (历史槽位仍在当前发布版中也拒绝) =="
echo "    确认 confirm        -> HTTP $(code -X POST "$BASE/reviewer/assignments/$PID/decision" \
  -H "X-Reviewer-Id: $R1" -H "X-Reviewer-Credential: $C1" \
  -H 'Content-Type: application/json' -d "{\"paper_id\":\"$PID\",\"decision\":\"confirm\"}") (404)"
echo "    回避 recuse         -> HTTP $(code -X POST "$BASE/reviewer/assignments/$PID/decision" \
  -H "X-Reviewer-Id: $R2" -H "X-Reviewer-Credential: $C2" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\":\"$PID\",\"decision\":\"recuse\",\"reason\":\"删除后才发现冲突\"}") (404)"
echo "    正式评语 (序号匹配) -> HTTP $(code -X POST "$BASE/reviewer/assignments/$PID/review" \
  -H "X-Reviewer-Id: $R1" -H "X-Reviewer-Credential: $C1" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\":\"$PID\",\"serial\":$SER,\"score\":2,\"comment\":\"删除后的新评语\"}") (404)"
echo "    评语更正提交        -> HTTP $(code -X POST "$BASE/reviewer/review-corrections/$PID" \
  -H "X-Reviewer-Id: $R1" -H "X-Reviewer-Credential: $C1" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\":\"$PID\",\"serial\":$SER,\"original_receipt\":\"$RECEIPT1\",\"score\":2,\"comment\":\"删除后的更正\"}") (404)"
echo "    会务方再发起更正    -> HTTP $(code -X POST "$BASE/papers/$PID/review-corrections" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\":\"$PID\",\"serial\":$SER,\"reviewer_id\":\"$R2\",\"reason\":\"删除后发起\"}") (404)"
echo "    -> 404 响应示例:"
curl -s -X POST "$BASE/reviewer/assignments/$PID/review" \
  -H "X-Reviewer-Id: $R1" -H "X-Reviewer-Credential: $C1" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\":\"$PID\",\"serial\":$SER,\"score\":2,\"comment\":\"删除后的新评语\"}" \
  | j 'd'

echo "== 6. 被拒请求不推进资料修订号, 读视图不再呈现删除槽位 =="
REV_NOW=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
echo "    当前修订号 $REV_NOW (应等于删除后 $REV_AFTER_DELETE)"
echo "    取稿列表中的 $PID: $(curl -s "$BASE/reviewer/assignments" -H "X-Reviewer-Id: $R1" -H "X-Reviewer-Credential: $C1" \
  | j '[a["paper_id"] for a in d["assignments"]]')"
echo "    states 中的 $PID: $(curl -s "$BASE/reviewer/assignments" -H "X-Reviewer-Id: $R1" -H "X-Reviewer-Credential: $C1" \
  | j 'd["states"].get("'"$PID"'")') (应为空 null)"
echo "    本人评语数: $(curl -s "$BASE/reviewer/reviews" -H "X-Reviewer-Id: $R1" -H "X-Reviewer-Credential: $C1" \
  | j 'len(d["reviews"])') (应为 0)"
echo "    待更正任务数: $(curl -s "$BASE/reviewer/review-corrections" -H "X-Reviewer-Id: $R1" -H "X-Reviewer-Credential: $C1" \
  | j 'len(d["corrections"])') (应为 0; 删除前的 pending 请求不再列出)"

echo "== 7. 同编号重新录入但不重新发布: 当前序号仍是删除冻结的旧序号, 继续 404 =="
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d "{
  \"paper_id\": \"$PID\", \"manuscript\": \"同编号重录稿\",
  \"topics\": [\"AI\"], \"institutions\": [\"Univ-Y\"]}"
echo "    重录未发布时确认    -> HTTP $(code -X POST "$BASE/reviewer/assignments/$PID/decision" \
  -H "X-Reviewer-Id: $R1" -H "X-Reviewer-Credential: $C1" \
  -H 'Content-Type: application/json' -d "{\"paper_id\":\"$PID\",\"decision\":\"confirm\"}") (404)"

echo "== 8. 重新发布分配 (发布序号推进): 评审人按新序号重新确认并提交 =="
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
SER2=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}" | j 'd["serial"]')
echo "    新发布序号: $SER2"
curl -s -X POST "$BASE/reviewer/assignments/$PID/decision" \
  -H "X-Reviewer-Id: $R1" -H "X-Reviewer-Credential: $C1" \
  -H 'Content-Type: application/json' -d "{\"paper_id\":\"$PID\",\"decision\":\"confirm\"}" \
  | j '{"state": d["state"], "serial": d["serial"], "changed": d["changed"]}'
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/$PID/decision" \
  -H "X-Reviewer-Id: $R2" -H "X-Reviewer-Credential: $C2" \
  -H 'Content-Type: application/json' -d "{\"paper_id\":\"$PID\",\"decision\":\"confirm\"}"
NEW_RECEIPT=$(curl -s -X POST "$BASE/reviewer/assignments/$PID/review" \
  -H "X-Reviewer-Id: $R1" -H "X-Reviewer-Credential: $C1" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\":\"$PID\",\"serial\":$SER2,\"score\":3,\"comment\":\"重录后重新提交的评语 R1\"}" \
  | j 'd["receipt"]')
echo "    新评语收据: $NEW_RECEIPT (旧收据 $RECEIPT1 不复活)"
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/$PID/review" \
  -H "X-Reviewer-Id: $R2" -H "X-Reviewer-Credential: $C2" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\":\"$PID\",\"serial\":$SER2,\"score\":4,\"comment\":\"重录后重新提交的评语 R2\"}"
echo "    取稿列表: $(curl -s "$BASE/reviewer/assignments" -H "X-Reviewer-Id: $R1" -H "X-Reviewer-Credential: $C1" \
  | j '{"serial": d["serial"], "papers": [a["paper_id"] for a in d["assignments"]], "states": d["states"]}')"

echo "== 9. 再次删除立即冻结序号 $SER2 的槽位; 删除凭据追加第二行 =="
curl -s -o /dev/null -X DELETE "$BASE/papers/$PID" -H "$ORG"
echo "    删除后确认          -> HTTP $(code -X POST "$BASE/reviewer/assignments/$PID/decision" \
  -H "X-Reviewer-Id: $R1" -H "X-Reviewer-Credential: $C1" \
  -H 'Content-Type: application/json' -d "{\"paper_id\":\"$PID\",\"decision\":\"confirm\"}") (404)"
echo "    删除后交评语        -> HTTP $(code -X POST "$BASE/reviewer/assignments/$PID/review" \
  -H "X-Reviewer-Id: $R1" -H "X-Reviewer-Credential: $C1" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\":\"$PID\",\"serial\":$SER2,\"score\":1,\"comment\":\"再次删除后的评语\"}") (404)"
curl -s "$BASE/papers/$PID/deletions" -H "$ORG" \
  | j '[{"id": x["id"], "state": x["state"], "published_serial": x["published_serial"], "published_plan": x["published_plan"], "deleted_at": x["deleted_at"]} for x in d["deletions"]]'

echo "== 10. 会务方追溯: 重新录入后可按编号查看两轮历史评语 (旧序号行保留) =="
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d "{
  \"paper_id\": \"$PID\", \"manuscript\": \"再次重录稿\",
  \"topics\": [\"AI\"], \"institutions\": [\"Univ-Z\"]}"
curl -s "$BASE/papers/$PID/reviews" -H "$ORG" \
  | j '{"serial": d["serial"], "current_slots": d["progress"]["slots"], "archived": [{"serial": a["serial"], "reviewer": a["reviewer_id"], "receipt": a["receipt"]} for a in d["archived_reviews"]]}'

echo "== 11. (补位路径) 删除 -> 重录 -> 补位发布同样不沿用旧确认/旧评语 =="
curl -s -o /dev/null -X DELETE "$BASE/papers/$PID" -H "$ORG"
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d "{
  \"paper_id\": \"$PID\", \"manuscript\": \"补位重录稿\",
  \"topics\": [\"AI\"], \"institutions\": [\"Univ-Z\"]}"
DRY=$(curl -s -X POST "$BASE/assignment/backfill/dry-run" -H "$ORG")
echo "    预演删除冻结槽位: $(echo "$DRY" | j '[{"paper_id": s["paper_id"], "reviewer": s["reviewer_id"]} for s in d["deleted_frozen_slots"]]')"
BSER3=$(curl -s -X POST "$BASE/assignment/backfill/publish" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $(echo "$DRY" | j 'd["revision"]'), \"base_serial\": $(echo "$DRY" | j 'd["serial"]')}")
echo "$BSER3" | j '{"serial": d["serial"], "carried_confirmations": d["carried_confirmations"], "carried_reviews": d["carried_reviews"], "released_deleted_confirmations": d["released_deleted_confirmations"]}'
SER3=$(echo "$BSER3" | j 'd["serial"]')
echo "    未确认直接交评语   -> HTTP $(code -X POST "$BASE/reviewer/assignments/$PID/review" \
  -H "X-Reviewer-Id: $R1" -H "X-Reviewer-Credential: $C1" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\":\"$PID\",\"serial\":$SER3,\"score\":5,\"comment\":\"未确认先交\"}") (409)"
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/$PID/decision" \
  -H "X-Reviewer-Id: $R1" -H "X-Reviewer-Credential: $C1" \
  -H 'Content-Type: application/json' -d "{\"paper_id\":\"$PID\",\"decision\":\"confirm\"}"
echo "    重新确认后交评语   -> HTTP $(code -X POST "$BASE/reviewer/assignments/$PID/review" \
  -H "X-Reviewer-Id: $R1" -H "X-Reviewer-Credential: $C1" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\":\"$PID\",\"serial\":$SER3,\"score\":5,\"comment\":\"补位重录后重新提交\"}") (200)"
