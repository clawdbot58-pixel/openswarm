/**
 * `LayoutBuilder` — UI for composing a layout from existing views.
 *
 * Drag a view from the palette onto the canvas, or click "Add" to
 * append it.  Edits are buffered locally until the user hits Save.
 */

import { useCallback, useMemo, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Check, FloppyDisk, Plus, Sparkle, X } from "@phosphor-icons/react";
import { LayoutRenderer } from "./LayoutRenderer";
import { ViewPalette } from "./ViewPalette";
import { ApiClientError, layoutsApi } from "../api";
import { cn } from "../utils/cn";
import { motion as motionTokens } from "../theme";
import type { LayoutConfig, LayoutConfigInput, PanelConfig, ViewConfig } from "../types";

interface LayoutBuilderProps {
  initialLayout?: LayoutConfig | null;
  views: ViewConfig[];
  onSaved?: (layout: LayoutConfig) => void;
  onCancel?: () => void;
}

let counter = 0;
const makeId = () => `panel-${Date.now().toString(36)}-${(counter += 1)}`;

const DEFAULT_PANEL: PanelConfig = {
  panel_id: "placeholder",
  view_id: "placeholder",
  position: { x: 0, y: 0, w: 6, h: 6 },
  pinned: false,
};

export function LayoutBuilder({
  initialLayout,
  views,
  onSaved,
  onCancel,
}: LayoutBuilderProps): JSX.Element {
  const [name, setName] = useState(initialLayout?.name ?? "My layout");
  const [panels, setPanels] = useState<PanelConfig[]>(() => initialLayout?.panels ?? []);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  const previewLayout = useMemo<LayoutConfig>(
    () => ({
      layout_id: initialLayout?.layout_id ?? "preview",
      name,
      panels,
      created_by: initialLayout?.created_by ?? "dashboard-user",
      created_at: initialLayout?.created_at ?? new Date().toISOString(),
      updated_at: new Date().toISOString(),
    }),
    [initialLayout, name, panels],
  );

  const addPanel = useCallback(
    (view: ViewConfig) => {
      setPanels((prev) => {
        const nextY = prev.reduce((acc, p) => Math.max(acc, p.position.y + p.position.h), 0);
        const panel: PanelConfig = {
          panel_id: makeId(),
          view_id: view.view_id,
          position: { ...DEFAULT_PANEL.position, y: nextY },
          pinned: false,
          title: view.name,
        };
        return [...prev, panel];
      });
    },
    [],
  );

  const removePanel = useCallback((panelId: string) => {
    setPanels((prev) => prev.filter((p) => p.panel_id !== panelId));
  }, []);

  const save = useCallback(async () => {
    if (panels.length === 0) {
      setError("Add at least one panel before saving.");
      return;
    }
    setSaving(true);
    setError(null);
    setSuccess(false);
    try {
      const input: LayoutConfigInput = { name, panels };
      const result = await layoutsApi.create(input);
      setSuccess(true);
      onSaved?.(result);
    } catch (err) {
      setError(err instanceof ApiClientError ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }, [name, panels, onSaved]);

  return (
    <div className="h-full grid grid-cols-12 gap-3" data-testid="layout-builder">
      <div className="col-span-3 min-h-0">
        <ViewPalette
          views={views}
          onDragStart={addPanel}
          className="h-full"
        />
      </div>

      <div className="col-span-9 min-h-0 flex flex-col surface">
        <header className="flex flex-wrap items-center gap-2 px-4 py-3 border-b border-ink-700/60">
          <div className="flex items-center gap-2 flex-1 min-w-0">
            <Sparkle size={14} weight="duotone" className="text-amber-glow" />
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="bg-transparent text-sm font-display text-ink-50 focus:outline-none focus:ring-1 focus:ring-amber-glow/60 rounded px-1 -mx-1 truncate"
              data-testid="layout-name"
            />
            <span className="text-[10px] font-mono text-ink-300">
              {panels.length} panel{panels.length === 1 ? "" : "s"}
            </span>
          </div>
          <div className="flex items-center gap-2">
            {onCancel && (
              <button
                type="button"
                onClick={onCancel}
                className="inline-flex items-center gap-1.5 h-8 px-3 rounded-md text-[11px] text-ink-300 hover:text-ink-50 ring-1 ring-ink-700/60 hover:ring-ink-500/60 focus-ring"
              >
                <X size={12} weight="bold" />
                Cancel
              </button>
            )}
            <button
              type="button"
              onClick={save}
              disabled={saving}
              data-testid="layout-save"
              className={cn(
                "inline-flex items-center gap-1.5 h-8 px-3 rounded-md text-[11px] font-semibold focus-ring transition-colors",
                success
                  ? "bg-moss-500 text-ink-950"
                  : "bg-amber-glow text-ink-950 hover:bg-amber-pulse",
                saving && "opacity-60",
              )}
            >
              {success ? <Check size={12} weight="bold" /> : <FloppyDisk size={12} weight="bold" />}
              {saving ? "Saving…" : success ? "Saved" : "Save layout"}
            </button>
          </div>
        </header>

        {error && (
          <div className="mx-4 mt-3 px-3 py-2 rounded-md bg-ember-500/10 ring-1 ring-ember-500/30 text-[11px] text-ember-400">
            {error}
          </div>
        )}

        <div className="flex-1 min-h-0 overflow-auto p-3">
          <AnimatePresence mode="popLayout">
            {panels.length === 0 ? (
              <motion.div
                key="empty"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={motionTokens.spring.gentle}
                className="h-full grid place-items-center text-center"
              >
                <div className="max-w-sm">
                  <div className="mx-auto h-10 w-10 rounded-full bg-ink-800/60 grid place-items-center mb-3">
                    <Plus size={18} weight="duotone" className="text-amber-glow" />
                  </div>
                  <h3 className="text-sm font-display text-ink-50">Build a layout</h3>
                  <p className="mt-1 text-xs text-ink-300 leading-relaxed">
                    Drag views from the left palette onto this canvas, or click the
                    <span className="mx-1 inline-flex items-center gap-1 text-amber-glow">
                      <Plus size={10} weight="bold" /> New
                    </span>
                    action to add them.
                  </p>
                </div>
              </motion.div>
            ) : (
              <motion.div
                key="canvas"
                initial={{ opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: 4 }}
                transition={motionTokens.spring.gentle}
                className="h-full"
              >
                <LayoutRenderer
                  layout={previewLayout}
                  views={views}
                  editable
                  onChange={setPanels}
                  onPanelRemove={removePanel}
                />
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </div>
    </div>
  );
}
