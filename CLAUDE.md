# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

On-demand multiplayer matchmaking on AWS. A player joins a queue; a dedicated
game server is provisioned only once enough players are waiting, and it
terminates itself when the match empties. Idle cost is near zero by design, so
*nothing runs when nobody is playing* is a hard constraint, not an aspiration.

Game servers come from Fargate by default. An Agones backend (a warm pool on a
Kubernetes cluster) sits behind the same interface and is selected with one
Terraform variable - see "Two backends" below.

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
In CI it arrives as `TF_VAR_matchmaker_api_key` from the `MATCHMAKER_API_KEY`
repository secret. The two must hold the same value, or local and CI applies
keep swapping the key the Lambda and game servers use. GitHub secrets can't be
read back; the live value is the Lambda's `API_KEY` environment variable.

Matchmaker tests, from `infrastructure/aws/lambda` (Python 3.13 in CI, to match
the Lambda runtime; a newer local Python is fine):

```bash
python3 -m venv .venv && source .venv/bin/activate   # once
pip install -r requirements-dev.txt                  # once
python -m pytest -q                                  # all of them
python -m pytest -q -k heartbeat                     # by name
```

The venv lives in `lambda/`, never in `lambda/matchmaker/` — that folder is
zipped wholesale. Tests run entirely against moto and in-process fakes; they
need no AWS credentials and touch no real resources.

Run the matchmaker locally against a k3s/Agones cluster, with DynamoDB in
memory (needs the certificates in `~/.ground-trace/allocator`):

```bash
python serve_local.py --allocator https://<node-ip>   # http://127.0.0.1:8080
```

Frontend, from `frontend/`:

```bash
npm run dev      # Vite dev server on :5173 (the only Cognito callback URL by default)
npm run build    # tsc -b && vite build
npm run lint     # eslint
```

`frontend/.env.local` holds the Cognito and API values; `.env.example` lists the
keys. Populate them from `terraform output` (`cognito_client_id`,
`cognito_hosted_ui_domain`, `matchmaker_api_url`).

Scripts, from the repo root:

```bash
bash scripts/run-server.sh            # launch a Fargate game server by hand, bypassing matchmaking
bash scripts/create-operator.sh       # create a dashboard login in Cognito
bash scripts/put-allocator-tls.sh     # upload Agones allocator certs to SSM (before orchestrator = agones)
```

The Lambda has no runtime dependencies beyond boto3, which the Python 3.13
runtime provides. `requirements-dev.txt` is test-only and never ships.

## Architecture

### Control plane vs. data plane

API Gateway is **control plane only**. It answers "where do I go?" once, then
the client talks straight to the game server over UDP. Gameplay traffic never
traverses the API and could not — it's an HTTP API and this is a 20 Hz UDP
loop. Any change that routes game traffic through AWS-managed HTTP is wrong.

### Two Lambda functions, one zip

Everything lives in `infrastructure/aws/lambda/matchmaker/`, zipped once and
deployed as two functions with different entry points:

- **`handler.py`** → `ground-trace-matchmaker`, behind every API route. A
  ~90-line router: auth, method/path dispatch, and mapping exceptions to status
  codes (`NotFoundError` → 404, `MatchmakerError` → 503, `ClientError` → 502
  with the AWS error code, anything else → 500 with a traceback in the log).
- **`ecs_events.py`** → `ground-trace-ecs-events`, invoked by EventBridge when a
  game server task reaches `STOPPED`. Deletes the session row, freeing its cap
  slot within seconds, and reports `TaskBilledSeconds`.

Both import the `mm` package, one module per job. Imports only point down this
list - nothing imports a module above it:

| Module | Owns |
|---|---|
| `mm/config.py` | Every environment read and tunable constant, plus `log` |
| `mm/responses.py` | `respond`, `MatchmakerError`, `NotFoundError` |
| `mm/metrics.py` | `emit_metric` (EMF) |
| `mm/ddb.py` | The two tables, `EXISTS_*` conditions, `condition_failed` |
| `mm/claims.py` | Claiming, releasing and stamping tickets |
| `mm/sessions.py` | Session rows: joinable lookup, cap count, heartbeat, IP cache, ending |
| `mm/dashboard.py` | The `/dashboard` snapshot |
| `mm/backends/` | Where servers come from: `fargate.py`, `agones.py`, and the switch in `__init__.py` |
| `mm/matching.py` | `try_form_match`, `live_waiting_tickets` |
| `mm/tickets.py` | Join, poll and leave |

State lives in two DynamoDB tables: `queue` (tickets) and `sessions` (servers),
each with a `status`-keyed GSI and TTL on `expiresAt`.

### Matching is level-triggered

`try_form_match` runs on **every join and every waiting poll**, not just when a
player arrives. Polls (every ~2s from the client) are the clock; nothing runs on
a timer. Running it only on join used to strand players whenever a match became
possible some other way - capacity freeing, a backfill slot opening, a failed
launch's claims being released. Running it often is safe because claims are
conditional writes.

Order within one evaluation: backfill an existing server with a free slot →
else require a full match's worth of players → else check the backend's
capacity → then claim tickets and provision.

Matching failures never fail the request: `tickets._match_opportunistically`
logs them and emits `MatchingErrors`. A join that returned 503 used to leave a
ticket whose id the client never received. The trade-off is that the Lambda
Errors alarm no longer sees these, which is why `MatchingErrors` has its own.

### Two backends

`mm/backends/__init__.py` defines a three-function contract and imports exactly
one implementation, chosen by `ORCHESTRATOR` (`var.orchestrator` in Terraform):

- `has_capacity()` - the cost ceiling. Fargate counts its own session rows
  against `MAX_CONCURRENT_SESSIONS`; Agones returns `True`, because the Fleet's
  size is the ceiling and the allocator reports an empty pool itself.
- `provision()` - returns a session id, or **`None` for "no server right now"**
  (an empty warm pool, allocation contention). `None` is an answer, not an
  error: matching releases the claimed players to retry on their next poll.
- `resolve_address(session)` - `(ip, port)`, or `None` while unknown.

Only the selected backend is imported, because each reads its own required
settings at import: loading `agones.py` on a Fargate deploy would `KeyError` on
missing `AGONES_*` variables every cold start. An unknown `ORCHESTRATOR` fails
the import on purpose. Switching is a Terraform variable change plus
`agones_allocator_url`; a `precondition` in `lambda.tf` rejects agones without
an `https://` URL at plan time.

Session rows carry `orchestrator`. The Fargate cap counts only Fargate rows
(and untagged ones, which predate the attribute), because nothing deletes Agones
rows when their server ends - `ecs_events` only hears about ECS tasks - and
leftovers would otherwise fill the cap after switching back.

Agones rows start as `status = "allocated"`, which `find_joinable_session`
ignores, so a server isn't offered for backfill until its own first heartbeat
reports a real player count.

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

`API_KEY` is read in `handler.py`, not `mm/config.py`, on purpose. The
ecs-events function imports `mm.config` and is deliberately given no key; in
config, that read would fail its every cold start.

Because the API key is recoverable, `MAX_CONCURRENT_SESSIONS` — not the key — is
the real ceiling on spend. It holds even against someone spamming the endpoint
with an extracted key. Treat it as a security control. It only holds because
session rows can't be created through the API: `heartbeat` refuses unknown ids
with a 404 (see the next section).

### Correctness invariants worth knowing before editing

- **`UpdateItem` creates the item when the key doesn't exist.** Every update to
  a row someone else may have deleted - a ticket the player cancelled, a session
  whose server ended - carries `ConditionExpression = ddb.EXISTS_TICKET` or
  `EXISTS_SESSION`, and treats a failed condition as "gone". Without it the
  write resurrects a partial row with no `expiresAt` that is never garbage
  collected; a session one counts against the cap forever. A blind heartbeat
  upsert once let anyone with the key fill the cap with made-up session ids.
- **TTL is garbage collection; timestamps are correctness.** DynamoDB TTL is
  best-effort (AWS promises only "typically within 48 hours"), so dead rows
  linger and read back like live ones. Liveness is always decided in code by
  age: sessions by `lastHeartbeat` against `SESSION_STALE_SECONDS` (45s),
  tickets by `lastSeenAt` against `TICKET_STALE_SECONDS` (10s, five missed
  polls) for matching and `TICKET_TTL_SECONDS` (300s) for expiry. Never treat
  row existence as proof of life.
- **Ticket liveness is the last poll, not the queue time.** Every poll refreshes
  `lastSeenAt` and `expiresAt` (conditionally - see above). Filtering on
  `queuedAt` used to match crashed clients and skip players who had simply
  waited five minutes.
- **DynamoDB applies `Limit` before `FilterExpression`.** `live_waiting_tickets`
  therefore queries with no `Limit` and pages until it has enough; `Limit=2`
  with a ghost among the two oldest tickets would hide every live player behind
  it.
- **A GSI lags its table.** Join queries the index milliseconds after writing
  its ticket, so the ticket can be missing; `try_form_match(new_ticket)` adds it
  back and counts `TicketNotYetIndexed`. Reads of a single ticket after a claim
  use `ConsistentRead`.
- **Provisioning must stay asynchronous.** API Gateway caps integrations at 29
  seconds and a Fargate cold start routinely exceeds that. `RunTask` is fired,
  `provisioning` is returned, and the client polls.
- **`RunTask` returns HTTP 200 even when placement failed** — the reason goes in
  `failures` and `tasks` comes back empty. Always check both.
- **A Fargate task cannot discover its own public IP.** The address is on the
  ENI, outside the container, and there is no instance metadata endpoint. IP
  resolution is driven by the client's own poll (`fargate._lookup_public_ip`),
  which caches to the row only if the row still exists.
- **The port comes from the session row**, not a constant. Every Fargate task
  listens on 7777, but an Agones server gets a host port from the Fleet's range.
- **Ticket claims are optimistic, not locked.** A conditional `UpdateItem`
  (`status` must still be `waiting`) means exactly one concurrent invocation
  wins; the loser rolls its claims back via `release_tickets`.
- **EventBridge delivers at least once.** `sessions.end_session` is a
  conditional delete returning the old row, so only the first delivery gets it
  back; `ecs_events` reports billed time only on that one.
- `active_session_count` uses a `Scan`. Fine at this size, acknowledged as the
  thing to revisit at volume.

### Metrics go out via stdout, not the API

`emit_metric` writes CloudWatch Embedded Metric Format JSON using `print`, not
the logger — Lambda's log format would prefix the line and stop CloudWatch
parsing it as EMF. This avoids a `PutMetricData` call in the request path and
needs no extra IAM permission.

Each data point is published twice: with no dimensions (what `dashboard.tf` and
the alarms query - adding a dimension to it would change its identity and blank
them) and with an `Orchestrator` dimension, for comparing backends. Per-session
values like `sessionId` go in as properties, never dimensions: each dimension
combination is a separately billed metric. Pass `timestamp=` (epoch ms) for
events reported after the fact, so they land in the right period.

Two places hardcode the namespace and must agree: the `METRICS_NAMESPACE`
default in `mm/config.py` (never set as a Lambda env var, so the default is what
is live) and `local.matchmaker_namespace` in `dashboard.tf`. A rename in one
place silently empties the CloudWatch dashboard.

Matching now re-runs on every waiting poll, so `CapacityCapHit`,
`ClaimContention` and `WarmPoolExhausted` count polls, not players. Read their
shape over time, not their totals.

## Working in this codebase

### Lambda packaging

`lambda.tf` zips `source_dir = lambda/matchmaker` wholesale (excluding
`__pycache__` and `.pyc`), so new `.py` files there ship automatically with no
Terraform change. Both functions get the same zip and redeploy together.

Lambda puts the function directory at the front of `sys.path`, so a top-level
module named after a stdlib one shadows it for boto3 too: a `queue.py` beside
`handler.py` would break urllib3's `import queue` and fail every invocation at
import. That is why everything lives under the one package name `mm`. New
modules go inside it.

A new entry point gets only the environment its Terraform gives it.
`test_ecs_events.py` imports `ecs_events` in a fresh interpreter with exactly
the ecs-events function's variables, because conftest sets everything and would
hide a dependency on one it won't have. Do the same for any new function.

### Tests

`infrastructure/aws/lambda/tests/`:

- `test_api.py` - the API through `lambda_handler` with real HTTP API v2 event
  shapes. Describes behaviour, not internals, so it survives refactors.
- `test_fixes.py`, `test_internals.py` - one test per defect fixed, several of
  them races only reachable by calling functions directly.
- `test_ecs_events.py` - the listener, fed EventBridge event shapes.
- `test_agones.py` - the Agones backend with a fake allocator, plus a real
  mutual-TLS handshake against a local HTTPS server using throwaway certs.

Things the harness depends on:

- **`mock_aws()` starts before the matchmaker is imported**, because its modules
  build boto3 clients at import, as a cold start does. A client created first
  would talk to real AWS.
- **Import modules, not names.** Callers write `backends.active.provision()`,
  `fargate.ecs`, `config.X` - never `from mm.backends import active`. Fixtures
  swap module attributes with `monkeypatch` (the fake ECS client onto
  `fargate.ecs`, the Agones backend onto `backends.active`); a name copied at
  import would keep pointing at the original.
- **moto can't do ECS with awsvpc networking**, so `FakeEcs`/`FakeEc2` stand in,
  reproducing RunTask's real 200-with-`failures` shape. They are autouse: no test
  can reach real ECS by forgetting to ask for them.
- **conftest's `compute_module` falls back silently** to `handler` if importing
  `mm.backends.fargate` raises `ImportError`. If tests suddenly fail with moto
  ECS errors, that import is broken.
- **No bytecode in `lambda/matchmaker`**: conftest sets
  `sys.dont_write_bytecode`, subprocess tests pass `-B`, and `__pycache__/` is
  gitignored, because the folder is what gets zipped.

### Terraform notes

- The S3 backend in `main.tf` hardcodes the bucket, key and region because
  backend blocks cannot reference variables. Locking is S3-native
  (`use_lockfile = true`, a `.tflock` object beside the state); the DynamoDB
  lock table pattern most guides show was deprecated in Terraform 1.11.
- An interrupted run leaves that `.tflock` behind and blocks every later plan,
  CI included. Check its `Who` field before `terraform force-unlock <ID>`: if it
  names a GitHub runner, an apply is genuinely in progress.
- A GSI is a **separate ARN from its table**. Granting `Query` on a table does
  not permit querying its index — `iam.tf` needs `table/.../index/*` too.
- `iam:PassRole` is distinct from `ecs:RunTask`. Granting only `RunTask`
  produces an AccessDenied that reads like `RunTask` is missing.
- Each function has its own role, scoped to what it does: the ecs-events role
  can only `DeleteItem` on the sessions table.
- The allocator TLS material is an SSM SecureString uploaded by
  `scripts/put-allocator-tls.sh`, **not** a Terraform resource: anything
  Terraform writes lands in state, and this holds a private key.
- The GitHub OIDC trust in `oidc.tf` pins the **immutable numeric IDs** in the
  `sub` claim (`repo:DrewCoding@122519034/ground-trace@1345255887:ref:...`), not
  the `repo:owner/name:*` form every guide shows. The provider ARN is
  constructed from the account ID rather than read via data source, which would
  require `iam:ListOpenIDConnectProviders` that `PowerUserAccess` does not grant.
- The CI role gets IAM actions only on `ground-trace-*` roles, listed one by one
  in `oidc.tf` (mirrored in `infrastructure/iam-bootstrap-policy.json`).
  Deleting a role needs `iam:ListInstanceProfilesForRole`, which surfaces as an
  AccessDenied mid-apply. A change to that policy is best applied locally: it
  alters the permissions of the role CI runs as.
- HCL formatting is inconsistent (4-space in the older files, 2-space in
  `cognito.tf` / `dashboard.tf` / `ecs_events.tf`). CI runs
  `terraform fmt -check` with `continue-on-error: true`, so it is advisory.
  Match the file you are editing.

### CI

`.github/workflows/terraform.yml` triggers on changes under
`infrastructure/aws/` or to the two workflow files - `plan` on PRs (posted as a
PR comment, truncated at 60k chars) and `apply` on pushes to `main`.

- **Deploys are gated on the tests.** The `terraform` job `needs` a `tests` job,
  which calls `matchmaker-tests.yml` (a reusable workflow with no push/PR
  trigger of its own). A red test means the apply is skipped and production
  keeps the last good code. Before the gate existed, broken commits deployed
  while the tests failed beside them.
- **Every push to `main` under `infrastructure/aws/` deploys.** Push only on a
  green local `python -m pytest -q`.
- The tests job receives no secrets and only `contents: read`, so the job that
  runs third-party pip packages can't obtain AWS credentials.
- `concurrency` is per ref with `cancel-in-progress: false`: runs on `main`
  queue rather than racing for the state lock, and a PR can't replace a pending
  deploy. Plan and apply also pass `-lock-timeout=10m` - before the plan file,
  since Terraform stops reading flags at the first positional argument.
- Frontend changes are not deployed by any pipeline. Credentials come from
  GitHub OIDC federation; there are no stored AWS access keys.

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
register: explain the trap, not the syntax. When moving code, the comments move
with it.

## Repo areas that are not real yet

- `backend/main.go` — a Go stub that prints `"huh."`. No build wired up.
- `scripts/startup.sh` — empty.
- `commands.md` — scratch notes holding one `admin-create-user` invocation.
- `frontend/README.md` — unmodified Vite boilerplate; ignore it.
- The Agones backend's cluster side - the Fleet, the allocator service and its
  TLS - is not in this repo yet. The matchmaker side is complete and tested;
  `orchestrator` stays `fargate` until a cluster exists to point it at.

Operator accounts are created by hand (`allow_admin_create_user_only = true`),
with `scripts/create-operator.sh`; there is no sign-up flow. The dashboard must
send the Cognito **ID token**, not the access token — Cognito's access token
carries `client_id` rather than an `aud` claim, and the authorizer is configured
with an audience, so using it yields a 401 that looks like a broken login.

Per `README.md`, next up: live frontend monitoring, a client/server build
pipeline, and Cognito-authenticated queue tickets.
