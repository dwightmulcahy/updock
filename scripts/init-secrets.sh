#!/usr/bin/env bash
# Create ./secrets with a random UI password and API token, and an empty SMTP password.
# Existing files are never overwritten. Usage: scripts/init-secrets.sh [secrets-dir]
set -euo pipefail
dir="${1:-$(cd "$(dirname "$0")/.." && pwd)/secrets}"
mkdir -p "$dir"
chmod 700 "$dir"
umask 077

rand() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }

make() {  # make NAME VALUE
  local f="$dir/$1"
  if [ -e "$f" ]; then
    echo "kept     $f"
  else
    printf '%s' "$2" > "$f"
    echo "created  $f"
  fi
}

make updock_password "$(rand)"
make updock_api_token "$(rand)"
make updock_smtp_password ""
echo
echo "UI login: admin / $(cat "$dir/updock_password")"
