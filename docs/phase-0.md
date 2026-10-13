# Phase 0: matchmaker fixes and restructure

Completed 2026-10-12, on branch `phase-0-matchmaker-fixes`. First phase of the
plan in [agones-research.md](agones-research.md): fix the current system and
cut the seam an Agones backend will plug into.

## What was wrong

The headline bug was that **matching only ran when a player joined**.
`try_form_match` was called from `join_queue` and nowhere else, so any change
that made a match possible without a new player arriving went unnoticed:

| What made a match possible | What happened |
|---|---|
| A server stopping, freeing capacity under the cap | Waiting players stayed waiting. The code comment said they'd "keep polling and get matched"; polling never ran matching |
| An opponent leaving, opening a backfill slot | A player already in the queue was never placed. Backfill only worked if the player joined *after* the slot opened |
| Claims released after a lost race (e.g. four simultaneous joins) | The two losing players waited for a fifth |
| `RunTask` failing and releasing its claims | Nothing retried |
| The joining ticket not yet visible in the GSI | Even the ordinary two-player path could miss: the ticket is written, then the index is queried milliseconds later, and GSIs update asynchronously |

The client deliberately has no deadline while waiting for an opponent, so a
stranded player sat on "Finding match..." indefinitely.

Tracing it turned up related defects:

- **Tickets aged out of matching but still read as waiting.** Matching ignored
  tickets queued more than 300s ago, but `poll_ticket` kept answering
  `waiting` until TTL physically deleted the row — which AWS only promises
  "within a few days". A patient player was told they were queued while
  being permanently unmatchable.
- **Ghost tickets.** A player whose client crashed stayed matchable for up to
  300s, so a real player could be paired with nobody and a server launched for
  one person.
- **Every finished match held a slot under the cap.** The game server never
  deregisters, and the cap counted every session row, so ended sessions
  counted until TTL removed them.
- **Heartbeats could create sessions.** The heartbeat `UpdateItem` was
  unconditional, so it created rows for made-up ids. With the API key extracted
  from a game build, a handful of requests filled the cap and stopped all
  matchmaking.
- **Matching failures failed the request.** A `RunTask` failure during join
  returned 503 after the ticket had already been written, leaving a ticket the
  client never learned the id of.
- **Minor:** `poll_ticket` returned a literal `7777` although the session row
  stores the port, and the README's throttling numbers were swapped.

All of these are covered by `tests/test_fixes.py`. Every test in that file
failed against the original handler and passes now; the 17 behaviors in
`tests/test_api.py` passed on both.

## What changed

### Matching is level-triggered

`try_form_match` now runs on every join *and every poll of a waiting ticket*.
Clients poll every 2 seconds, so the polls are the clock — the same pattern the
README already used for IP resolution. Nothing runs on a timer and nothing runs
while nobody is waiting. A join also passes its own ticket in, covering the
case where the index hasn't caught up; `TicketNotYetIndexed` counts how often
that happens.

Matching errors are caught, logged with a traceback, and counted as
`MatchingErrors` (with its own alarm), instead of failing the join or poll. The
next poll retries.

| Alternative | Why not |
|---|---|
| DynamoDB Streams on both tables | Two triggers to keep in sync, no ordering guarantee relative to GSI propagation, and many matchers running in parallel |
| EventBridge Scheduler | One-minute minimum granularity, and it runs while nobody is playing |
| Step Functions | Below |

### Ticket liveness

Each poll records `lastSeenAt` and pushes `expiresAt` forward. Matching ignores
tickets not seen for 10s (five missed polls); a poll reports `expired` once a
ticket hasn't been seen for 300s, whether or not TTL has deleted it. A player
who keeps polling never expires.

`live_waiting_tickets` queries without `Limit`, because DynamoDB applies
`Limit` before the filter — `Limit=2` with a ghost among the oldest two would
hide the live tickets behind it.

### Session rows track billing exactly

A new function, `ecs-events`, receives ECS `STOPPED` task events through
EventBridge. For each one it deletes the session row and emits
`TaskBilledSeconds`, computed as Fargate bills: from `pullStartedAt` to
`stoppedAt`, rounded up, one-minute minimum.

A session row now exists from `RunTask` until the task stops, which is
exactly the billed span. So the cap counts what's actually costing money, and
a slot frees within seconds of a server ending.

ECS delivers events at least once. The delete is conditional on the row
existing, so only the first delivery gets the row back and only that one emits
the metric. Tasks launched by hand (no `SESSION_ID`) are still reported,
flagged `manual`.

`TaskBilledSeconds` was originally planned as `SessionLifetimeSeconds`, emitted
from `deregister`. That would never have produced a data point: nothing calls
`deregister`.

### No blind upserts

Every `UpdateItem` against a row that might have been deleted is now
conditional on it existing: heartbeats (unknown sessions get 404), cached IPs,
ticket stamps, releases and keep-alives. Besides closing the heartbeat DoS, this
stops a write that races a delete from resurrecting a partial row with no
`expiresAt` — one that would never be garbage collected and, for a session,
would count against the cap forever.

### Code layout

The 521-line `handler.py` became two entry points over a package:

```
lambda/matchmaker/
  handler.py        API: auth, routing, errors
  ecs_events.py     EventBridge: ECS task stopped
  mm/
    config.py  ddb.py  responses.py  metrics.py
    claims.py       ticket transitions (shared by tickets and matching)
    sessions.py     session rows
    matching.py     try_form_match
    tickets.py      join / poll / leave
    dashboard.py
    backends/       compute seam; fargate.py today, agones.py next
```

Everything is inside one package so no module can shadow the standard library.
Lambda puts the function directory first on `sys.path`, so a top-level
`queue.py`, `http.py` or `secrets.py` would break boto3's imports.

The backend interface is two functions, `provision()` and
`resolve_address(session)`, selected by `ORCHESTRATOR` (default `fargate`).

### Why not Step Functions

- **Wrong shape.** The routes are short request handlers called every couple
  of seconds per player, not long-running workflows.
- **Cost.** At ~3 state transitions per poll, a single waiting player is 5,400
  Standard transitions an hour — about $0.135/hour, several times the ~$0.028
  the game server itself costs. The 4,000-transition monthly free tier would
  be gone within an hour. Express workflows are cheap but add a hop for
  nothing.
- **It doesn't remove the race.** Executions still contend for the same
  tickets, so conditional claims are needed regardless.
- **The one fitting use has a better tool.** Retries and lifecycle tracking for
  provisioning are covered by the ECS state-change events, which are free and
  also supply the "session over" signal.
- **No Agones equivalent.** An Agones allocation is a single synchronous call;
  a workflow would exist only on the Fargate side of the comparison.

## Operational changes to know about

- **`terraform plan` against the live account:** 10 to add, 2 to change, 0 to
  destroy. Merging to `main` applies it.
- **Client-visible:** a join no longer returns 503 when matching fails, and a
  heartbeat for an unknown session returns 404. Neither needs a Unity change:
  the client already treats a waiting join normally, and the server's
  heartbeat only ever targets its own, existing session.
- **Metric semantics:** `CapacityCapHit` and `ClaimContention` now fire per
  poll rather than per join, so their totals rise. Read their trend.
- **New metrics:** `TaskBilledSeconds`, `MatchingErrors`, `TicketNotYetIndexed`,
  with dashboard widgets and two new alarms (`matching-errors`,
  `ecs-events-errors`). Custom metrics bill hourly only when data is sent, so
  they cost nothing while idle.
- **DynamoDB:** one extra write per poll (the keep-alive), plus its GSI copy.
  At a 2s poll interval that's well under a cent per player-hour.

## Deliberately not done

- **Hard server cap.** The count-then-`RunTask` check can be passed by two
  invocations claiming different pairs at once. A true cap needs an atomic
  counter, incremented before `RunTask` and decremented by `ecs-events`.
  Bounded overshoot at this scale; not worth it yet.
- **A reaper for wedged servers.** A task that's running but no longer
  heartbeating still bills, and still (correctly) counts against the cap.
  `ecs:StopTask` is granted to the matchmaker and still unused; stopping stale
  sessions would trigger `ecs-events` to clean up the rest.
- **The `Orchestrator` metric dimension.** Adding a dimension changes each
  metric's identity and blanks existing dashboard widgets, so it lands in
  Phase 2 alongside the dashboard change, when there's a second orchestrator
  to tell apart.
- **Cost on the dashboard.** The billed-time widget shows task-minutes, not
  dollars. Fargate rates change, and a hardcoded rate would quietly drift.
