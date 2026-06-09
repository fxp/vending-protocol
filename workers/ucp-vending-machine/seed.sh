#!/usr/bin/env bash
# Seed the catalog from catalog.example.json.
# Usage:
#   ./seed.sh                              # localhost:8787 with default credentials
#   BASE_URL=https://vm.example.workers.dev ./seed.sh

set -euo pipefail
BASE_URL=${BASE_URL:-http://localhost:8787}
CLIENT_ID=${CLIENT_ID:-vending}
CLIENT_SECRET=${CLIENT_SECRET:-secret}

TOKEN=$(curl -sf -X POST "$BASE_URL/oauth/token" \
  -d "grant_type=client_credentials&client_id=$CLIENT_ID&client_secret=$CLIENT_SECRET" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

echo "Token acquired"

jq -c '.[]' catalog.example.json | while read -r item; do
  id=$(echo "$item" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
  resp=$(curl -sf -X POST "$BASE_URL/catalog" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "$item")
  echo "Seeded: $id"
done

echo "Done — catalog seeded at $BASE_URL"
