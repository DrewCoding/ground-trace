import { useAuth } from "react-oidc-context";
import { cognitoLogout } from "./auth";
import { Dashboard } from "./Dashboard";
import "./App.css";

export default function App() {
  const auth = useAuth();

  if (auth.isLoading) {
    return <Shell>
      <p className="muted">Signing in...</p>
    </Shell>;
  }

  if (auth.error) {
    return <Shell>
      <p className="error">{auth.error.message}</p>
      <button onClick={() => void auth.signinRedirect()}>Try again</button>
    </Shell>;
  }

  if (!auth.isAuthenticated) {
    return <Shell>
      <p className="muted">
        Operator access only. Accounts are created manually - there is no
        sign-up.
      </p>
      <button onClick={() => void auth.signinRedirect()}>Sign in</button>
    </Shell>;
  }

  return (
    <Shell user={auth.user?.profile.email as string | undefined}>
      <Dashboard />
    </Shell>
  );
}

function Shell({
  children,
  user,
}: {
  children: React.ReactNode;
  user?: string;
}) {
  return (
    <main>
      <header>
        <div>
          <h1>ground-trace</h1>
          <p className="muted">Matchmaker status</p>
        </div>
        {user && (
          <div className="session">
            <span className="muted">{user}</span>
            <button onClick={cognitoLogout}>Sign out</button>
          </div>
        )}
      </header>
      {children}
    </main>
  );
}
