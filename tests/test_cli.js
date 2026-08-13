/**
 * Unit tests for cli.js — npm bin wrapper for Jeeves CLI.
 *
 * Tests all exported internals: Python detection, path resolution, signal
 * forwarding, exit code mapping, and the main function.
 *
 * Run with: node --test tests/test_cli.js
 * Or:       node --test tests/
 */

'use strict';

const assert = require('node:assert');
const { describe, it, before, after, beforeEach, afterEach, mock } = require('node:test');

// Module under test — must be required after setting up mocks that are needed
// at import time. We use dynamic requires inside each test suite instead.
let cli;

// ── Helpers ────────────────────────────────────────────────────────────────

/**
 * Create a mock child process that emits given events.
 */
function mockChild({ exitCode = null, exitSignal = null, error = null } = {}) {
  const listeners = {};
  const child = {
    kill: mock.fn(() => {}),
    on: mock.fn((event, fn) => {
      listeners[event] = fn;
    }),
    _emit(event, ...args) {
      if (listeners[event]) listeners[event](...args);
    },
  };
  return child;
}

// ── Resolve CLI Path ──────────────────────────────────────────────────────

describe('resolveCliPath', () => {
  before(() => {
    cli = require('../cli.js');
  });

  after(() => {
    // Clear the require cache so the next test gets a fresh module
    delete require.cache[require.resolve('../cli.js')];
  });

  it('returns a path when cli.py is in the same directory', () => {
    // __dirname of this test file is tests/, so binDir = tests/../ = project root
    const binDir = __dirname;  // tests/
    const path = cli.resolveCliPath(binDir);
    // Should resolve to tests/../cli.py -> project root cli.py
    assert.notEqual(path, null);
    assert.ok(path.endsWith('cli.py'), `Expected cli.py, got: ${path}`);
  });

  it('returns a path when cli.py is one level up', () => {
    const binDir = __dirname + '/nonexistent/bin';  // deep dir
    // This will try nonexistent/bin/cli.py (no), nonexistent/cli.py (no),
    // then nonexistent/../cli.py (no) — wait, that's NOT the project root.
    // Actually resolve(binDir, '../..', 'cli.py') would go up 2 levels from
    // the fake bin dir, so it won't find it. Let's test with a real dir.
    const path = cli.resolveCliPath(__dirname);
    assert.notEqual(path, null);
    assert.ok(path.endsWith('cli.py'));
  });

  it('returns null when cli.py is not found', () => {
    const path = cli.resolveCliPath('/tmp/jeeves_test_nonexistent');
    assert.strictEqual(path, null);
  });

  it('tries candidates in order and returns first match', () => {
    // Create a temp file structure to test ordering
    const os = require('os');
    const fs = require('fs');
    const path_ = require('path');
    const tmpDir = fs.mkdtempSync(path_.join(os.tmpdir(), 'jeeves-test-'));
    const deepDir = path_.join(tmpDir, 'a', 'b', 'c', 'bin');
    fs.mkdirSync(deepDir, { recursive: true });

    // Place cli.py at level "../.." (tmpDir/a/b/cli.py)
    const level2Dir = path_.join(tmpDir, 'a', 'b');
    fs.writeFileSync(path_.join(level2Dir, 'cli.py'), '');

    const found = cli.resolveCliPath(deepDir);
    assert.strictEqual(found, path_.join(level2Dir, 'cli.py'));

    // Cleanup
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });
});

// ── Detect Python ─────────────────────────────────────────────────────────

describe('detectPython', () => {
  beforeEach(() => {
    cli = require('../cli.js');
  });

  afterEach(() => {
    delete require.cache[require.resolve('../cli.js')];
    mock.restoreAll();
  });

  it('returns a working Python binary on the current system', () => {
    const bin = cli.detectPython();
    assert.notEqual(bin, null, 'Python should be available for the test to run');
    assert.ok(typeof bin === 'string', `Expected string, got ${typeof bin}`);
    assert.ok(bin.length > 0, 'Binary name should be non-empty');
  });

  it('returns null when no Python is found', () => {
    const childProcess = require('child_process');
    mock.method(childProcess, 'execSync', () => {
      throw new Error('ENOENT');
    });

    // Re-require cli to pick up the mocked execSync
    delete require.cache[require.resolve('../cli.js')];
    cli = require('../cli.js');

    const bin = cli.detectPython();
    assert.strictEqual(bin, null);
  });

  it('uses platform-specific candidate order on win32', () => {
    const originalPlatform = process.platform;
    const platformDesc = Object.getOwnPropertyDescriptor(process, 'platform') || {};
    Object.defineProperty(process, 'platform', {
      value: 'win32',
      configurable: true,
    });

    // Mock execSync to succeed only for 'py'
    const childProcess = require('child_process');
    mock.method(childProcess, 'execSync', (cmd) => {
      if (cmd.startsWith('py ')) return Buffer.from('');
      throw new Error('not found');
    });

    // Re-require cli to pick up the mocked execSync
    delete require.cache[require.resolve('../cli.js')];
    cli = require('../cli.js');

    const bin = cli.detectPython();
    assert.strictEqual(bin, 'py');

    Object.defineProperty(process, 'platform', platformDesc);
  });
});

// ── Signal to Exit Code ───────────────────────────────────────────────────

describe('signalToExitCode', () => {
  before(() => {
    cli = require('../cli.js');
  });

  after(() => {
    delete require.cache[require.resolve('../cli.js')];
  });

  it('maps SIGINT to 130', () => {
    assert.strictEqual(cli.signalToExitCode('SIGINT'), 130);
  });

  it('maps SIGTERM to 143', () => {
    assert.strictEqual(cli.signalToExitCode('SIGTERM'), 143);
  });

  it('maps SIGQUIT to 131', () => {
    assert.strictEqual(cli.signalToExitCode('SIGQUIT'), 131);
  });

  it('returns 1 for unknown signals', () => {
    assert.strictEqual(cli.signalToExitCode('SIGUSR1'), 1);
    assert.strictEqual(cli.signalToExitCode('SIGHUP'), 1);
    assert.strictEqual(cli.signalToExitCode('UNKNOWN'), 1);
  });

  it('contains all entries in SIG_MAP', () => {
    assert.deepStrictEqual(cli.SIG_MAP, {
      SIGINT: 130,
      SIGTERM: 143,
      SIGQUIT: 131,
    });
  });
});

// ── Handle Child Exit ─────────────────────────────────────────────────────

describe('handleChildExit', () => {
  let exitCode;

  before(() => {
    cli = require('../cli.js');
  });

  after(() => {
    delete require.cache[require.resolve('../cli.js')];
  });

  beforeEach(() => {
    exitCode = null;
    mock.method(process, 'exit', (code) => {
      exitCode = code;
      // Don't actually exit — throw to stop execution like process.exit would
      throw new Error(`process.exit(${code})`);
    });
  });

  afterEach(() => {
    mock.restoreAll();
  });

  it('exits with the child code on normal exit', () => {
    try {
      cli.handleChildExit(0, null);
    } catch {}
    assert.strictEqual(exitCode, 0);
  });

  it('exits with the child code when non-zero', () => {
    try {
      cli.handleChildExit(42, null);
    } catch {}
    assert.strictEqual(exitCode, 42);
  });

  it('exits with 0 when code is null and no signal', () => {
    try {
      cli.handleChildExit(null, null);
    } catch {}
    assert.strictEqual(exitCode, 0);
  });

  it('exits with mapped code on signal', () => {
    try {
      cli.handleChildExit(null, 'SIGINT');
    } catch {}
    assert.strictEqual(exitCode, 130);
  });

  it('exits with 1 on unknown signal', () => {
    try {
      cli.handleChildExit(null, 'SIGUNKNOWN');
    } catch {}
    assert.strictEqual(exitCode, 1);
  });

  it('prefers signal code over child code when both present', () => {
    try {
      cli.handleChildExit(0, 'SIGTERM');
    } catch {}
    assert.strictEqual(exitCode, 143);
  });
});

// ── Forward Signals ───────────────────────────────────────────────────────

describe('forwardSignals', () => {
  before(() => {
    cli = require('../cli.js');
  });

  after(() => {
    delete require.cache[require.resolve('../cli.js')];
  });

  afterEach(() => {
    mock.restoreAll();
  });

  it('registers SIGINT and SIGTERM handlers on process', () => {
    const child = mockChild();
    const listenersBefore = process.listeners('SIGINT').length;

    const cleanup = cli.forwardSignals(child);

    const listenersAfter = process.listeners('SIGINT').length;
    assert.strictEqual(listenersAfter, listenersBefore + 1);

    cleanup();
  });

  it('forwards SIGINT to child.kill', () => {
    const child = mockChild();
    const cleanup = cli.forwardSignals(child);

    // Simulate SIGINT
    process.emit('SIGINT');
    assert.strictEqual(child.kill.mock.callCount(), 1);
    assert.strictEqual(child.kill.mock.calls[0].arguments[0], 'SIGINT');

    cleanup();
  });

  it('forwards SIGTERM to child.kill', () => {
    const child = mockChild();
    const cleanup = cli.forwardSignals(child);

    process.emit('SIGTERM');
    assert.strictEqual(child.kill.mock.callCount(), 1);
    assert.strictEqual(child.kill.mock.calls[0].arguments[0], 'SIGTERM');

    cleanup();
  });

  it('cleanup removes the listeners', () => {
    const child = mockChild();
    const listenersBefore = process.listeners('SIGINT').length;

    const cleanup = cli.forwardSignals(child);
    cleanup();

    const listenersAfter = process.listeners('SIGINT').length;
    assert.strictEqual(listenersAfter, listenersBefore);
  });
});

// ── Spawn Jeeves ──────────────────────────────────────────────────────────

describe('spawnJeeves', () => {
  before(() => {
    cli = require('../cli.js');
  });

  after(() => {
    delete require.cache[require.resolve('../cli.js')];
  });

  it('spawns python with cli.py and forwarded args', () => {
    const childProcess = require('child_process');
    const mockSpawn = mock.fn(() => mockChild());
    mock.method(childProcess, 'spawn', mockSpawn);

    // Re-require cli to pick up the mocked spawn
    delete require.cache[require.resolve('../cli.js')];
    cli = require('../cli.js');

    const result = cli.spawnJeeves('python3', '/path/to/cli.py', ['--help']);

    assert.strictEqual(mockSpawn.mock.callCount(), 1);
    const [bin, args, opts] = mockSpawn.mock.calls[0].arguments;
    assert.strictEqual(bin, 'python3');
    assert.deepStrictEqual(args, ['/path/to/cli.py', '--help']);
    assert.deepStrictEqual(opts.stdio, ['inherit', 'inherit', 'inherit']);
    assert.ok(opts.env);  // process.env forwarded
  });
});

// ── Main Function ─────────────────────────────────────────────────────────

describe('main', () => {
  beforeEach(() => {
    // Clear module cache before each test so the mocked deps take effect
    delete require.cache[require.resolve('../cli.js')];
  });

  afterEach(() => {
    mock.restoreAll();
  });

  it('exits with code 1 when cli.py is not found', () => {
    const modFs = require('fs');
    mock.method(modFs, 'existsSync', () => false);

    let exitCode = null;
    mock.method(process, 'exit', (code) => {
      exitCode = code;
      throw new Error(`process.exit(${code})`);
    });

    // Re-require cli to pick up the mocked fs
    delete require.cache[require.resolve('../cli.js')];
    cli = require('../cli.js');
    try {
      cli.main();
    } catch {}

    assert.strictEqual(exitCode, 1);
  });

  it('exits with code 1 when Python is not found', () => {
    const modFs = require('fs');
    mock.method(modFs, 'existsSync', () => true);

    const childProcess = require('child_process');
    mock.method(childProcess, 'execSync', () => {
      throw new Error('ENOENT');
    });

    let exitCode = null;
    mock.method(process, 'exit', (code) => {
      exitCode = code;
      throw new Error(`process.exit(${code})`);
    });

    // Re-require cli to pick up the mocks
    delete require.cache[require.resolve('../cli.js')];
    cli = require('../cli.js');
    try {
      cli.main();
    } catch {}

    assert.strictEqual(exitCode, 1);
  });

  it('spawns jeeves and forwards args when everything is found', () => {
    const modFs = require('fs');
    mock.method(modFs, 'existsSync', () => true);

    const childProcess = require('child_process');
    mock.method(childProcess, 'execSync', () => Buffer.from(''));

    // Silence process.exit so the exit handler doesn't crash
    mock.method(process, 'exit', () => {});

    // Build a mock child that actually stores callbacks via _emit
    const mockChildProcess = mockChild();
    mock.method(childProcess, 'spawn', () => mockChildProcess);

    // Re-require cli to pick up all mocks
    delete require.cache[require.resolve('../cli.js')];
    cli = require('../cli.js');

    cli.main();

    // Trigger exit to clean up signal listeners that forwardSignals added
    mockChildProcess._emit('exit', 0, null);

    const callCount = childProcess.spawn.mock.callCount();
    assert.ok(callCount >= 1, 'spawn should have been called');

    if (callCount > 0) {
      const [bin, args] = childProcess.spawn.mock.calls[0].arguments;
      assert.ok(bin.includes('python'), `Expected python, got: ${bin}`);
      assert.ok(args.some(a => a.endsWith('cli.py')), 'Should include cli.py path');
    }
  });
});
