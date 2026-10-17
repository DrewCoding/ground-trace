# Phase 6: Measure and write it up

Goal: real numbers for the trade-off the main README only argues — Fargate's
scale-to-zero with a cold start on every match, against an Agones warm pool
with instant allocation and continuous cost — and a write-up a reader can
check.

**Done when:**

- [ ] Both paths measured with the same client build, region and procedure
- [ ] The README has a "Fargate vs. Agones" section with the numbers and when
      each one wins
- [ ] CLAUDE.md and agones-research.md reflect what was learned

**Depends on:** Phase 5.

---

## What to measure

| Question | Fargate | Agones |
|---|---|---|
| How long does a player wait? | `TimeToMatchSeconds` plus `ProvisionDurationSeconds` (cold start) | `TimeToMatchSeconds` plus allocation latency |
| What does a match cost? | `TaskBilledSeconds` × the per-second rate | Node-hours ÷ matches served — **not** per session |
| What does idle cost? | ~$0 | Warm pool + system nodes + $0.10/hour control plane |
| What happens when demand exceeds capacity? | `CapacityCapHit`, players wait | `WarmPoolExhausted`, players wait for the autoscaler |

`TaskBilledSeconds` is Fargate-only, because Agones bills per node, not per
game server. So the cost comparison is done at the bill level — Cost Explorer
for the window, or node-hours from the node groups — not by comparing one
metric across both paths.

All matchmaker metrics carry the `Orchestrator` dimension from Phase 2, so the
dashboards split cleanly.

## Procedure

1. Same client and server build on both paths (one commit — see the
   version-mismatch lesson in [README.md](README.md)).
2. Run N matches on Fargate, then switch `ORCHESTRATOR` and run N on Agones,
   in the same session, same region.
3. Include a cold Agones case — the warm pool exhausted — so the comparison
   isn't only best case versus worst case.
4. Record bill-level cost for the window.

## The write-up

A README section that answers:

- The measured wait on each path, and the cost of each.
- **Where the lines cross**: the matches-per-hour above which an always-on
  warm pool costs less than its latency is worth — or, framed for this
  project's actual traffic, why scale-to-zero still wins here.
- What AWS's own case against Agones at scale (see agones-research.md, "The
  case against") does and doesn't apply to a single-region game this size.

Then update CLAUDE.md with anything about the Agones path a future session
needs, and fill in agones-research.md's open questions with what was found.
