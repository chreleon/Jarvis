#!/usr/bin/env node

/**
 * cli.js — npm bin wrapper for Jeeves CLI.
 *
 * Installed via `npm install -g .` or `npm install -g @chreleon/jeeves`.
 * Run with: `jeeves [args]`
 *
 * Detects Python (python3 or python), resolves cli.py relative to itself,
 * spawns it as a child process, and forwards all I/O.
 *
 * Exports internal functions for unit testing.
 */

'use strict';

/** @module cli
 *
 * Uses module-level require() objects (cp, fs, path) instead of destructured
 * references so that unit tests can mock individual methods via mock.method()
 * on the module properties.
 */

const cp = require('child_process');
const fs = require('fs');
const path = require('path');

// ── Internal: Python script resolution ─────────────────────────────────────

/**
 * Try to find cli.py relative to the given bin directory.
 * Returns the first matching path, or null.
 *
 * @param {string} binDir - Directory containing this wrapper script.
 * @returns {string|null}
 */
function resolveCliPath(binDir) {
  const candidates = [
    path.resolve(binDir, 'cli.py'),
    path.resolve(binDir, '..', 'cli.py'),
    path.resolve(binDir, '..', '..', 'cli.py'),
  ];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }
  return null;
}

// ── Internal: Python detection ─────────────────────────────────────────────

/**
 * Find a working Python binary on the current PATH.
 * Returns the binary name, or null if none found.
 *
 * @returns {string|null}
 */
function detectPython() {
  const candidates = process.platform === 'win32'
    ? ['python', 'python3', 'py']
    : ['python3', 'python'];

  for (const bin of candidates) {
    try {
      cp.execSync(`${bin} --version`, { stdio: 'ignore' });
      return bin;
    } catch {
      continue;
    }
  }
  return null;
}

// ── Internal: Signal forwarding ────────────────────────────────────────────

/**
 * Forward process signals to the child process.
 *
 * @param {import('child_process').ChildProcess} child
 * @returns {() => void} Cleanup function to remove listeners.
 */
function forwardSignals(child) {
  const onSigint = () => { child.kill('SIGINT'); };
  const onSigterm = () => { child.kill('SIGTERM'); };

  process.on('SIGINT', onSigint);
  process.on('SIGTERM', onSigterm);

  // Return cleanup so tests can remove listeners
  return () => {
    process.off('SIGINT', onSigint);
    process.off('SIGTERM', onSigterm);
  };
}

// ── Internal: Exit code mapping ────────────────────────────────────────────

/**
 * Signal-to-exit-code mapping per POSIX conventions.
 */
const SIG_MAP = { SIGINT: 130, SIGTERM: 143, SIGQUIT: 131 };

/**
 * Map a signal name to a POSIX exit code.
 *
 * @param {string} signal
 * @returns {number}
 */
function signalToExitCode(signal) {
  return SIG_MAP[signal] || 1;
}

/**
 * Handle child process exit: forward the exit code or mapped signal code.
 *
 * @param {number|null} code
 * @param {string|null} signal
 */
function handleChildExit(code, signal) {
  if (signal) {
    process.exit(signalToExitCode(signal));
  }
  process.exit(code !== null ? code : 0);
}

// ── Internal: Spawn helper ─────────────────────────────────────────────────

/**
 * Spawn Python with cli.py and forward all arguments.
 *
 * @param {string} pythonBin
 * @param {string} cliPath
 * @param {string[]} args
 * @returns {import('child_process').ChildProcess}
 */
function spawnJeeves(pythonBin, cliPath, args) {
  return cp.spawn(pythonBin, [cliPath, ...args], {
    stdio: ['inherit', 'inherit', 'inherit'],
    env: { ...process.env },
  });
}

// ── Main ───────────────────────────────────────────────────────────────────

function main() {
  const cliPath = resolveCliPath(__dirname);
  if (!cliPath) {
    console.error(
      'jeeves: Could not find cli.py.\n' +
      '  Expected it next to cli.js or in a parent directory.\n' +
      '  Reinstall the package: npm install -g @chreleon/jeeves'
    );
    process.exit(1);
  }

  const pythonBin = detectPython();
  if (!pythonBin) {
    console.error(
      'jeeves: Python is required but not found on your PATH.\n' +
      '  Install Python 3.11 or 3.12 from https://python.org\n' +
      '  Then reinstall: pip install -r requirements.txt'
    );
    process.exit(1);
  }

  const args = process.argv.slice(2);
  const child = spawnJeeves(pythonBin, cliPath, args);

  const cleanup = forwardSignals(child);

  child.on('exit', (code, signal) => {
    cleanup();
    handleChildExit(code, signal);
  });

  child.on('error', (err) => {
    cleanup();
    console.error(`jeeves: Failed to start Python: ${err.message}`);
    process.exit(1);
  });
}

// Run when executed directly (not when required as a module for testing)
if (require.main === module) {
  main();
}

// ── Exports for unit testing ────────────────────────────────────────────────

module.exports = {
  resolveCliPath,
  detectPython,
  forwardSignals,
  signalToExitCode,
  handleChildExit,
  spawnJeeves,
  main,
  SIG_MAP,
};
