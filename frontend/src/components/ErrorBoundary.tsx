import { Component, type ReactNode } from "react";
import { Banner } from "./Banner";

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  failed: boolean;
}

/**
 * Keeps an unexpected rendering problem from blanking the page. The form
 * stays usable, and the next search (which gives this boundary a new key)
 * replaces the message.
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { failed: true };
  }

  render() {
    if (this.state.failed) {
      return (
        <Banner
          tone="error"
          tag="Display problem"
          title="The answer from the server could not be shown"
        >
          <p>
            This is a problem with this page, not a search result. Your ticket is still in the form.
            Press Search to try again.
          </p>
        </Banner>
      );
    }
    return this.props.children;
  }
}
