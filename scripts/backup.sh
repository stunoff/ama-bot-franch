#!/usr/bin/env bash
set -euo pipefail
# Run from the project root. The runner journals the operation, stops writes,
# creates a private consistent snapshot and resumes only previously running services.
exec python3 scripts/operations-runner.py --backup-root "${1:?Usage: scripts/backup.sh /absolute/backup/root}" --request-backup
