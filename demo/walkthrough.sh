#!/usr/bin/env bash
# 端到端演示：结果投递 → 更正 → 断连重试 → 冲突保护 → 模拟 LIMS 查询。
#
# 为确定性复现“入库后断开回执”，本脚本自己管理一个独立 worker：
# 先停掉 compose 自带的 worker，让修订停在 pending，完成断连注入后再启动。
#
# 前置：docker compose up --build 已启动（API 在 8000，模拟 LIMS 在 8001）。
set -euo pipefail

API="${API_URL:-http://localhost:8000}"
LIMS="${LIMS_URL:-http://localhost:8001}"
TASK="DEMO-T-1"

j() { python3 -m json.tool; }

echo "### 0. 重置模拟 LIMS，并暂停自带 worker，保证断连场景可控"
curl -s -X POST "$LIMS/lims/admin/reset" | j
docker compose stop worker1 worker2 >/dev/null 2>&1 || \
  echo "（非 compose 环境跳过 worker 停止；请确保没有其他 worker 在运行）"

echo
echo "### 1. 提交第 1 版结果（revision=1，分配终身不变的 delivery_id）"
REV1=$(curl -s -X POST "$API/v1/tasks/$TASK/results" \
  -H 'Content-Type: application/json' \
  -d '{"result_id":"R-1","sample_code":"S-100","item":"Pb","value":"0.012"}')
echo "$REV1" | j
DID1=$(echo "$REV1" | python3 -c 'import json,sys;print(json.load(sys.stdin)["revision"]["delivery_id"])')

echo
echo "### 2. worker 暂停期间修订保持 pending"
curl -s "$API/v1/tasks/$TASK" | j

echo
echo "### 3. 模拟“入库后断开回执”：LIMS 已提交数据，但响应流中断"
curl -s -X POST "$LIMS/lims/results" \
  -H 'Content-Type: application/json' \
  -d "{\"delivery_id\":\"$DID1\",\"task_id\":\"$TASK\",\"revision\":1,\"result_id\":\"R-1\",\"sample_code\":\"S-100\",\"item\":\"Pb\",\"value\":\"0.012\",\"simulate\":\"disconnect_after_store\"}" \
  -o /dev/null -w "断连请求 http_code=%{http_code}（响应不完整，符合预期）\n" || true
echo "--- LIMS 侧数据其实已落库 ---"
curl -s "$LIMS/lims/tasks/$TASK" | j

echo
echo "### 4. 恢复 worker：凭同一 delivery_id 重试 → 幂等重放，不重复入库"
docker compose start worker1 worker2 >/dev/null 2>&1 || \
  echo "（非 compose 环境：请手动启动 python -m app.worker_main）"
sleep 3
curl -s "$API/v1/tasks/$TASK" | j
curl -s "$LIMS/lims/tasks/$TASK" | j

echo
echo "### 5. 提交更正（value=0.015 → revision=2，新 delivery_id、新快照）"
curl -s -X POST "$API/v1/tasks/$TASK/results" \
  -H 'Content-Type: application/json' \
  -d '{"result_id":"R-1","sample_code":"S-100","item":"Pb","value":"0.015"}' | j
sleep 3

echo
echo "### 6. 更正后：本地当前修订=2 且已同步；LIMS 最高修订前进、旧值不覆盖新值"
curl -s "$API/v1/tasks/$TASK" | j
curl -s "$LIMS/lims/tasks/$TASK" | j

echo
echo "### 7. 手动重新触发已同步修订 → 409（防止重复投递）"
curl -s -X POST "$API/v1/tasks/$TASK/revisions/1/retry" -w "\nhttp_code=%{http_code}\n"

echo
echo "### 8. 同 ID 异内容保护：相同 delivery_id 改值投递被 LIMS 拒绝"
curl -s -X POST "$LIMS/lims/results" \
  -H 'Content-Type: application/json' \
  -d "{\"delivery_id\":\"$DID1\",\"task_id\":\"$TASK\",\"revision\":1,\"result_id\":\"R-1\",\"sample_code\":\"S-100\",\"item\":\"Pb\",\"value\":\"9.99\"}" \
  -w "\nhttp_code=%{http_code}\n"

echo
echo "### 9. LIMS 事件流：核对 stored（入库）与 idempotent_replay（重放）"
curl -s "$LIMS/lims/events" | j
