#!/bin/sh
set -eu

# Fail before migration if required production configuration is invalid.
python -m kebivo.first_run
python -c 'from kebivo.settings import Settings; Settings.from_env()'
alembic upgrade head
exec "$@"
