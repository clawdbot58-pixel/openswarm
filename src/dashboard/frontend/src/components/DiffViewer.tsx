/**
 * `DiffViewer` — renders a unified diff string as side-by-side HTML.
 *
 * We avoid pulling in a heavy diff renderer when the kernel already
 * gives us a unified diff.  A simple line-by-line parser renders an
 * aria-accessible, themeable view with stable semantics.
 */

import { useMemo } from "react";
import { ArrowsLeftRight, Plus, Minus } from "@phosphor-icons/react";
import { motion } from "framer-motion";
import { cn } from "../utils/cn";
import { motion as motionTokens } from "../theme";

interface DiffViewerProps {
  diff: string;
  className?: string;
  emptyText?: string;
}

interface DiffLine {
  kind: "meta" | "context" | "add" | "remove" | "info";
  oldLine: number | null;
  newLine: number | null;
  text: string;
}

function parseUnifiedDiff(raw: string): DiffLine[] {
  if (!raw) return [];
  const lines = raw.split(/\r?\n/);
  const out: DiffLine[] = [];
  let oldLine = 0;
  let newLine = 0;
  for (const line of lines) {
    if (line.startsWith("diff --git") || line.startsWith("index ")) {
      out.push({ kind: "info", oldLine: null, newLine: null, text: line });
      continue;
    }
    if (line.startsWith("--- ") || line.startsWith("+++ ")) {
      out.push({ kind: "info", oldLine: null, newLine: null, text: line });
      continue;
    }
    if (line.startsWith("@@")) {
      const match = /@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
      if (match) {
        oldLine = Number(match[1]);
        newLine = Number(match[2]);
      }
      out.push({ kind: "meta", oldLine: null, newLine: null, text: line });
      continue;
    }
    if (line.startsWith("+")) {
      out.push({ kind: "add", oldLine: null, newLine: newLine, text: line.slice(1) });
      newLine += 1;
    } else if (line.startsWith("-")) {
      out.push({ kind: "remove", oldLine: oldLine, newLine: null, text: line.slice(1) });
      oldLine += 1;
    } else if (line.startsWith(" ")) {
      out.push({ kind: "context", oldLine, newLine, text: line.slice(1) });
      oldLine += 1;
      newLine += 1;
    } else if (line === "") {
      continue;
    } else {
      out.push({ kind: "context", oldLine, newLine, text: line });
    }
  }
  return out;
}

export function DiffViewer({ diff, className, emptyText }: DiffViewerProps): JSX.Element {
  const lines = useMemo(() => parseUnifiedDiff(diff), [diff]);
  const counts = useMemo(() => {
    let adds = 0;
    let dels = 0;
    for (const l of lines) {
      if (l.kind === "add") adds += 1;
      if (l.kind === "remove") dels += 1;
    }
    return { adds, dels };
  }, [lines]);

  if (!diff || diff.trim().length === 0) {
    return (
      <div className={cn("p-6 text-center text-sm text-ink-300", className)}>
        {emptyText ?? "No diff for this commit."}
      </div>
    );
  }

  return (
    <div className={cn("surface overflow-hidden", className)}>
      <div className="flex items-center justify-between px-4 py-3 border-b border-ink-700/60">
        <div className="flex items-center gap-2">
          <ArrowsLeftRight size={14} weight="duotone" className="text-amber-glow" />
          <h3 className="panel-title">Diff</h3>
        </div>
        <div className="flex items-center gap-3 text-[11px] font-mono">
          <span className="inline-flex items-center gap-1 text-moss-400">
            <Plus size={10} weight="bold" /> {counts.adds}
          </span>
          <span className="inline-flex items-center gap-1 text-ember-400">
            <Minus size={10} weight="bold" /> {counts.dels}
          </span>
        </div>
      </div>
      <div className="overflow-auto max-h-[60vh] font-mono text-[11px] leading-[1.55]">
        <table className="w-full border-collapse">
          <tbody>
            {lines.map((line, idx) => (
              <motion.tr
                key={idx}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                transition={{ ...motionTokens.spring.gentle, delay: Math.min(idx, 20) * 0.005 }}
                className={cn(
                  line.kind === "add" && "bg-moss-500/8",
                  line.kind === "remove" && "bg-ember-500/10",
                  line.kind === "meta" && "bg-ink-800/40",
                )}
              >
                <td className="select-none px-2 py-0.5 text-right text-ink-400 w-12 border-r border-ink-700/40 tabular-nums">
                  {line.oldLine ?? ""}
                </td>
                <td className="select-none px-2 py-0.5 text-right text-ink-400 w-12 border-r border-ink-700/40 tabular-nums">
                  {line.newLine ?? ""}
                </td>
                <td className="px-2 py-0.5 text-ink-100 whitespace-pre-wrap break-words">
                  {line.kind === "add" ? (
                    <span className="text-moss-400">+ </span>
                  ) : line.kind === "remove" ? (
                    <span className="text-ember-400">- </span>
                  ) : line.kind === "meta" ? (
                    <span className="text-sea-400">@ </span>
                  ) : (
                    <span className="text-ink-400">  </span>
                  )}
                  {line.text}
                </td>
              </motion.tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
