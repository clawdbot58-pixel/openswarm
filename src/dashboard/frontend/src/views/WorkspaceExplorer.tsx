/**
 * `WorkspaceExplorer` — file tree, Monaco editor, and diff viewer.
 *
 * Three-column layout: tree | editor | diff.  Selecting a file fetches
 * its content; selecting a commit fetches the diff.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Editor from "@monaco-editor/react";
import { motion } from "framer-motion";
import { FolderOpen, GitCommit, PencilSimpleLine } from "@phosphor-icons/react";
import { FileTree } from "../components/FileTree";
import { workspacesApi, ApiClientError } from "../api";
import { detectLanguage } from "../utils/language";
import { formatBytes, formatTime, formatRelative, truncate } from "../utils/format";
import { motion as motionTokens } from "../theme";
import type { CommitInfo, FileContent, FileEntry, ViewConfig, WorkspaceSummary } from "../types";

interface WorkspaceExplorerProps {
  config: ViewConfig;
  /** Optional pre-selected workflow id. */
  workflowId?: string;
}

const DEFAULT_HEIGHT = "100%";

export function WorkspaceExplorerView({ config, workflowId }: WorkspaceExplorerProps): JSX.Element {
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[]>([]);
  const [activeId, setActiveId] = useState<string | undefined>(workflowId);
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [history, setHistory] = useState<CommitInfo[]>([]);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [fileContent, setFileContent] = useState<FileContent | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [activeCommit, setActiveCommit] = useState<string | null>(null);
  const [, setDiff] = useState<string>("");
  const [loadingFiles, setLoadingFiles] = useState(false);
  const [loadingWorkspace, setLoadingWorkspace] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await workspacesApi.list();
        if (!cancelled) {
          setWorkspaces(list);
          setActiveId((prev) => prev ?? list[0]?.workflow_id);
          setLoadingWorkspace(false);
        }
      } catch (err) {
        if (!cancelled) {
          setFileError(err instanceof ApiClientError ? err.message : String(err));
          setLoadingWorkspace(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!activeId) return;
    let cancelled = false;
    setLoadingFiles(true);
    Promise.all([
      workspacesApi.files(activeId),
      workspacesApi.history(activeId).catch(() => [] as CommitInfo[]),
    ])
      .then(([fs, hist]) => {
        if (cancelled) return;
        setFiles(fs);
        setHistory(hist);
        setActiveCommit(hist[0]?.hash ?? null);
      })
      .catch((err) => {
        if (!cancelled) setFileError(err instanceof ApiClientError ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoadingFiles(false);
      });
    return () => {
      cancelled = true;
    };
  }, [activeId]);

  useEffect(() => {
    if (!activeCommit || !activeId) {
      setDiff("");
      return;
    }
    let cancelled = false;
    workspacesApi
      .diff(activeId, activeCommit)
      .then((d) => {
        if (!cancelled) setDiff(d);
      })
      .catch((err) => {
        if (!cancelled) setFileError(err instanceof ApiClientError ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [activeId, activeCommit]);

  const onSelectFile = useCallback(
    async (path: string) => {
      setSelectedFile(path);
      if (!activeId) return;
      setFileContent(null);
      setFileError(null);
      try {
        const content = await workspacesApi.file(activeId, path);
        setFileContent(content);
      } catch (err) {
        setFileError(err instanceof ApiClientError ? err.message : String(err));
      }
    },
    [activeId],
  );

  const language = useMemo(() => (selectedFile ? detectLanguage(selectedFile) : "plaintext"), [selectedFile]);

  return (
    <section className="h-full flex flex-col" data-testid="workspace-explorer">
      <header className="px-4 py-3 border-b border-ink-700/60 flex items-center gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          <FolderOpen size={14} weight="duotone" className="text-amber-glow" />
          <h2 className="panel-title">Workspace</h2>
        </div>
        <select
          value={activeId ?? ""}
          onChange={(e) => {
            setActiveId(e.target.value || undefined);
            setSelectedFile(null);
            setFileContent(null);
          }}
          className="bg-ink-900/60 ring-1 ring-inset ring-ink-700/60 rounded h-7 px-2 text-[11px] text-ink-100 focus:outline-none focus:ring-amber-glow/60"
          data-testid="workspace-selector"
        >
          <option value="">— pick a workspace —</option>
          {workspaces.map((w) => (
            <option key={w.workflow_id} value={w.workflow_id}>
              {w.workflow_id.slice(0, 8)} · {w.file_count} files
            </option>
          ))}
        </select>
        {activeId && (
          <div className="ml-auto flex items-center gap-3 text-[10px] text-ink-300 font-mono">
            <span>{files.length} files</span>
            <span>·</span>
            <span>{formatBytes(files.reduce((acc, f) => acc + (f.is_dir ? 0 : f.size), 0))}</span>
          </div>
        )}
      </header>

      {fileError && (
        <div className="mx-4 mt-3 px-3 py-2 rounded-md bg-ember-500/10 ring-1 ring-ember-500/30 text-[11px] text-ember-400">
          {fileError}
        </div>
      )}

      {loadingWorkspace ? (
        <div className="flex-1 grid place-items-center text-sm text-ink-300">Loading workspaces…</div>
      ) : !activeId ? (
        <div className="flex-1 grid place-items-center text-sm text-ink-300">No workspace selected.</div>
      ) : (
        <div className="flex-1 min-h-0 grid grid-cols-12 gap-0">
          <div className="col-span-3 border-r border-ink-700/60 min-h-0">
            <FileTree
              files={files}
              onSelect={onSelectFile}
              selectedPath={selectedFile}
              loading={loadingFiles}
            />
          </div>
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={motionTokens.spring.gentle}
            className="col-span-6 min-h-0 flex flex-col"
          >
            <div className="px-3 py-2 border-b border-ink-700/60 flex items-center gap-2">
              <PencilSimpleLine size={12} weight="duotone" className="text-ink-300" />
              <h3 className="data-label flex-1">
                {selectedFile ? truncate(selectedFile, 60) : "Editor"}
              </h3>
              {fileContent && (
                <span className="text-[10px] text-ink-300 font-mono">
                  {formatBytes(fileContent.size)} · {language}
                </span>
              )}
            </div>
            <div className="flex-1 min-h-0 bg-[#0c0c10]">
              {selectedFile ? (
                <Editor
                  height={DEFAULT_HEIGHT}
                  theme="vs-dark"
                  language={language}
                  value={fileContent?.content ?? ""}
                  loading={<div className="p-4 text-xs text-ink-300 font-mono">loading…</div>}
                  options={{
                    readOnly: true,
                    minimap: { enabled: false },
                    fontSize: 12,
                    fontFamily: "Geist Mono, JetBrains Mono, monospace",
                    wordWrap: "on",
                    scrollBeyondLastLine: false,
                    renderLineHighlight: "gutter",
                    padding: { top: 12, bottom: 12 },
                  }}
                />
              ) : (
                <div className="h-full grid place-items-center text-sm text-ink-300">
                  <div className="text-center max-w-xs">
                    <PencilSimpleLine size={22} className="mx-auto text-ink-400 mb-2" weight="duotone" />
                    <p>Select a file from the tree to inspect or diff it.</p>
                    <p className="text-[10px] font-mono text-ink-400 mt-2">view: {config.view_id}</p>
                  </div>
                </div>
              )}
            </div>
          </motion.div>
          <div className="col-span-3 border-l border-ink-700/60 min-h-0 flex flex-col">
            <div className="px-3 py-2 border-b border-ink-700/60 flex items-center gap-2">
              <GitCommit size={12} weight="duotone" className="text-ink-300" />
              <h3 className="data-label flex-1">History</h3>
            </div>
            <div className="flex-1 overflow-y-auto">
              {history.length === 0 ? (
                <div className="p-4 text-xs text-ink-300 text-center">No commits yet.</div>
              ) : (
                <ul className="divide-y divide-ink-700/40">
                  {history.map((commit) => (
                    <li key={commit.hash}>
                      <button
                        type="button"
                        onClick={() => setActiveCommit(commit.hash)}
                        data-testid="commit-row"
                        data-active={activeCommit === commit.hash ? "true" : undefined}
                        className={cn(
                          "w-full text-left p-3 hover:bg-ink-800/40 focus-ring",
                          activeCommit === commit.hash && "bg-amber-glow/10",
                        )}
                      >
                        <div className="flex items-center justify-between text-[10px] font-mono text-ink-300">
                          <span className="text-amber-glow">{commit.hash.slice(0, 7)}</span>
                          <span>{formatRelative(commit.timestamp)}</span>
                        </div>
                        <div className="mt-1 text-[11px] text-ink-100 truncate">{commit.message}</div>
                        <div className="mt-0.5 text-[10px] text-ink-300 font-mono">
                          {commit.agent_id} · {formatTime(commit.timestamp)}
                        </div>
                        <div className="mt-1 flex items-center gap-2 text-[10px] font-mono">
                          <span className="text-moss-400">+{commit.insertions}</span>
                          <span className="text-ember-400">-{commit.deletions}</span>
                          <span className="text-ink-400">·</span>
                          <span className="text-ink-300">{commit.files_changed.length} files</span>
                        </div>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

// Local import to satisfy strict mode without changing the public surface.
import { cn } from "../utils/cn";
