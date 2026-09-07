#!/bin/sh
set -e
mkdir -p "${OTS_DATA_DIR:-/data}/staging"
echo "Applying database migrations..."
python -m alembic upgrade head
exec "$@"
