#!/bin/sh
# Optional manual helper. Docker runtime healthchecks use healthcheck.py directly.

set -eu

cd "$(dirname "$0")"
exec python healthcheck.py "$@"
