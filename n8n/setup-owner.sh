#!/bin/sh
# n8n/setup-owner.sh
#
# Patches the n8n owner account directly in Postgres after n8n runs its
# own migrations. Runs in a postgres:16-alpine container (has psql, no node).
#
# n8n v2 always creates a blank owner row during migration but won't populate
# it from N8N_DEFAULT_ADMIN_* if any row already exists. This script fills it.
#
# The bcrypt hash below is for "canvasly_dev" (cost 10) — regenerate with:
#   docker run --rm node:20-alpine -e "const b=require('bcryptjs');console.log(b.hashSync('canvasly_dev',10))"

set -e

EMAIL="${N8N_DEFAULT_ADMIN_EMAIL:-admin@canvasly.local}"
FIRST="${N8N_DEFAULT_ADMIN_FIRST_NAME:-Canvasly}"
LAST="${N8N_DEFAULT_ADMIN_LAST_NAME:-Admin}"

DB_HOST="${DB_POSTGRESDB_HOST:-postgres}"
DB_PORT="${DB_POSTGRESDB_PORT:-5432}"
DB_NAME="${DB_POSTGRESDB_DATABASE:-canvasly}"
DB_USER="${DB_POSTGRESDB_USER:-canvasly}"
SCHEMA="${DB_POSTGRESDB_SCHEMA:-n8n}"

export PGPASSWORD="${DB_POSTGRESDB_PASSWORD:-canvasly_dev}"

# bcrypt hash of "canvasly_dev" at cost 10
HASH='$2a$10$lm5.3klsxKhY1gY1gwBdkeL83qzlKm53B137NXM.NijmNN1Cg./TS'

echo "[n8n-owner] Waiting for n8n migrations..."
for i in $(seq 1 30); do
    COUNT=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
        -tAc "SELECT COUNT(*) FROM ${SCHEMA}.migrations;" 2>/dev/null || echo "0")
    if [ "${COUNT:-0}" -gt 0 ] 2>/dev/null; then
        echo "[n8n-owner] Migrations ready ($COUNT rows). Patching owner..."
        break
    fi
    echo "[n8n-owner] Attempt $i/30 — not ready yet, retrying in 3s..."
    sleep 3
done

psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" <<SQL
UPDATE ${SCHEMA}."user" SET
    email                   = '${EMAIL}',
    "firstName"             = '${FIRST}',
    "lastName"              = '${LAST}',
    password                = '${HASH}',
    "personalizationAnswers"= '{}'::json,
    "roleSlug"              = 'global:owner',
    disabled                = false
WHERE "roleSlug" = 'global:owner' OR email IS NULL OR email = '';
SQL

RESULT=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
    -tAc "SELECT email FROM ${SCHEMA}.\"user\" WHERE email='${EMAIL}';" 2>/dev/null)

if [ "$RESULT" = "$EMAIL" ]; then
    echo "[n8n-owner] Done. Login: ${EMAIL} / canvasly_dev"
else
    echo "[n8n-owner] WARNING: patch may have failed. Row: ${RESULT:-empty}"
fi
