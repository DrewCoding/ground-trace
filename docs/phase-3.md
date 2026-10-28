# Phase 3: Helm packaging

Goal: the Fleet, its autoscaler and the Agones install settings as versioned,
parameterised Helm charts and values files in this repo — one source of truth
for the local cluster and EKS, and the thing Argo CD deploys in Phase 4.

**Done when:**

- [x] `helm install` of the chart reproduces Phase 1's Fleet on k3s
      (2026-10-17: Ready in 7s, labelled `ground-trace/build=dev2`, matchmaker
      key read from the `matchmaker-key` Secret)
- [x] A buffer autoscaler keeps a warm pool at the configured size (allocating
      the only Ready server brought up a replacement in 17s)
- [~] Switching between local and EKS is a values file, not an edit —
      `values-eks.yaml` exists for both charts, with placeholders until Phase 5
      gives it a cluster to be checked against
- [x] Nothing secret is in the repo. The allocator's certificates are passed
      from `~/.ground-trace/allocator/` by `scripts/install-agones-local.sh`;
      the matchmaker key is a Secret created out of band. (`local-dev-key`
      appears in the local values comments: it only opens `serve_local.py` on
      this machine.)

**Installing locally:**

```bash
bash scripts/install-agones-local.sh        # Agones, with values-local.yaml + certs
kubectl create secret generic matchmaker-key --from-literal=key=local-dev-key
helm upgrade --install game-server-fleet infrastructure/k8s/charts/game-server-fleet \
  -f infrastructure/k8s/charts/game-server-fleet/values-local.yaml
```

The Phase 1 Fleet was made with `kubectl apply`, and Helm won't adopt a
resource it didn't create, so it was deleted before the first chart install.

**Depends on:** Phase 1 (a Fleet that works, to package).

---

## Layout

```
infrastructure/k8s/
  agones/
    values-local.yaml     # Agones install settings for k3s
    values-eks.yaml       # ... for EKS
  charts/
    game-server-fleet/
      Chart.yaml
      values.yaml         # defaults
      values-local.yaml
      values-eks.yaml
      templates/
        fleet.yaml
        fleetautoscaler.yaml
```

Agones itself isn't vendored — it's installed from its own chart
(`agones/agones`), with these values files.

## Steps

### 1. Agones install values

Move Phase 1's `--set` flags into files:

- **local:** `agones.ping.install=false`; reduced replicas if the box is small.
- **EKS:** default replicas, and scheduling onto a dedicated system node pool
  (Agones prefers nodes labelled, and tolerates the taint,
  `agones.dev/agones-system=true` — Phase 5 creates that pool).

The game server port range (`gameservers.minPort` / `maxPort`) is an Agones
install value, not a Fleet one. Keep it in sync with the firewall / security
group.

### 2. The Fleet chart

Template Phase 1's `fleet.yaml`. Values worth exposing:

| Value | Why |
|---|---|
| `image.repository`, `image.tag` | The tag is what a deploy changes (Phase 4) |
| `health.initialDelaySeconds` | Unity boot time differs by machine |
| `emptyShutdownSeconds` | Passed to the server as `EMPTY_SHUTDOWN_SECONDS` |
| `matchmaker.url`, `matchmaker.keySecret` | Phase 2's heartbeats; the key is a Secret *reference*, never a value |
| `buildVersion` | A label on every GameServer — see below |

### 3. Warm pool: FleetAutoscaler

A `Buffer` policy keeps N servers `Ready` ahead of demand:

- `bufferSize` — the warm pool. Each one is idle capacity you pay for (node
  space on EKS).
- `maxReplicas` — the spending ceiling on this path, replacing
  `MAX_CONCURRENT_SESSIONS`. Treat it as the same security control.
- Autoscalers re-evaluate every 30s by default.

Only a **webhook** autoscaler is documented as able to scale a Fleet to zero.
That's deliberate scope: the comparison is *Fargate scale-to-zero vs. an Agones
warm pool*, not two scale-to-zero designs.

### 4. Secrets stay out of git

The matchmaker API key becomes a Kubernetes Secret created out of band —
`kubectl create secret generic ...` locally, or External Secrets reading from
Secrets Manager on EKS — and the chart references it by name. This matters more
from Phase 4 on: with GitOps, whatever is in the repo gets deployed. (Today the
key sits in plain text in the ECS task definition; this path shouldn't copy
that.)

### 5. Version labels

Label every GameServer with the build version it runs (the image tag works).
Allocation selectors can then match on it, so a client is only ever allocated a
server of its own build — the version-aware matching that would have prevented
the 2026-10-16 mismatch incident, at the cost of one label and one selector
field.

### 6. Validate

```bash
helm lint infrastructure/k8s/charts/game-server-fleet
helm template ... | kubectl apply --dry-run=server -f -
```

Server-side dry run catches schema errors against the real Agones CRDs.

## Open questions

- ~~Buffer policy with `minReplicas: 0`~~ — answered by server-side dry run
  against Agones 1.61: `minReplicas: 0` is **accepted**, but it doesn't make a
  Buffer Fleet scale to zero. `bufferSize: 0` is rejected, and the autoscaler
  targets `allocated + bufferSize`, so at least `bufferSize` servers are
  always Ready. Scale-to-zero on Agones still means a webhook autoscaler.
