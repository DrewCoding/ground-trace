set -euo pipefail

# Uploads the matchmaker's allocator credentials - the CA that signed the
# allocator's certificate, and the client certificate and key on its
# allowlist - as the one SecureString parameter the deployed Lambda reads
# (infrastructure/aws/lambda.tf). Run once; re-run only if the certificates in
# ~/.ground-trace/allocator are reissued.
#
# Kept out of Terraform on purpose: anything Terraform writes is stored in its
# state, and this contains a private key.
#
# Usage: bash scripts/put-allocator-tls.sh

ALLOCATOR="${ALLOCATOR_CERTS:-$HOME/.ground-trace/allocator}"
PARAMETER="${AGONES_TLS_PARAMETER:-/ground-trace/agones/allocator-tls}"
REGION="${AWS_REGION:-us-west-1}"

for f in "$ALLOCATOR/ca.crt" "$ALLOCATOR/client.crt" "$ALLOCATOR/client.key"; do
  if [ ! -f "$f" ]; then
    echo "Missing $f - see docs/phase-2.md." >&2
    exit 1
  fi
done

# The request goes in on stdin rather than as --value: arguments are visible
# to every user on the machine through ps, and this one holds a private key.
jq -n \
  --arg name "$PARAMETER" \
  --rawfile ca "$ALLOCATOR/ca.crt" \
  --rawfile cert "$ALLOCATOR/client.crt" \
  --rawfile key "$ALLOCATOR/client.key" \
  '{Name: $name, Type: "SecureString", Overwrite: true,
    Value: ({ca: $ca, cert: $cert, key: $key} | tojson)}' |
  aws ssm put-parameter --region "$REGION" --cli-input-json file:///dev/stdin >/dev/null

echo "Uploaded $PARAMETER ($REGION)."
