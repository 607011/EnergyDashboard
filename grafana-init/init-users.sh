#!/usr/bin/env bash
# Creates/updates Grafana users from GRAFANA_USERS
# (format: "login:password:role:email,login2:password2:role2:email2").
# Role is one of Viewer/Editor/Admin, defaults to Viewer if omitted. The email is
# optional; it's what "Sign in with Google" matches an existing Grafana user by.
set -uo pipefail

ADMIN_USER="admin"
ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-admin}"
BASE_URL="http://grafana:3000"

if [ -z "${GRAFANA_USERS:-}" ]; then
  echo "GRAFANA_USERS not set, nothing to provision."
  exit 0
fi

IFS=',' read -ra ENTRIES <<< "$GRAFANA_USERS"
for entry in "${ENTRIES[@]}"; do
  entry="$(echo "$entry" | xargs)" # trim whitespace
  [ -z "$entry" ] && continue

  login="$(cut -d: -f1 <<< "$entry")"
  password="$(cut -d: -f2 <<< "$entry")"
  role="$(cut -d: -f3 <<< "$entry")"
  role="${role:-Viewer}"
  email="$(cut -d: -f4 <<< "$entry")"

  if [ -z "$login" ] || [ -z "$password" ]; then
    echo "Skipping invalid entry: '$entry' (expected login:password[:role[:email]])"
    continue
  fi

  echo "==> Ensuring user '$login' (role: $role)"

  http_code=$(curl -s -o /tmp/resp.json -w "%{http_code}" -u "$ADMIN_USER:$ADMIN_PASSWORD" \
    -X POST "$BASE_URL/api/admin/users" \
    -H "Content-Type: application/json" \
    -d "$(jq -nc --arg name "$login" --arg login "$login" --arg email "$email" --arg password "$password" '{name:$name, login:$login, email:$email, password:$password, OrgId:1}')")

  if [ "$http_code" = "200" ]; then
    user_id=$(jq -r '.id' /tmp/resp.json)
    echo "    created (id=$user_id)"
  else
    echo "    create returned $http_code ($(jq -r '.message // "?"' /tmp/resp.json 2>/dev/null)), assuming user exists -- syncing password"
    user_id=$(curl -s -u "$ADMIN_USER:$ADMIN_PASSWORD" "$BASE_URL/api/users/lookup?loginOrEmail=$login" | jq -r '.id // empty')
    if [ -z "$user_id" ]; then
      echo "    could not find or create user '$login', skipping"
      continue
    fi
    curl -s -u "$ADMIN_USER:$ADMIN_PASSWORD" -X PUT "$BASE_URL/api/admin/users/$user_id/password" \
      -H "Content-Type: application/json" \
      -d "$(jq -nc --arg password "$password" '{password:$password}')" > /dev/null
    echo "    password synced (id=$user_id)"
  fi

  if [ -n "$email" ]; then
    curl -s -u "$ADMIN_USER:$ADMIN_PASSWORD" -X PUT "$BASE_URL/api/users/$user_id" \
      -H "Content-Type: application/json" \
      -d "$(jq -nc --arg name "$login" --arg login "$login" --arg email "$email" '{name:$name, login:$login, email:$email}')" > /dev/null
    echo "    email set to $email"
  fi

  curl -s -u "$ADMIN_USER:$ADMIN_PASSWORD" -X PATCH "$BASE_URL/api/org/users/$user_id" \
    -H "Content-Type: application/json" \
    -d "$(jq -nc --arg role "$role" '{role:$role}')" > /dev/null
  echo "    role set to $role"
done

echo "User provisioning done."
