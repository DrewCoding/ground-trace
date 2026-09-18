# Custom AWS Matchmaker

Cloud infrastructure for an on-demand multiplayer game backend. Players hit
"Find Match"; a dedicated game server is provisioned on AWS Fargate only once
there are enough players for a match, and it terminates itself when the match
is over.

Nothing runs when nobody is playing. Idle cost is under ten cents a month.

---

![Architecture Diagram](./diagram/Architecture-Diagram.png)

The dotted UDP line is the important one: **API Gateway is control plane only.**
It answers "where do I go?" once, then the client talks straight to the Fargate
task. Gameplay traffic never touches the API. It couldn't, since API Gateway
is HTTP and this is a 20 Hz UDP game loop.

### Resource inventory

| Service         | Resource                              | Notes                                                                                                   |
| --------------- | ------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| **VPC**         | `ground-trace-vpc` 10.0.0.0/16        | 2 public + 2 private subnets across `us-west-1a` / `1c`; AZs resolved by data source, not hardcoded     |
|                 | Security group                        | Inbound UDP 7777 from `0.0.0.0/0` — clients connect from arbitrary residential IPs                      |
|                 | _(no NAT gateway)_                    | Deliberate; see design notes                                                                            |
| **ECR**         | `ground-trace-game-server`            | Scan-on-push, lifecycle policy keeps 5 images                                                           |
| **ECS**         | `ground-trace-cluster`                | Fargate only, Container Insights enabled                                                                |
|                 | Task definition rev 4                 | 512 CPU / 1024 MB, `awsvpc`, UDP 7777, env-configured                                                   |
| **Lambda**      | `ground-trace-matchmaker`             | `python3.13`, 256 MB, boto3 only — no dependencies to package                                           |
| **API Gateway** | HTTP API (v2)                         | `POST /queue`, `GET·DELETE /queue/{ticketId}`, `POST /sessions/{id}/heartbeat`, `DELETE /sessions/{id}` |
| **DynamoDB**    | `ground-trace-queue`                  | PK `ticketId`, GSI `status-queuedAt-index`, TTL on `expiresAt`, on-demand                               |
|                 | `ground-trace-sessions`               | PK `sessionId`, GSI `status-createdAt-index`, TTL on `expiresAt`, on-demand                             |
| **CloudWatch**  | `/ecs/ground-trace-game-server`       | 7-day retention                                                                                         |
|                 | `/aws/lambda/ground-trace-matchmaker` | Lambda logs                                                                                             |
|                 | Container Insights                    | Per-task CPU/memory metrics                                                                             |
