# Phase 4: Argo CD and the build pipeline

Goal: the cluster's state is whatever is in git. A merge that changes the
server image tag rolls out a new server version without dropping any match in
progress — and one pipeline builds client and server from the same commit.

**Done when:**

- [ ] Argo CD manages Agones and the Fleet chart from this repo
- [ ] Changing the image tag in git rolls the Fleet; `Allocated` servers keep
      running until their match ends
- [ ] CI builds the Unity server (and client) from a commit, pushes the image,
      and bumps the tag
- [ ] Nothing is applied to the cluster by hand except bootstrapping Argo CD

**Depends on:** Phase 3 (charts to deploy).

---

## Steps

### 1. Install Argo CD

Via its Helm chart (`argo/argo-cd` from `https://argoproj.github.io/argo-helm`)
into an `argocd` namespace. Reach the UI with `kubectl port-forward` — no
ingress needed (Traefik is disabled on the local cluster anyway).

Self-hosted rather than EKS's managed Argo CD "capability": that's billed
hourly, and running it yourself is both free and the more useful skill.

### 2. App of apps

One root `Application`, pointing at a directory of `Application` manifests:

| App | Source | Sync |
|---|---|---|
| `agones` | Agones' Helm chart, values from `infrastructure/k8s/agones/` | Manual — CRD upgrades deserve a look first |
| `game-server-fleet` | `infrastructure/k8s/charts/game-server-fleet`, environment values file | Automated, with prune and self-heal |

### 3. Check these before relying on it

- **Argo CD's Helm vs. Agones' Helm requirement.** Argo CD renders charts with
  the Helm it bundles. Agones 1.61 moved to Helm 4 and requires 4.1.3+.
  Confirm the Argo CD version you install bundles a Helm that can render the
  Agones chart. If not: pin Agones to 1.60 (last Helm 3 release), or install
  Agones outside Argo CD and let it manage only the Fleet.
- **CRD size.** Agones' CRDs are large enough that client-side apply can hit
  Kubernetes' annotation size limit. Enable `ServerSideApply=true` on the
  `agones` app — Akamai's Agones-on-k3s guide installs with server-side apply
  for the same reason.

### 4. Rolling updates without dropping matches

A Fleet rolling update replaces `Ready` servers and leaves `Allocated` ones
alone until their match ends and they shut down. So "deploy a new server
version" is a git commit, and players mid-match never notice.

### 5. Where the image lives

The local cluster can't use Docker Desktop's images, and Phase 1's
`docker save` / `scp` doesn't fit a pipeline. Options:

| Registry | Catch |
|---|---|
| ECR (exists) | Pull tokens expire every 12 hours; the cluster needs a refresher job |
| GitHub Container Registry | Fits GitHub Actions; a long-lived pull token works from anywhere |
| In-cluster registry | Local only; EKS would need a different answer |

Decide before writing the pipeline.

### 6. Build pipeline

This is the README's "Up Next: CI/CD pipeline for client and server builds",
and it's what actually prevents version mismatches:

1. A commit to the Unity repo triggers a build of the **Linux dedicated
   server and the client, from the same commit**.
2. The server image is pushed, tagged with the commit SHA (never `latest`).
3. The pipeline commits the new tag to the Fleet values in this repo.
4. Argo CD rolls it out.

The hard part is Unity in CI: it needs a licence activated on the runner.
GameCI (`game-ci/unity-builder`) is the usual route; work out licence
activation for your Unity licence type first, before anything else here.

## Open questions

- Registry choice (step 5).
- Unity licence activation in CI.
- Whether to keep pushing to ECR for the Fargate path from the same pipeline —
  probably yes, so both paths always run the same build.
