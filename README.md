# Custom AWS Matchmaker

Cloud infrastructure for an on-demand multiplayer game backend. Players hit
"Find Match"; a dedicated game server is provisioned on AWS Fargate only once
there are enough players for a match, and it terminates itself when the match
is over.

Nothing runs when nobody is playing. Idle cost is under ten cents a month.

---

![Architecture Diagram](./diagram/Architecture-Diagram.png)
