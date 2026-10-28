set -euo pipefail

# Installs or upgrades Agones on the local k3s cluster with this repo's
# values plus the allocator certificates, which live outside the repo.
#
# Use this rather than a bare `helm upgrade`. With generateTLS=false and no
# tlsCert given, the chart doesn't fail - it silently installs sample
# certificates bundled inside the chart, and the matchmaker's next
# allocation dies in the TLS handshake.
#
# Usage: bash scripts/install-agones-local.sh

AGONES_VERSION="1.61.0"
CERTS="${CERTS:-$HOME/.ground-trace/allocator}"
VALUES="$(dirname "$0")/../infrastructure/k8s/agones/values-local.yaml"

for f in server.crt server.key client.crt; do
  if [ ! -f "$CERTS/$f" ]; then
    echo "Missing $CERTS/$f - see docs/phase-2.md, step 1." >&2
    exit 1
  fi
done

helm repo add agones https://agones.dev/chart/stable >/dev/null 2>&1 || true

# No --reuse-values: the values file and these flags are the whole
# configuration, so the install can't drift from what's in the repo.
#
# The allowlist key must end in .crt - the allocator silently skips any
# other file name and then rejects every client.
helm upgrade --install agones agones/agones \
  --namespace agones-system --create-namespace \
  --version "$AGONES_VERSION" \
  -f "$VALUES" \
  --set-file agones.allocator.tlsCert="$CERTS/server.crt" \
  --set-file agones.allocator.tlsKey="$CERTS/server.key" \
  --set-file 'agones.allocator.clientCAs.matchmaker\.crt'="$CERTS/client.crt"

# Certificates are mounted from secrets, and without generateTLS nothing
# rolls the allocator when they change.
kubectl rollout restart deploy/agones-allocator -n agones-system
kubectl rollout status deploy/agones-allocator -n agones-system --timeout=180s
