#!/bin/sh
# Import and publish demo workflows before starting n8n so production webhook
# registrations are available on the first Docker Compose boot.

set -e

WORKFLOWS_DIR="${WORKFLOWS_DIR:-/workflows}"

echo "[n8n-start] Importing workflows from ${WORKFLOWS_DIR}..."
for file in "${WORKFLOWS_DIR}"/*.json; do
    [ -f "$file" ] || continue
    echo "[n8n-start] Importing: $(basename "$file")"
    n8n import:workflow --input="$file"
done

echo "[n8n-start] Publishing workflows..."
n8n list:workflow | while IFS='|' read -r workflow_id workflow_name; do
    [ -n "$workflow_id" ] || continue
    echo "[n8n-start] Publishing: ${workflow_name} (${workflow_id})"
    n8n publish:workflow --id="$workflow_id"
done

echo "[n8n-start] Starting n8n..."
exec n8n start
