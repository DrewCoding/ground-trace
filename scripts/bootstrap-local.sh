set -euo pipefail

# The only thing applied to the local cluster by hand. Creates the Secrets that
# can't live in git, installs Argo CD, and hands it the root Application -
# after that, Agones and the game server Fleet come from git.
#
# Safe to re-run: every step is an apply or an upgrade.
#
# Usage: bash scripts/bootstrap-local.sh
# Then:  sync the agones app once (it's manual), e.g. from the UI:
#        kubectl port-forward -n argocd svc/argocd-server 8081:80

ARGOCD_CHART_VERSION="10.11.6"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ALLOCATOR="${ALLOCATOR_CERTS:-$HOME/.ground-trace/allocator}"
WEBHOOK="${WEBHOOK_CERTS:-$HOME/.ground-trace/webhook}"
MATCHMAKER_KEY="${MATCHMAKER_KEY:-local-dev-key}"

for f in "$ALLOCATOR/server.crt" "$ALLOCATOR/server.key" "$ALLOCATOR/client.crt" \
         "$WEBHOOK/server.crt" "$WEBHOOK/server.key"; do
  if [ ! -f "$f" ]; then
    echo "Missing $f - see docs/phase-2.md (allocator) and docs/phase-4.md (webhook)." >&2
    exit 1
  fi
done

# create --dry-run | apply, so re-running updates instead of failing on
# "already exists".
apply_secret() {
  kubectl create secret "$@" --dry-run=client -o yaml | kubectl apply -f -
}

echo "Creating Secrets..."
kubectl create namespace agones-system --dry-run=client -o yaml | kubectl apply -f -

# The admission webhook's key. values-local.yaml carries the matching public
# cert as the caBundle; the two must come from the same pair.
apply_secret generic agones-cert -n agones-system \
  --from-file=server.crt="$WEBHOOK/server.crt" \
  --from-file=server.key="$WEBHOOK/server.key"

# The allocator's serving cert, and the allowlist of client certs it accepts.
# The allowlist key must end in .crt: the allocator silently skips any other
# file name and then rejects every client.
apply_secret tls allocator-tls -n agones-system \
  --cert="$ALLOCATOR/server.crt" --key="$ALLOCATOR/server.key"
apply_secret generic allocator-client-ca -n agones-system \
  --from-file=matchmaker.crt="$ALLOCATOR/client.crt"

# What game servers send as x-api-key with their heartbeats.
apply_secret generic matchmaker-key -n default --from-literal=key="$MATCHMAKER_KEY"

echo "Installing Argo CD..."
helm repo add argo https://argoproj.github.io/argo-helm >/dev/null 2>&1 || true
helm upgrade --install argocd argo/argo-cd \
  --namespace argocd --create-namespace \
  --version "$ARGOCD_CHART_VERSION" \
  -f "$REPO/infrastructure/k8s/argocd/values-local.yaml" \
  --wait

kubectl apply -f "$REPO/infrastructure/k8s/argocd/root-local.yaml"

echo
echo "Done. Argo CD admin password:"
echo "  kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath='{.data.password}' | base64 -d"
echo "UI: kubectl port-forward -n argocd svc/argocd-server 8081:80  ->  http://localhost:8081"
