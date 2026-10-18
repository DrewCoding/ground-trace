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

Lambda tests, from `infrastructure/aws/lambda/` (pytest + moto, no AWS needed):

```bash
python -m pytest                                  # everything
python -m pytest tests/test_fixes.py              # one file
python -m pytest -k backfill                      # by name
```

Dependencies are in `requirements-dev.txt` and are test-only. The deployed
Lambda still depends on nothing beyond boto3, which the Python 3.13 runtime
provides. On Windows, create the venv at a short path: moto's nested layout
breaks the 260-character path limit. The frontend and Go backend have no tests.

The tests drive `handler.lambda_handler` and `ecs_events.lambda_handler` with
real event shapes rather than calling internals, so they survive refactoring.
moto models DynamoDB faithfully but crashes on ECS awsvpc networking, so
`conftest.py` swaps the ECS/EC2 clients for small fakes. moto's GSIs update
instantly, so index-lag races have to be stubbed (see `test_internals.py`).

## Architecture

### Control plane vs. data plane

API Gateway is **control plane only**. It answers "where do I go?" once, then
the client talks straight to the Fargate task over UDP 7777. Gameplay traffic
never traverses the API and could not — it's an HTTP API and this is a 20 Hz UDP
loop. Any change that routes game traffic through AWS-managed HTTP is wrong.

### Two Lambdas, one package

`lambda/matchmaker/` holds two entry points sharing the `mm` package, zipped
once and deployed as two functions:

- [handler.py](infrastructure/aws/lambda/matchmaker/handler.py) — every API
  route: auth, method/path dispatch, exception → status mapping.
- [ecs_events.py](infrastructure/aws/lambda/matchmaker/ecs_events.py) — ECS
  `STOPPED` task events from EventBridge (`ecs_events.tf`). Deletes the
  session row and emits `TaskBilledSeconds`. Its own role can only
  `DeleteItem` on sessions.

Inside `mm/`, imports flow one way: `tickets` → `matching` → `claims`,
`sessions`, `backends` → `config`/`ddb`. `claims` exists so `tickets` and
`matching` can share ticket transitions without importing each other. Keep it
acyclic.

`mm/backends/` is the compute seam: a backend is a module exposing
`has_capacity() -> bool`, `provision() -> session_id | None` (None = no server
right now, players are released for the next poll) and
`resolve_address(session) -> (ip, port) | None`, selected by the
`ORCHESTRATOR` env var (default `fargate`; an unknown value fails the cold
start). Only the selected backend is imported, because each reads its own
required env vars at import. `fargate.py` launches a task per match;
`agones.py` allocates from a Fleet's warm pool over REST with mTLS — see
`docs/phase-2.md` for the allocator's response codes and why the cluster runs
our own allocator certificates. Session rows carry `orchestrator`, and the
Fargate cap counts only Fargate rows, since nothing deletes Agones rows.

State lives in two DynamoDB tables: `queue` (tickets) and `sessions`
(servers), each with a `status`-keyed GSI and TTL on `expiresAt`.

`try_form_match` runs on every join **and every poll of a waiting ticket**, in
this order: backfill an existing server with a free slot → else require a full
match's worth of players → else check the concurrency cap → then claim tickets
and provision. Errors inside it are caught by `tickets._match_opportunistically`
so they never fail the request; they surface as the `MatchingErrors` metric and
alarm instead of Lambda `Errors`.

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

- **TTL is garbage collection; age is correctness.** DynamoDB TTL deletes only
  "within a few days", and expired items still come back from reads, queries
  and scans until then. Never treat row existence as proof of liveness.
  Sessions are judged by `lastHeartbeat` against `SESSION_STALE_SECONDS` (45s);
  tickets by `lastSeenAt` against `TICKET_STALE_SECONDS` (10s, ghost cutoff for
  matching) and `TICKET_TTL_SECONDS` (300s, when a poll reports `expired`).
- **Matching is level-triggered.** Anything that could make a match possible
  must be noticed by a later poll, not only by a join. If you add a new reason
  a match can become possible, it's covered automatically — don't add a second
  trigger without a reason polls can't cover.
- **No blind upserts.** `UpdateItem` creates missing items. Every update to a
  row someone else may delete carries `ddb.EXISTS_TICKET` / `ddb.EXISTS_SESSION`.
  Without it, a heartbeat with a made-up id creates a session that counts
  against the cap (that was a real DoS with the extractable API key), and a
  write racing a delete resurrects a partial row with no `expiresAt` that is
  never garbage collected.
- **A session row exists exactly while its task is billed** — written after
  `RunTask` succeeds, deleted by `ecs_events` on `STOPPED`. That's why
  `active_session_count` deliberately counts every row, stale or not: a wedged
  server still costs money. The game server never deregisters (by design; see
  `SessionHeartbeat.cs` in the Unity repo), so don't put end-of-session logic in
  `deregister` — it never runs.
- **`ecs_events` dedupes via the conditional delete.** ECS delivers at least
  once; only the delivery that gets the row back emits metrics. Anything else
  that must happen once per ended session belongs behind that same check.
- **Provisioning must stay asynchronous.** API Gateway caps integrations at 29
  seconds and a Fargate cold start routinely exceeds that. `RunTask` is fired,
  `provisioning` is returned, and the client polls.
- **`RunTask` returns HTTP 200 even when placement failed** — the reason goes in
  `failures` and `tasks` comes back empty. Always check both.
- **A Fargate task cannot discover its own public IP.** The address is on the
  ENI, outside the container, and there is no instance metadata endpoint. IP
  resolution is driven by the client's own poll (`fargate.resolve_address`),
  which caches to the session row. No timer, no idle compute.
- **Ticket claims are optimistic, not locked.** A conditional `UpdateItem`
  (`status` must still be `waiting`) means exactly one concurrent invocation
  wins; the loser rolls its claims back via `release_tickets`.
- **The server cap is soft.** Count-then-`RunTask` is check-then-act; two
  invocations claiming different pairs can both pass. Accepted at this scale;
  a hard cap needs an atomic counter decremented by `ecs_events`.
- `live_waiting_tickets` queries **without `Limit`**: DynamoDB applies `Limit`
  before `FilterExpression`, so a ghost among the first N would hide live
  tickets. It pages until it has enough instead.
- `active_session_count` uses a `Scan`. Fine at this size, acknowledged as the
  thing to revisit at volume.
- Because matching re-runs on every waiting poll, `CapacityCapHit` and
  `ClaimContention` count polls, not players. Read their trend, not totals.

### Metrics go out via stdout, not the API

`emit_metric` writes CloudWatch Embedded Metric Format JSON using `print`, not
the logger — Lambda's log format would prefix the line and stop CloudWatch
parsing it as EMF. This avoids a `PutMetricData` call in the request path and
needs no extra IAM permission.

Two places hardcode the namespace and must agree: the `METRICS_NAMESPACE`
default in `mm/config.py` (never set as a Lambda env var, so the default is
what is live) and `local.matchmaker_namespace` in `dashboard.tf`. A rename in
one place silently empties the CloudWatch dashboard.

Pass per-event values (`sessionId`, `taskArn`, `stopCode`) as `emit_metric`
keyword properties, never as dimensions: each unique dimension combination is
a separately billed CloudWatch metric. Adding a dimension to an existing
metric also changes its identity, so dashboard widgets querying it without
that dimension go blank — the planned `Orchestrator` dimension has to land
together with a dashboard update.

## Working in this codebase

### Lambda packaging

`lambda.tf` zips `source_dir = lambda/matchmaker` wholesale (minus bytecode,
via `excludes`), so new files there ship automatically with no Terraform
change. Tests live in the sibling `lambda/tests/` precisely so they don't ship.
`conftest.py` sets `sys.dont_write_bytecode` for the same reason.

**Never add a top-level module there named after anything in the standard
library** — `queue.py`, `http.py`, `secrets.py`, `types.py`, and so on. Lambda
puts the function directory at the front of `sys.path`, so it shadows the
stdlib module for boto3/urllib3 too and fails every invocation at import. New
code goes inside `mm/`, where only the package name itself has to be safe.

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
  `cognito.tf` / `dashboard.tf` / `ecs_events.tf`). CI runs `terraform fmt
  -check` with `continue-on-error: true`, so it is advisory. Match the file you
  are editing; new files use 2-space.

### CI

`.github/workflows/terraform.yml` triggers **only on changes under
`infrastructure/`** — `plan` on PRs (posted as a PR comment, truncated at 60k
chars) and `apply` on merges to `main`. Frontend and backend changes are not
deployed by any pipeline. Credentials come from GitHub OIDC federation; there
are no stored AWS access keys.

`.github/workflows/matchmaker-tests.yml` runs pytest on changes under
`infrastructure/aws/lambda/`. It needs no credentials. Note that the Terraform
workflow's `apply` doesn't wait on it: a merge deploys even if tests failed on
the PR, so don't merge red.

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
