#!/usr/bin/env bash
# 本轮用法端到端示例: 当前发布版的统一 UTC 评审截止时刻
#   录入/发布 -> 一确认未交/一未确认 -> 设置截止 (携带 资料修订号 + 发布序号) ->
#   查看各槽位状态 -> 截止到达后: 逾期槽位不能再确认/交评语, 已交评语与收据仍有效 ->
#   补位预演释放逾期槽位 (即使已确认), 且不再把该稿分给该逾期评审人 -> 补位发布 ->
#   旧版截止不约束新发布版 (新版可再设自己的截止)
# 截止时刻必须显式携带时区 (Z / +00:00 / +08:00), 服务端归一化为 UTC, 且晚于当前时刻。
# 建议对新库运行: ./examples/deadlines.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }

echo "== 1. 录入 4 名不同机构评审人 (容量 3) 与论文 P1 =="
for spec in 'R1 r1-secret Inst-A' 'R2 r2-secret Inst-B' 'R3 r3-secret Inst-C' 'R4 r4-secret Inst-D'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"$3\", \"capacity\": 3, \"avoid_papers\": []}"
done
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 匿名稿全文...", "topics": ["AI"], "institutions": ["Univ-X"]}'

echo "== 2. 普通预演并发布, 记下 revision / serial =="
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
SERIAL=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}" | j 'd["serial"]')
PLAN=$(curl -s "$BASE/assignment" -H "$ORG")
R1=$(echo "$PLAN" | j "d[\"plan\"][\"P1\"][0]")
R2=$(echo "$PLAN" | j "d[\"plan\"][\"P1\"][1]")
echo "    P1 槽位: $R1 / $R2; revision=$REV serial=$SERIAL"

echo "== 3. R1 截止前确认并交正式评语; R2 仅确认未交; 确认会推进资料修订号 =="
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $R1" -H "X-Reviewer-Credential: r1-secret" \
  -H 'Content-Type: application/json' -d '{"paper_id":"P1","decision":"confirm"}'
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $R1" -H "X-Reviewer-Credential: r1-secret" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\":\"P1\",\"serial\":$SERIAL,\"score\":4,\"comment\":\"R1 的正式评语\"}"
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $R2" -H "X-Reviewer-Credential: r2-secret" \
  -H 'Content-Type: application/json' -d '{"paper_id":"P1","decision":"confirm"}'

echo "== 4. 会务方设置统一 UTC 评审截止 (取最新 revision + 当前 serial) =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
# 演示时刻: 当前 UTC 时刻 +2 分钟 (真实使用可直接给 2026-11-01T12:00:00Z / +08:00)
DEADLINE=$(python3 -c 'from datetime import datetime,timezone,timedelta; print((datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat().replace("+00:00","Z"))')
curl -s -X POST "$BASE/review-deadline" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"base_serial\": $SERIAL, \"deadline_at\": \"$DEADLINE\"}" \
  | j "{'changed': d['changed'], 'serial': d['serial'], 'deadline_at': d['review_deadline']['deadline_at']}"

echo "== 5. 同版同值重试 -> changed=false (不产生新变更); 异值 -> 409; 版本不符 -> 409 =="
curl -s -X POST "$BASE/review-deadline" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"base_serial\": $SERIAL, \"deadline_at\": \"$DEADLINE\"}" \
  | j "{'changed': d['changed']}"
OTHER=$(python3 -c 'from datetime import datetime,timezone,timedelta; print((datetime.now(timezone.utc)+timedelta(days=1)).isoformat().replace("+00:00","Z"))')
curl -s -o /dev/null -w '同版异值           -> HTTP %{http_code}\n' -X POST "$BASE/review-deadline" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"base_serial\": $SERIAL, \"deadline_at\": \"$OTHER\"}"
curl -s -o /dev/null -w '修订号过期         -> HTTP %{http_code}\n' -X POST "$BASE/review-deadline" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"base_serial\": $((SERIAL+1)), \"deadline_at\": \"$OTHER\"}"
curl -s -o /dev/null -w '无时区/过去时刻    -> HTTP 422\n' -X POST "$BASE/review-deadline" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"base_serial\": $SERIAL, \"deadline_at\": \"2026-10-04T09:00:00\"}"

echo "== 6. 查看各槽位状态 (未确认/已确认未交/已交/已回避/逾期) 与汇总 =="
curl -s "$BASE/review-deadline" -H "$ORG" \
  | j "{'serial': d['serial'], 'deadline_expired': d['deadline_expired'], 'summary': d['summary'], 'slots': [(s['paper_id'], s['reviewer_id'], s['state']) for s in d['slots']]}"

echo "== 7. 等待截止到达 (约 130 秒; 可手动把截止设近后观察) =="
sleep 130

echo "== 8. 截止到达后: R2 已确认未交 -> 逾期, 不能再交评语; 未确认者也不能再确认 =="
curl -s -o /dev/null -w '逾期槽位交评语     -> HTTP %{http_code} (409)\n' -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $R2" -H "X-Reviewer-Credential: r2-secret" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\":\"P1\",\"serial\":$SERIAL,\"score\":5,\"comment\":\"赶在最后补交\"}"
echo "    R1 已交评语保持有效: 相同内容重试仍幂等返回原收据"
curl -s -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $R1" -H "X-Reviewer-Credential: r1-secret" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\":\"P1\",\"serial\":$SERIAL,\"score\":4,\"comment\":\"R1 的正式评语\"}" \
  | j "{'changed': d['changed'], 'receipt': d['receipt']}"
curl -s "$BASE/review-deadline" -H "$ORG" \
  | j "{'deadline_expired': d['deadline_expired'], 'summary': d['summary']}"

echo "== 9. 补位预演: R1 (已交) 槽位固定; R2 逾期槽位即使已确认也释放, 且不得再分 P1 给 R2 =="
DRY=$(curl -s -X POST "$BASE/assignment/backfill/dry-run" -H "$ORG")
echo "$DRY" | j "{'feasible': d['feasible'], 'fixed': d['fixed'], 'plan': d['plan'], 'fixed_problems': d['fixed_problems'], 'overdue_slots': d['overdue_slots']}"

echo "== 10. 补位发布 (双版本复核): 沿用 R1 确认与评语; 释放 R2 逾期确认; 新版不再含 R2 =="
DRY_REV=$(echo "$DRY" | j 'd["revision"]'); DRY_SER=$(echo "$DRY" | j 'd["serial"]')
curl -s -X POST "$BASE/assignment/backfill/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $DRY_REV, \"base_serial\": $DRY_SER}" \
  | j "{'serial': d['serial'], 'carried_confirmations': d['carried_confirmations'], 'carried_reviews': d['carried_reviews'], 'released_overdue_confirmations': d['released_overdue_confirmations'], 'plan': d['plan']}"

echo "== 11. 旧版截止不约束新发布版: 当前版本无截止, 可独立再设 =="
curl -s "$BASE/review-deadline" -H "$ORG" | j "{'review_deadline': d['review_deadline'], 'deadline_expired': d['deadline_expired']}"
NEW_REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
NEW_SER=$(curl -s "$BASE/assignment" -H "$ORG" | j 'd["serial"]')
NEW_DL=$(python3 -c 'from datetime import datetime,timezone,timedelta; print((datetime.now(timezone.utc)+timedelta(days=7)).isoformat().replace("+00:00","Z"))')
curl -s -X POST "$BASE/review-deadline" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $NEW_REV, \"base_serial\": $NEW_SER, \"deadline_at\": \"$NEW_DL\"}" \
  | j "{'changed': d['changed'], 'serial': d['serial']}"
