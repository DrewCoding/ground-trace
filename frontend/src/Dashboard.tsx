import { useCallback, useEffect, useState } from "react";
import { useAuth } from "react-oidc-context";

interface Session {
  id: string;
  status: string;
  playerCount: number;
  maxPlayers: number;
  ageSeconds: number;
  heartbeatAgeSeconds: number;
  stale: boolean;
  ready: boolean;
}

interface Ticket {
  id: string;
  waitingSeconds: number;
}

interface Snapshot {
  generatedAt: number;
  sessions: Session[];
  queue: Ticket[];
  capacity: { active: number; max: number; playersPerMatch: number };
}

const POLL_MS = 3000;

function duration(seconds: number) {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}m ${s}s`;
}

export function Dashboard() {
  const auth = useAuth();
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchSnapshot = useCallback(async () => {
    // The ID token, not the access token. Cognito's access token carries
    // client_id rather than an aud claim, so an authorizer configured with an
    // audience is the usual cause of a 401 that looks like a broken login.
    const token = auth.user?.id_token;
    if (!token) return;

    try {
      const res = await fetch(`${import.meta.env.VITE_API_URL}/dashboard`, {
        headers: { Authorization: `Bearer ${token}` },
      });

      if (!res.ok) {
        setError(`${res.status} ${res.statusText}`);
        return;
      }

      setSnapshot(await res.json());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "request failed");
    }
  }, [auth.user]);

  useEffect(() => {
    void fetchSnapshot();
    const timer = setInterval(() => void fetchSnapshot(), POLL_MS);
    return () => clearInterval(timer);
  }, [fetchSnapshot]);

  if (!snapshot) {
    return <p className="muted">{error ?? "Loading..."}</p>;
  }

  const { sessions, queue, capacity } = snapshot;

  return (
    <div className="dashboard">
      {error && <p className="error">Last refresh failed: {error}</p>}

      <section className="stats">
        <Stat label="Active servers" value={`${capacity.active} / ${capacity.max}`} />
        <Stat label="In queue" value={queue.length} />
        <Stat label="Players per match" value={capacity.playersPerMatch} />
      </section>

      <section>
        <h2>Game servers</h2>
        {sessions.length === 0 ? (
          <p className="muted">
            Nothing running. Servers only exist while a match does.
          </p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Session</th>
                <th>Status</th>
                <th>Players</th>
                <th>Uptime</th>
                <th>Heartbeat</th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => (
                <tr key={s.id} className={s.stale ? "stale" : undefined}>
                  <td className="mono">{s.id}</td>
                  <td>
                    {s.status}
                    {!s.ready && <span className="tag">booting</span>}
                    {/* A row can outlive its container: the matchmaker treats
                        a stale heartbeat as dead regardless of TTL. */}
                    {s.stale && <span className="tag warn">stale</span>}
                  </td>
                  <td>
                    {s.playerCount} / {s.maxPlayers}
                  </td>
                  <td>{duration(s.ageSeconds)}</td>
                  <td>{duration(s.heartbeatAgeSeconds)} ago</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section>
        <h2>Queue</h2>
        {queue.length === 0 ? (
          <p className="muted">Nobody waiting.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Ticket</th>
                <th>Waiting</th>
              </tr>
            </thead>
            <tbody>
              {queue.map((t) => (
                <tr key={t.id}>
                  <td className="mono">{t.id}</td>
                  <td>{duration(t.waitingSeconds)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <p className="muted footer">
        Refreshes every {POLL_MS / 1000}s. Identifiers are truncated.
      </p>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="stat">
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
    </div>
  );
}
