# Phase 1: Agones on a local cluster

Goal: the existing Unity server running as an Agones Fleet on a local cluster,
reachable from a Windows client, with the server lifecycle driven by Agones.
No AWS, no cost, and no matchmaker yet — allocation is done by hand with
`kubectl`. Wiring the matchmaker in is Phase 2.

**Done when:**

- [ ] Unity servers in the Fleet reach `Ready`, and stay `Ready` while unallocated
- [ ] An allocated server accepts a Windows client at the address Agones reports
- [ ] After the match empties, the server shuts itself down through the SDK and
      the Fleet replaces it

Why a Linux machine rather than Minikube on Windows:
[agones-research.md](agones-research.md#local-development). Where this fits in
the overall plan: [README.md](README.md).

## The two machines

| | Linux box | Windows PC |
|---|---|---|
| Role | Runs the cluster: k3s + Agones | Unity builds, Docker image builds, game client |
| Needs | x86_64 (the Unity server build is `x86_64`), 4 GB RAM minimum, 8 GB comfortable, same LAN as the PC | Already set up: Docker Desktop, Unity 2022.3 with Linux Dedicated Server build support, OpenSSH client (built into Windows) |
| Installs | k3s (includes `kubectl`), Helm 4 | Nothing new; the Agones Unity SDK is a package added in step C1 |

Commands below assume a Debian/Ubuntu box; the k3s and Helm installers work on
most distributions, only the firewall step differs.

---

## Part A — Cluster (Linux box)

### A1. Install k3s, pinned

```bash
curl -sfL https://get.k3s.io | \
  INSTALL_K3S_VERSION=v1.36.5+k3s1 \
  INSTALL_K3S_EXEC="--disable=traefik --write-kubeconfig-mode=644" \
  sh -

echo 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml' >> ~/.bashrc
source ~/.bashrc

kubectl get nodes -o wide
```

- **Pinned to 1.36** because Agones 1.61 supports Kubernetes 1.34–1.36 only.
  k3s's `latest` channel is already 1.37, and `stable` will move there too.
- **`--disable=traefik`:** we don't need an ingress, and k3s's built-in
  load balancer gives Traefik ports 80 and 443 on the node. The Agones
  allocator wants 443 as well (Phase 2), and k3s leaves the second service
  `Pending` when the port is taken.
- **`--write-kubeconfig-mode=644`** lets `kubectl` and `helm` run without sudo.
  Helm doesn't know k3s's kubeconfig path, hence `KUBECONFIG`.

**Check:** the node is `Ready`, and its `INTERNAL-IP` is the box's LAN address
(the one the Windows PC would use to reach it). Agones hands that address to
clients, so if it's a Docker bridge or VPN address instead, reinstall with
`--node-ip=<lan-ip>` added to `INSTALL_K3S_EXEC`.

### A2. Open the game port range

Agones gives each game server a UDP port from 7000–8000 on the node. If `ufw`
is active (`sudo ufw status`):

```bash
sudo ufw allow 7000:8000/udp
```

### A3. Install Helm 4

Agones 1.61 requires Helm 4.1.3 or later.

```bash
curl -fsSL -o get_helm.sh https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-4
chmod 700 get_helm.sh
./get_helm.sh
helm version
```

### A4. Install Agones

```bash
helm repo add agones https://agones.dev/chart/stable
helm repo update
helm install agones agones/agones \
  --namespace agones-system --create-namespace \
  --version 1.61.0 \
  --set agones.ping.install=false

kubectl get pods -n agones-system --watch    # until everything is Running
```

The ping service measures player latency for multi-region placement, which a
single local cluster doesn't need.

If the box has under 8 GB of RAM, also pass
`--set agones.controller.replicas=1,agones.extensions.replicas=1,agones.allocator.replicas=1`
— the defaults run two, two and three of each.

**Milestone A:** every pod in `agones-system` is `Running`.

---

## Part B — Prove the network path

Before any Unity code, check that a game server on the cluster is reachable
from Windows, using Agones' own example server. If this fails, the problem is
the cluster or the network, not your SDK integration.

On the Linux box:

```bash
kubectl create -f https://raw.githubusercontent.com/googleforgames/agones/release-1.61.0/examples/simple-game-server/gameserver.yaml
kubectl get gs --watch          # wait for Ready; note ADDRESS and PORT

nc -u <ADDRESS> <PORT>          # type: Hello   expect: ACK: Hello
```

Then from the Windows PC, in PowerShell (Windows has no `nc`):

```powershell
$udp = New-Object System.Net.Sockets.UdpClient
$udp.Client.ReceiveTimeout = 3000
$udp.Connect("<ADDRESS>", <PORT>)
$msg = [Text.Encoding]::ASCII.GetBytes("Hello`n")
[void]$udp.Send($msg, $msg.Length)
$from = New-Object System.Net.IPEndPoint([Net.IPAddress]::Any, 0)
[Text.Encoding]::ASCII.GetString($udp.Receive([ref]$from))   # ACK: Hello
$udp.Close()
```

A timeout here when `nc` worked on the box means the firewall (A2) or the
node's address (A1).

Clean up: `kubectl delete gs --all`

**Milestone B:** the Windows PC gets the `ACK`.

---

## Part C — Unity server integration (Windows, Unity repo)

### C1. Add the Agones Unity SDK

In `Packages/manifest.json`, under `dependencies`:

```json
"com.googleforgames.agones": "https://github.com/agones-dev/agones.git?path=/sdks/unity#v1.61.0"
```

Pin the tag to the Agones version running in the cluster.

### C2. Agones lifecycle in the server

All of this is server-only and active only when the environment variable
`ORCHESTRATOR=agones` is set — the Fleet in Part E sets it. Agones doesn't
document setting any variables of its own, so detection uses ours, and the
same image keeps working on Fargate.

1. **Ready once listening.** A new `AgonesLifecycle` component, on the same
   GameObject as the SDK's `Agones.AgonesSdk` component, waits for
   `NetworkManager.Singleton.IsServer` (the same wait `SessionHeartbeat` does),
   then `await Connect()` and `await Ready()`. Health pings are sent
   automatically every 5s.
2. **Empty-server timer starts at allocation, not boot.**
   [MatchManager.cs:91](../../Assets/Scripts/Networking/MatchManager.cs) starts a 60s
   shutdown timer when the server spawns. Correct on Fargate, where a server
   only exists once a match has formed; in a warm pool it would kill every
   unallocated server after a minute, and the Fleet would replace them
   forever. Under Agones, skip it there and start it from `WatchGameServer`
   when the state becomes `Allocated`.
3. **Shut down through the SDK.**
   [MatchManager.cs:184-185](../../Assets/Scripts/Networking/MatchManager.cs)
   calls `Application.Quit()`. Under Agones, `await Shutdown()` first, so
   Agones removes the GameServer cleanly instead of treating a dead process as
   unhealthy. **Never** call `.Wait()` on the returned Task — the SDK docs warn
   it deadlocks.
4. **Player list (needed for Phase 2 backfill; can come last).** On connect and
   disconnect, `POST` to the sidecar's
   `http://localhost:9358/v1beta1/lists/players:addValue` / `:removeValue`
   with `UnityWebRequest`. The Unity SDK has no Counters/Lists support, so it's
   raw HTTP.
5. **Leave `SessionHeartbeat` alone.** Without `SESSION_ID` it disables itself,
   which is what should happen under Agones for now.

### C3. Optional fast loop: SDK without a cluster

To iterate on C2 without rebuilding images, run Agones' SDK sidecar on its
own: download `agonessdk-server-1.61.0.zip` from the Agones release page and
run the Windows binary with `--local`. Start a Windows dedicated-server build
with `ORCHESTRATOR=agones` set; the sidecar's log shows each `Ready`, health
ping and `Shutdown`. Simulate allocation by editing the file passed with `-f`,
which `WatchGameServer` picks up live.

---

## Part D — Get the server image onto the cluster

The cluster can't see Docker Desktop's images, so each build is copied over.
On Windows, from the Unity project root, after a Linux Dedicated Server build
into `Builds/LinuxServer`:

```powershell
docker build -f Dockerfile.server -t game-server:dev1 .
docker save game-server:dev1 -o game-server-dev1.tar
scp game-server-dev1.tar <user>@<linux-box>:
```

On the Linux box:

```bash
sudo k3s ctr -n k8s.io images import game-server-dev1.tar
sudo k3s crictl images | grep game-server
```

**Use a new tag for every build** (`dev2`, `dev3`, ...) and never `latest`.
Kubernetes always tries to pull a `:latest` image from a registry, which here
fails with `ErrImagePull` even though the image is sitting on the node.

**Build the game clients from the same commit as this server image**, with no
local changes on either side. Netcode for GameObjects rejects any client whose
`NetworkConfig` hash differs from the server's, and that hash includes every
registered network prefab (`Assets/DefaultNetworkPrefabs.asset`, which Unity
regenerates on its own) and the Netcode package version. The simplest way to
guarantee a match is to build everything on one machine.

---

## Part E — Fleet and allocation

On the Linux box, save as `fleet.yaml`:

```yaml
apiVersion: agones.dev/v1
kind: Fleet
metadata:
  name: ground-trace
spec:
  replicas: 1
  template:
    spec:
      ports:
        - name: default
          portPolicy: Dynamic      # random host port in 7000-8000
          containerPort: 7777      # what NetworkBootstrapUI listens on (GAME_PORT)
          protocol: UDP
      health:
        # Default is 5s. A Unity server takes a few seconds to boot before it
        # can send its first health ping; don't let a slow start read as dead.
        initialDelaySeconds: 30
      template:
        spec:
          containers:
            - name: game-server
              image: game-server:dev1
              imagePullPolicy: IfNotPresent   # imported locally; never pull
              env:
                - name: ORCHESTRATOR
                  value: agones
                - name: EMPTY_SHUTDOWN_SECONDS
                  value: "60"
```

And `allocation.yaml`:

```yaml
apiVersion: allocation.agones.dev/v1
kind: GameServerAllocation
spec:
  selectors:
    - matchLabels:
        agones.dev/fleet: ground-trace
```

Then:

```bash
kubectl apply -f fleet.yaml
kubectl get gs --watch
```

**Check 1:** the server reaches `Ready` — and is still `Ready` two minutes
later. If it cycles (new names appearing every minute), step C2.2 isn't
working.

```bash
kubectl create -f allocation.yaml -o yaml
```

**Check 2:** `status.state` is `Allocated`. The other possible values are
`UnAllocated` (no Ready server) and `Contention`. Note `status.address` and
`status.ports[0].port`.

On Windows, start two game clients and use `NetworkBootstrapUI`'s manual
connect fields with that address and port. Play, then quit both.

**Check 3:** after `EMPTY_SHUTDOWN_SECONDS`, the allocated server disappears
from `kubectl get gs` and a fresh `Ready` one takes its place.

To roll out a new build: import the new tag (Part D), change `image:` in
`fleet.yaml`, and `kubectl apply -f fleet.yaml` again.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| GameServer stuck in `Scheduled` / `RequestReady` | The server never called `Ready()`. `kubectl logs <gameserver-name> --all-containers` shows both the Unity log and the Agones sidecar |
| GameServer goes `Unhealthy` shortly after starting | Health pings started too late: raise `health.initialDelaySeconds`, or check `Connect()` succeeded |
| Pod in `ErrImagePull` / `ImagePullBackOff` | `:latest` tag, missing `imagePullPolicy: IfNotPresent`, or the image wasn't imported with `-n k8s.io` |
| `kubectl get gs` address isn't the LAN IP | k3s picked another interface; reinstall with `--node-ip` (A1) |
| Box reaches the server, Windows doesn't | Firewall (A2) |
| Client connects, then is dropped; server log says `NetworkConfig mismatch` | Client and server built from different project states — different branch, commit, local changes or Unity version. See Part D |
| Allocator service `Pending` (Phase 2) | Something else holds port 443 — Traefik, if A1's flag was missed |

## Sources

- [Agones installation requirements](https://agones.dev/site/docs/installation/) — supported Kubernetes versions
- [Agones Helm install](https://agones.dev/site/docs/installation/install-agones/helm/)
- [Agones quickstart: create a game server](https://agones.dev/site/docs/getting-started/create-gameserver/) and [create a fleet](https://agones.dev/site/docs/getting-started/create-fleet/)
- [Agones Unity SDK](https://agones.dev/site/docs/guides/client-sdks/unity/) and [local SDK server](https://agones.dev/site/docs/guides/client-sdks/local/)
- [Agones GameServer reference](https://agones.dev/site/docs/reference/gameserver/) — health defaults, port policies
- [k3s release channels](https://update.k3s.io/v1-release/channels) and [k3s ServiceLB](https://docs.k3s.io/networking/networking-services)
- [Helm install](https://helm.sh/docs/intro/install/)
- [Akamai: Xonotic on K3s and Agones](https://www.akamai.com/cloud/guides/set-up-a-xonotic-game-server-with-k3s-and-agones/)
