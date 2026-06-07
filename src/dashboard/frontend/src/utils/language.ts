/**
 * Detect programming language from a filename, for the Monaco editor.
 * Kept tiny: covers the common set, falls back to plaintext.
 */

const EXT_MAP: Record<string, string> = {
  ts: "typescript",
  tsx: "typescript",
  js: "javascript",
  jsx: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  py: "python",
  rs: "rust",
  go: "go",
  rb: "ruby",
  java: "java",
  kt: "kotlin",
  swift: "swift",
  c: "c",
  h: "c",
  cpp: "cpp",
  cc: "cpp",
  cxx: "cpp",
  hpp: "cpp",
  cs: "csharp",
  php: "php",
  sh: "shell",
  bash: "shell",
  zsh: "shell",
  yml: "yaml",
  yaml: "yaml",
  toml: "ini",
  json: "json",
  json5: "json",
  jsonc: "json",
  md: "markdown",
  mdx: "markdown",
  html: "html",
  htm: "html",
  css: "css",
  scss: "scss",
  sass: "scss",
  less: "less",
  sql: "sql",
  xml: "xml",
  vue: "html",
  svelte: "html",
  lua: "lua",
  r: "r",
  dart: "dart",
  zig: "zig",
  ex: "elixir",
  exs: "elixir",
  erl: "erlang",
  hs: "haskell",
  pl: "perl",
  proto: "protobuf",
  graphql: "graphql",
  gql: "graphql",
};

const NAME_MAP: Record<string, string> = {
  Dockerfile: "dockerfile",
  Makefile: "makefile",
  Rakefile: "ruby",
  Gemfile: "ruby",
  ".bashrc": "shell",
  ".zshrc": "shell",
  ".gitignore": "ini",
  ".env": "shell",
};

export function detectLanguage(path: string): string {
  const base = path.split("/").pop() ?? path;
  if (NAME_MAP[base]) return NAME_MAP[base];
  const idx = base.lastIndexOf(".");
  if (idx < 0) return "plaintext";
  const ext = base.slice(idx + 1).toLowerCase();
  return EXT_MAP[ext] ?? "plaintext";
}
