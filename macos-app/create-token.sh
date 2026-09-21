#!/usr/bin/env bash
# Creates a read-only Grafana service account for the app (once) and writes its config:
#   ~/Library/Application Support/PV Monitor/config.json   (mode 600)
#
#   macos-app/create-token.sh [https://grafana.example.net]
#
# Needs the Grafana admin password (GRAFANA_ADMIN_PASSWORD in ../.env). The token has the Viewer
# role, so the app can read dashboards and query the data, nothing more. A new run adds a new token
# (old ones stay valid until deleted under Administration > Users and access > Service accounts).
set -euo pipefail
cd "$(dirname "$0")"

url="${1:-$(grep -E '^GRAFANA_ROOT_URL=' ../.env | cut -d= -f2-)}"
url="${url%/}"
[ -n "$url" ] || { echo "No URL: pass it as an argument or set GRAFANA_ROOT_URL in ../.env" >&2; exit 2; }
password="$(grep -E '^GRAFANA_ADMIN_PASSWORD=' ../.env | cut -d= -f2-)"
[ -n "$password" ] || { echo "GRAFANA_ADMIN_PASSWORD not found in ../.env" >&2; exit 2; }
name="pv-monitor"
api() { curl -sf -u "admin:$password" -H 'Content-Type: application/json' "$@"; }

id="$(api "$url/api/serviceaccounts/search?query=$name" | jq -r --arg n "$name" '.serviceAccounts[]|select(.name==$n)|.id' | head -1)"
if [ -z "$id" ]; then
  id="$(api -X POST "$url/api/serviceaccounts" -d "{\"name\":\"$name\",\"role\":\"Viewer\"}" | jq -r .id)"
  echo "Created service account '$name' (id $id, role Viewer)"
else
  echo "Service account '$name' exists (id $id)"
fi
token="$(api -X POST "$url/api/serviceaccounts/$id/tokens" -d "{\"name\":\"macos-app-$(date +%Y%m%d-%H%M%S)\"}" | jq -r .key)"
[ -n "$token" ] && [ "$token" != null ] || { echo "Could not create a token" >&2; exit 1; }

dir="$HOME/Library/Application Support/PV Monitor"
mkdir -p "$dir"
umask 077
jq -n --arg url "$url" --arg token "$token" '{url:$url, token:$token}' > "$dir/config.json"
chmod 600 "$dir/config.json"
echo "Wrote $dir/config.json"
