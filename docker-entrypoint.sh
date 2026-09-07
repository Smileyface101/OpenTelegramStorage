#!/bin/sh
set -e
mkdir -p "${OTG_DATA_DIR:-/data}/staging"
echo "Applying database migrations..."
python -m alembic upgrade head
exec "$@"
