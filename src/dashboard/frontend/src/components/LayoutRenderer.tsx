/**
 * `LayoutRenderer` — renders a `LayoutConfig` as a draggable,
 * resizable grid of `View` panels.
 *
 * Panels without a matching `ViewConfig` render an `UnknownView`
 * placeholder so the dashboard never displays a blank rectangle.
 */

import { useCallback, useMemo, useState } from "react";
import { Responsive, WidthProvider, type Layout as RGLLayout, type Layouts } from "react-grid-layout";
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";
import { motion, AnimatePresence } from "framer-motion";
import { DotsSix, Lock, PushPin, X } from "@phosphor-icons/react";
import { View, UnknownView } from "../views";
import { cn } from "../utils/cn";
import { motion as motionTokens } from "../theme";
import type { LayoutConfig, PanelConfig, ViewConfig } from "../types";

const ResponsiveGrid = WidthProvider(Responsive);

const COLS = { lg: 12, md: 10, sm: 6, xs: 4, xxs: 2 };
const BREAKPOINTS = { lg: 1200, md: 996, sm: 768, xs: 480, xxs: 0 };
const ROW_HEIGHT = 60;

interface LayoutRendererProps {
  layout: LayoutConfig;
  views: ViewConfig[];
  editable?: boolean;
  onChange?: (next: PanelConfig[]) => void;
  onPanelRemove?: (panelId: string) => void;
  onAgentClick?: (agentId: string) => void;
}

interface ResolvedPanel extends PanelConfig {
  view: ViewConfig | null;
  title: string;
}

function resolvePanels(layout: LayoutConfig, views: ViewConfig[]): ResolvedPanel[] {
  const panels = normalisePanels(layout);
  return panels.map((panel, idx) => {
    const view = views.find((v) => v.view_id === panel.view_id) ?? null;
    return {
      ...panel,
      view,
      title: panel.title ?? view?.name ?? `Panel ${idx + 1}`,
    };
  });
}

/**
 * Normalise the layout's panel list.  The Phase 8 prompt spec uses
 * `panels: PanelConfig[]`; the Phase 7 backend stores an opaque
 * `panes: { view_id: PanelConfig | PanelPosition }`.  We accept
 * both and produce a canonical array.
 */
function normalisePanels(layout: LayoutConfig): PanelConfig[] {
  if (Array.isArray(layout.panels) && layout.panels.length > 0) {
    return layout.panels;
  }
  if (layout.panes && typeof layout.panes === "object") {
    return Object.entries(layout.panes).map(([viewId, raw], idx) => {
      if (raw && typeof raw === "object" && "view_id" in raw) {
        const p = raw as PanelConfig;
        return {
          panel_id: p.panel_id ?? `${viewId}-${idx}`,
          view_id: p.view_id,
          position: p.position ?? { x: 0, y: idx * 4, w: 6, h: 4 },
          pinned: Boolean(p.pinned),
          ...(p.title ? { title: p.title } : {}),
        };
      }
      const position = raw as { x: number; y: number; w: number; h: number };
      return {
        panel_id: `${viewId}-${idx}`,
        view_id: viewId,
        position: { x: position.x, y: position.y, w: position.w, h: position.h },
        pinned: false,
      };
    });
  }
  return [];
}

export function LayoutRenderer({
  layout,
  views,
  editable = false,
  onChange,
  onPanelRemove,
  onAgentClick,
}: LayoutRendererProps): JSX.Element {
  const panels = useMemo(() => resolvePanels(layout, views), [layout, views]);
  const [hoveredPanel, setHoveredPanel] = useState<string | null>(null);

  const rglLayouts = useMemo<Layouts>(() => {
    const lg: RGLLayout[] = panels.map((p) => ({
      i: p.panel_id,
      x: p.position.x,
      y: p.position.y,
      w: p.position.w,
      h: p.position.h,
      minW: 2,
      minH: 3,
      static: p.pinned || !editable,
    }));
    return {
      lg,
      md: scaleLayout(lg, COLS.md, COLS.lg),
      sm: scaleLayout(lg, COLS.sm, COLS.lg),
      xs: scaleLayout(lg, COLS.xs, COLS.lg),
      xxs: scaleLayout(lg, COLS.xxs, COLS.lg),
    };
  }, [panels, editable]);

  const handleLayoutChange = useCallback(
    (next: RGLLayout[]) => {
      if (!editable) return;
      const updated: PanelConfig[] = panels.map((p) => {
        const item = next.find((n) => n.i === p.panel_id);
        if (!item) return p;
        return {
          ...p,
          position: { x: item.x, y: item.y, w: item.w, h: item.h },
        };
      });
      onChange?.(updated);
    },
    [editable, onChange, panels],
  );

  if (panels.length === 0) {
    return (
      <div className="h-full grid place-items-center text-center text-sm text-ink-300">
        <div>
          <p className="text-base text-ink-100 font-display">Empty layout</p>
          <p className="mt-1 text-xs">
            {editable
              ? "Drag views from the palette onto the grid."
              : "This layout has no panels. Create one in the layout builder."}
          </p>
        </div>
      </div>
    );
  }

  return (
    <ResponsiveGrid
      className="layout"
      layouts={rglLayouts}
      breakpoints={BREAKPOINTS}
      cols={COLS}
      rowHeight={ROW_HEIGHT}
      margin={[12, 12]}
      containerPadding={[12, 12]}
      onLayoutChange={handleLayoutChange}
      isDraggable={editable}
      isResizable={editable}
      draggableHandle=".panel-drag-handle"
      compactType="vertical"
      preventCollision={false}
    >
      {panels.map((panel) => (
        <div
          key={panel.panel_id}
          data-testid="layout-panel"
          data-panel-id={panel.panel_id}
          data-view-id={panel.view_id}
          onMouseEnter={() => setHoveredPanel(panel.panel_id)}
          onMouseLeave={() => setHoveredPanel(null)}
          className="surface flex flex-col overflow-hidden"
        >
          <div
            className={cn(
              "flex items-center gap-2 px-3 py-2 border-b border-ink-700/60 select-none",
              editable && "panel-drag-handle cursor-grab active:cursor-grabbing",
            )}
          >
            {editable ? <DotsSix size={12} weight="bold" className="text-ink-400" /> : null}
            <h3 className="text-[11px] font-medium text-ink-100 tracking-tight truncate flex-1">
              {panel.title}
            </h3>
            <div className="flex items-center gap-1">
              {panel.pinned && (
                <PushPin size={11} weight="bold" className="text-amber-glow" aria-label="Pinned" />
              )}
              {editable && (
                <AnimatePresence>
                  {hoveredPanel === panel.panel_id && (
                    <motion.button
                      type="button"
                      onClick={() => onPanelRemove?.(panel.panel_id)}
                      initial={{ opacity: 0, scale: 0.9 }}
                      animate={{ opacity: 1, scale: 1 }}
                      exit={{ opacity: 0, scale: 0.9 }}
                      transition={motionTokens.spring.gentle}
                      className="text-ink-300 hover:text-ember-400 focus-ring rounded p-0.5"
                      aria-label={`Remove ${panel.title}`}
                      data-testid="panel-remove"
                    >
                      <X size={12} weight="bold" />
                    </motion.button>
                  )}
                </AnimatePresence>
              )}
              {!editable && !panel.pinned && <Lock size={10} weight="bold" className="text-ink-500" />}
            </div>
          </div>
          <div className="flex-1 min-h-0 overflow-hidden">
            {panel.view ? <View config={panel.view} onAgentClick={onAgentClick} /> : <UnknownView viewId={panel.view_id} />}
          </div>
        </div>
      ))}
    </ResponsiveGrid>
  );
}

/**
 * Scale a layout's x/width coordinates between column counts.
 * Preserves the visual proportions of each panel.
 */
function scaleLayout(input: RGLLayout[], targetCols: number, sourceCols: number): RGLLayout[] {
  if (targetCols === sourceCols) return input;
  const scale = targetCols / sourceCols;
  return input.map((item) => ({
    ...item,
    x: Math.round(item.x * scale),
    w: Math.max(1, Math.round(item.w * scale)),
  }));
}
