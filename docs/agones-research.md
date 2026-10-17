# Agones: research notes

Researched 2026-10-12. Agones ships roughly every six weeks, so re-check
versions and feature maturity before acting on anything here.

## Why this was researched

The current design trades latency for cost. Nothing runs while players wait,
so every new match pays a 30–60 second Fargate cold start. The README notes
that production games solve this with warm pools, and names Agones as one way
to run them.

The question was what an Agones version of this matchmaker would look like,
what it would cost, and what would have to change. The intent is a **second
implementation alongside the Fargate one**, measured against it, not a
replacement. Replacing it would break the project's headline property: idle
cost under ten cents a month.

## Summary

- Agones is healthy and actively maintained: a CNCF Sandbox project since
  December 2025, co-developed by Google and Ubisoft, releasing about every six
  weeks.
- Its concepts map almost one-to-one onto what `handler.py` already does by
  hand, including backfill.
- Open Match, the matchmaker usually paired with Agones, is effectively
  stalled. The existing Lambda would stay as the matchmaker and call the Agones
  allocator in place of `RunTask`.
- Four constraints shape the design: the Unity SDK lacks Counters and Lists,
  EKS Fargate cannot host game servers, nodes need public IPs and an open port
  range, and only a webhook autoscaler scales to zero.
- An always-on EKS cluster costs more than a thousand times today's idle bill.
  Develop locally and run EKS only for measurement windows.

## Project health

- **Governance.** Accepted into the CNCF at Sandbox level on 2025-12-21,
  donated by Google. Co-developed with Ubisoft in 2017; Ubisoft runs it in
  production games and still contributes maintainers. 250+ contributors.
- **Repository.** Moved from `googleforgames/agones` to `agones-dev/agones` in
  v1.57.0. Older tutorials and package paths use the old org.
- **Release cadence.** About every six weeks:

  | Version | Released | Notable |
  |---|---|---|
  | v1.61.0 | Sep 24 | Helm 3 → 4 upgrade, breaking for some pipelines |
  | v1.60.0 | Aug 12 | Kubernetes 1.34–1.36; `PortPolicyNone` stable |
  | v1.59.0 | Jul 1 | FleetAutoscaler flapping fix; `PortRanges` stable |
  | v1.58.0 | May 20 | Python SDK; GameServers no longer block in `Starting` waiting for a Pod IP |
  | v1.57.0 | Apr 8 | Repo moved to `agones-dev`; init and sidecar container support |

- **Helm 4.1.3+ is now required.** Most published tutorials assume Helm 3 and
  won't install current Agones as written.

## How the current design maps to Agones

| Current implementation | Agones equivalent |
|---|---|
| `provision_session` calls `RunTask` | Matchmaker requests a `GameServerAllocation`, which atomically moves one server from `Ready` to `Allocated` |
| `resolve_public_ip` (DescribeTasks → DescribeNetworkInterfaces) | Allocation response includes `address`, `addresses`, `ports` and `nodeName` |
| `claim_ticket` conditional `UpdateItem` | Allocation is atomic on the server side. Ticket claiming stays in the matchmaker, but there's no longer a race over servers |
| `find_joinable_session` backfill | Ordered allocation selectors: first an `Allocated` server with free player slots, then a fresh `Ready` one |
| 10s heartbeat to the `sessions` table | SDK `Health()`, called automatically by the Unity SDK every 5s by default |
| `EMPTY_SHUTDOWN_SECONDS` self-termination | SDK `Shutdown()` |
| `SESSION_STALE_SECONDS` liveness check | Agones marks servers that stop health-checking as unhealthy and replaces them |
| `MAX_CONCURRENT_SESSIONS` | Fleet / FleetAutoscaler `maxReplicas` |
| Cold start on every match | Fleet with a buffer autoscaler keeps N servers already `Ready` |

The backfill match is close. Agones' player-capacity pattern is described as
"find me a GameServer that is already allocated, with room for n players",
falling back to a Ready one. That is the same priority order as
`try_form_match`.

```yaml
apiVersion: allocation.agones.dev/v1
kind: GameServerAllocation
spec:
  selectors:
    # Tried first: a running match with room (backfill)
    - matchLabels:
        agones.dev/fleet: ground-trace
      gameServerState: Allocated
      lists:
        players:
          minAvailable: 1
    # Fallback: a warm server from the buffer
    - matchLabels:
        agones.dev/fleet: ground-trace
      lists:
        players:
          minAvailable: 1
```

Selectors are tried in order until one matches. The default scheduling
strategy, `Packed`, bin-packs servers onto as few nodes as possible, which
suits a cluster that scales its nodes.

## Matchmaking layer

| Option | Status | Fit |
|---|---|---|
| **Existing Lambda calls the Agones allocator service** | Agones' documented "allocation from fleet" pattern | **Recommended.** Keeps queueing, ticket claiming, metrics and the API unchanged; only the provisioning call changes |
| Open Match 1 | Last release v1.8.1, December 2023 (the release page omits the year; its dependency bumps date it). Tested on Kubernetes 1.24–1.25 and Helm 3. No ARM image | Stale. Current Agones needs Helm 4 and supports Kubernetes 1.34–1.36 |
| Open Match 2 | Public preview, a single horizontally scalable container. Its repo doesn't mention Agones | Too early to build on |
| Nakama | No official Agones fleet adapter. Heroic Labs has invited the community to write one | Community workarounds only |

### Reference architecture

AWS publishes [Guidance for Game Server Hosting Using Agones and Open Match on
Amazon EKS][aws-guidance] ([sample code][aws-sample]). It deploys two EKS
clusters in two regions joined by VPC peering, routes clients through Global
Accelerator, and runs Open Match's director and a latency-based match function
that picks the region to allocate in. Terraform is applied in three stages:
cluster, in-cluster Helm charts, then cross-cluster networking.

It's a useful reference for multi-region allocation and cert handling, but far
heavier than this project needs, and it depends on Open Match 1.

## Constraints that shape the design

### The Unity SDK has no Counters or Lists

Every Counter and List function is marked unimplemented in the Unity SDK, and
backfill depends on a `players` List. The workaround is small: the Agones
sidecar exposes the same operations over plain HTTP on localhost, so the Unity
server can call them with `UnityWebRequest`.

| Operation | Endpoint |
|---|---|
| Read list | `GET /v1beta1/lists/players` |
| Set capacity / values | `PATCH /v1beta1/lists/players` |
| Player joined | `POST /v1beta1/lists/players:addValue` |
| Player left | `POST /v1beta1/lists/players:removeValue` |

The port comes from `AGONES_SDK_HTTP_PORT` (default 9358). Read the variable
rather than hardcoding the port.

Two caveats:

- Counters and Lists are **Beta**, enabled by default, behind the
  `CountsAndLists` feature gate.
- List changes can take about a second to reach the allocator. The current
  design already has an equivalent window (heartbeat interval vs. stale
  cutoff), so this is the same class of problem, not a new one.

The Unity SDK is installed through the Package Manager:

```json
"com.googleforgames.agones": "https://github.com/agones-dev/agones.git?path=/sdks/unity#v1.61.0"
```

Never call `.Wait()` on the Task returned by `Shutdown()`. The docs warn it
deadlocks.

### EKS Fargate cannot host game servers

AWS's documentation is explicit: Fargate pods "can't specify `HostPort` or
`HostNetwork`", and Agones exposes game servers through host ports. Fargate
pods also run only in private subnets behind a NAT gateway, which is the
component this project deliberately removed. EKS does not offer Fargate Spot.

Game servers therefore need EC2 nodes (managed node group, Karpenter or EKS
Auto Mode). The billing model changes from per-task to per-node, and a warm
pool means paying for node capacity while it sits idle.

### Nodes need public IPs and an open port range

Agones picks the address it returns from the node, preferring external DNS,
then external IP, then internal DNS, then internal IP. A node without a public
IP hands clients an address they can't reach. The existing public subnets fit
this, provided they assign public IPs on launch.

The default dynamic port range is **UDP 7000–8000**, so the security group
changes from the current single `7777` rule.

Under the default `Dynamic` port policy only the host port is assigned
dynamically. The container port stays whatever the spec declares, so the Unity
server can keep listening on 7777 inside the container. (`Passthrough` would
instead make the server listen on the dynamically chosen port.)

The matchmaker side is already done: since Phase 0, `poll_ticket` returns
whatever `resolve_address` gives it, and the Fargate backend reads the port
from the session row instead of a literal 7777. An Agones backend stores the
allocation's port in the row and nothing else changes. The Unity project (the
parent folder of this repo) already handles both ends:

- **Client.** `MatchmakerClient.cs` deserializes `port` into
  `TicketResponse` and connects with `bootstrap.ConnectTo(match.ip,
  (ushort)match.port)`. It never assumes 7777, so a dynamic host port works
  without client changes.
- **Server.** `NetworkBootstrapUI.ConfigureServerFromEnvironment` listens on
  `GAME_PORT`, falling back to 7777, bound to `0.0.0.0`. Under `Dynamic` the
  container port stays fixed, so this works unchanged.

This makes `Dynamic` the right port policy. `Passthrough` would also need the
server to learn its assigned port at runtime, from the SDK's `GameServer()`
status, because Agones doesn't set `GAME_PORT` for it.

### Only a webhook autoscaler scales a Fleet to zero

| Policy | Maturity | Notes |
|---|---|---|
| Buffer | Not stated | Keeps N `Ready` servers. A warm pool by design |
| Webhook | Not stated | Calls your endpoint for a desired replica count. The only policy documented as able to scale to 0 |
| Counter, List | Beta | Scale on aggregate counter/list capacity |
| Schedule, Chain | Not stated | Time-based and composed policies |
| Wasm | Alpha | Off by default, behind `WasmAutoscaler` |

Autoscalers re-evaluate every 30 seconds by default.

That makes the honest comparison **Fargate scale-to-zero vs. an Agones warm
pool**, not two scale-to-zero designs. Agones scale-to-zero means writing and
hosting a webhook, which is possible but gives up most of the latency benefit
that motivates using Agones.

### Calling the allocator from outside the cluster requires mTLS

The `agones-allocator` service is exposed through a load balancer and serves
gRPC and REST, by default on a shared port that uses an experimental gRPC
server with noticeably worse performance. Agones' Helm chart can split them.
Every request must present a client certificate that is on the allocator's
allowlist (`allocator-client-ca`). The Lambda would need to load a client
certificate and key, for example from Secrets Manager.

Calling the Kubernetes API directly to create `GameServerAllocation`
resources is the alternative. It avoids the cert handling but needs EKS
authentication from the Lambda and doesn't support multi-cluster allocation.

### System footprint

A default Helm install runs about nine system pods:

| Component | Default replicas |
|---|---|
| Controller | 2 |
| Extensions | 2 |
| Allocator | 3 |
| Ping | 2 |

Plus an SDK sidecar in every game server pod (30m CPU request). For
production, Agones recommends a dedicated node pool for these, labelled and
tainted `agones.dev/agones-system=true`. For a demo, replica counts can be
lowered through Helm values.

## Cost

| Item | Price |
|---|---|
| EKS control plane, standard support | $0.10/hour (~$73/month) |
| EKS control plane, extended support | $0.60/hour |
| Managed Argo CD (EKS Capability) | Billed hourly per capability, plus usage |
| Nodes | EC2 pricing for system and game server node groups |

Extended support applies once a cluster's Kubernetes version ages out of
standard support, so falling behind on upgrades costs six times as much.

An always-on cluster lands somewhere above $100/month once nodes are included,
against under $0.10/month for the current design. The workable pattern is:
develop locally, `terraform apply` EKS for a measurement window, then
`terraform destroy`. A few hours costs a few dollars.

Self-hosting Argo CD with Helm is cheaper than the managed capability.

## The case against

AWS's guide on [migrating from Agones to GameLift][aws-migrate] argues that
Agones gets expensive at global scale:

- About 30–40 supporting pods in the home region, and 20–25 in each additional
  region, before any game server runs.
- Cross-region networking, the allocator, matchmaking, DDoS relays, ping
  endpoints and blue/green deploy tooling are all yours to build and run.
- Data transfer can reach 40–50% of hosting cost.
- Beyond the EKS control-plane SLA, availability is your responsibility.

AWS sells GameLift, so weigh accordingly. These are still real costs, and they
mark where Agones stops being the right tool. The same post maps the concepts
across:

| Agones | GameLift Servers |
|---|---|
| Fleet spec and Pod template | Container group definition |
| Allocator / custom director | Game session queue |
| Pull-based `Allocate()` | Push-based `OnStartGameSession` |
| `Ready()`, `Health()`, `Shutdown()` | `InitSDK()`, `ProcessReady()`, `ProcessEnding()` |
| Open Match | FlexMatch |
| IP and port from `GameServer.Status` | Placement notifications via SNS |

## Local development

The SDK sidecar runs on its own, with no cluster:

```bash
./sdk-server.linux.amd64 --local -f ./gameserver.yaml
```

Binaries for each OS come in `agonessdk-server-<version>.zip` on the release
page. State lives in memory. Ready, Allocate, Reserve, Shutdown, SetLabel and
SetAnnotation all work, and edits to the YAML file are picked up live and fire
`WatchGameServer()` events. Counters and Lists work only when declared under
`status` in that file. Local mode permits transitions a real cluster wouldn't,
such as Shutdown back to Ready.

On Linux, run the SDK container and the game server with `--network=host` so
they share localhost:

```bash
docker run --network=host --rm us-docker.pkg.dev/agones-images/release/agones-sdk:1.61.0 --local
```

On Windows and macOS, host networking doesn't behave that way. Use Docker
Compose with `network_mode: service:gameserver` so both containers share one
network namespace.

Agones' Unity SDK can't be exercised inside the Unity Editor without a reachable
sidecar. `Connect()` waits up to 30 seconds and then returns `false`. Guard SDK
calls in Editor builds.

For a full local cluster, Agones documents only Minikube. On Windows that
means the cluster lives inside Docker Desktop's VM: the address Agones reports
is unreachable, so a narrowed UDP range is published through Docker and clients
connect to `127.0.0.1`. k3d has the same indirection plus a load-balancer proxy
in front of UDP, and Agones doesn't document it.

This project instead runs **k3s installed natively on a Linux machine on the
LAN** (see [phase-1.md](phase-1.md)). Nodes then have real, reachable IPs and
host ports, like EKS nodes: Agones reports an address the Windows client can
use directly, and the full 7000–8000 range works. k3s isn't on Agones' list of
supported platforms (GKE, AKS, EKS, Minikube), but Akamai publishes a guide
running Agones on a stock k3s install.

## Proposed scope

1. **Local cluster.** Agones on native k3s on a LAN Linux machine, installed
   with Helm 4. Step-by-step in [phase-1.md](phase-1.md).
2. **Fleet.** The existing Unity server image with the Agones SDK added: Ready
   on boot, Shutdown when empty, `players` List updated over the sidecar's HTTP
   API.
3. **Allocation.** A second provisioning path in the matchmaker that calls the
   allocator in place of `RunTask`, selected by configuration so the Fargate
   path stays the default.
4. **Warm pool.** A buffer autoscaler with a small buffer.
5. **GitOps.** Fleet and autoscaler packaged as a Helm chart and deployed by
   Argo CD. Agones Fleet rolling updates replace idle servers without killing
   ones mid-match, so a new server version ships without dropping live games.
6. **Measurement.** EKS brought up briefly to compare `TimeToMatchSeconds` and
   cost per match against the Fargate path, with the results written into the
   README.

## Open questions

Not verified during this research:

- **Allocation with no capacity.** Partly answered: Agones' fleet quickstart
  lists the possible `status.state` values as `Allocated`, `UnAllocated` (no
  GameServer matched) and `Contention` (too many concurrent requests). Still
  unchecked is how the allocator's *REST* endpoint reports those — check the
  gRPC proto / Swagger before writing Phase 2's failure path.
- **Buffer autoscaler with `minReplicas: 0`.** The docs only state that
  webhook autoscalers can scale to zero.
- **Unity Linux ARM64 server builds.** These would decide whether Graviton
  nodes are an option.
- **Karpenter and allocated servers.** Secondary sources say the
  `karpenter.sh/do-not-disrupt` annotation doesn't protect against node expiry
  or Spot interruption. Confirm against Karpenter's docs before running game
  servers on Karpenter-managed Spot nodes.
- **Open Match 2.** No published timeline for general availability.
- **Node pricing.** EC2 costs for a minimal node layout weren't priced.

## Sources

Agones:

- [Releases](https://github.com/googleforgames/agones/releases)
- [Integration pattern: allocation from fleet](https://agones.dev/site/docs/integration-patterns/allocation-from-fleet/)
- [Integration pattern: player capacity](https://agones.dev/site/docs/integration-patterns/player-capacity/)
- [GameServerAllocation reference](https://agones.dev/site/docs/reference/gameserverallocation/)
- [GameServer reference](https://agones.dev/site/docs/reference/gameserver/)
- [FleetAutoscaler reference](https://agones.dev/site/docs/reference/fleetautoscaler/)
- [Allocator service](https://agones.dev/site/docs/advanced/allocator-service/)
- [Client SDKs](https://agones.dev/site/docs/guides/client-sdks/)
- [Unity SDK](https://agones.dev/site/docs/guides/client-sdks/unity/)
- [REST SDK](https://agones.dev/site/docs/guides/client-sdks/rest/)
- [Local development](https://agones.dev/site/docs/guides/client-sdks/local/)
- [Install with Helm](https://agones.dev/site/docs/installation/install-agones/helm/)

CNCF:

- [Agones moves to the CNCF](https://www.cncf.io/blog/2026/03/23/agones-moves-to-the-cncf-a-new-era-for-open-source-multiplayer-game-infrastructure/)
- [CNCF project page](https://www.cncf.io/projects/agones/)

Matchmaking:

- [Open Match releases](https://github.com/googleforgames/open-match/releases)
- [Open Match v1.8.1](https://github.com/googleforgames/open-match/releases/tag/v1.8.1)
- [Open Match 2](https://github.com/googleforgames/open-match2)
- [Heroic Labs forum: Nakama with fleet game servers](https://forum.heroiclabs.com/t/nakama-matchmaking-integration-with-fleet-gameservers/295)

AWS:

- [Guidance: Agones and Open Match on EKS][aws-guidance]
- [Guidance sample code][aws-sample]
- [Migrating from Agones to GameLift][aws-migrate]
- [EKS Fargate considerations](https://docs.aws.amazon.com/eks/latest/userguide/fargate.html)
- [EKS pricing](https://aws.amazon.com/eks/pricing/)

[aws-guidance]: https://docs.aws.amazon.com/solutions/game-server-hosting-using-agones-and-open-match-on-amazon-eks/
[aws-sample]: https://github.com/aws-solutions-library-samples/guidance-for-game-server-hosting-on-amazon-eks
[aws-migrate]: https://aws.amazon.com/blogs/gametech/migrating-from-kubernetes-and-agones-to-amazon-gamelift-servers/
