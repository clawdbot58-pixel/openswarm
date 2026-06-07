/**
 * Error boundary — top-level safety net for the dashboard.
 *
 * On error, surfaces a calm recovery screen with the option to reset
 * the React tree.  Logs to console; in a future phase this could
 * forward to the introspection telemetry.
 */

import { Component, type ErrorInfo, type ReactNode } from "react";
import { Warning, ArrowsClockwise } from "@phosphor-icons/react";
import { motion } from "framer-motion";
import { motion as motionTokens } from "../theme";

interface Props {
  children: ReactNode;
  fallbackTitle?: string;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  override state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    // eslint-disable-next-line no-console
    console.error("Dashboard crashed:", error, info);
  }

  reset = (): void => {
    this.setState({ hasError: false, error: null });
  };

  override render(): ReactNode {
    if (!this.state.hasError) return this.props.children;
    return (
      <div className="min-h-[100dvh] flex items-center justify-center p-6">
        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={motionTokens.spring.gentle}
          className="surface max-w-md w-full p-8 text-center"
        >
          <div className="mx-auto h-12 w-12 rounded-full bg-ember-500/12 grid place-items-center mb-4">
            <Warning size={22} weight="bold" className="text-ember-400" />
          </div>
          <h1 className="text-xl font-display font-semibold text-ink-50">
            {this.props.fallbackTitle ?? "The swarm stumbled"}
          </h1>
          <p className="mt-2 text-sm text-ink-300 leading-relaxed">
            Something went wrong rendering this view. The dashboard recovered
            its balance; you can re-enter below.
          </p>
          {this.state.error?.message && (
            <pre className="mt-4 p-3 rounded-md bg-ink-800/60 text-left text-xs text-ink-300 font-mono overflow-auto max-h-32">
              {this.state.error.message}
            </pre>
          )}
          <button
            type="button"
            onClick={this.reset}
            className="mt-6 inline-flex items-center gap-2 rounded-md bg-amber-glow/15 px-4 py-2 text-sm font-medium text-amber-glow hover:bg-amber-glow/25 focus-ring transition-colors"
          >
            <ArrowsClockwise size={16} weight="bold" />
            Try again
          </button>
        </motion.div>
      </div>
    );
  }
}
