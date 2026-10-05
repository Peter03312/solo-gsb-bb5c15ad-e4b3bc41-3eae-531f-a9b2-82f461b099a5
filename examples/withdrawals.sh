#!/usr/bin/env bash
# 本轮用法端到端示例: 会务方撤回现存论文
#   录入 -> 预演/发布 -> 评审人确认/交评语 -> 发布反馈快照并收到作者异议 ->
#   会务方携带 论文编号 + 当前资料修订号 + 非空原因撤回 (同一事务生效):
#     评审人立即不能取稿/确认/回避/交评语/更正, 该稿反馈访问码失效,
#     待处理异议变为 expired (作者原查询凭据仍可查状态), 当前发布版/发布序号不变,
#     撤回推进资料修订号; 已处理异议与评语/快照/分配历史保留供会务方追溯 ->
#   匹配当前修订号的同原因重试幂等 (changed=false, 不推进修订号), 异原因 409,
#   版本不符 409, 未知论文 404, 空原因 422, 同编号不能重新录入 ->
#   后续普通分配/补位跳过撤回稿 (其锁定不阻塞其他论文), 其他论文确认与评语规则不变
# 建议对新库运行: ./examples/withdrawals.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }

echo "== 1. 录入 6 名不同机构评审人 + 2 篇论文 =="
for i in 1 2 3 4 5 6; do
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"R$i\", \"credential\": \"r$i-secret\", \"topics\": [\"AI\"],
    \"institution\": \"Inst-$i\", \"capacity\": 3, \"avoid_papers\": []}"
done
for spec in 'P1 Univ-X' 'P2 Univ-Y'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"paper_id\": \"$1\", \"manuscript\": \"$1 匿名稿全文...\", \"topics\": [\"AI\"],
    \"institutions\": [\"$2\"]}"
done

echo "== 2. 普通预演 + 发布 =="
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
PUB=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}")
SERIAL=$(echo "$PUB" | j 'd["serial"]')
echo "$PUB" | j "{'serial': d['serial'], 'plan': d['plan']}"
cred() { echo "r$(echo "$1" | sed 's/R//')-secret"; }
A1=$(echo "$PUB" | j 'd["plan"]["P1"][0]'); B1=$(echo "$PUB" | j 'd["plan"]["P1"][1]')

echo "== 3. P1 两名评审人确认并各交一份正式评语 =="
for rid in "$A1" "$B1"; do
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
    -H "X-Reviewer-Id: $rid" -H "X-Reviewer-Credential: $(cred "$rid")" \
    -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/review" \
    -H "X-Reviewer-Id: $rid" -H "X-Reviewer-Credential: $(cred "$rid")" \
    -H 'Content-Type: application/json' \
    -d "{\"paper_id\": \"P1\", \"serial\": $SERIAL, \"score\": 4, \"comment\": \"$rid 对 P1 的评语\"}"
done

echo "== 4. 会务方发布 P1 反馈快照; 作者持访问码提交一条 (待处理) 异议 =="
CODE=$(curl -s -X POST "$BASE/papers/P1/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SERIAL}" \
  | j 'd["access_code"]')
echo "P1 快照访问码: $CODE"
TOKEN=$(curl -s -X POST "$BASE/feedback-objections" -H 'Content-Type: application/json' -d "{
    \"access_code\": \"$CODE\", \"label\": 1, \"reason\": \"评语一存在事实错误, 请核查\"}" \
  | j 'd["query_token"]')
echo "作者异议查询凭据: $TOKEN"

echo "== 5. 会务方撤回 P1: 携带当前资料修订号 + 非空原因 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/papers/P1/withdrawal" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"base_revision\": $REV, \"reason\": \"作者声明一稿多投, 经编委会确认撤稿\"}" \
  | j "{'ok': d['ok'], 'state': d['state'], 'changed': d['changed'], 'revision': d['revision'],
        'reason': d['withdrawal']['reason'], 'serial_at_withdrawal': d['withdrawal']['published_serial'],
        'slots_at_withdrawal': d['withdrawal']['published_plan'],
        'invalidated_snapshots': d['invalidated_snapshots'], 'expired_objections': d['expired_objections']}"

echo "== 5b. 匹配当前修订号的同原因重试 -> 原记录 changed=false, 不推进修订号 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/papers/P1/withdrawal" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"base_revision\": $REV, \"reason\": \"作者声明一稿多投, 经编委会确认撤稿\"}" \
  | j "{'changed': d['changed'], 'revision': d['revision'], 'invalidated': d['invalidated_snapshots'], 'expired': d['expired_objections']}"
echo "-- 异原因 409 / 版本不符 409 / 未知论文 404 / 空原因 422 --"
curl -s -o /dev/null -w '异原因           -> HTTP %{http_code}\n' -X POST "$BASE/papers/P1/withdrawal" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"base_revision\": $REV, \"reason\": \"另一个撤回原因\"}"
curl -s -o /dev/null -w '过期修订号       -> HTTP %{http_code}\n' -X POST "$BASE/papers/P1/withdrawal" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d '{"paper_id": "P1", "base_revision": 0, "reason": "x"}'
curl -s -o /dev/null -w '未知论文         -> HTTP %{http_code}\n' -X POST "$BASE/papers/GHOST/withdrawal" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"GHOST\", \"base_revision\": $REV, \"reason\": \"x\"}"
curl -s -o /dev/null -w '空原因           -> HTTP %{http_code}\n' -X POST "$BASE/papers/P1/withdrawal" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"base_revision\": $REV, \"reason\": \"   \"}"
curl -s -o /dev/null -w '路径体不一致     -> HTTP %{http_code}\n' -X POST "$BASE/papers/P1/withdrawal" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P2\", \"base_revision\": $REV, \"reason\": \"x\"}"

echo "== 6. 撤回即时生效 (评审人侧) =="
curl -s -o /dev/null -w '取稿 (A1)        -> HTTP %{http_code} (200 但列表不含 P1)\n' \
  "$BASE/reviewer/assignments" -H "X-Reviewer-Id: $A1" -H "X-Reviewer-Credential: $(cred "$A1")"
curl -s "$BASE/reviewer/assignments" -H "X-Reviewer-Id: $A1" -H "X-Reviewer-Credential: $(cred "$A1")" \
  | j "{'assignments': [a['paper_id'] for a in d['assignments']]}"
curl -s -o /dev/null -w 'B1 确认          -> HTTP %{http_code}\n' -X POST \
  "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $B1" -H "X-Reviewer-Credential: $(cred "$B1")" \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'
curl -s -o /dev/null -w 'B1 回避          -> HTTP %{http_code}\n' -X POST \
  "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $B1" -H "X-Reviewer-Credential: $(cred "$B1")" \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "recuse", "reason": "x"}'
curl -s -o /dev/null -w 'A1 交评语        -> HTTP %{http_code}\n' -X POST \
  "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $A1" -H "X-Reviewer-Credential: $(cred "$A1")" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SERIAL, \"score\": 3, \"comment\": \"撤稿后评语\"}"

echo "== 7. 反馈访问码立即失效 (持码者统一 404, 不透露论文是否存在); 待处理异议变 expired =="
curl -s -o /dev/null -w '持码读取快照     -> HTTP %{http_code}\n' "$BASE/feedback-snapshots/$CODE"
curl -s -o /dev/null -w '失效码再提异议   -> HTTP %{http_code}\n' -X POST "$BASE/feedback-objections" \
  -H 'Content-Type: application/json' \
  -d "{\"access_code\": \"$CODE\", \"label\": 2, \"reason\": \"撤稿后新异议\"}"
curl -s "$BASE/feedback-objections/$TOKEN" \
  | j "{'paper_id': d['paper_id'], 'state': d['state'], 'resolved_at': d['resolved_at']}"

echo "== 8. 会务方追溯: 当前发布版与发布序号不变 (P1 带 withdrawn 标记), 评语/快照/异议保留 =="
curl -s "$BASE/assignment" -H "$ORG" \
  | j "{'serial': d['serial'], 'withdrawn_papers': d['withdrawn_papers'],
        'P1_slots': d['papers']['P1']['slots'], 'P1_withdrawn': d['papers']['P1']['withdrawn']}"
curl -s "$BASE/papers/P1/reviews" -H "$ORG" \
  | j "{'withdrawn': d['withdrawn'], 'slots': [{'r': s['reviewer_id'], 'state': s['state'], 'submitted': s['submitted']} for s in d['slots']]}"
curl -s "$BASE/papers/P1/withdrawal" -H "$ORG" | j "d['withdrawal']"
curl -s "$BASE/papers/P1/feedback-snapshots" -H "$ORG" \
  | j "{'versions': len(d['snapshots']), 'active': [v['active'] for v in d['snapshots']]}"
curl -s "$BASE/papers/P1/objections" -H "$ORG" \
  | j "[{'label': o['label'], 'state': o['state']} for o in d['objections']]"

echo "== 9. 同编号不能重新录入; 撤回稿不可再更新/删除; 不可发起更正/发布快照 =="
curl -s -o /dev/null -w '同编号重新录入   -> HTTP %{http_code}\n' -X POST "$BASE/papers" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d '{"paper_id": "P1", "manuscript": "new", "topics": ["AI"], "institutions": ["Univ-Z"]}'
curl -s -o /dev/null -w '更新撤回稿       -> HTTP %{http_code}\n' -X PUT "$BASE/papers/P1" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d '{"manuscript": "new", "topics": ["AI"], "institutions": ["Univ-Z"]}'
curl -s -o /dev/null -w '删除撤回稿       -> HTTP %{http_code}\n' -X DELETE "$BASE/papers/P1" -H "$ORG"
curl -s -o /dev/null -w '撤回稿发起更正   -> HTTP %{http_code}\n' -X POST \
  "$BASE/papers/P1/review-corrections" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SERIAL, \"reviewer_id\": \"$A1\", \"reason\": \"撤稿后更正\"}"
curl -s -o /dev/null -w '撤回稿发布快照   -> HTTP %{http_code}\n' -X POST \
  "$BASE/papers/P1/feedback-snapshot" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"serial\": $SERIAL}"

echo "== 10. 后续普通分配跳过撤回稿 (其锁定随撤回清除, 不阻塞其他论文) =="
# 撤回前先给 P1 加一把锁: 撤回时该锁定在同一事务内清除
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -o /dev/null -X POST "$BASE/assignment/locks" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"locks\": {\"P1\": [\"R1\"]}}"
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
echo "-- 撤回后再提交含 P1 的锁定表 -> 404 (已撤回论文不参与分配, 整表拒绝) --"
curl -s -o /dev/null -w '锁定撤回稿       -> HTTP %{http_code}\n' -X POST "$BASE/assignment/locks" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"locks\": {\"P1\": [\"R1\"]}}"
echo "-- 锁定表中 P1 已随撤回清除 --"
curl -s "$BASE/assignment/locks" -H "$ORG" | j "d['locks']"
echo "-- 普通预演/发布: 方案只剩 P2, P1 不出现在 plan/unassigned/diagnostics --"
DRY=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG")
echo "$DRY" | j "{'feasible': d['feasible'], 'plan_keys': sorted(d['plan'].keys()), 'unassigned': d['unassigned']}"
NREV=$(echo "$DRY" | j 'd["revision"]')
curl -s -o /dev/null -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $NREV}"

echo "== 11. P2 (未撤回) 的确认与评语沿用既有规则, 不受 P1 撤回影响 =="
# 普通重新发布后按新方案取 P2 的两名评审人
ASSIGN=$(curl -s "$BASE/assignment" -H "$ORG")
NSER=$(echo "$ASSIGN" | j 'd["serial"]')
A2=$(echo "$ASSIGN" | j 'd["plan"]["P2"][0]')
B2=$(echo "$ASSIGN" | j 'd["plan"]["P2"][1]')
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P2/decision" \
  -H "X-Reviewer-Id: $A2" -H "X-Reviewer-Credential: $(cred "$A2")" \
  -H 'Content-Type: application/json' -d '{"paper_id": "P2", "decision": "confirm"}'
curl -s -X POST "$BASE/reviewer/assignments/P2/review" \
  -H "X-Reviewer-Id: $A2" -H "X-Reviewer-Credential: $(cred "$A2")" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P2\", \"serial\": $NSER, \"score\": 5, \"comment\": \"P2 评语正常提交\"}" \
  | j "{'ok': d['ok'], 'receipt': d['receipt']}"
echo "-- P2 的另一评审人 $B2 取稿正常 (P1 撤回不影响其任务) --"
curl -s "$BASE/reviewer/assignments" -H "X-Reviewer-Id: $B2" -H "X-Reviewer-Credential: $(cred "$B2")" \
  | j "{'assignments': [a['paper_id'] for a in d['assignments']]}"

echo "完成。"
