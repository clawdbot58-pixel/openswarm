#!/usr/bin/env node

/**
 * OpenSwarm - Simple command to start the swarm
 *
 * Usage:
 *   openswarm start    - Start everything (Telegram + CLI)
 *   openswarm stop     - Stop the swarm
 *   openswarm status  - Check status
 *   openswarm help    - Show help
 *
 * Just runs: starts Telegram bot by default
 */

import { spawn } from 'node:child_process';
import { fileURLToPath } from 'url';
import path from 'path';
import fs from 'fs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PACKAGE_ROOT = path.join(__dirname, '..');

// PID file for tracking
const PID_FILE = path.join(PACKAGE_ROOT, '.openswarm.pid');

// Colors
const c = {
  green: '\x1b[32m',
  red: '\x1b[31m',
  cyan: '\x1b[36m',
  yellow: '\x1b[33m',
  dim: '\x1b[2m',
  reset: '\x1b[0m'
};

const color = (c, s) => `${c}${s}${c.reset}`;

// Commands
const commands = {
  start: async () => {
    // Check if already running
    if (fs.existsSync(PID_FILE)) {
      const pid = fs.readFileSync(PID_FILE, 'utf8').trim();
      try {
        process.kill(pid, 0);
        console.log(color(c.red, '❌ Already running (PID: ' + pid + ')'));
        console.log(color(c.dim, '  Run: openswarm stop'));
        return;
      } catch {
        // Process not running, clean up
        fs.unlinkSync(PID_FILE);
      }
    }

    console.log(color(c.cyan, '🤖 OpenSwarm starting...'));

    // Start Telegram bot in background
    const botProcess = spawn('node', ['src/telegram/bot.js'], {
      cwd: PACKAGE_ROOT,
      stdio: 'inherit',
      detached: false
    });

    // Save PID
    fs.writeFileSync(PID_FILE, botProcess.pid.toString());

    botProcess.on('exit', (code) => {
      console.log(color(c.yellow, '⚠️ Bot exited with code: ' + code));
      if (fs.existsSync(PID_FILE)) fs.unlinkSync(PID_FILE);
    });

    console.log(color(c.green, '✅ Started!'));
    console.log(color(c.dim, '  Telegram bot is running'));
    console.log(color(c.dim, '  Say hi to your bot on Telegram!'));
  },

  stop: async () => {
    if (!fs.existsSync(PID_FILE)) {
      console.log(color(c.yellow, '⚠️ Not running'));
      return;
    }

    const pid = parseInt(fs.readFileSync(PID_FILE, 'utf8').trim());

    try {
      process.kill(pid, 'SIGTERM');
      console.log(color(c.green, '✅ Stopped'));
      fs.unlinkSync(PID_FILE);
    } catch (e) {
      console.log(color(c.red, '❌ Could not stop: ' + e.message));
      fs.unlinkSync(PID_FILE);
    }
  },

  status: async () => {
    if (!fs.existsSync(PID_FILE)) {
      console.log(color(c.yellow, '🔴 Not running'));
      return;
    }

    const pid = parseInt(fs.readFileSync(PID_FILE, 'utf8').trim());

    try {
      process.kill(pid, 0);
      console.log(color(c.green, '🟢 Running (PID: ' + pid + ')'));
    } catch {
      console.log(color(c.yellow, '⚠️ PID file exists but process not running'));
      fs.unlinkSync(PID_FILE);
    }
  },

  help: () => {
    console.log(`
${color(c.cyan, 'OpenSwarm')}

${color(c.bold, 'Usage:')}
  openswarm <command>

${color(c.bold, 'Commands:')}
  start    ${color(c.dim, '- Start the swarm (Telegram bot)')}
  stop     ${color(c.dim, '- Stop the swarm')}
  status   ${color(c.dim, '- Check if running')}
  help     ${color(c.dim, '- Show this message')}

${color(c.bold, 'Quick Start:')}
  ${color(c.green, 'openswarm start')}
  → Starts Telegram bot
  → Talk to your bot!

${color(c.bold, 'Examples:')}
  openswarm start     # Start
  openswarm status    # Check running
  openswarm stop      # Stop

${color(c.dim, 'Telegram: Just message the bot naturally!')}
    `.replace('<command>', '').replace('<bold>', '\x1b[1m'));
  }
};

// Parse command
const cmd = process.argv[2] || 'help';
const action = commands[cmd];

if (!action) {
  console.log(color(c.red, 'Unknown command: ' + cmd));
  console.log(color(c.dim, 'Run: openswarm help'));
  process.exit(1);
}

action().catch(e => {
  console.log(color(c.red, 'Error: ' + e.message));
  process.exit(1);
});
