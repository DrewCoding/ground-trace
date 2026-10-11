import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { AuthProvider } from "react-oidc-context";
import { cognitoConfig } from "./auth";
import App from "./App";
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AuthProvider {...cognitoConfig}>
      <App />
    </AuthProvider>
  </StrictMode>
);
