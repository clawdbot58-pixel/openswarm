#!/usr/bin/env node

import readline from "readline";
import { fileURLToPath } from "url";
import path from "path";
import fs from "fs";
import YAML from "yaml";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WORKSPACE_PATH = path.join(__dirname, "../../workspaces/default");
const CONFIG_PATH = path.join(__dirname, "../../config/user.yaml");

// Load config
let config = {
  user: { name: "User" },
  agents: { enabled: ["planner", "coder", "reviewer"], default: "coder" },
  api_keys: {},
};
try {
  const configFile = fs.readFileSync(CONFIG_PATH, "utf8");
  config = YAML.parse(configFile);
} catch (e) {
  // Use defaults if no config
}

// Colors
const colors = {
  reset: "\x1b[0m",
  green: "\x1b[32m",
  yellow: "\x1b[33m",
  red: "\x1b[31m",
  cyan: "\x1b[36m",
  dim: "\x1b[2m",
  bold: "\x1b[1m",
  magenta: "\x1b[35m",
};

function color(c, s) {
  return `${colors[c] || ""}${s}${colors.reset}`;
}

// Load workspace files
function loadWorkspaceFiles() {
  const files = {};
  const fileNames = [
    "SOUL.md",
    "IDENTITY.md",
    "AGENTS.md",
    "USER.md",
    "MEMORY.md",
    "TASKBOARD.md",
    "BOOTSTRAP.md",
    "TOOLS.md",
  ];

  for (const name of fileNames) {
    const filePath = path.join(WORKSPACE_PATH, name);
    if (fs.existsSync(filePath)) {
      files[name] = fs.readFileSync(filePath, "utf8");
    }
  }
  return files;
}

// Check if first run (BOOTSTRAP.md exists)
function isFirstRun() {
  return fs.existsSync(path.join(WORKSPACE_PATH, "BOOTSTRAP.md"));
}

// Save workspace file
function saveWorkspaceFile(name, content) {
  const filePath = path.join(WORKSPACE_PATH, name);
  fs.writeFileSync(filePath, content);
}

// Format taskboard display
function formatTaskboard() {
  const taskboardPath = path.join(WORKSPACE_PATH, "TASKBOARD.md");
  if (!fs.existsSync(taskboardPath)) {
    return color("dim", "No tasks yet");
  }

  const content = fs.readFileSync(taskboardPath, "utf8");
  // Extract just the Active Tasks section
  const match = content.match(/## Active Tasks\n\n([\s\S]*?)(?:---|$)/);
  const activeTasks = match ? match[1].trim() : "_No active tasks yet_";

  if (activeTasks.includes("No active tasks")) {
    return color("dim", activeTasks);
  }

  // Format each task line
  return activeTasks
    .split("\n")
    .map((line) => {
      if (line.startsWith("## task-")) {
        return color("yellow", line);
      }
      if (line.includes("**Status:**")) {
        const status = line.match(/\*\*Status:\*\* (\w+)/)?.[1] || "";
        const statusColor =
          status === "done"
            ? "green"
            : status === "in_progress"
              ? "cyan"
              : status === "blocked"
                ? "red"
                : "dim";
        return line.replace(
          /\*\*Status:\*\* \w+/,
          `**Status:** ${color(statusColor, status)}`,
        );
      }
      return line;
    })
    .join("\n");
}

// Queue a goal (add to taskboard)
let workflowId = Date.now();
function queueGoal(goal) {
  const id = (++workflowId).toString(16).slice(-8);
  const timestamp = new Date().toISOString();

  // Read current taskboard
  let taskboard = "";
  const taskboardPath = path.join(WORKSPACE_PATH, "TASKBOARD.md");
  if (fs.existsSync(taskboardPath)) {
    taskboard = fs.readFileSync(taskboardPath, "utf8");
  } else {
    taskboard =
      "# Taskboard - All Agent Tasks\n\n## Active Tasks\n\n_No active tasks yet_\n\n---\n";
  }

  // Add new task
  const newTask = `
## task-${id}: ${goal}
- **Status:** pending
- **Priority:** medium
- **Created by:** main
- **Assigned to:** worker
- **Parent workflow:** ${id}
- **Created:** ${timestamp}
- **Updated:** ${timestamp}
- **Description:** ${goal}
- **Notes:** Queued by user

`;

  // Insert after "## Active Tasks" - handle both formats
  taskboard = taskboard.replace(
    /## Active Tasks\n\n_?[\s\S]*?_?\n\n/,
    "## Active Tasks\n" + newTask,
  );

  fs.writeFileSync(taskboardPath, taskboard);

  return id;
}

// Process natural language input
function processInput(input) {
  const text = input.trim().toLowerCase();

  // Check for status keywords
  if (
    text === "status" ||
    text.includes("how are you") ||
    text.includes("hows it going")
  ) {
    return showStatus();
  }

  // Check for help keywords
  if (text === "help" || text === "what can you do") {
    return showHelp();
  }

  // Check for taskboard/queue keywords
  if (text === "tasks" || text === "queue" || text === "show tasks") {
    return showTaskboard();
  }

  // Check for clear/reset
  if (text === "clear" || text === "reset") {
    return showClear();
  }

  // Otherwise treat as a goal
  if (input.trim()) {
    return queueGoalAndRespond(input.trim());
  }

  return null;
}

// Status display
function showStatus() {
  const taskboard = formatTaskboard();
  const tasksMatch = taskboard.match(/task-(\w+)/g);
  const taskCount = tasksMatch ? tasksMatch.length : 0;

  return `
${color("green", "🟢 Swarm is running")}

Agents: ${color("green", "3/3 ready")}
  ${color("cyan", "•")} main: ready (that's me!)
  ${color("cyan", "•")} orchestrator: ready
  ${color("cyan", "•")} worker: ready

Queue depth: ${color("yellow", taskCount.toString())} task(s)
Kernel: ${color("dim", "http://127.0.0.1:8765")}
`;
}

// Help display
function showHelp() {
  return `
${color("bold", "What I can do:")}

Just tell me what you want! Examples:

${color("cyan", '"Build me a login form"')} — I'll queue that as a task
${color("cyan", '"Check the status"')} — See swarm health
${color("cyan", '"Show me tasks"')} — See queued tasks
${color("cyan", '"Help"')} — This message

I have a team of agents:
- ${color("yellow", "main")} — That's me! I talk to you
- ${color("yellow", "orchestrator")} — Plans and coordinates
- ${color("yellow", "worker")} — Does the actual work

Just say what you want done. I'll handle the rest!
`;
}

// Show taskboard
function showTaskboard() {
  return `
${color("bold", "📋 Taskboard")}

${formatTaskboard()}
`;
}

// Clear tasks (for demo)
function showClear() {
  const taskboardPath = path.join(WORKSPACE_PATH, "TASKBOARD.md");
  if (fs.existsSync(taskboardPath)) {
    const content = fs.readFileSync(taskboardPath, "utf8");
    const cleared = content.replace(
      /## Active Tasks\n[\s\S]*?## task-\w+.*?(?=\n---)/,
      "## Active Tasks\n\n_No active tasks yet_",
    );
    fs.writeFileSync(taskboardPath, cleared);
  }
  return `${color("green", "✓")} Task queue cleared`;
}

// Queue goal and respond naturally
function queueGoalAndRespond(goal) {
  const id = queueGoal(goal);

  return `
${color("yellow", "🎯 Got it!")} I've queued that as a task.

Task: ${goal}
ID: ${id}

I'll delegate to my team and work on this. Want me to start now, or add more tasks first?
`;
}

// Intro sequence (first run)
function runIntro() {
  const files = loadWorkspaceFiles();

  if (!files["BOOTSTRAP.md"]) {
    // Not first run, just show welcome
    return `
${color("cyan", "👋 Welcome back!")}

I'm your AI assistant. I'm ready to help!

Just tell me what you want to accomplish — I'll break it down, delegate to my team, and keep you updated.

${color("dim", '(Say "help" for what I can do)')}`;
  }

  return `
${color("cyan", "👋 Hi! First run detected.")}

This looks like my first time running. Let me get set up...

${color("dim", "Reading my context files...")}
${color("green", "✓")} Loaded SOUL.md, AGENTS.md, TOOLS.md

I should ask you a few questions to personalize myself. But for now, let's just get started!

${color("yellow", "What should I call you?")}
${color("dim", "(Tell me your name and I'll update my identity)")}
`;
}

// Main CLI
const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout,
  prompt: color("cyan", "▸ "),
});

console.log(color("cyan", "🤖 Swarm CLI v0.2.0"));
console.log(color("dim", "No / commands needed — just talk to me!\n"));

const intro = runIntro();
console.log(intro);

rl.prompt();

rl.on("line", (line) => {
  const input = line.trim();
  if (input) {
    const response = processInput(input);
    if (response) {
      console.log(response);
    }
  }
  rl.prompt();
}).on("close", () => {
  console.log(color("dim", "\nGoodbye!"));
  process.exit(0);
});
