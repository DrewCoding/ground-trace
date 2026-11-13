# Player identities, for queueing into matches.
#
# A second pool, sharing nothing with the operator pool in cognito.tf. That
# one admits only accounts created by hand; this one has open sign-up, which
# is exactly why they are not the same directory. A player token is issued by
# a different pool, so the dashboard's authorizer rejects it, and the reverse
# holds for an operator token on the queue routes - no group or scope check
# has to be right for that to stay true.
#
# What this buys is one account per verified email address, and through the
# matchmaker, one place in the queue per account. It is not one account per
# person: anyone with a second address can register twice.

resource "aws_cognito_user_pool" "players" {
  name = "${var.project_name}-players"

  # Sign-in is by email, and Cognito refuses a second account for an address
  # already registered. Case-insensitive so Foo@ and foo@ are one account.
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  username_configuration {
    case_sensitive = false
  }

  # The account doesn't exist as far as sign-in is concerned until the code
  # emailed to it is entered, which is what ties an account to an address its
  # owner can actually read.
  #
  # Sent through Cognito's built-in sender, which is capped at roughly 50
  # messages a day per account. Enough to test with; a real launch needs
  # email_configuration pointed at SES.
  verification_message_template {
    default_email_option = "CONFIRM_WITH_CODE"
    email_subject        = "Your verification code"
    email_message        = "Your verification code is {####}"
  }

  # Looser than the operator pool: these guard a place in a game queue, and
  # the password is typed into a game window rather than a password manager.
  password_policy {
    minimum_length                   = 8
    require_lowercase                = true
    require_uppercase                = false
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 7
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

resource "aws_cognito_user_pool_client" "game" {
  name         = "${var.project_name}-game"
  user_pool_id = aws_cognito_user_pool.players.id

  # The game build can't keep a secret any better than a browser can.
  generate_secret = false

  # The game has its own login form and talks to Cognito's API directly, so
  # there is no hosted UI, no OAuth flow and no callback URL. USER_PASSWORD
  # sends the password over TLS to Cognito; SRP would avoid sending it at all
  # but means carrying an SRP implementation in the client.
  explicit_auth_flows = [
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  # The ID token is what the client sends to the matchmaker. Short, because
  # it can't be revoked; the refresh token is what keeps a player signed in.
  access_token_validity  = 1
  id_token_validity      = 1
  refresh_token_validity = 30

  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }

  # A wrong password and an unknown address get the same answer, so the
  # login form can't be used to find out who has an account.
  prevent_user_existence_errors = "ENABLED"
}

# The queue routes' gate. Same mechanism as the dashboard's authorizer, aimed
# at the other pool: API Gateway verifies the token itself and hands the
# Lambda its claims, of which the matchmaker uses sub as the player's id.
resource "aws_apigatewayv2_authorizer" "players" {
  api_id           = aws_apigatewayv2_api.matchmaker.id
  name             = "${var.project_name}-players"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    audience = [aws_cognito_user_pool_client.game.id]
    issuer   = "https://${aws_cognito_user_pool.players.endpoint}"
  }
}

# The two values the Unity client needs, for MatchmakerClient's Inspector.
output "player_pool_client_id" {
  value = aws_cognito_user_pool_client.game.id
}

output "player_pool_region" {
  value = var.aws_region
}
