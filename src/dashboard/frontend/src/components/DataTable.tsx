/**
 * `DataTable` — generic, dense data table for arbitrary data
 * sources.  Used by the `custom` view type to render anything the
 * ConfigAPI hands us.
 *
 * Renders the first 5 fields of each row, with type-aware formatting.
 * No infinite-scroll bells, just a clean reading experience.
 */

import { useMemo } from "react";
import { motion } from "framer-motion";
import { Database, Hash, Stack } from "@phosphor-icons/react";
import { cn } from "../utils/cn";
import { formatBytes, formatCount, formatTime } from "../utils/format";
import { motion as motionTokens } from "../theme";

interface DataTableProps {
  title: string;
  data: unknown[];
  className?: string;
  emptyText?: string;
  maxRows?: number;
}

type Row = Record<string, unknown>;

function isRow(value: unknown): value is Row {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function inferColumns(rows: Row[]): string[] {
  const seen = new Set<string>();
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (!seen.has(key)) seen.add(key);
      if (seen.size >= 6) break;
    }
    if (seen.size >= 6) break;
  }
  return Array.from(seen);
}

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") {
    if (Number.isInteger(value) && Math.abs(value) < 1_000_000) return value.toLocaleString("en-US");
    return formatCount(value);
  }
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "string") {
    if (/^\d{4}-\d{2}-\d{2}T/.test(value)) return formatTime(value);
    if (value.length > 80) return `${value.slice(0, 80)}…`;
    return value;
  }
  if (Array.isArray(value)) return `[${value.length}]`;
  if (typeof value === "object") return "{…}";
  return String(value);
}

function formatBytesCell(value: unknown): string | null {
  if (typeof value !== "number") return null;
  if (value < 1024 * 1024 && value >= 0) return formatBytes(value);
  return null;
}

export function DataTable({ title, data, className, emptyText, maxRows = 50 }: DataTableProps): JSX.Element {
  const rows = useMemo<Row[]>(() => (Array.isArray(data) ? data.filter(isRow) : []), [data]);
  const columns = useMemo(() => inferColumns(rows), [rows]);
  const visible = rows.slice(0, maxRows);
  const truncated = rows.length > maxRows;

  return (
    <div className={cn("surface overflow-hidden", className)}>
      <div className="flex items-center justify-between px-4 py-3 border-b border-ink-700/60">
        <div className="flex items-center gap-2 min-w-0">
          <Database size={14} weight="duotone" className="text-amber-glow" />
          <h3 className="panel-title truncate">{title}</h3>
        </div>
        <div className="text-[11px] text-ink-300 font-mono">{rows.length} rows</div>
      </div>

      {rows.length === 0 ? (
        <div className="px-6 py-12 text-center text-sm text-ink-300">{emptyText ?? "No data yet."}</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="text-[10px] uppercase tracking-[0.16em] text-ink-300 bg-ink-800/30">
                <th className="px-3 py-2 w-8 font-medium">
                  <Hash size={11} weight="bold" />
                </th>
                {columns.map((col) => (
                  <th key={col} className="px-3 py-2 font-medium font-mono normal-case tracking-normal text-ink-200">
                    {col}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {visible.map((row, idx) => (
                <motion.tr
                  key={idx}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  transition={{ ...motionTokens.spring.gentle, delay: Math.min(idx, 12) * 0.015 }}
                  className="border-t border-ink-700/40 hover:bg-ink-800/30 transition-colors"
                >
                  <td className="px-3 py-2 text-[10px] font-mono text-ink-300 tabular-nums">
                    {idx + 1}
                  </td>
                  {columns.map((col) => {
                    const raw = row[col];
                    const bytes = formatBytesCell(raw);
                    return (
                      <td
                        key={col}
                        className={cn(
                          "px-3 py-2 text-xs text-ink-100 max-w-[280px] truncate",
                          typeof raw === "number" && "font-mono tabular-nums",
                        )}
                        title={typeof raw === "string" ? raw : undefined}
                      >
                        {bytes ?? formatCell(raw)}
                      </td>
                    );
                  })}
                </motion.tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {truncated && (
        <div className="px-4 py-2 border-t border-ink-700/60 text-[10px] text-ink-300 font-mono flex items-center gap-1.5">
          <Stack size={11} weight="bold" />
          Showing {maxRows} of {rows.length}
        </div>
      )}
    </div>
  );
}
