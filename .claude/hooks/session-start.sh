#!/bin/bash
# Prepare a web session to run this project's tests.
#
# C1 executes every job in a container, so without a Docker daemon the sandbox
# tests cannot run at all — and a suite that cannot run is worse than a failing
# one, because it reports nothing.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# --- Python dependencies -------------------------------------------------------
# Installed for the interpreter that runs pytest, so the suite can import both the
# project's deps and the project itself.
pip install --quiet --disable-pip-version-check --root-user-action=ignore boto3 structlog pytest >/dev/null
for tool in ruff mypy; do
  command -v "$tool" >/dev/null 2>&1 || pip install --quiet --disable-pip-version-check --root-user-action=ignore "$tool" >/dev/null
done

# --- Docker daemon -------------------------------------------------------------
if ! docker info >/dev/null 2>&1; then
  mkdir -p /etc/docker
  # Docker Hub's blob CDN is refused by egress policy in this environment;
  # mirror.gcr.io serves the same images and is reachable.
  if [ ! -s /etc/docker/daemon.json ]; then
    printf '{"registry-mirrors": ["https://mirror.gcr.io"]}\n' > /etc/docker/daemon.json
  fi
  nohup dockerd >/tmp/dockerd.log 2>&1 &
  for _ in $(seq 1 30); do
    if docker info >/dev/null 2>&1; then break; fi
    sleep 1
  done
fi

if docker info >/dev/null 2>&1; then
  echo "session-start: docker ready, python deps installed"
else
  # Never fail the session over this — say what is missing instead.
  echo "session-start: WARNING docker daemon did not start; sandbox tests (C1+) will not run. See /tmp/dockerd.log" >&2
fi
