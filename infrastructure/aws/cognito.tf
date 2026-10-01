# Operator identities for the status dashboard.
#
# Deliberately separate from any future player directory. Players would
# self-register through an open sign-up flow; operators are created by hand.
# Keeping them in one pool would mean a public sign-up endpoint writing into
# the same directory that gates the admin surface, with only group membership
# and app-client scoping standing between the two. Separate pools make that
# structurally impossible rather than merely unlikely.
#
# This is a user pool, not an identity pool: it issues JWTs that API Gateway
# validates natively. An identity pool would hand the browser temporary AWS
# credentials, which is not something a dashboard should ever hold.

variable "dashboard_callback_urls" {
  type        = list(string)
  default     = ["http://localhost:5173/"]
  description = "OAuth redirect targets. Add the CloudFront URL once it exists."
}

resource "aws_cognito_user_pool" "operators" {
  name = "${var.project_name}-operators"

  # No self-service sign-up. Operators are created with admin-create-user.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 3
  }

  # Optional rather than required so first login isn't blocked on setup.
  # Worth switching to "ON" for anything beyond a personal project.
  mfa_configuration = "OPTIONAL"

  software_token_mfa_configuration {
    enabled = true
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  tags = {
    Project = var.project_name
  }
}

resource "aws_cognito_user_pool_domain" "operators" {
  domain       = "${var.project_name}-ops-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.operators.id
}

resource "aws_cognito_user_pool_client" "dashboard" {
  name         = "${var.project_name}-dashboard"
  user_pool_id = aws_cognito_user_pool.operators.id

  # A browser cannot keep a secret, so this is a public client using the
  # authorization code flow with PKCE. The implicit flow is the older pattern
  # and leaks tokens through the URL fragment.
  generate_secret = false

  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]

  callback_urls = var.dashboard_callback_urls
  logout_urls   = var.dashboard_callback_urls

  access_token_validity  = 1
  id_token_validity      = 1
  refresh_token_validity = 30

  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }

  # Returns a distinct error for a user that doesn't exist vs a bad password
  # when disabled - enabled here so failures are indistinguishable.
  prevent_user_existence_errors = "ENABLED"
}

# API Gateway validates Cognito's JWTs itself. No Lambda authorizer, no
# Lambda@Edge - the token never reaches application code unverified.
resource "aws_apigatewayv2_authorizer" "dashboard" {
  api_id           = aws_apigatewayv2_api.matchmaker.id
  name             = "${var.project_name}-dashboard"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    audience = [aws_cognito_user_pool_client.dashboard.id]
    issuer   = "https://${aws_cognito_user_pool.operators.endpoint}"
  }
}

output "cognito_user_pool_id" {
  value = aws_cognito_user_pool.operators.id
}

output "cognito_client_id" {
  value = aws_cognito_user_pool_client.dashboard.id
}

output "cognito_hosted_ui_domain" {
  value = "https://${aws_cognito_user_pool_domain.operators.domain}.auth.${var.aws_region}.amazoncognito.com"
}
