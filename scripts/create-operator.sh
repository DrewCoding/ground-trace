set -euo pipefail

# Creates a dashboard operator in the Cognito user pool. The pool has no
# self-service sign-up, so this is the only way an account comes into being.
#
# Usage: bash scripts/create-operator.sh <email> <password>

REGION="${AWS_REGION:-us-west-1}"
PROJECT="${PROJECT_NAME:-ground-trace}"

if [ "$#" -ne 2 ]; then
  echo "Usage: $0 <email> <password>" >&2
  exit 1
fi

# The pool signs in by email, so the email is the username.
EMAIL="$1"
PASSWORD="$2"

echo "Resolving user pool..."
POOL_ID="${USER_POOL_ID:-$(aws cognito-idp list-user-pools --region "$REGION" --max-results 60 \
  --query "UserPools[?Name=='${PROJECT}-operators'].Id | [0]" --output text)}"

if [ -z "$POOL_ID" ] || [ "$POOL_ID" = "None" ]; then
  echo "Could not find user pool ${PROJECT}-operators - is the infrastructure applied?" >&2
  exit 1
fi

echo "  pool:   $POOL_ID"

if aws cognito-idp admin-get-user --region "$REGION" --user-pool-id "$POOL_ID" \
    --username "$EMAIL" > /dev/null 2>&1; then
  echo "User $EMAIL already exists." >&2
  exit 1
fi

# SUPPRESS skips the temporary-password email - the password is set directly
# below, so there is nothing to send.
echo "Creating user..."
aws cognito-idp admin-create-user --region "$REGION" \
  --user-pool-id "$POOL_ID" \
  --username "$EMAIL" \
  --user-attributes Name=email,Value="$EMAIL" Name=email_verified,Value=true \
  --message-action SUPPRESS > /dev/null

# --permanent moves the user straight to CONFIRMED instead of forcing a
# password change on first login. If the password fails the pool's policy
# (12+ chars, upper, lower, number, symbol), the user would be left behind
# with no usable password, so remove it rather than leave a half-made account.
echo "Setting password..."
if ! aws cognito-idp admin-set-user-password --region "$REGION" \
    --user-pool-id "$POOL_ID" \
    --username "$EMAIL" \
    --password "$PASSWORD" \
    --permanent; then
  echo "Password rejected - removing the half-created user." >&2
  aws cognito-idp admin-delete-user --region "$REGION" \
    --user-pool-id "$POOL_ID" --username "$EMAIL"
  exit 1
fi

echo
echo "  Operator $EMAIL created."
