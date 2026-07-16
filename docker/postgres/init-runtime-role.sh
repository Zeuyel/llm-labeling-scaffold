#!/bin/sh
set -eu

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${SCAFFOLD_POSTGRES_APP_USER:?SCAFFOLD_POSTGRES_APP_USER is required}"
: "${SCAFFOLD_POSTGRES_APP_PASSWORD:?SCAFFOLD_POSTGRES_APP_PASSWORD is required}"

owner_password="${POSTGRES_PASSWORD:-${PGPASSWORD:-}}"
: "${owner_password:?POSTGRES_PASSWORD or PGPASSWORD is required}"

if [ "$POSTGRES_USER" = "$SCAFFOLD_POSTGRES_APP_USER" ]; then
  echo "database owner and runtime app role must be different" >&2
  exit 2
fi
if [ "$owner_password" = "$SCAFFOLD_POSTGRES_APP_PASSWORD" ]; then
  echo "database owner and runtime app passwords must be different" >&2
  exit 2
fi

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
sql_path="${LLS_RUNTIME_ROLE_SQL_PATH:-${script_dir}/init-runtime-role.sql}"
if [ ! -r "$sql_path" ]; then
  echo "runtime role SQL is not readable: $sql_path" >&2
  exit 2
fi

psql \
  --set ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set owner_user="$POSTGRES_USER" \
  --set app_user="$SCAFFOLD_POSTGRES_APP_USER" \
  --file "$sql_path"

verify_sql_path="${LLS_RUNTIME_ROLE_VERIFY_SQL_PATH:-}"
if [ -n "$verify_sql_path" ]; then
  if [ ! -r "$verify_sql_path" ]; then
    echo "runtime role verification SQL is not readable: $verify_sql_path" >&2
    exit 2
  fi
  psql \
    --set ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --set owner_user="$POSTGRES_USER" \
    --set app_user="$SCAFFOLD_POSTGRES_APP_USER" \
    --file "$verify_sql_path"
fi
