/**
 * Application root.
 *
 * Responsibilities:
 *   * Open a WebSocket to /stream and reflect its status in the header.
 *   * Load every saved layout from the ConfigAPI and let the user
 *     switch between them.
 *   * Provide a builder mode for composing new layouts.
 *   * Catch render errors via ErrorBoundary.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Hexagon, Kanban, Plus, Sliders, Sun } from "@phosphor-icons/react";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { ConnectionIndicator, LivePulse } from "./components/ConnectionIndicator";
import { LayoutRenderer } from "./components/LayoutRenderer";
import { LayoutBuilder } from "./components/LayoutBuilder";
import { useLayouts, useViews } from "./hooks/useViews";
import { useWebSocket } from "./hooks/useWebSocket";
import { cn } from "./utils/cn";
import { motion as motionTokens } from "./theme";
import type { LayoutConfig, ViewConfig } from "./types";

type Mode = "view" | "build";

interface AppProps {
  initialLayoutId?: string;
}

export function App({ initialLayoutId }: AppProps): JSX.Element {
  const { data: layouts, loading: layoutsLoading, refresh: refreshLayouts } = useLayouts();
  const { data: views, loading: viewsLoading } = useViews();
  const { status: wsStatus } = useWebSocket();
  const [mode, setMode] = useState<Mode>("view");
  const [activeLayoutId, setActiveLayoutId] = useState<string | undefined>(initialLayoutId);

  // Pick a default layout once data lands.
  useEffect(() => {
    if (activeLayoutId || layouts.length === 0) return;
    const first = layouts[0];
    if (first) setActiveLayoutId(first.layout_id);
  }, [activeLayoutId, layouts]);

  const activeLayout: LayoutConfig | null = useMemo(() => {
    if (!activeLayoutId) return null;
    return layouts.find((l) => l.layout_id === activeLayoutId) ?? null;
  }, [activeLayoutId, layouts]);

  const handleNewLayout = useCallback(() => {
    setActiveLayoutId(undefined);
    setMode("build");
  }, []);

  const handleSaved = useCallback(
    (layout: LayoutConfig) => {
      void refreshLayouts();
      setActiveLayoutId(layout.layout_id);
      setMode("view");
    },
    [refreshLayouts],
  );

  const isLoading = layoutsLoading || viewsLoading;
  const theme = "dark"; // Phase 8 is dark-first.

  return (
    <ErrorBoundary>
      <div className={cn("min-h-[100dvh] flex flex-col", theme === "dark" && "dark")} data-theme={theme}>
        <TopBar
          layouts={layouts}
          activeLayoutId={activeLayoutId}
          onSelect={setActiveLayoutId}
          onNew={handleNewLayout}
          wsStatus={wsStatus}
          mode={mode}
          onModeChange={setMode}
          loading={isLoading}
        />

        <main className="flex-1 min-h-0 px-3 pb-3 pt-2">
          <AnimatePresence mode="wait">
            {mode === "view" ? (
              <motion.div
                key={`view-${activeLayoutId ?? "none"}`}
                initial={{ opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -4 }}
                transition={motionTokens.spring.gentle}
                className="h-full"
              >
                {activeLayout ? (
                  <LayoutRenderer layout={activeLayout} views={views} />
                ) : (
                  <EmptyState
                    loading={isLoading}
                    hasLayouts={layouts.length > 0}
                    onNew={handleNewLayout}
                  />
                )}
              </motion.div>
            ) : (
              <motion.div
                key="build"
                initial={{ opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -4 }}
                transition={motionTokens.spring.gentle}
                className="h-full"
              >
                <LayoutBuilder
                  initialLayout={activeLayout}
                  views={views}
                  onSaved={handleSaved}
                  onCancel={() => setMode("view")}
                />
              </motion.div>
            )}
          </AnimatePresence>
        </main>
      </div>
    </ErrorBoundary>
  );
}

// ---------------------------------------------------------------------------
// Top bar
// ---------------------------------------------------------------------------

interface TopBarProps {
  layouts: LayoutConfig[];
  activeLayoutId: string | undefined;
  onSelect: (id: string) => void;
  onNew: () => void;
  wsStatus: import("./types").WebSocketStatus;
  mode: Mode;
  onModeChange: (mode: Mode) => void;
  loading: boolean;
}

function TopBar({
  layouts,
  activeLayoutId,
  onSelect,
  onNew,
  wsStatus,
  mode,
  onModeChange,
  loading,
}: TopBarProps): JSX.Element {
  return (
    <header className="sticky top-0 z-30 px-3 py-2.5 flex items-center gap-3 border-b border-ink-700/60 bg-ink-950/70 backdrop-blur-md">
      <div className="flex items-center gap-2.5">
        <div className="relative h-7 w-7 rounded-lg bg-ink-900/60 grid place-items-center ring-1 ring-amber-glow/30 overflow-hidden">
          <Hexagon size={16} weight="duotone" className="text-amber-glow" />
          <span className="absolute inset-0 scanline opacity-50" />
        </div>
        <div className="flex flex-col leading-none">
          <span className="text-sm font-display font-semibold text-ink-50 tracking-tight">OpenSwarm</span>
          <span className="text-[9px] uppercase tracking-[0.2em] text-ink-300">swarm dashboard</span>
        </div>
      </div>

      <div className="ml-2 flex items-center gap-1">
        <LayoutPicker
          layouts={layouts}
          activeLayoutId={activeLayoutId}
          onSelect={onSelect}
          disabled={loading}
        />
        <button
          type="button"
          onClick={onNew}
          className="inline-flex items-center gap-1 h-7 px-2 rounded-md text-[11px] text-ink-300 hover:text-ink-50 ring-1 ring-ink-700/60 hover:ring-amber-glow/40 focus-ring"
          data-testid="new-layout"
        >
          <Plus size={11} weight="bold" />
          New layout
        </button>
      </div>

      <div className="ml-auto flex items-center gap-2">
        <LivePulse />
        <ConnectionIndicator status={wsStatus} />
        <div className="ml-1 inline-flex items-center rounded-md ring-1 ring-ink-700/60 overflow-hidden">
          <ModeButton active={mode === "view"} onClick={() => onModeChange("view")}>
            <Kanban size={11} weight="bold" /> View
          </ModeButton>
          <ModeButton active={mode === "build"} onClick={() => onModeChange("build")}>
            <Sliders size={11} weight="bold" /> Build
          </ModeButton>
        </div>
        <button
          type="button"
          aria-label="Theme (locked to dark)"
          className="h-7 w-7 grid place-items-center rounded-md text-ink-300 ring-1 ring-ink-700/60 hover:ring-amber-glow/40 focus-ring"
        >
          <Sun size={12} weight="bold" />
        </button>
      </div>
    </header>
  );
}

function ModeButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}): JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      data-active={active ? "true" : undefined}
      className={cn(
        "inline-flex items-center gap-1 h-7 px-2 text-[11px] font-medium focus-ring transition-colors",
        active
          ? "bg-amber-glow/15 text-amber-glow"
          : "text-ink-300 hover:text-ink-50",
      )}
    >
      {children}
    </button>
  );
}

function LayoutPicker({
  layouts,
  activeLayoutId,
  onSelect,
  disabled,
}: {
  layouts: LayoutConfig[];
  activeLayoutId: string | undefined;
  onSelect: (id: string) => void;
  disabled: boolean;
}): JSX.Element {
  if (layouts.length === 0) {
    return (
      <span className="text-[11px] text-ink-300 font-mono">
        {disabled ? "loading layouts…" : "no layouts yet"}
      </span>
    );
  }
  return (
    <div className="relative">
      <select
        value={activeLayoutId ?? ""}
        onChange={(e) => onSelect(e.target.value)}
        disabled={disabled}
        data-testid="layout-picker"
        className="appearance-none bg-ink-900/60 ring-1 ring-inset ring-ink-700/60 rounded h-7 pl-2.5 pr-7 text-[11px] text-ink-100 cursor-pointer focus:outline-none focus:ring-amber-glow/60"
      >
        {layouts.map((l) => (
          <option key={l.layout_id} value={l.layout_id}>
            {l.name}
          </option>
        ))}
      </select>
      <span
        aria-hidden="true"
        className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-ink-400 text-[10px]"
      >
        ▾
      </span>
    </div>
  );
}

function EmptyState({
  loading,
  hasLayouts,
  onNew,
}: {
  loading: boolean;
  hasLayouts: boolean;
  onNew: () => void;
}): JSX.Element {
  return (
    <div className="h-full grid place-items-center">
      <div className="text-center max-w-md">
        <div className="mx-auto h-12 w-12 rounded-2xl bg-ink-900/60 ring-1 ring-amber-glow/30 grid place-items-center mb-4">
          <Hexagon size={22} weight="duotone" className="text-amber-glow" />
        </div>
        <h1 className="text-xl font-display font-semibold text-ink-50">
          {loading ? "Warming up the swarm…" : hasLayouts ? "Pick a layout above" : "Compose your first layout"}
        </h1>
        <p className="mt-2 text-sm text-ink-300 leading-relaxed">
          {loading
            ? "Loading views and layouts from the dashboard backend."
            : hasLayouts
              ? "Use the layout picker to switch between saved configurations, or start fresh."
              : "The ConfigAPI has no layouts yet. Build one to give the swarm a place to live."}
        </p>
        {!loading && !hasLayouts && (
          <button
            type="button"
            onClick={onNew}
            className="mt-5 inline-flex items-center gap-2 rounded-md bg-amber-glow text-ink-950 h-9 px-4 text-sm font-semibold hover:bg-amber-pulse focus-ring"
          >
            <Plus size={14} weight="bold" /> Build a layout
          </button>
        )}
      </div>
    </div>
  );
}

export default App;

// Used in tests and other entry points.
export type { ViewConfig };
