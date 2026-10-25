#!/bin/sh
set -eu

# Fail before migration if required production configuration is invalid.
python -c 'from kebivo.settings import Settings; Settings.from_env()'
alembic upgrade head
exec "$@"
