#!/usr/bin/env bash
# Commit whatever the poller captured, naming it in the message.
# Takes one argument: a label for the run, e.g. "war poll".
set -euo pipefail

LABEL="${1:-capture}"

if [ -z "$(git status --porcelain data)" ]; then
  echo "no data changes to commit"
  exit 0
fi

# Name the endpoints that actually changed, stripping the timestamp from each
# capture filename so the message reads as a list of sources rather than paths.
ENDPOINTS=$(git status --porcelain data/raw 2>/dev/null \
  | awk '{print $NF}' \
  | xargs -r -n1 basename \
  | sed -E 's/-[0-9]{8}T[0-9]{6}Z\.json\.gz$//' \
  | sort -u \
  | paste -sd', ' - || true)
[ -z "$ENDPOINTS" ] && ENDPOINTS="derived tables only"

git config user.name "coc-telemetry-bot"
git config user.email "coc-telemetry-bot@users.noreply.github.com"
git add data
git commit -m "${LABEL}: ${ENDPOINTS}"

# The concurrency group prevents overlapping runs, but a rebase keeps the push
# safe if one ever slips through.
git pull --rebase --autostash
git push
