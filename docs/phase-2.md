# Phase 2: Agones backend in the matchmaker

Goal: a second backend, `mm/backends/agones.py`, that hands players a server
from an Agones Fleet instead of launching a Fargate task — selected by
`ORCHESTRATOR=agones`, with everything else in the matchmaker unchanged.

**Done when:**

- [ ] With `ORCHESTRATOR=agones`, two queued players get the address of an
      already-`Ready` server on the poll after matching — no `provisioning` wait
- [ ] An empty warm pool leaves players queued, and they're matched on a later
      poll once a server is `Ready`
- [ ] A lone player is backfilled into an allocated server with a free slot
- [ ] Both backends pass the same pytest suite; Fargate stays the default

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
  allocator's allowlist (`allocator-client-ca`). `load_cert_chain` needs files,
  so the Lambda reads the cert and key from Secrets Manager at cold start and
  writes them to `/tmp`.
- **Server certificate.** The allocator's own cert is self-signed by default.
  Pin its CA (from the cluster's allocator TLS secret) rather than disabling
  verification.
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

**Check before writing this:** how the *REST* endpoint reports `UnAllocated`
and `Contention` — a status code, or a 200 with a state field. The fleet
quickstart only shows the CRD form. See the open questions in
agones-research.md.

### 3. Backfill with ordered selectors

One allocation request with two selectors, tried in order: an `Allocated`
server whose `players` list has room, then a fresh `Ready` one. This replaces
`find_joinable_session` for the Agones path. It depends on Phase 1's step C2.4
(the server maintaining its `players` list over the sidecar's HTTP API).

Two players racing for the last slot is already handled: the client re-queues
when a join is rejected. Reserving the slot at allocation time (allocation-time
list actions) is a later optimization — confirm the installed Agones version
supports it first.

### 4. Session identity and heartbeats

Use the Agones GameServer name as the `sessionId`. The server can read its own
name through the SDK's `GameServer()`, so `SessionHeartbeat` can report against
it. Two details:

- A **warm, unallocated** server has no session row yet, and Phase 0 made
  heartbeats for unknown sessions return 404. The server should only start
  heartbeating once it's `Allocated`.
- Under Agones the cap is the Fleet's `maxReplicas` (Phase 3), so
  `active_session_count` isn't used on this path.

### 5. Session end — decide

On Fargate, `ecs_events` deletes the row when the task stops. Agones has no
equivalent signal reaching AWS. Options:

| Option | Trade-off |
|---|---|
| Server calls `DELETE /sessions/{id}`, then `Shutdown()` | Reliable here, unlike Fargate: the server controls when it shuts down and can await the request first. Rows from crashed servers still linger |
| Rely on heartbeat staleness, TTL as garbage collection | No new code. Rows linger on the dashboard; harmless to matching, since nothing counts them on this path |

Probably both: delete on clean shutdown, staleness for crashes.

### 6. Toggle and metrics

- Add `ORCHESTRATOR` as a Terraform variable, default `fargate`.
- Add the `Orchestrator` metric dimension, **together with** the dashboard
  update — a new dimension changes each metric's identity and blanks widgets
  that query it without one (see CLAUDE.md).
- New metrics: `WarmPoolExhausted`, and allocation latency as the Agones
  counterpart to `ProvisionDurationSeconds`.

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

- REST allocator response shape for `UnAllocated` / `Contention`.
- Whether to put the build version into allocation selectors now (see
  "Version-aware matching" in [README.md](README.md)) — cheap to add here.
