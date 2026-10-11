# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

On-demand multiplayer matchmaking on AWS. A player joins a queue; a dedicated
Fargate game server is provisioned only once enough players are waiting, and it
terminates itself when the match empties. Idle cost is near zero by design, so
*nothing runs when nobody is playing* is a hard constraint, not an aspiration.

Read `README.md` first — it documents the architecture and the reasoning behind
each design decision in depth. This file covers what isn't in there.

## Commands

All Terraform runs from `infrastructure/aws`:

```bash
terraform init
terraform plan
terraform apply
terraform fmt -recursive
terraform output matchmaker_api_url   # endpoint the Unity client needs
```

`matchmaker_api_key` is a required variable with no default. It lives in
`terraform.tfvars`, which is gitignored — plan and apply both fail without it.
In CI it arrives as `TF_VAR_matchmaker_api_key` from a repository secret.

Frontend, from `frontend/`:

```bash
npm run dev      # Vite dev server on :5173 (the only Cognito callback URL by default)
npm run build    # tsc -b && vite build
npm run lint     # eslint
```

`frontend/.env.local` holds the Cognito and API values; `.env.example` lists the
keys. Populate them from `terraform output` (`cognito_client_id`,
`cognito_hosted_ui_domain`, `matchmaker_api_url`).

Launch a game server by hand, bypassing matchmaking entirely:

```bash
bash scripts/run-server.sh
```

**There are no tests and no test runner anywhere in this repo** — no pytest, no
`requirements.txt`, no Vitest. Don't go looking for one. The Lambda has no
dependencies beyond boto3, which the Python 3.13 runtime provides.

## Architecture

### Control plane vs. data plane

API Gateway is **control plane only**. It answers "where do I go?" once, then
the client talks straight to the Fargate task over UDP 7777. Gameplay traffic
never traverses the API and could not — it's an HTTP API and this is a 20 Hz UDP
loop. Any change that routes game traffic through AWS-managed HTTP is wrong.

### The matchmaker is one Lambda behind every route

[infrastructure/aws/lambda/matchmaker/handler.py](infrastructure/aws/lambda/matchmaker/handler.py)
is a single ~520-line file handling all six routes via manual method/path
dispatch in `lambda_handler`. State lives in two DynamoDB tables: `queue`
(tickets) and `sessions` (servers), each with a `status`-keyed GSI and TTL on
`expiresAt`.

Match formation is re-evaluated on every join (`try_form_match`), in this order:
backfill an existing server with a free slot → else require a full match's worth
of players → else check the concurrency cap → then claim tickets and provision.

### Two independent auth paths

This is the easiest thing to break, because it spans three files:

- **Game client routes** (`/queue`, `/sessions/*`) authenticate with a shared
  `x-api-key` header, compared using `hmac.compare_digest`. That key ships
  inside the game build and is therefore recoverable.
- **`GET /dashboard`** is gated by a Cognito JWT authorizer declared in
  `cognito.tf` and attached to the route in `apigateway.tf`. The handler
  **explicitly exempts this one route** from the `x-api-key` check, because such
  requests arrive with a Bearer token and no key. If you add or move an
  authorizer in Terraform, the handler's exemption condition has to move with it.

Because the API key is recoverable, `MAX_CONCURRENT_SESSIONS` — not the key — is
the real ceiling on spend. It holds even against someone spamming the endpoint
with an extracted key. Treat it as a security control.

### Correctness invariants worth knowing before editing

- **TTL is garbage collection; `lastHeartbeat` is correctness.** DynamoDB TTL is
  best-effort (AWS promises only "typically within 48 hours"), so a dead
  server's row can linger. Liveness is decided in code by age against
  `SESSION_STALE_SECONDS` (45s). Never treat row existence as proof a server is
  alive. Both `find_joinable_session` and the dashboard's `stale` flag depend on
  this.
- **Provisioning must stay asynchronous.** API Gateway caps integrations at 29
  seconds and a Fargate cold start routinely exceeds that. `RunTask` is fired,
  `provisioning` is returned, and the client polls.
- **`RunTask` returns HTTP 200 even when placement failed** — the reason goes in
  `failures` and `tasks` comes back empty. Always check both.
- **A Fargate task cannot discover its own public IP.** The address is on the
  ENI, outside the container, and there is no instance metadata endpoint. IP
  resolution is driven by the client's own poll (`resolve_public_ip`), which
  caches to the session row. No timer, no idle compute.
- **Ticket claims are optimistic, not locked.** A conditional `UpdateItem`
  (`status` must still be `waiting`) means exactly one concurrent invocation
  wins; the loser rolls its claims back via `release_tickets`.
- `active_session_count` uses a `Scan`. Fine at this size, acknowledged as the
  thing to revisit at volume.

### Metrics go out via stdout, not the API

`emit_metric` writes CloudWatch Embedded Metric Format JSON using `print`, not
the logger — Lambda's log format would prefix the line and stop CloudWatch
parsing it as EMF. This avoids a `PutMetricData` call in the request path and
needs no extra IAM permission.

Two places hardcode the namespace and must agree: the `METRICS_NAMESPACE`
default in `handler.py` (never set as a Lambda env var, so the default is what
is live) and `local.matchmaker_namespace` in `dashboard.tf`. A rename in one
place silently empties the CloudWatch dashboard.

## Working in this codebase

### Lambda packaging

`lambda.tf` zips `source_dir = lambda/matchmaker` wholesale, so new `.py` files
beside `handler.py` ship automatically with no Terraform change, as long as
`handler.lambda_handler` still exists.

One caveat if you split the file up: **do not create `queue.py` there.** Lambda
puts the function directory at the front of `sys.path`, so it would shadow the
stdlib `queue` module that urllib3 (under boto3) imports, failing at import time
on every invocation.

### Terraform notes

- The S3 backend in `main.tf` hardcodes the bucket, key and region because
  backend blocks cannot reference variables. Locking is S3-native
  (`use_lockfile = true`); the DynamoDB lock table pattern most guides show was
  deprecated in Terraform 1.11.
- A GSI is a **separate ARN from its table**. Granting `Query` on a table does
  not permit querying its index — `iam.tf` needs `table/.../index/*` too.
- `iam:PassRole` is distinct from `ecs:RunTask`. Granting only `RunTask`
  produces an AccessDenied that reads like `RunTask` is missing.
- The GitHub OIDC trust in `oidc.tf` pins the **immutable numeric IDs** in the
  `sub` claim (`repo:DrewCoding@122519034/ground-trace@1345255887:ref:...`), not
  the `repo:owner/name:*` form every guide shows. The provider ARN is
  constructed from the account ID rather than read via data source, which would
  require `iam:ListOpenIDConnectProviders` that `PowerUserAccess` does not grant.
- HCL formatting is inconsistent (4-space in the older files, 2-space in
  `cognito.tf` / `dashboard.tf`). CI runs `terraform fmt -check` with
  `continue-on-error: true`, so it is advisory. Match the file you are editing.

### CI

`.github/workflows/terraform.yml` triggers **only on changes under
`infrastructure/`** — `plan` on PRs (posted as a PR comment, truncated at 60k
chars) and `apply` on merges to `main`. Frontend and backend changes are not
deployed by any pipeline. Credentials come from GitHub OIDC federation; there
are no stored AWS access keys.

### CORS is doing real work

`apigateway.tf` allows only `GET` and does not list `x-api-key` as an allowed
header. That is deliberate: a malicious page cannot reach the mutating routes
cross-origin even with a leaked key. Adding `x-api-key` to `allow_headers` or
widening `allow_methods` would remove that protection. (Note the comment block
there still describes the dashboard route as unauthenticated, which predates the
JWT authorizer.)

### Comment style

Comments throughout explain *why*, usually by naming the concrete failure mode
avoided ("Indexing straight into tasks[0] would raise IndexError and throw away
the only useful information"). Several record observed behaviour — a session row
advertised a dead server for 139 seconds before TTL caught it. Match that
register: explain the trap, not the syntax.

## Repo areas that are not real yet

- `backend/main.go` — a Go stub that prints `"huh."`. No build wired up.
- `scripts/startup.sh` — empty.
- `commands.md` — scratch notes holding one `admin-create-user` invocation.
- `frontend/README.md` — unmodified Vite boilerplate; ignore it.

Operator accounts are created by hand (`allow_admin_create_user_only = true`);
there is no sign-up flow. The dashboard must send the Cognito **ID token**, not
the access token — Cognito's access token carries `client_id` rather than an
`aud` claim, and the authorizer is configured with an audience, so using it
yields a 401 that looks like a broken login.

Per `README.md`, next up: live frontend monitoring, a client/server build
pipeline, and Cognito-authenticated queue tickets.
