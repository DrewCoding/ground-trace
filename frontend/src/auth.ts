import type { AuthProviderProps } from "react-oidc-context";

export const cognitoConfig: AuthProviderProps = {
  authority: import.meta.env.VITE_COGNITO_AUTHORITY,
  client_id: import.meta.env.VITE_COGNITO_CLIENT_ID,
  redirect_uri: import.meta.env.VITE_REDIRECT_URI,

  // Authorization code flow with PKCE. The implicit flow would return tokens
  // in the URL fragment, where they end up in browser history.
  response_type: "code",
  scope: "openid email profile",

  // Strip ?code=&state= after the redirect. Without this, a page refresh
  // tries to redeem an authorization code that has already been used, and
  // Cognito rejects it - which looks like a broken login rather than a
  // replayed request.
  onSigninCallback: () => {
    window.history.replaceState({}, document.title, window.location.pathname);
  },
};

// Cognito doesn't implement the OIDC end_session_endpoint, so the library's
// signoutRedirect() silently fails to clear the hosted-UI session: the next
// login skips the password prompt and looks like it never logged out. The
// hosted UI's own /logout has to be hit directly.
export function cognitoLogout() {
  const domain = import.meta.env.VITE_COGNITO_DOMAIN;
  const clientId = import.meta.env.VITE_COGNITO_CLIENT_ID;
  const logoutUri = import.meta.env.VITE_REDIRECT_URI;

  window.location.href =
    `${domain}/logout?client_id=${clientId}` +
    `&logout_uri=${encodeURIComponent(logoutUri)}`;
}
