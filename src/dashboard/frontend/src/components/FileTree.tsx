/**
 * `FileTree` — recursive file tree with expand/collapse.
 *
 * Renders a workspace's files and directories.  Selection is
 * controlled by the parent (WorkspaceExplorer).
 */

import { useMemo, useState } from "react";
import { CaretDown, CaretRight, File, Folder, FolderOpen, MagnifyingGlass } from "@phosphor-icons/react";
import { motion, AnimatePresence } from "framer-motion";
import { cn } from "../utils/cn";
import { formatBytes, formatRelative } from "../utils/format";
import { motion as motionTokens } from "../theme";
import type { FileEntry } from "../types";

interface FileTreeProps {
  files: FileEntry[];
  onSelect: (path: string) => void;
  selectedPath: string | null;
  loading?: boolean;
  className?: string;
}

interface TreeNode {
  name: string;
  path: string;
  isDir: boolean;
  size: number;
  modified: string;
  children: TreeNode[];
}

function buildTree(entries: FileEntry[]): TreeNode[] {
  const root: Record<string, TreeNode> = {};
  for (const entry of entries) {
    const parts = entry.path.split("/").filter(Boolean);
    let cursor: Record<string, TreeNode> | null = null;
    let fullPath = "";
    for (let i = 0; i < parts.length; i += 1) {
      const part = parts[i]!;
      fullPath = fullPath ? `${fullPath}/${part}` : part;
      const isLast = i === parts.length - 1;
      if (!cursor) cursor = root;
      const map = cursor as unknown as Record<string, TreeNode>;
      if (!map[part]) {
        map[part] = {
          name: part,
          path: fullPath,
          isDir: !isLast ? true : entry.is_dir,
          size: isLast ? entry.size : 0,
          modified: isLast ? entry.modified_at : "",
          children: [],
        };
      }
      const node = map[part]!;
      if (isLast) {
        node.isDir = entry.is_dir;
        node.size = entry.size;
        node.modified = entry.modified_at;
      }
      // descend into the freshly-touched node on the next iteration
      const childMap: Record<string, TreeNode> = {};
      for (const child of node.children) childMap[child.name] = child;
      cursor = childMap;
    }
  }
  // Convert the root map into a sorted array.
  const toArray = (map: Record<string, TreeNode>): TreeNode[] => {
    const arr = Object.values(map);
    arr.sort((a, b) => {
      if (a.isDir !== b.isDir) return a.isDir ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    for (const node of arr) {
      if (node.children.length > 0) {
        // children are stored as an array; build a synthetic map for recursion
        const childMap: Record<string, TreeNode> = {};
        for (const child of node.children) childMap[child.name] = child;
        node.children = toArray(childMap);
      }
    }
    return arr;
  };
  return toArray(root);
}

export function FileTree({ files, onSelect, selectedPath, loading, className }: FileTreeProps): JSX.Element {
  const tree = useMemo(() => buildTree(files), [files]);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set([tree[0]?.path].filter(Boolean) as string[]));
  const [query, setQuery] = useState("");

  const toggle = (path: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const expandAll = () => setExpanded(new Set(files.filter((f) => f.is_dir).map((f) => f.path)));

  if (loading) {
    return <div className="p-4 text-xs text-ink-300 font-mono">loading files…</div>;
  }
  if (tree.length === 0) {
    return (
      <div className="p-4 text-center text-xs text-ink-300">
        <p>No files yet.</p>
        <p className="mt-1 text-[10px] text-ink-400">Agents will commit into this workspace as they execute.</p>
      </div>
    );
  }

  return (
    <div className={cn("flex flex-col h-full", className)}>
      <div className="p-2 border-b border-ink-700/60 flex items-center gap-2">
        <div className="relative flex-1">
          <MagnifyingGlass
            size={11}
            weight="bold"
            className="absolute left-2 top-1/2 -translate-y-1/2 text-ink-400 pointer-events-none"
          />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter files"
            className="w-full bg-ink-800/60 ring-1 ring-inset ring-ink-700/60 rounded h-7 pl-7 pr-2 text-[11px] text-ink-100 focus:outline-none focus:ring-amber-glow/60"
          />
        </div>
        <button
          type="button"
          onClick={expandAll}
          className="text-[10px] text-ink-300 hover:text-amber-glow uppercase tracking-widest focus-ring"
        >
          expand
        </button>
      </div>
      <ul className="flex-1 overflow-y-auto p-1.5 text-xs font-mono">
        {tree.map((node) => (
          <TreeItem
            key={node.path}
            node={node}
            depth={0}
            expanded={expanded}
            onToggle={toggle}
            onSelect={onSelect}
            selectedPath={selectedPath}
            query={query.trim().toLowerCase()}
          />
        ))}
      </ul>
    </div>
  );
}

interface TreeItemProps {
  node: TreeNode;
  depth: number;
  expanded: Set<string>;
  onToggle: (path: string) => void;
  onSelect: (path: string) => void;
  selectedPath: string | null;
  query: string;
}

function TreeItem({ node, depth, expanded, onToggle, onSelect, selectedPath, query }: TreeItemProps): JSX.Element | null {
  const isExpanded = expanded.has(node.path);
  const isSelected = !node.isDir && selectedPath === node.path;
  const matchesQuery = !query || node.name.toLowerCase().includes(query);

  if (!matchesQuery) {
    // still recurse for directory contents
    if (!node.isDir) return null;
  }

  return (
    <li>
      <button
        type="button"
        onClick={() => (node.isDir ? onToggle(node.path) : onSelect(node.path))}
        data-testid={!node.isDir ? "file-row" : undefined}
        data-path={node.path}
        data-selected={isSelected ? "true" : undefined}
        className={cn(
          "group flex items-center gap-1.5 w-full text-left rounded px-1.5 py-1 hover:bg-ink-800/60 focus-ring",
          isSelected && "bg-amber-glow/12 text-amber-glow",
        )}
        style={{ paddingLeft: `${depth * 12 + 6}px` }}
      >
        {node.isDir ? (
          <>
            <span className="text-ink-400 w-3 inline-flex">
              {isExpanded ? <CaretDown size={10} weight="bold" /> : <CaretRight size={10} weight="bold" />}
            </span>
            <span className="text-amber-pulse">
              {isExpanded ? <FolderOpen size={12} weight="duotone" /> : <Folder size={12} weight="duotone" />}
            </span>
            <span className="text-ink-100 truncate">{node.name}</span>
          </>
        ) : (
          <>
            <span className="w-3" />
            <File size={12} weight="duotone" className="text-ink-300" />
            <span className="truncate flex-1 text-ink-100">{node.name}</span>
            <span className="text-[10px] text-ink-400 font-mono">{formatBytes(node.size)}</span>
          </>
        )}
      </button>
      <AnimatePresence initial={false}>
        {node.isDir && isExpanded && (
          <motion.ul
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={motionTokens.spring.gentle}
            className="overflow-hidden"
          >
            {node.children.map((child) => (
              <TreeItem
                key={child.path}
                node={child}
                depth={depth + 1}
                expanded={expanded}
                onToggle={onToggle}
                onSelect={onSelect}
                selectedPath={selectedPath}
                query={query}
              />
            ))}
          </motion.ul>
        )}
      </AnimatePresence>
      {!node.isDir && node.modified && (
        <div
          className="text-[10px] text-ink-400 font-mono"
          style={{ paddingLeft: `${depth * 12 + 22}px` }}
        >
          {formatRelative(node.modified)}
        </div>
      )}
    </li>
  );
}
