#!/usr/bin/env node

// Telegram Bot - Same natural language interface as CLI
import { Bot } from 'grammy';
import { fileURLToPath } from 'url';
import path from 'path';
import fs from 'fs';
import YAML from 'yaml';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WORKSPACE_PATH = path.join(__dirname, '../../workspaces/default');
const CONFIG_PATH = path.join(__dirname, '../../config/user.yaml');

// Load config
let config = { user: { name: 'User' }, api_keys: {} };
try {
  const configFile = fs.readFileSync(CONFIG_PATH, 'utf8');
  config = YAML.parse(configFile);
} catch (e) {
  // Use defaults
}

// Get Telegram token from config or env
const TELEGRAM_TOKEN = process.env.TELEGRAM_TOKEN || config.api_keys?.telegram;

if (!TELEGRAM_TOKEN) {
  console.error('❌ Telegram token not found!');
  console.error('Set TELEGRAM_TOKEN env var or add api_keys.telegram to config/user.yaml');
  process.exit(1);
}

// Colors for console output
const colors = {
  reset: '\x1b[0m',
  green: '\x1b[32m',
  yellow: '\x1b[33m',
  red: '\x1b[31m',
  cyan: '\x1b[36m',
  dim: '\x1b[2m',
  bold: '\x1b[1m'
};

function color(c, s) { return `${colors[c] || ''}${s}${colors.reset}`; }

// ============ SAME LOGIC AS CLI ============

function formatTaskboard() {
  const taskboardPath = path.join(WORKSPACE_PATH, 'TASKBOARD.md');
  if (!fs.existsSync(taskboardPath)) {
    return 'No tasks yet';
  }

  const content = fs.readFileSync(taskboardPath, 'utf8');
  const match = content.match(/## Active Tasks\n\n([\s\S]*?)(?:---|$)/);
  const activeTasks = match ? match[1].trim() : '_No active tasks yet_';

  if (activeTasks.includes('No active tasks')) {
    return activeTasks;
  }

  // Format tasks for Telegram (plain text)
  return activeTasks.split('\n').map(line => {
    if (line.startsWith('## ')) {
      return `📋 ${line.replace('## task-', '')}`;
    }
    if (line.includes('**Status:**')) {
      return line.replace(/\*\*/g, '');
    }
    return line;
  }).join('\n');
}

let workflowId = Date.now();

function queueGoal(goal) {
  const id = (++workflowId).toString(16).slice(-8);
  const timestamp = new Date().toISOString();

  let taskboard = '';
  const taskboardPath = path.join(WORKSPACE_PATH, 'TASKBOARD.md');
  if (fs.existsSync(taskboardPath)) {
    taskboard = fs.readFileSync(taskboardPath, 'utf8');
  } else {
    taskboard = '# Taskboard - All Agent Tasks\n\n## Active Tasks\n\n_No active tasks yet_\n\n---\n';
  }

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
- **Notes:** Queued via Telegram

`;

  taskboard = taskboard.replace(
    /## Active Tasks\n\n_?[\s\S]*?_?\n\n/,
    "## Active Tasks\n" + newTask
  );

  fs.writeFileSync(taskboardPath, taskboard);
  return id;
}

function processInput(input) {
  const text = input.trim().toLowerCase();

  // Keywords that trigger special responses
  if (text === 'status' || text.includes('how are') || text.includes('hows it')) {
    return showStatus();
  }

  if (text === 'help' || text === 'what can you do') {
    return showHelp();
  }

  if (text === 'tasks' || text === 'queue' || text === 'show tasks') {
    return showTaskboard();
  }

  // Everything else is a goal
  if (input.trim()) {
    const id = queueGoal(input.trim());
    return `🎯 Got it! Queued as task.

Task: ${input.trim()}
ID: ${id}

I'll delegate to my team and work on this. Want me to start now, or add more tasks first?`;
  }

  return null;
}

function showStatus() {
  const taskboard = formatTaskboard();
  const tasksMatch = taskboard.match(/task-(\w+)/g);
  const taskCount = tasksMatch ? tasksMatch.length : 0;

  return `🟢 Swarm is running

Agents: 3/3 ready
  • main: ready
  • orchestrator: ready
  • worker: ready

Queue depth: ${taskCount} task(s)
Kernel: http://127.0.0.1:8765`;
}

function showHelp() {
  return `What I can do:

Just tell me what you want! Examples:

• "Build me a login form" — queue task
• "Check the status" — see swarm health
• "Show me tasks" — see queue

I have a team:
• main — talks to you
• orchestrator — plans
• worker — does the work

Just say what you want done!`;
}

function showTaskboard() {
  return `📋 Taskboard\n\n${formatTaskboard()}`;
}

// ============ TELEGRAM BOT ============

const bot = new Bot(TELEGRAM_TOKEN);

// Handle messages
bot.on('message', async (ctx) => {
  const text = ctx.message.text;
  const response = processInput(text);

  if (response) {
    await ctx.reply(response);
  }
});

// Handle commands (optional - for power users)
bot.command('start', async (ctx) => {
  await ctx.reply(`👋 Hi! I'm your AI assistant.

Just tell me what you want — no commands needed.

Try: "Build me something" or "Check status"`);
});

bot.command('status', async (ctx) => {
  await ctx.reply(showStatus());
});

bot.command('help', async (ctx) => {
  await ctx.reply(showHelp());
});

bot.command('tasks', async (ctx) => {
  await ctx.reply(showTaskboard());
});

// Error handling
bot.catch((err) => {
  console.error(color('red', 'Bot error:'), err);
});

// Start bot
console.log(color('cyan', '🤖 Telegram Bot starting...'));
console.log(color('dim', 'Press Ctrl+C to stop'));

// Run bot
bot.start();
