#!/usr/bin/env bash
# Records a manual reading of the heat pump's electricity meter ("sneaker protocol").
#
#   scripts/meter-reading.sh 12345.6                      # now
#   scripts/meter-reading.sh 12345.6 "2026-09-21 08:00"   # reading taken earlier (local time)
#
# Environment: PI_HOST (default 192.168.0.2), PI_DIR (default ~/se10k),
#              LOCAL=1 to write to the local docker compose stack instead of the Pi,
#              FORCE=1 to accept a value lower than the previous reading.
set -euo pipefail

kwh="${1:-}"
at="${2:-}"
if ! [[ "$kwh" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "Usage: $0 <meter reading in kWh> [\"YYYY-MM-DD HH:MM\"]" >&2
  exit 2
fi

name="${WEISHAUPT_NAME:-wgb14}"
key="ts:weishaupt:${name}:electric_reading_kwh"

if [ -n "$at" ]; then
  if ! ts_s=$(date -j -f "%Y-%m-%d %H:%M" "$at" +%s 2>/dev/null || date -d "$at" +%s 2>/dev/null); then
    echo "Cannot parse time '$at' (use \"YYYY-MM-DD HH:MM\")" >&2
    exit 2
  fi
  ts_ms="${ts_s}000"
else
  ts_ms="*"
fi

redis_cli() {
  if [ "${LOCAL:-}" = "1" ]; then
    docker compose exec -T redis redis-cli "$@"
  else
    # shellcheck disable=SC2029
    ssh "${PI_HOST:-192.168.0.2}" "cd ${PI_DIR:-~/se10k} && docker compose exec -T redis redis-cli $(printf '%q ' "$@")"
  fi
}

# Typo protection: a meter only counts up.
last=$(redis_cli TS.GET "$key" 2>/dev/null | tr '\r' '\n' | sed -n 2p || true)
if [[ "$last" =~ ^[0-9]+([.][0-9]+)?$ ]] && awk "BEGIN{exit !($kwh < $last)}" && [ "${FORCE:-}" != "1" ]; then
  echo "Refusing: $kwh kWh is below the previous reading ($last kWh). Typo? Use FORCE=1 to override." >&2
  exit 1
fi

# Retention 0 = keep forever; these readings are precious and few.
redis_cli TS.ADD "$key" "$ts_ms" "$kwh" RETENTION 0 ON_DUPLICATE LAST LABELS device "$name" field electric_reading_kwh >/dev/null
if [[ "$last" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  awk "BEGIN{printf \"Recorded %s kWh (+%.1f kWh since the previous reading)\n\", $kwh, $kwh - $last}"
else
  echo "Recorded $kwh kWh (first reading)"
fi
