# GitHub Actions authenticates to AWS by federation rather than by storing
# access keys as repository secrets. Actions presents a short-lived OIDC token
# describing which repo and ref is running; STS exchanges it for temporary
# credentials. Nothing long-lived is stored anywhere, so there is nothing to
# rotate and nothing to leak.
#
# Bootstrap note: this is applied locally the first time, because CI cannot
# assume a role that does not exist yet.

variable "github_repository" {
  type        = string
  default     = "DrewCoding/ground-trace"
  description = "owner/repo allowed to assume the deploy role."
}

# Referenced, not managed. AWS permits one OIDC provider per URL per account,
# and this one already existed - it's shared account-level infrastructure
# rather than something this project owns. A data source means destroying this
# project can't take out anything else that federates through it.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

data "aws_iam_policy_document" "github_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # THE important line. Without a sub condition scoped to this repository,
    # any GitHub Actions workflow anywhere in the world could assume this
    # role. This is the classic OIDC misconfiguration.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:*"]
    }
  }
}

resource "aws_iam_role" "github_actions" {
  name               = "${var.project_name}-github-actions"
  description        = "Assumed by GitHub Actions to run Terraform."
  assume_role_policy = data.aws_iam_policy_document.github_assume.json

  # Actions runs are short; no reason to hand out an 8-hour credential.
  max_session_duration = 3600
}

# Broad on services, deliberately excludes IAM user/account management.
resource "aws_iam_role_policy_attachment" "github_actions_power" {
  role       = aws_iam_role.github_actions.name
  policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"
}

data "aws_iam_policy_document" "github_actions_extra" {
  # PowerUserAccess stops short of IAM, but Terraform has to manage this
  # project's roles - scoped to the project prefix rather than granted whole.
  statement {
    sid    = "ManageProjectIam"
    effect = "Allow"
    actions = [
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:GetRole",
      "iam:ListRoles",
      "iam:TagRole",
      "iam:UpdateRole",
      "iam:AttachRolePolicy",
      "iam:DetachRolePolicy",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:GetRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:PassRole",
    ]
    resources = ["arn:aws:iam::*:role/${var.project_name}-*"]
  }

  statement {
    sid    = "ManageProjectPolicies"
    effect = "Allow"
    actions = [
      "iam:CreatePolicy",
      "iam:DeletePolicy",
      "iam:GetPolicy",
      "iam:GetPolicyVersion",
      "iam:ListPolicyVersions",
      "iam:CreatePolicyVersion",
      "iam:DeletePolicyVersion",
    ]
    resources = ["arn:aws:iam::*:policy/${var.project_name}-*"]
  }

  # No write permissions on the OIDC provider: it's shared account-level
  # infrastructure this project only reads. The data source needs
  # iam:GetOpenIDConnectProvider, which PowerUserAccess already allows.

  # Read and write the remote state, including the .tflock object.
  statement {
    sid    = "TerraformState"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
    ]
    resources = [
      "arn:aws:s3:::ground-trace-tfstate-970208041269",
      "arn:aws:s3:::ground-trace-tfstate-970208041269/*",
    ]
  }
}

resource "aws_iam_role_policy" "github_actions_extra" {
  name   = "${var.project_name}-github-actions-extra"
  role   = aws_iam_role.github_actions.id
  policy = data.aws_iam_policy_document.github_actions_extra.json
}

output "github_actions_role_arn" {
  value       = aws_iam_role.github_actions.arn
  description = "role-to-assume for aws-actions/configure-aws-credentials."
}
