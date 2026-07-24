#!/bin/sh
# n8n/import-workflows.sh
# Imports all workflow JSON files into a running n8n instance.
# Run after n8n is healthy.

set -e

N8N_URL="${N8N_URL:-http://n8n:5678}"
N8N_USER="${N8N_BASIC_AUTH_USER:-admin}"
N8N_PASS="${N8N_BASIC_AUTH_PASSWORD:-canvasly_dev}"
WORKFLOWS_DIR="${WORKFLOWS_DIR:-/workflows}"

echo "[n8n-import] Waiting for n8n to be ready at ${N8N_URL}..."
STATUS="000"
for i in $(seq 1 30); do
    BODY=$(wget -qO- "${N8N_URL}/healthz" 2>/dev/null || echo "")
    if echo "$BODY" | grep -q "status"; then
        STATUS="200"
        echo "[n8n-import] n8n is ready."
        break
    fi
    echo "[n8n-import] Attempt ${i}/30 — not ready, retrying in 5s..."
    sleep 5
done

if [ "$STATUS" != "200" ]; then
    echo "[n8n-import] ERROR: n8n did not become ready in time."
    exit 1
fi

echo "[n8n-import] Importing workflows from ${WORKFLOWS_DIR}..."
for file in "${WORKFLOWS_DIR}"/*.json; do
    [ -f "$file" ] || continue
    echo "[n8n-import] Importing: $(basename "$file")"
    # n8n v1+ CLI: --userId removed; --separate flag imports each file independently
    n8n import:workflow --input="$file" 2>&1 || \
        echo "[n8n-import] WARNING: failed to import $(basename "$file") (may already exist)"
done

echo "[n8n-import] All workflows imported."
