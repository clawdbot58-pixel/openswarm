/**
 * Vite entry point.  Mounts the React app and seeds the global
 * providers we need (none yet, but the seam is here for future
 * theme / auth / data contexts).
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles/index.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("Root container #root not found in index.html");
}

const root = createRoot(container);
root.render(
  <StrictMode>
    <App />
  </StrictMode>,
);
