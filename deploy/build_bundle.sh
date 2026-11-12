#!/usr/bin/env bash
# Builds dist/grok-desk-install.sh: a single self-extracting installer (code only, no secrets).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p dist
RAW="logs/raw/pumpportal_migration.json logs/raw/dexscreener_pair.json logs/raw/rugcheck_report.json
logs/raw/rugcheck_insiders_graph.json logs/raw/rugcheck_insiders_graph_A3qwGJEEc6Vzq17gWj3T97J1ArR3fHD5wgNmR2pkpump.json
logs/raw/rpc_getAccountInfo.json logs/raw/rpc_getProgramAccounts_token.json logs/raw/rpc_getProgramAccounts_token2022_actual_owner.json logs/raw/rpc_getTokenSupply.json logs/raw/rpc_getTokenLargestAccounts_429.json"
tar --sort=name --owner=0 --group=0 --mtime='2026-11-12' --exclude=__pycache__ -czf dist/code.tgz \
  *.py agents web tests tools deploy requirements.txt SPEC.md SPEC_VERBATIM.md README.md decisions.md fields_mapping.json fields_mapping.md settings_seed.json $RAW
SUM=$(sha256sum dist/code.tgz | cut -d' ' -f1)
{
  echo '#!/usr/bin/env bash'
  echo '# Grok desk self-extracting installer. Code only — secrets are generated on the server.'
  echo 'set -euo pipefail'
  echo 'T=$(mktemp -d); trap "rm -rf $T" EXIT'
  echo "sed '1,/^__PAYLOAD__\$/d' \"\$0\" | base64 -d > \$T/code.tgz"
  echo "echo \"$SUM  \$T/code.tgz\" | sha256sum -c --quiet"
  echo 'mkdir -p $T/src && tar -xzf $T/code.tgz -C $T/src'
  echo 'GROK_DESK_SRC=$T/src bash $T/src/deploy/install.sh'
  echo 'exit 0'
  echo '__PAYLOAD__'
  base64 -w 76 dist/code.tgz
} > dist/grok-desk-install.sh
echo "dist/grok-desk-install.sh $(wc -c < dist/grok-desk-install.sh) bytes, payload sha256 $SUM"
