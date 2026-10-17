# Docs

Research and plans for running the game servers on Agones (Kubernetes)
alongside the existing Fargate design, and comparing the two.

The Fargate path stays the default throughout. The point isn't to replace it —
it's to build the warm-pool alternative that the main README's design notes
only describe, and measure the trade-off with real numbers.

| Doc | What it is |
|---|---|
| [agones-research.md](agones-research.md) | What Agones is, how it maps onto this matchmaker, constraints, costs, sources |
| [phase-0.md](phase-0.md) | Matchmaker fixes and the backend seam. **Done, deployed 2026-10-16** |
| [phase-1.md](phase-1.md) | Agones on a local k3s cluster; Unity server integration. **In progress** |
| [phase-2.md](phase-2.md) | Agones backend in the matchmaker |
| [phase-3.md](phase-3.md) | Helm packaging for the Fleet |
| [phase-4.md](phase-4.md) | Argo CD (GitOps) and the build pipeline |
| [phase-5.md](phase-5.md) | EKS, for measurement windows only |
| [phase-6.md](phase-6.md) | Measure both paths and write it up |

## Order

```
Phase 0 ──┬──> Phase 1 ──> Phase 3 ──> Phase 4 ──┐
          │                                      ├──> Phase 5 ──> Phase 6
          └──> Phase 2 ──────────────────────────┘
```

Phases 1 and 2 can run in parallel once Phase 0 is in: one is cluster and
Unity work, the other is Python in the matchmaker. Phase 2's end-to-end test
needs a cluster, so it finishes after Phase 1.

## Toggle, not fork

Both orchestrators live in the same codebase, switched by configuration:

| Layer | Switch |
|---|---|
| Matchmaker | `ORCHESTRATOR=fargate\|agones` env var, selecting a module in `mm/backends/` (seam built in Phase 0; Terraform variable arrives in Phase 2) |
| Unity server | `ORCHESTRATOR=agones` env var, set by the Fleet. One image runs on both |
| Client | No switch. It reads `ip` and `port` from the poll response either way |

Work happens on short-lived branches per phase, merged as each lands with the
toggle defaulting to Fargate. A long-lived `agones` branch was rejected: the
comparison needs both paths running against the same matchmaker and metrics,
most of the code is shared, and `main` is what CI deploys and what a reviewer
sees.

The work spans two repos: `ground-trace` (this one) and the Unity project one
folder up. Branch in whichever a phase touches.

## Lessons that apply to every phase

- **Client and server must be built from the same commit.** Netcode for
  GameObjects refuses any client whose `NetworkConfig` hash differs from the
  server's, and that hash covers every registered network prefab and the
  Netcode package's own protocol version. On 2026-10-16 an ECR image built a
  day before the client produced 18 rejected joins and a re-queue loop. The
  only symptom is `NetworkConfig mismatch` in the server log.
- **Agones 1.61 needs Kubernetes 1.34–1.36 and Helm 4.1.3+.** Pin both.
- **Anything Kubernetes costs money in AWS only while it exists.** EKS is
  brought up for measurement and torn down after (Phase 5).

## Not in any phase yet

- **Version-aware matching.** The matchmaker can't tell a client and server of
  different builds apart, and the client can't tell "server full" from "wrong
  version", so it re-queues straight back into the incompatible server. Fix:
  clients send their build version with each join, servers report theirs, and
  matching only pairs equal versions. Agones gives this almost for free
  (allocation selectors on a version label — see Phase 3).
- **A hard server cap** (atomic counter) and **a reaper** for servers that are
  running but no longer heartbeating. See phase-0.md, "Deliberately not done".
