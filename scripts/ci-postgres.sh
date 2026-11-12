#!/usr/bin/env bash
# Starts the CI database: PostgreSQL 17, production's major (render.yaml postgresMajorVersion),
# on localhost:5432 with the tokensage superuser and an empty `tokensage` database. Until
# 2026-11-02 CI ran the runner image's own PostgreSQL 16 (full audit 2026-11-02, TS-7).
#
# Installed from the PostgreSQL project's own apt repository rather than run as a service
# container: those pull from Docker Hub or a mirror, whose shared-IP quotas fail a run before its
# first step (#97; 2026-10-27 audit CH-13). The runner's preinstalled 16 is dropped first so 17
# takes port 5432. TrenchScanner's scripts/ci-postgres.sh does the same for its major.
set -euo pipefail

MAJOR=${PG_MAJOR:-17}

if [[ ! -x /usr/lib/postgresql/$MAJOR/bin/postgres ]]; then
  # The runner's clusters are stopped and empty; dropping them frees 5432 for the new one.
  pg_lsclusters --no-header 2>/dev/null | while read -r version name _; do
    sudo pg_dropcluster --stop "$version" "$name"
  done
  sudo install -d /usr/share/postgresql-common/pgdg
  sudo curl -fsSL --retry 3 -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    https://www.postgresql.org/media/keys/ACCC4CF8.asc
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" |
    sudo tee /etc/apt/sources.list.d/pgdg.list >/dev/null
  # Only the new list: refreshing every source on the runner costs most of a minute.
  sudo apt-get update -qq -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/pgdg.list \
    -o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=0
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "postgresql-$MAJOR" >/dev/null
fi

# The package makes no cluster when the runner's postgresql-common has create_main_cluster off,
# and then the start below failed silently and psql found no socket (CI on 2026-11-12, #105).
if ! pg_lsclusters --no-header 2>/dev/null | awk -v v="$MAJOR" '$1 == v && $2 == "main" { found = 1 } END { exit !found }'; then
  sudo pg_createcluster "$MAJOR" main --port 5432
fi
# "already running" is fine; any other reason shows in the log, with the clusters below
sudo pg_ctlcluster "$MAJOR" main start || true
pg_lsclusters
sudo -u postgres psql -v ON_ERROR_STOP=1 -q \
  -c "CREATE USER tokensage WITH SUPERUSER PASSWORD 'tokensage';" \
  -c "CREATE DATABASE tokensage OWNER tokensage;"
pg_isready -h localhost -p 5432 -U tokensage
psql postgresql://tokensage:tokensage@localhost:5432/tokensage -Atc 'SHOW server_version'
