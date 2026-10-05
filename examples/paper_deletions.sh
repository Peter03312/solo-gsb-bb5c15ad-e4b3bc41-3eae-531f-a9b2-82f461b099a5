#!/usr/bin/env bash
# 本轮用法端到端示例: 删除已发布反馈的论文 —— 反馈生命周期 + 同编号重录 + 迁移
#
#   1) 发布/确认/评语/快照/作者异议 -> 会务方删除 P1:
#      同一写事务内旧快照访问码失效、待处理异议过期 (响应含失效快照数/过期异议数);
#      持码读取与异议提交对旧码返回与无效码同形的 404; 查询凭据仍可查 expired;
#      历史快照/异议/删除凭据由会务方追溯;
#   2) 同编号重新录入 -> 重新发布分配 (发布序号推进) -> 新快照新访问码:
#      旧码持续 404, 不恢复效力; 删除凭据记录每次删除;
#   3) 迁移 (v2, 含 paper_deletions): 空目标实例恢复后旧码死、新码活;
#      把旧快照失效标记改回有效并重算校验和的伪造快照 -> 422 矛盾快照、不留部分数据;
#      v1 旧格式快照 (无删除记载) 恢复时沿用原有判断, 不推断删除历史。
#
# 需要两个实例:
#   源实例 (演示删除/重录):  BASE  默认 http://localhost:8000 (建议用空库)
#   目标实例 (必须为空库):   BASE2 默认 http://localhost:8001
# 目标实例可用空库启动, 例如:
#   DB_PATH=./data/app-target.db PORT=8001 ORGANIZER_KEY=dev-organizer-key python -m app.main
# 用法: ./examples/paper_deletions.sh [BASE_URL] [BASE2_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
BASE2="${2:-http://localhost:8001}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }
SNAP=/tmp/review-deletions-snapshot.json
FORGED=/tmp/review-deletions-forged.json
LEGACY=/tmp/review-deletions-legacy-v1.json

code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

echo "== 1. 源实例录入 R1/R2 与论文 P1, 发布分配 =="
for spec in 'R1 r1-secret Inst-A' 'R2 r2-secret Inst-B'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"$3\", \"capacity\": 3, \"avoid_papers\": []}"
done
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 匿名稿全文...", "topics": ["AI"], "institutions": ["Univ-X"]}'
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
SER=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}" | j 'd["serial"]')
echo "    已发布: revision=$REV serial=$SER"

echo "== 2. 两评审人确认并交评语, 会务方发布快照, 作者持码提交异议 =="
for spec in 'R1 r1-secret 4 R1 评语' 'R2 r2-secret 5 R2 评语'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
    -H "X-Reviewer-Id: $1" -H "X-Reviewer-Credential: $2" \
    -H 'Content-Type: application/json' -d '{"paper_id":"P1","decision":"confirm"}'
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/review" \
    -H "X-Reviewer-Id: $1" -H "X-Reviewer-Credential: $2" \
    -H 'Content-Type: application/json' \
    -d "{\"paper_id\":\"P1\",\"serial\":$SER,\"score\":$3,\"comment\":\"$4\"}"
done
OLD_CODE=$(curl -s -X POST "$BASE/papers/P1/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SER}" | j 'd["access_code"]')
OBJ_TOKEN=$(curl -s -X POST "$BASE/feedback-objections" -H 'Content-Type: application/json' \
  -d "{\"access_code\":\"$OLD_CODE\",\"label\":1,\"reason\":\"评语一引用的对比实验并非本文方法\"}" \
  | j 'd["query_token"]')
echo "    旧快照访问码: $OLD_CODE"
echo "    删除前持码读取 -> HTTP $(code "$BASE/feedback-snapshots/$OLD_CODE") (200)"

echo "== 3. 会务方删除 P1: 同一写事务原子失效快照码 + 过期待处理异议 =="
curl -s -X DELETE "$BASE/papers/P1" -H "$ORG" > /tmp/pd-delete-resp.json
python3 - <<'PY'
import json
d = json.load(open("/tmp/pd-delete-resp.json"))["deletion"]
print("    ok 失效快照:", d["invalidated_snapshots"], "过期异议:", d["expired_objections"],
      "冻结序号:", d["published_serial"], "冻结槽位:", d["published_plan"])
print("    删除时刻:", d["deleted_at"])
PY

echo "== 4. 旧码持码读取 / 提交新异议: 与无效码同形 404 (不透露论文是否存在) =="
BOGUS=fbk-00000000000000000000000000000000
for desc_url in \
  "旧码读取|$BASE/feedback-snapshots/$OLD_CODE" \
  "无效码读取|$BASE/feedback-snapshots/$BOGUS"; do
  desc=${desc_url%%|*}; url=${desc_url#*|}
  echo "    $desc -> HTTP $(code "$url") (404)"
done
echo "    旧码提交异议 -> HTTP $(code -X POST "$BASE/feedback-objections" \
  -H 'Content-Type: application/json' \
  -d "{\"access_code\":\"$OLD_CODE\",\"label\":2,\"reason\":\"删除后提交\"}") (404)"

echo "== 5. 作者查询凭据继续可查 (expired); 会务方可追溯历史快照/异议/删除凭据 =="
curl -s "$BASE/feedback-objections/$OBJ_TOKEN" \
  | j '{"paper_id": d["paper_id"], "state": d["state"], "resolved_at": d["resolved_at"]}'
curl -s "$BASE/papers/P1/feedback-snapshots" -H "$ORG" \
  | j '[{"version": s["version"], "active": s["active"]} for s in d["snapshots"]]'
curl -s "$BASE/papers/P1/deletions" -H "$ORG" \
  | j '[{"id": x["id"], "state": x["state"], "invalidated_snapshots": x["invalidated_snapshots"], "expired_objections": x["expired_objections"]} for x in d["deletions"]]'

echo "== 6. 同编号重新录入 + 重新发布分配 (发布序号推进): 旧码仍 404 =="
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "同编号重录稿", "topics": ["AI"], "institutions": ["Univ-Y"]}'
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
SER2=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}" | j 'd["serial"]')
echo "    重新发布: serial=$SER2"
echo "    未重新发布分配前按旧序号重发相同收据快照 -> 409 (不返回旧码/不覆盖历史):"
echo "      HTTP $(code -X POST "$BASE/papers/P1/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": 1}") (409)"
echo "    重新发布分配后旧码读取 -> HTTP $(code "$BASE/feedback-snapshots/$OLD_CODE") (404, 不复活)"

echo "== 7. 重录后重新确认/交评语, 发布新快照: version+1 与新访问码 =="
for spec in 'R1 r1-secret 3 重录 R1 评语' 'R2 r2-secret 4 重录 R2 评语'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
    -H "X-Reviewer-Id: $1" -H "X-Reviewer-Credential: $2" \
    -H 'Content-Type: application/json' -d '{"paper_id":"P1","decision":"confirm"}'
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/review" \
    -H "X-Reviewer-Id: $1" -H "X-Reviewer-Credential: $2" \
    -H 'Content-Type: application/json' \
    -d "{\"paper_id\":\"P1\",\"serial\":$SER2,\"score\":$3,\"comment\":\"$4\"}"
done
NEW_CODE=$(curl -s -X POST "$BASE/papers/P1/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SER2}" | j 'd["access_code"]')
echo "    新快照访问码: $NEW_CODE"
echo "    新码读取 -> HTTP $(code "$BASE/feedback-snapshots/$NEW_CODE") (200)"
echo "    旧码读取 -> HTTP $(code "$BASE/feedback-snapshots/$OLD_CODE") (404)"
curl -s "$BASE/papers/P1/feedback-snapshots" -H "$ORG" \
  | j '[{"version": s["version"], "serial": s["serial"], "active": s["active"]} for s in d["snapshots"]]'

echo "== 8. 导出 v2 一致快照 (含 paper_deletions 删除凭据) 并本地复算校验和 =="
curl -s "$BASE/migration/export" -H "$ORG" > "$SNAP"
python3 - "$SNAP" <<'PY'
import hashlib, json, sys
snap = json.load(open(sys.argv[1]))
assert snap["format_version"] == 2, snap["format_version"]
dels = snap["data"]["paper_deletions"]
assert len(dels) >= 1 and dels[0]["invalidated_snapshots"] == 1
canon = json.dumps(snap["data"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
assert "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest() == snap["checksum"]
print(f"    format_version=2, 删除凭据 {len(dels)} 条, 校验和一致")
PY

echo "== 9. 空目标实例恢复合法快照: 旧码死/新码活/删除凭据可追溯 =="
curl -s -X POST "$BASE2/migration/restore" -H "$ORG" -H 'Content-Type: application/json' \
  --data-binary @"$SNAP" | j "{'ok': d['ok'], 'format_version': d['format_version'], 'revision': d['revision'], 'serial': d['serial'], 'paper_deletions': d['counts']['paper_deletions']}"
echo "    恢复后新码读取 -> HTTP $(code "$BASE2/feedback-snapshots/$NEW_CODE") (200)"
echo "    恢复后旧码读取 -> HTTP $(code "$BASE2/feedback-snapshots/$OLD_CODE") (404)"
echo "    旧码提交异议 -> HTTP $(code -X POST "$BASE2/feedback-objections" \
  -H 'Content-Type: application/json' \
  -d "{\"access_code\":\"$OLD_CODE\",\"label\":1,\"reason\":\"旧码复活\"}") (404)"
curl -s "$BASE2/papers/P1/deletions" -H "$ORG" \
  | j '{"deletions": [{"id": x["id"], "published_serial": x["published_serial"]} for x in d["deletions"]]}'

echo "== 10. 复活攻击: 把删除前旧快照 invalidated 改回有效并重算校验和 -> 422, 不留部分数据 =="
python3 - "$SNAP" "$OLD_CODE" "$FORGED" <<'PY'
import hashlib, json, sys
snap_path, old_code, out_path = sys.argv[1:4]
snap = json.load(open(snap_path))
s = next(x for x in snap["data"]["feedback_snapshots"] if x["access_code"] == old_code)
assert s["invalidated"] is True
s["invalidated"] = False  # 攻击者尝试让删除前旧码复活 (同编号已重录)
canon = json.dumps(snap["data"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
snap["checksum"] = "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()
json.dump(snap, open(out_path, "w"), ensure_ascii=False)
print("    伪造快照已生成:", out_path)
PY
echo "    伪造快照恢复 -> HTTP $(curl -s -o /tmp/pd-forged-resp.json -w '%{http_code}' \
  -X POST "$BASE2/migration/restore" -H "$ORG" -H 'Content-Type: application/json' \
  --data-binary @"$FORGED") (422)"
python3 -c 'import json; print("   ", json.load(open("/tmp/pd-forged-resp.json"))["detail"])'

echo "== 11. 旧格式 v1 (无删除记载) 恢复沿用原有判断, 不推断删除历史 =="
python3 - "$SNAP" "$OLD_CODE" "$LEGACY" <<'PY'
import hashlib, json, sys
snap_path, old_code, out_path = sys.argv[1:4]
snap = json.load(open(snap_path))
# 等价于旧软件导出的 v1 快照: 不含 paper_deletions, 旧码标记保持有效
snap["format_version"] = 1
del snap["data"]["paper_deletions"]
next(x for x in snap["data"]["feedback_snapshots"] if x["access_code"] == old_code)["invalidated"] = False
canon = json.dumps(snap["data"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
snap["checksum"] = "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()
json.dump(snap, open(out_path, "w"), ensure_ascii=False)
print("    v1 等价快照已生成:", out_path)
PY
echo "    (v1 演示需要另一个空库实例, 此处仅说明: v1 恢复 200; 旧序号旧码仍按发布序号口径 404)"

echo "== 演示完成: 删除原子失效可核验, 同编号重录旧码不复活, 迁移保留删除失效依据 =="
