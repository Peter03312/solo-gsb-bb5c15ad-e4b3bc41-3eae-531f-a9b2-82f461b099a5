#!/usr/bin/env bash
# 本轮用法端到端示例: 作者对匿名反馈快照评语的异议
#   发布快照 -> 作者凭有效访问码对评语 1/2 提交异议 (随机查询凭据) ->
#   持凭据查状态 (即使快照码随后失效) -> 同理由幂等/异理由冲突 ->
#   会务方查看异议与冻结的原反馈 -> 驳回 (不影响快照) /
#   受理 (非空更正原因, 原子核对序号+收据, 走既有更正请求流程, 原访问码立即失效) ->
#   发布序号变化: 未处理异议自动过期, 历史供会务方追溯
# 前置: 建议对新库运行 (脚本会自行建数据; 若编号已存在可忽略 409)。
# 用法: ./examples/objections.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }

echo "== 1. 录入 4 名不同机构评审人 + 2 篇论文 =="
for spec in 'R1 r1-secret Inst-A' 'R2 r2-secret Inst-B' 'R3 r3-secret Inst-C' 'R4 r4-secret Inst-D'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"$3\", \"capacity\": 3, \"avoid_papers\": []}"
done
for P in P1 P2; do
  curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"paper_id\": \"$P\", \"manuscript\": \"$P 匿名稿全文...\", \"topics\": [\"AI\"],
    \"institutions\": [\"Univ-$P\"]}"
done

echo "== 2. 预演 + 发布, 两人确认并各交一份正式评语 =="
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
PUB=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}")
SER=$(echo "$PUB" | j 'd["serial"]')
echo "当前发布序号 serial=$SER"
for P in P1 P2; do
  RA=$(echo "$PUB" | j "d[\"plan\"][\"$P\"][0]")
  RB=$(echo "$PUB" | j "d[\"plan\"][\"$P\"][1]")
  for H in "$RA r$(echo "$RA" | sed 's/R//')-secret" "$RB r$(echo "$RB" | sed 's/R//')-secret"; do
    set -- $H
    curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/$P/decision" \
      -H "X-Reviewer-Id: $1" -H "X-Reviewer-Credential: $2" \
      -H 'Content-Type: application/json' -d "{\"paper_id\": \"$P\", \"decision\": \"confirm\"}"
  done
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/$P/review" \
    -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: r$(echo "$RA" | sed 's/R//')-secret" \
    -H 'Content-Type: application/json' \
    -d "{\"paper_id\": \"$P\", \"serial\": $SER, \"score\": 4, \"comment\": \"$P 评语一: 选题重要。\"}"
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/$P/review" \
    -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: r$(echo "$RB" | sed 's/R//')-secret" \
    -H 'Content-Type: application/json' \
    -d "{\"paper_id\": \"$P\", \"serial\": $SER, \"score\": 2, \"comment\": \"$P 评语二: 实验不足。\"}"
done

echo "== 3. 会务方为 P1/P2 发布匿名反馈快照 =="
SNAP1=$(curl -s -X POST "$BASE/papers/P1/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SER}")
CODE1=$(echo "$SNAP1" | j 'd["access_code"]')
SNAP2=$(curl -s -X POST "$BASE/papers/P2/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SER}")
CODE2=$(echo "$SNAP2" | j 'd["access_code"]')
echo "P1 access_code=$CODE1"; echo "P2 access_code=$CODE2"

echo "== 4. 作者凭 P1 当前有效访问码对标号 1 的评语提交非空异议 (无需任何凭据头) =="
OBJ=$(curl -s -X POST "$BASE/feedback-objections" -H 'Content-Type: application/json' -d "{
  \"access_code\": \"$CODE1\", \"label\": 1,
  \"reason\": \"评语一引用的对比实验并非本文方法, 存在事实错误, 请核查\"}")
echo "$OBJ"
TOKEN=$(echo "$OBJ" | j 'd["query_token"]')
echo "-> 随机查询凭据: $TOKEN (响应不含评审人编号/机构/收据)"

echo "== 4b. 同快照同标号: 同理由重试幂等 (changed=false, 原凭据); 异理由 409 =="
curl -s -X POST "$BASE/feedback-objections" -H 'Content-Type: application/json' -d "{
  \"access_code\": \"$CODE1\", \"label\": 1,
  \"reason\": \"  评语一引用的对比实验并非本文方法, 存在事实错误, 请核查 \"}" \
  | j '(d["changed"], d["query_token"])'
curl -s -o /dev/null -w '异理由提交       -> HTTP %{http_code} (409 冲突, 已存异议不变)\n' \
  -X POST "$BASE/feedback-objections" -H 'Content-Type: application/json' -d "{
  \"access_code\": \"$CODE1\", \"label\": 1, \"reason\": \"另一个完全不同的理由\"}"
curl -s -o /dev/null -w '空白理由         -> HTTP %{http_code} (422)\n' \
  -X POST "$BASE/feedback-objections" -H 'Content-Type: application/json' -d "{
  \"access_code\": \"$CODE1\", \"label\": 1, \"reason\": \"   \"}"
curl -s -o /dev/null -w '无效/已失效码    -> HTTP %{http_code} (统一 404, 不透露论文是否存在)\n' \
  -X POST "$BASE/feedback-objections" -H 'Content-Type: application/json' -d "{
  \"access_code\": \"fbk-deadbeef\", \"label\": 1, \"reason\": \"x\"}"

echo "== 5. 作者持查询凭据查看处理状态 (无需任何其他凭据) =="
curl -s "$BASE/feedback-objections/$TOKEN"; echo

echo "== 6. 会务方查看 P1 异议与冻结的原反馈 (按匿名标号定位评审人, 含目标收据) =="
curl -s "$BASE/papers/P1/objections" -H "$ORG" | python3 -m json.tool

echo "== 7. 会务方驳回 P1 异议 (可附驳回说明; 驳回不影响快照, 访问码仍可读) =="
OID=$(echo "$SNAP1" >/dev/null; curl -s "$BASE/papers/P1/objections" -H "$ORG" | j 'd["objections"][0]["objection_id"]')
curl -s -X POST "$BASE/objections/$OID/decision" -H "$ORG" -H 'Content-Type: application/json' \
  -d '{"decision": "reject", "note": "经评审小组复核, 原评语引用准确, 异议不成立"}' \
  | j '(d["objection"]["state"], d["objection"]["reject_note"])'
curl -s -o /dev/null -w '驳回后快照码仍可读 -> HTTP %{http_code}\n' "$BASE/feedback-snapshots/$CODE1"
echo "-> 作者凭据可见驳回状态与说明:"
curl -s "$BASE/feedback-objections/$TOKEN" \
  | j '(d["state"], d["resolution"]["note"])'
curl -s -o /dev/null -w '重复处理已驳回异议 -> HTTP %{http_code} (409)\n' \
  -X POST "$BASE/objections/$OID/decision" -H "$ORG" -H 'Content-Type: application/json' \
  -d '{"decision": "reject"}'

echo "== 8. P2: 作者对标号 2 提交异议, 会务方受理 (非空更正原因) =="
OBJ2=$(curl -s -X POST "$BASE/feedback-objections" -H 'Content-Type: application/json' -d "{
  \"access_code\": \"$CODE2\", \"label\": 2,
  \"reason\": \"评语二所称缺陷在修订稿第 4 节已有回应, 请重新评估\"}")
TOKEN2=$(echo "$OBJ2" | j 'd["query_token"]')
OID2=$(curl -s "$BASE/papers/P2/objections" -H "$ORG" | j 'd["objections"][0]["objection_id"]')
ACC=$(curl -s -X POST "$BASE/objections/$OID2/decision" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d '{"decision": "accept", "reason": "作者异议成立: 请核对修订稿第 4 节并更正评分与评语"}')
echo "$ACC" | j '(d["objection"]["state"], d["objection"]["reviewer_id"],
                  d["objection"]["target_receipt"], d["invalidated_snapshots"])'
echo "-> 受理走既有更正请求流程: 原访问码立即失效"
curl -s -o /dev/null -w '   P2 旧访问码读取 -> HTTP %{http_code} (404)\n' "$BASE/feedback-snapshots/$CODE2"
curl -s -o /dev/null -w '   凭旧码再提异议   -> HTTP %{http_code} (已失效码不得提交新异议)\n' \
  -X POST "$BASE/feedback-objections" -H 'Content-Type: application/json' \
  -d "{\"access_code\": \"$CODE2\", \"label\": 1, \"reason\": \"旧码新异议\"}"
echo "-> 作者凭据仍可查状态 (与快照码是否失效无关), 状态为 accepted:"
curl -s "$BASE/feedback-objections/$TOKEN2" | j '(d["state"], d["resolution"]["reason"])'

echo "== 8b. 受理校验: 序号变化/收据变化/已有待更正请求 -> 409 且不改异议状态 (空原因 422 见第 9 步) =="
# 已受理的异议再次处理 -> 409 终态冲突
curl -s -o /dev/null -w '   重复处理已受理异议 -> HTTP %{http_code} (409)\n' \
  -X POST "$BASE/objections/$OID2/decision" -H "$ORG" -H 'Content-Type: application/json' \
  -d '{"decision": "reject"}'

echo "== 9. 被定位的评审人按既有流程查看待更正任务并提交更正 =="
R_P2B=$(echo "$PUB" | j 'd["plan"]["P2"][1]')
RC_P2B="r$(echo "$R_P2B" | sed 's/R//')-secret"
TASK=$(curl -s "$BASE/reviewer/review-corrections" \
  -H "X-Reviewer-Id: $R_P2B" -H "X-Reviewer-Credential: $RC_P2B")
ORIG=$(echo "$TASK" | j 'd["corrections"][0]["original"]["receipt"]')
echo "评审人 $R_P2B 的待更正任务原收据: $ORIG"
curl -s -o /dev/null -X POST "$BASE/reviewer/review-corrections/P2" \
  -H "X-Reviewer-Id: $R_P2B" -H "X-Reviewer-Credential: $RC_P2B" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P2\", \"serial\": $SER, \"original_receipt\": \"$ORIG\",
       \"score\": 4, \"comment\": \"复核修订稿第 4 节: 缺陷已有回应, 调整评价。\"}"
echo "-> 更正完成后, 会务方按现有完整性规则重新发布快照 (新版本+新访问码), 旧码保持失效"
SNAP2B=$(curl -s -X POST "$BASE/papers/P2/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SER}")
echo "$SNAP2B" | j '(d["version"], d["changed"])'
CODE2B=$(echo "$SNAP2B" | j 'd["access_code"]')
curl -s "$BASE/feedback-snapshots/$CODE2B"; echo

echo "== 9b. 凭新快照码对 P2 标号 1 再提一条 pending 异议, 演示受理空更正原因 -> 422 且状态不变 =="
OID2B=$(curl -s -X POST "$BASE/feedback-objections" -H 'Content-Type: application/json' \
  -d "{\"access_code\": \"$CODE2B\", \"label\": 1, \"reason\": \"P2 对评语一的异议\"}" \
  | j 'd["query_token"]' >/dev/null; \
  curl -s "$BASE/papers/P2/objections?state=pending" -H "$ORG" | j 'd["objections"][0]["objection_id"]')
curl -s -o /dev/null -w '   受理空更正原因   -> HTTP %{http_code} (422, 异议保持 pending)\n' \
  -X POST "$BASE/objections/$OID2B/decision" -H "$ORG" -H 'Content-Type: application/json' \
  -d '{"decision": "accept", "reason": "   "}'
curl -s "$BASE/papers/P2/objections?state=pending" -H "$ORG" \
  | j '[(o["label"], o["state"]) for o in d["objections"]]'

echo "== 10. 会务方全局查看异议 (?state= 可按状态过滤) =="
curl -s "$BASE/objections" -H "$ORG" \
  | j '[(o["paper_id"], o["label"], o["state"]) for o in d["objections"]]'
curl -s "$BASE/objections?state=rejected" -H "$ORG" \
  | j '[(o["paper_id"], o["state"]) for o in d["objections"]]'

echo "== 11. 发布序号变化: 未处理异议随补位发布在同一事务内标记过期 =="
# P1 再发一条 pending 异议 (标号 2), 随后补位发布推进序号
curl -s -o /dev/null -X POST "$BASE/feedback-objections" -H 'Content-Type: application/json' -d "{
  \"access_code\": \"$CODE1\", \"label\": 2, \"reason\": \"P1 对评语二的新异议\"}"
TOKEN3=$(curl -s -X POST "$BASE/feedback-objections" -H 'Content-Type: application/json' -d "{
  \"access_code\": \"$CODE1\", \"label\": 2, \"reason\": \"P1 对评语二的新异议\"}" | j 'd["query_token"]')
DRY=$(curl -s -X POST "$BASE/assignment/backfill/dry-run" -H "$ORG")
BP=$(curl -s -X POST "$BASE/assignment/backfill/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $(echo "$DRY" | j 'd["revision"]'), \"base_serial\": $(echo "$DRY" | j 'd["serial"]')}")
NSER=$(echo "$BP" | j 'd["serial"]')
echo "补位发布后 serial=$NSER"
curl -s "$BASE/feedback-objections/$TOKEN3" | j 'd["state"]'
echo "-> 对已过期异议尝试受理 -> 409, 状态保持 expired; 历史供会务方追溯:"
OID3=$(curl -s "$BASE/papers/P1/objections?state=expired" -H "$ORG" | j 'd["objections"][0]["objection_id"]')
curl -s -o /dev/null -w '   受理已过期异议   -> HTTP %{http_code}\n' \
  -X POST "$BASE/objections/$OID3/decision" -H "$ORG" -H 'Content-Type: application/json' \
  -d '{"decision": "accept", "reason": "试图受理已过期异议"}'
curl -s "$BASE/papers/P1/objections" -H "$ORG" \
  | j '[(o["label"], o["state"], o["frozen_feedback"]["score"]) for o in d["objections"]]'
echo "完成。"
