# Phase 2: Agones backend in the matchmaker

Goal: a second backend, `mm/backends/agones.py`, that hands players a server
from an Agones Fleet instead of launching a Fargate task — selected by
`ORCHESTRATOR=agones`, with everything else in the matchmaker unchanged.

**Done when:**

- [x] With `ORCHESTRATOR=agones`, two queued players get the address of an
      already-`Ready` server on the poll after matching — no `provisioning` wait
      (Unity clients through `serve_local.py`, 2026-10-17)
- [x] An empty warm pool leaves players queued, and they're matched on a later
      poll once a server is `Ready` (tests; the real allocator's 429 was seen
      leaving players waiting through `serve_local.py`)
- [ ] A lone player is backfilled into an allocated server with a free slot
      (covered by `test_agones.py`; needs the heartbeat-enabled server image
      to confirm in the real game)
- [x] Both backends pass the pytest suite; Fargate stays the default. The
      Agones tests are their own file rather than the same suite run twice

**Depends on:** Phase 0 (the backend seam). The end-to-end test needs Phase 1's
cluster.

---

## How the backend fits the seam

Phase 0 defined a backend as two functions. For Agones:

| Function | Fargate | Agones |
|---|---|---|
| `provision()` | `RunTask`, write a `provisioning` row, return immediately | Allocate from the Fleet; the response already has the address, so write the row with `publicIp` and `port` set |
| `resolve_address(session)` | Look up the task's ENI on poll, cache it | Read it from the row — it was known at allocation |

So with Agones a matched player skips `provisioning` entirely. That latency
difference is what Phase 6 measures.

The port now comes from the allocation (a dynamic host port in 7000–8000).
Phase 0 already made `poll_ticket` return the row's port instead of a literal
7777, and the Unity client already connects to whatever port it's given, so
no client change.

## Steps

### 1. Call the allocator — standard library only

The Lambda's "boto3 only, nothing to package" property is worth keeping.
Agones' allocator serves REST as well as gRPC, so it can be called with
`urllib` and an `ssl.SSLContext`:

- **mTLS.** Every request must present a client certificate on the
  allocator's allowlist (`allocator-client-ca`). `load_cert_chain` needs files:
  locally they're read from paths in `AGONES_CA_FILE`,
  `AGONES_CLIENT_CERT_FILE` and `AGONES_CLIENT_KEY_FILE`. The deployed Lambda
  (Phase 5) will need them from Secrets Manager, written to `/tmp`.
- **Server certificate — don't use Helm's.** Probed on 2026-10-17: the
  chart's generated server cert has an empty subject and no SAN (it only gets
  an IP SAN if `allocator.service.loadBalancerIP` is set). Python 3.13+'s
  default context rejects it twice over — hostname checking, and strict
  X.509 mode ("Subject name empty") — so using it means relaxing both. Worse,
  the chart regenerates the CA, server cert *and* client cert on every
  `helm upgrade`, silently breaking any matchmaker that pinned them.
- **What's installed instead.** Our own certs, created once and kept outside
  the repo in `~/.ground-trace/allocator/`: a private CA, a server cert it
  signed (`IP:10.0.0.55` plus the in-cluster DNS names, `serverAuth`), and a
  self-signed matchmaker client cert (`clientAuth`). Installed with:

  ```bash
  D=~/.ground-trace/allocator
  helm upgrade agones agones/agones -n agones-system --version 1.61.0 --reuse-values \
    --set agones.allocator.generateTLS=false \
    --set agones.allocator.generateClientTLS=false \
    --set-file agones.allocator.tlsCert=$D/server.crt \
    --set-file agones.allocator.tlsKey=$D/server.key \
    --set-file 'agones.allocator.clientCAs.matchmaker\.crt'=$D/client.crt
  kubectl rollout restart deploy/agones-allocator -n agones-system
  ```

  The matchmaker trusts the CA, not the server cert, so the server cert can be
  reissued (an EKS load balancer name, Phase 5) without touching it. Two traps
  hit on the way: the allocator **only loads allowlist entries whose key ends
  in `.crt`** (anything else is silently skipped, and every client is
  rejected as "signed by unknown authority"); and Helm 4's server-side apply
  left a removed `clientCAs` key behind in the secret, which needed a
  `kubectl patch` to delete.
- **Never name the module after a stdlib one** — `secrets.py` is the tempting
  wrong name here. It goes in `mm/`.
- gRPC was rejected because it needs native dependencies packaged into the zip.

### 2. Map allocation results

Agones documents three allocation states:

| State | Meaning | Matchmaker response |
|---|---|---|
| `Allocated` | Got a server | Write the session row with address and port; stamp tickets |
| `UnAllocated` | No server matched — the warm pool is empty | Release the claimed tickets, emit `WarmPoolExhausted`. Level-triggered matching retries on the next poll |
| `Contention` | Too many concurrent requests | Treat like `UnAllocated` |

**Answered by probing the local allocator (2026-10-17):** the REST endpoint
(`POST /gameserverallocation`) reports outcomes as HTTP status codes, with no
state field in any body:

| Outcome | Status | Body |
|---|---|---|
| Allocated | 200 | `gameServerName`, `address`, `ports[0].port`, plus `addresses`, `nodeName`, labels |
| UnAllocated | 429 | `{"code": 8, "message": "there is no available GameServer to allocate"}` (gRPC `RESOURCE_EXHAUSTED`) |
| Contention | 409 (assumed) | gRPC `ABORTED`; couldn't be reproduced on one node |
| No client cert | — | Refused in the TLS handshake |

`mm/backends/agones.py` maps 429 → `WarmPoolExhausted` and 409 →
`AllocationContention`, both returning `None` from `provision()` so matching
releases the players for the next poll. Anything else is a failure. A real
allocation took 0.197s.

### 3. Backfill — decided: heartbeats into the shared path

**Decision (2026-10-17):** backfill on the Agones path goes through the same
`find_joinable_session` as Fargate, fed by the server's heartbeats (step 4).
No `players` list, no ordered selectors, no Agones-specific code in
`matching.py`.

Why: step 4 is needed anyway, and once an Agones server heartbeats, its
session row carries a real `status` and `playerCount` — exactly what the
shared backfill reads. Keeping backfill shared also keeps Phase 6 fair: the
two backends then differ only in how a server is obtained. Its weaknesses are
Fargate's too: backfill trusts a heartbeat up to 10s old, and two players
racing for the last slot are resolved by the client re-queuing the one that's
rejected.

The rejected alternative, for reference: one allocation request with two
selectors — an `Allocated` server whose `players` list has room, then a fresh
`Ready` one — with the server maintaining that list over the sidecar's HTTP
API (Phase 1 C2.4). It would let Agones reserve the slot atomically
(allocation-time list actions), so it stays available as a later
optimization.

### 4. Session identity and heartbeats

The session is named after the GameServer: `provision()` uses the
allocation's `gameServerName` as the `sessionId`. On the server,
`AgonesLifecycle` records `ObjectMeta.Name` when the watch reports
`Allocated`, and `SessionHeartbeat` waits for it instead of `SESSION_ID`.

- A **warm, unallocated** server has no session row yet, and Phase 0 made
  heartbeats for unknown sessions return 404, so heartbeats only start once
  it's `Allocated`.
- The row is written `status: allocated`, which `find_joinable_session`
  ignores, until the first heartbeat replaces it with the server's own status.
  The watch can report `Allocated` before the matchmaker has written the row,
  so the very first heartbeat may 404; the next one, 10s later, lands.
- The pod needs `MATCHMAKER_URL` and `MATCHMAKER_KEY`
  (`infrastructure/k8s/fleet.yaml`). Locally that's `serve_local.py` at the
  node's address, started with `--host 0.0.0.0` — a pod's `127.0.0.1` is its
  own.
- Under Agones the cap is the Fleet's size (`maxReplicas` in Phase 3), so
  `agones.has_capacity()` is always true and `active_session_count` counts
  only Fargate rows.

### 5. Session end — decided: staleness, with TTL as garbage collection

On Fargate, `ecs_events` deletes the row when the task stops. Agones has no
equivalent signal reaching AWS. Options considered:

| Option | Trade-off |
|---|---|
| Server calls `DELETE /sessions/{id}`, then `Shutdown()` | Reliable here, unlike Fargate: the server controls when it shuts down and can await the request first. Rows from crashed servers still linger |
| Rely on heartbeat staleness, TTL as garbage collection | No new code. Rows linger on the dashboard; harmless to matching, since nothing counts them on this path |

**Decision (2026-10-17): staleness only — no new code.** With heartbeats
feeding backfill (step 3), an ended server's row stops being refreshed and is
stale within `SESSION_STALE_SECONDS` (45s), so `find_joinable_session` never
offers it. Nothing counts Agones rows either: the Fargate cap skips them and
the Agones cap is the Fleet's size. The only visible effect is ended sessions
lingering on the dashboard, flagged stale, until TTL removes them.

Deleting on clean shutdown was rejected because it would reverse a deliberate
rule (CLAUDE.md: the game server never deregisters, and end-of-session logic
doesn't belong in `deregister`) for a cosmetic gain, and it would still leave
crashed servers to staleness. If the dashboard noise matters later, filter
stale Agones rows out of `dashboard_snapshot` rather than adding a delete.

### 6. Toggle and metrics

- **Done:** `ORCHESTRATOR` Terraform variable, default `fargate`, validated to
  `fargate|agones`.
- **Done, without a dashboard change:** the `Orchestrator` dimension. Adding a
  dimension changes a metric's identity and would blank every widget and alarm
  querying it without one — so instead each data point is emitted with two
  EMF dimension sets, `[[], ["Orchestrator"]]`. The undimensioned metric the
  dashboard reads is unchanged; the per-orchestrator copy is new.
- **Done:** `WarmPoolExhausted`, `AllocationContention`, and
  `AllocationDurationSeconds` (the Agones counterpart to
  `ProvisionDurationSeconds`). They aren't on the dashboard yet: the deployed
  stack doesn't run Agones until Phase 5, so widgets for them belong with the
  Phase 6 comparison.

### 7. Test

- **pytest** with a fake allocator, the same way `conftest.py` fakes ECS: every
  allocation state, backfill ordering, cert loading.
- **Local end-to-end without AWS.** The deployed Lambda can't reach a cluster
  on the LAN, and the home cluster's allocator shouldn't be exposed to the
  internet to make it. Instead, run the matchmaker locally: a small HTTP shim
  around `lambda_handler`, DynamoDB Local (or moto in server mode) for the
  tables, and the k3s allocator at the Linux box's LAN address. Point the Unity
  client's `matchmakerUrl` at the shim. That's the full loop — client, matcher,
  allocator, game server — with nothing in AWS.

The real Lambda only runs the Agones path once EKS exists (Phase 5).

## Open questions

- ~~REST allocator response shape for `UnAllocated` / `Contention`.~~
  Answered in step 2.
- Whether to put the build version into allocation selectors now (see
  "Version-aware matching" in [README.md](README.md)) — cheap to add here.
