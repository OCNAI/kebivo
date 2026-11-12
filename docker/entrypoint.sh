#!/bin/sh
set -eu

# Fail before migration if required production configuration is invalid.
python -m kebivo.first_run
python -m kebivo.settings
alembic upgrade head
exec "$@"
