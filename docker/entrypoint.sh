#!/bin/sh
set -eu

# New database, WAL, backup and lock files are readable only by the app user.
umask 077

# Fail before migration if required production configuration is invalid.
python -m kebivo.first_run
python -m kebivo.settings
# Lock /data for this container's lifetime, back up before an upgrade, migrate, then
# replace this process with the app (the lock descriptor is inherited).
exec python -m kebivo.startup "$@"
