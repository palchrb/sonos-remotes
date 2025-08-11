#!/usr/bin/env bash
DEVICE="remote1"
SPEAKER="Edith"
API="http://localhost:5000"
INACTIVE_THRESHOLD=3600
MAP_FILE="./rfid_mappings.json"

# Init: sett speaker én gang
curl -s -X POST "$API/set_speaker" \
     -H 'Content-Type: application/json' \
     -d "{\"device_id\":\"$DEVICE\",\"speaker\":\"$SPEAKER\"}" > /dev/null

LAST_ACTIVITY=$(date +%s)

mosquitto_sub -h localhost -t 'zigbee2mqtt/edith_remote' -v \
| while read -r topic payload; do
    NOW=$(date +%s)
    if (( NOW - LAST_ACTIVITY >= INACTIVE_THRESHOLD )); then
      curl -s -X POST "$API/set_speaker" \
           -H 'Content-Type: application/json' \
           -d "{\"device_id\":\"$DEVICE\",\"speaker\":\"$SPEAKER\"}" > /dev/null
    fi
    LAST_ACTIVITY=$NOW

    action=$(echo "$payload" | jq -r '.action // empty')
    case "$action" in
      # Hold → play_by_card (alt. direkte playlink via backend)
      *_hold|brightness_move_*)
        # Bruk action som card_id
        curl -s -X POST "$API/play_by_card" \
             -H 'Content-Type: application/json' \
             -d "{\"device_id\":\"$DEVICE\",\"card_id\":\"$action\"}"
        ;;
      arrow_left_click)
        curl -s -X POST "$API/previous" \
             -H 'Content-Type: application/json' \
             -d "{\"device_id\":\"$DEVICE\"}"
        ;;
      arrow_right_click)
        curl -s -X POST "$API/next" \
             -H 'Content-Type: application/json' \
             -d "{\"device_id\":\"$DEVICE\"}"
        ;;
      on|off)
        curl -s -X POST "$API/play_pause" \
             -H 'Content-Type: application/json' \
             -d "{\"device_id\":\"$DEVICE\"}"
        ;;
    esac
  done
