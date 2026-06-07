/**
 * `LogFilterBar` — filter controls for the log stream.
 *
 * Inline filter row: search input + agent/type/severity selects.
 * Designed for density; never wraps awkwardly.
 */

import { Funnel, MagnifyingGlass, X } from "@phosphor-icons/react";
import { cn } from "../utils/cn";
import type { LogFilter } from "../hooks/useLogs";

interface LogFilterBarProps {
  filter: LogFilter;
  onChange: (next: LogFilter) => void;
  agents: string[];
  envelopeTypes: string[];
  severities: string[];
  totalCount: number;
  visibleCount: number;
  onClear?: () => void;
}

export function LogFilterBar({
  filter,
  onChange,
  agents,
  envelopeTypes,
  severities,
  totalCount,
  visibleCount,
  onClear,
}: LogFilterBarProps): JSX.Element {
  const update = (patch: Partial<LogFilter>) => onChange({ ...filter, ...patch });

  const active = Boolean(filter.agent || filter.workflowId || filter.envelopeType || filter.severity || filter.search);

  return (
    <div className="flex flex-wrap items-center gap-2 p-3 border-b border-ink-700/60 bg-ink-900/40">
      <div className="relative flex-1 min-w-[200px]">
        <MagnifyingGlass
          size={13}
          weight="bold"
          className="absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-400 pointer-events-none"
        />
        <input
          type="search"
          aria-label="Search logs"
          placeholder="Search payloads…"
          value={filter.search}
          onChange={(e) => update({ search: e.target.value })}
          className={cn(
            "w-full bg-ink-800/60 ring-1 ring-inset ring-ink-700/60 rounded-md",
            "pl-8 pr-3 h-8 text-xs text-ink-100 placeholder:text-ink-400",
            "focus:outline-none focus:ring-amber-glow/60 transition-shadow",
          )}
        />
      </div>

      <SelectChip
        value={filter.agent}
        onChange={(v) => update({ agent: v })}
        options={agents}
        placeholder="Agent"
        testId="filter-agent"
      />
      <SelectChip
        value={filter.envelopeType}
        onChange={(v) => update({ envelopeType: v })}
        options={envelopeTypes}
        placeholder="Type"
        testId="filter-type"
      />
      <SelectChip
        value={filter.severity}
        onChange={(v) => update({ severity: v })}
        options={severities}
        placeholder="Severity"
        testId="filter-severity"
      />

      <div className="ml-auto flex items-center gap-2">
        <span className="text-[10px] font-mono text-ink-300">
          {visibleCount.toLocaleString()} / {totalCount.toLocaleString()}
        </span>
        {active && (
          <button
            type="button"
            onClick={() => onClear?.()}
            className="inline-flex items-center gap-1 px-2 h-7 text-[11px] text-ink-300 hover:text-ink-100 rounded-md ring-1 ring-inset ring-ink-700/60 hover:ring-ink-500/60 focus-ring"
          >
            <X size={11} weight="bold" />
            Clear
          </button>
        )}
        <span className="hidden sm:inline-flex items-center gap-1 text-[10px] uppercase tracking-widest text-ink-300">
          <Funnel size={10} weight="bold" />
          Filter
        </span>
      </div>
    </div>
  );
}

interface SelectChipProps {
  value: string;
  onChange: (next: string) => void;
  options: string[];
  placeholder: string;
  testId?: string;
}

function SelectChip({ value, onChange, options, placeholder, testId }: SelectChipProps): JSX.Element {
  return (
    <div className="relative">
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        data-testid={testId}
        className={cn(
          "appearance-none bg-ink-800/60 ring-1 ring-inset ring-ink-700/60 rounded-md",
          "h-8 pl-3 pr-7 text-[11px] font-medium text-ink-100 cursor-pointer",
          "focus:outline-none focus:ring-amber-glow/60 transition-shadow",
          value && "ring-amber-glow/40 text-amber-glow",
        )}
      >
        <option value="">{placeholder}</option>
        {options.map((opt) => (
          <option key={opt} value={opt}>
            {opt}
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
