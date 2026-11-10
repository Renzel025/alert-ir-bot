#!/usr/bin/env bash
# Local end-to-end test without Lark (DRY_RUN=1): fake the events Lark would send.
#   ./scripts/simulate.sh alert          # a MonitorFlow alert arrives
#   ./scripts/simulate.sh reply normal   # the check person replies in the thread
#   ./scripts/simulate.sh reply escalate
#   ./scripts/simulate.sh pd acknowledged
# Needs ALERT_CHAT_ID, ALERT_BOT_SENDER_ID and a check_person in policy.yaml set to the values below.
set -euo pipefail
BOT=${BOT:-http://127.0.0.1:8080}
CHAT=${ALERT_CHAT_ID:-oc_test}
ALERT_BOT=${ALERT_BOT_SENDER_ID:-cli_alertbot}
CHECK_PERSON=${CHECK_PERSON:-ou_checkperson}
TOKEN=${LARK_VERIFICATION_TOKEN:-}
ALERT_MSG_ID=${ALERT_MSG_ID:-om_alert_$(date +%s)}

event() {  # $1 message_id  $2 sender  $3 text  $4 root_id
  curl -s -X POST "$BOT/lark/events" -H 'Content-Type: application/json' -d @- <<EOF
{"schema":"2.0",
 "header":{"event_type":"im.message.receive_v1","token":"$TOKEN"},
 "event":{"sender":{"sender_id":{"open_id":"$2"},"sender_type":"user"},
          "message":{"message_id":"$1","chat_id":"$CHAT","message_type":"text",
                     "root_id":"$4","content":$(python3 -c 'import json,sys;print(json.dumps({"text":sys.argv[1]}))' "$3")}}}
EOF
  echo
}

case "${1:-}" in
  alert)
    echo "alert message id: $ALERT_MSG_ID  (export ALERT_MSG_ID=$ALERT_MSG_ID for the reply step)"
    event "$ALERT_MSG_ID" "$ALERT_BOT" $'[FIRING:2] provider_drop_rate\nSeverity: P1\nalert time: 2026-11-03 15:16:00\nalert value: < 0.5\nproviderId=pg_soft recent=120 baseline=300 ratio=0.4\nproviderId=jili recent=80 baseline=200 ratio=0.4' "" ;;
  reply)
    event "om_reply_$(date +%s)" "$CHECK_PERSON" "${2:-normal}" "$ALERT_MSG_ID" ;;
  pd)
    curl -s -X POST "$BOT/pagerduty/status" -H 'Content-Type: application/json' \
      -H "X-Status-Secret: ${PD_STATUS_SECRET:-change-me}" \
      -d "{\"dedup_key\":\"${DEDUP_KEY:-provider_drop_rate-0}\",\"status\":\"${2:-acknowledged}\",\"user\":\"jeno\",\"time\":\"$(date -u +%FT%TZ)\"}"; echo ;;
  *) echo "usage: $0 alert | reply [normal|escalate] | pd [acknowledged|resolved]"; exit 1 ;;
esac
