import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles/base.css";
import "./styles/layout.css";
import "./styles/form.css";
import "./styles/outcome.css";
import "./styles/card.css";
import "./styles/resolution.css";

const container = document.getElementById("root");
if (container) {
  createRoot(container, {
    // Rendering problems are shown on the page by the error boundary. They are
    // not written to the console, so ticket text can never end up in a log.
    onCaughtError: () => undefined,
  }).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
