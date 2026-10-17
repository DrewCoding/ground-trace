# Phase 5: EKS, for measurement windows

Goal: the same Agones setup on EKS, brought up with Terraform for a
measurement session and destroyed afterwards — so the real matchmaker Lambda
can run the Agones path against real AWS networking.

**Done when:**

- [ ] `terraform apply` in a separate root brings up a cluster that Argo CD
      populates from the same charts as the local cluster
- [ ] The deployed matchmaker, switched to `ORCHESTRATOR=agones`, matches
      players onto EKS game servers
- [ ] `terraform destroy` returns the account to its idle cost

**Depends on:** Phase 2 (the backend) and Phase 4 (GitOps).

---

## Cost — the reason this is temporary

| Item | Price |
|---|---|
| EKS control plane, standard support | $0.10/hour (~$73/month) |
| EKS control plane, extended support | $0.60/hour — once the cluster's Kubernetes version ages out of standard support |
| Nodes | EC2 pricing, system pool + game server pool |

An always-on cluster costs over a thousand times today's idle bill. A
measurement session of a few hours costs a few dollars. Keep the Kubernetes
version within both standard support and Agones' supported range (1.34–1.36
for Agones 1.61).

## Steps

### 1. A separate Terraform root

`infrastructure/eks/`, with its **own state key** in the existing state
bucket. `terraform destroy` there can never touch the Fargate stack.

It reads the existing VPC (by tag, or remote state) rather than creating one.

**Keep it out of CI.** `.github/workflows/terraform.yml` only plans and applies
`infrastructure/aws`, so this root is never applied on merge. That's
deliberate: a merge must not be able to start a $73/month cluster. Apply and
destroy it by hand.

### 2. Nodes: EC2, not Fargate

EKS Fargate can't host game servers. AWS's docs: Fargate pods "can't specify
`HostPort` or `HostNetwork`", and Agones exposes game servers on host ports.
Fargate pods also only run in private subnets behind a NAT gateway — the
component this project removed. (EKS doesn't offer Fargate Spot either.)

Two managed node groups:

| Pool | Placement | Notes |
|---|---|---|
| System | Any subnet | Labelled and tainted `agones.dev/agones-system=true`, so game servers never land there and Agones' controllers do |
| Game servers | **Public subnets, public IP on launch** | Agones hands clients the node's address, preferring external over internal. A node without a public IP gives players an address they can't reach |

### 3. Networking

- Security group: UDP 7000–8000 inbound, matching the Agones port range.
- The allocator is exposed through a LoadBalancer Service. The Lambda runs
  outside the VPC and has no fixed egress IP, so the allocator is reachable
  from the internet and **mTLS is the access control** — the client cert in
  Secrets Manager from Phase 2.
- Matchmaker IAM: `secretsmanager:GetSecretValue` on that secret.

### 4. Bootstrap

Install Argo CD (Phase 4) and point it at the EKS values files. Everything
else follows from git.

### 5. The measurement window

1. `terraform apply` in `infrastructure/eks`; wait for Argo CD to sync.
2. Set the matchmaker's `ORCHESTRATOR` to `agones` and apply
   `infrastructure/aws`.
3. Run the Phase 6 measurements.
4. **Set `ORCHESTRATOR` back to `fargate` and apply — before destroying.**
   Destroying first leaves the live matchmaker pointing at a cluster that no
   longer exists, and nobody can get a match.
5. `terraform destroy` in `infrastructure/eks`.

A billing alarm on EKS charges is a cheap backstop against step 5 being
forgotten.

## Open questions

- Which load balancer the allocator Service gets (the in-tree default vs. the
  AWS Load Balancer Controller), and whether that matters for a short window.
- Node instance sizes — not priced yet; size the game server pool from Phase
  1's observed memory per server.
