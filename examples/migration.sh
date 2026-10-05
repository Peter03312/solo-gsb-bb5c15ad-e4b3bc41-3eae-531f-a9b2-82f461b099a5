#!/usr/bin/env bash
# 本轮用法端到端示例: 会务方实例迁移 (一致业务数据快照导出 -> 空实例恢复)
#   源实例录入/发布/确认/评语/快照 -> 导出快照 (含格式版本与内容校验和) ->
#   本地复算校验和 -> 空目标实例恢复 -> 修订号/发布序号/凭据/访问码按原规则工作
#   本轮新增: 跨记录核验——已因更正/撤回失效的快照, 改回失效标记并重算校验和
#   的伪造快照恢复时 422 拒绝 (矛盾快照), 目标实例不留部分数据。
#
# 需要两个实例:
#   源实例 (已有业务数据):   BASE  默认 http://localhost:8000
#   目标实例 (必须为空库):   BASE2 默认 http://localhost:8001
# 目标实例可用空库启动, 例如:
#   DB_PATH=./data/app-target.db PORT=8001 ORGANIZER_KEY=dev-organizer-key python -m app.main
# 用法: ./examples/migration.sh [BASE_URL] [BASE2_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
BASE2="${2:-http://localhost:8001}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }
SNAP=/tmp/review-migration-snapshot.json
FORGED=/tmp/review-migration-forged.json

echo "== 1. 源实例录入 2 名评审人与论文 P1 并发布 =="
for spec in 'R1 r1-secret Inst-A' 'R2 r2-secret Inst-B'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"$3\", \"capacity\": 3, \"avoid_papers\": []}"
done
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 匿名稿全文...", "topics": ["AI"], "institutions": ["Univ-X"]}'
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
SERIAL=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}" | j 'd["serial"]')
echo "    已发布: revision=$REV serial=$SERIAL"

echo "== 2. 评审人确认并交正式评语, 会务方发布 P1 反馈快照 v1 =="
for RID in R1 R2; do
  CRED=$( [ "$RID" = R1 ] && echo r1-secret || echo r2-secret )
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
    -H "X-Reviewer-Id: $RID" -H "X-Reviewer-Credential: $CRED" \
    -H 'Content-Type: application/json' -d '{"paper_id":"P1","decision":"confirm"}'
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/review" \
    -H "X-Reviewer-Id: $RID" -H "X-Reviewer-Credential: $CRED" \
    -H 'Content-Type: application/json' \
    -d "{\"paper_id\":\"P1\",\"serial\":$SERIAL,\"score\":3,\"comment\":\"$RID 对 P1 的初版评语\"}"
done
OLD_RECEIPT=$(curl -s "$BASE/papers/P1/reviews" -H "$ORG" \
  | j '[s for s in d["slots"] if s["reviewer_id"]=="R1"][0]["review"]["receipt"]')
CODE_V1=$(curl -s -X POST "$BASE/papers/P1/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SERIAL}" | j 'd["access_code"]')
echo "    v1 访问码: $CODE_V1 (旧评语收据 $OLD_RECEIPT)"

echo "== 3. 会务方发起评语更正, R1 完成更正, 再重发快照 v2 (v1 访问码立即失效) =="
curl -s -o /dev/null -X POST "$BASE/papers/P1/review-corrections" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\":\"P1\",\"serial\":$SERIAL,\"reviewer_id\":\"R1\",\"reason\":\"评分与评语不符, 请更正\"}"
curl -s -o /dev/null -X POST "$BASE/reviewer/review-corrections/P1" \
  -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret' \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\":\"P1\",\"serial\":$SERIAL,\"original_receipt\":\"$OLD_RECEIPT\",\"score\":5,\"comment\":\"R1 对 P1 的更正后评语\"}"
CODE_V2=$(curl -s -X POST "$BASE/papers/P1/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SERIAL}" | j 'd["access_code"]')
echo "    v2 访问码: $CODE_V2"
echo "    旧码读取 -> HTTP $(curl -s -o /dev/null -w '%{http_code}' "$BASE/feedback-snapshots/$CODE_V1") (404, 已更正失效)"

echo "== 4. 会务方导出一致业务数据快照 (与并发写入隔离, 附格式版本与校验和) =="
curl -s "$BASE/migration/export" -H "$ORG" > "$SNAP"
python3 - "$SNAP" <<'PY'
import json, sys
snap = json.load(open(sys.argv[1]))
data = snap["data"]
print(f"    format={snap['format']} version={snap['format_version']}")
print(f"    checksum={snap['checksum']}")
print(f"    revision={data['revision']} serial={data['published']['serial'] if data['published'] else None}")
print("    记录数:", {k: len(v) for k, v in data.items() if isinstance(v, list)})
PY

echo "== 5. 本地复算内容校验和 (核对快照完整) =="
python3 - "$SNAP" <<'PY'
import hashlib, json, sys
snap = json.load(open(sys.argv[1]))
canon = json.dumps(snap["data"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
digest = "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()
assert digest == snap["checksum"], "校验和不符, 快照已损坏!"
print("    校验和一致:", digest)
PY

echo "== 6. 攻击演示: 把已失效的 v1 快照标记改回有效并重算校验和 =="
python3 - "$SNAP" "$CODE_V1" "$FORGED" <<'PY'
import hashlib, json, sys
snap_path, code_v1, out_path = sys.argv[1:4]
snap = json.load(open(snap_path))
for s in snap["data"]["feedback_snapshots"]:
    if s["access_code"] == code_v1:
        assert s["invalidated"] is True, "v1 在源实例本应已失效"
        s["invalidated"] = False  # 攻击者尝试复活旧访问码
canon = json.dumps(snap["data"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
snap["checksum"] = "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()
json.dump(snap, open(out_path, "w"), ensure_ascii=False)
print("    已伪造快照 (失效标记改回有效, 校验和已重算):", out_path)
PY

echo "== 7. 伪造快照恢复 -> 422 (矛盾快照: 依据快照版本/更正记录, v1 应当已失效); 不留部分数据 =="
code=$(curl -s -o /tmp/forged-resp.json -w '%{http_code}' -X POST "$BASE2/migration/restore" \
  -H "$ORG" -H 'Content-Type: application/json' --data-binary @"$FORGED")
echo "    HTTP $code"; python3 -c 'import json; print("   ", json.load(open("/tmp/forged-resp.json"))["detail"])'
[ "$code" = "422" ]
echo "    目标实例仍为空 (revision=$(curl -s "$BASE2/meta" -H "$ORG" | j 'd["revision"]'))"

echo "== 8. 合法快照恢复成功 (连续更正/同序号再次发布/跨序号历史快照均可迁移) =="
curl -s -X POST "$BASE2/migration/restore" -H "$ORG" -H 'Content-Type: application/json' \
  --data-binary @"$SNAP" | j "{'ok': d['ok'], 'revision': d['revision'], 'serial': d['serial'], 'counts': d['counts']}"

echo "== 9. 恢复后仅当前有效码可读、可提异议; 旧码仍 404, 历史版本供会务方追溯 =="
echo "    v2 当前码读取 -> HTTP $(curl -s -o /dev/null -w '%{http_code}' "$BASE2/feedback-snapshots/$CODE_V2") (200)"
echo "    v1 旧码读取   -> HTTP $(curl -s -o /dev/null -w '%{http_code}' "$BASE2/feedback-snapshots/$CODE_V1") (404)"
echo "    v2 提交异议   -> HTTP $(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE2/feedback-objections" \
  -H 'Content-Type: application/json' \
  -d "{\"access_code\":\"$CODE_V2\",\"label\":1,\"reason\":\"迁移后对当前有效反馈的异议\"}") (200)"
echo "    v1 旧码提异议 -> HTTP $(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE2/feedback-objections" \
  -H 'Content-Type: application/json' \
  -d "{\"access_code\":\"$CODE_V1\",\"label\":1,\"reason\":\"旧码复活尝试\"}") (404)"
curl -s "$BASE2/papers/P1/feedback-snapshots" -H "$ORG" \
  | j '[{"version": s["version"], "active": s["active"], "serial": s["serial"]} for s in d["snapshots"]]'

echo "== 10. 重复恢复 -> 409; 非会务方 -> 401 =="
curl -s -o /dev/null -w '重复恢复           -> HTTP %{http_code}\n' -X POST "$BASE2/migration/restore" \
  -H "$ORG" -H 'Content-Type: application/json' --data-binary @"$SNAP"
curl -s -o /dev/null -w '无密钥恢复         -> HTTP %{http_code}\n' -X POST "$BASE2/migration/restore" \
  -H 'Content-Type: application/json' --data-binary @"$SNAP"

echo "== 迁移完成: 合法快照已恢复; 复活旧访问码的伪造快照被跨记录核验拒绝 =="

