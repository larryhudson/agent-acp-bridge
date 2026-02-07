#!/usr/bin/env node

/**
 * LSP Bridge Daemon for Claude Code Hooks
 *
 * Spawns multiple language servers (TypeScript, Ruff for Python), communicates
 * via LSP JSON-RPC, and exposes an HTTP API over a Unix socket so that
 * PostToolUse hooks can request diagnostics for individual files.
 *
 * Usage:
 *   node lsp-bridge.mjs
 *
 * API:
 *   POST /diagnostics  { "file": "/absolute/path.ts" }  → diagnostics JSON
 *   GET  /health                                         → { "status": "ok", servers: [...] }
 *   POST /shutdown                                       → graceful shutdown
 */

import { spawn, execSync } from "child_process";
import { createServer } from "http";
import {
  readFileSync,
  writeFileSync,
  unlinkSync,
  existsSync,
  mkdirSync,
  appendFileSync,
} from "fs";
import { join, resolve, extname } from "path";
import { createHash } from "crypto";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const PROJECT_DIR =
  process.env.CLAUDE_PROJECT_DIR || process.argv[2] || process.cwd();

// Derive a stable socket path from the project dir (keeps path short for Unix socket limit)
const dirHash = createHash("md5")
  .update(PROJECT_DIR)
  .digest("hex")
  .slice(0, 12);
const SOCKET_PATH =
  process.env.LSP_BRIDGE_SOCKET || `/tmp/claude-lsp-${dirHash}.sock`;
const STATE_DIR = join(PROJECT_DIR, ".claude", "hooks");
const PID_FILE = join(STATE_DIR, "lsp-bridge.pid");
const SOCKET_FILE = join(STATE_DIR, "lsp-bridge.socket");
const LOG_FILE = join(STATE_DIR, "lsp-bridge.log");

// ---------------------------------------------------------------------------
// Logging
// ---------------------------------------------------------------------------

function log(level, msg) {
  const ts = new Date().toISOString();
  const line = `[${ts}] [${level}] ${msg}\n`;
  try {
    appendFileSync(LOG_FILE, line);
  } catch {
    // ignore write errors
  }
  if (level === "ERROR") {
    process.stderr.write(line);
  }
}

// ---------------------------------------------------------------------------
// YAML Config Loading
// ---------------------------------------------------------------------------

const CONFIG_PATH = join(STATE_DIR, "lsp-servers.yaml");

function loadConfig() {
  if (!existsSync(CONFIG_PATH)) {
    log("ERROR", `Config file not found: ${CONFIG_PATH}`);
    process.exit(1);
  }

  // Use the project venv Python (has PyYAML) or fall back to system Python
  const venvPython = join(PROJECT_DIR, ".venv", "bin", "python3");
  const pythonBin = existsSync(venvPython) ? venvPython : "python3";

  try {
    const json = execSync(
      `${pythonBin} -c "import yaml, json, sys; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))" "${CONFIG_PATH}"`,
      { encoding: "utf-8", timeout: 5000 },
    );
    return JSON.parse(json);
  } catch (err) {
    log("ERROR", `Failed to load config (is PyYAML installed?): ${err.message}`);
    process.exit(1);
  }
}

/**
 * Resolve a server command by checking search paths, then $PATH.
 */
function resolveCommand(serverConfig) {
  for (const searchPath of serverConfig.search || []) {
    const absPath = join(PROJECT_DIR, searchPath);
    if (existsSync(absPath)) return absPath;
  }
  // Fall back to $PATH
  try {
    return execSync(`which ${serverConfig.command}`, { encoding: "utf-8" }).trim();
  } catch {
    return null;
  }
}

/**
 * Check that all required files exist.
 */
function checkRequirements(serverConfig) {
  for (const req of serverConfig.requires || []) {
    if (!existsSync(join(PROJECT_DIR, req))) return false;
  }
  return true;
}

/**
 * Interpolate ${PROJECT_DIR} and ${PATH} in env values.
 */
function interpolateEnv(env) {
  const result = {};
  for (const [key, value] of Object.entries(env || {})) {
    result[key] = value
      .replace(/\$\{PROJECT_DIR\}/g, PROJECT_DIR)
      .replace(/\$\{PATH\}/g, process.env.PATH || "");
  }
  return result;
}

/**
 * Build extension → { id, server } map from config.
 */
function buildExtensionMap(config) {
  const map = {};
  for (const [serverName, server] of Object.entries(config.servers || {})) {
    for (const [ext, langId] of Object.entries(server.extensions || {})) {
      map[ext] = { id: langId, server: serverName };
    }
  }
  return map;
}

// Module-level extension map, populated in main()
let extToLanguage = {};

// ---------------------------------------------------------------------------
// LSP JSON-RPC Client — generic, works with any LSP server
// ---------------------------------------------------------------------------

class LSPClient {
  /**
   * @param {string} name - Human-readable name for logging (e.g. "typescript", "ruff")
   * @param {object} spawnOpts - { command, args, cwd, env }
   * @param {object} initOpts - { rootUri, rootPath, workspaceFolders }
   */
  constructor(name, spawnOpts, initOpts) {
    this.name = name;
    this._spawnOpts = spawnOpts;
    this._initOpts = initOpts;
    this._nextId = 1;
    this._pending = new Map(); // id → { resolve, reject, timer }
    this._diagnostics = new Map(); // uri → { diagnostics, waiters[] }
    this._buffer = Buffer.alloc(0);
    this._contentLength = -1;
    this._initialized = false;
    this._openDocuments = new Map(); // uri → version
    this._serverProcess = null;
    this._alive = false;
  }

  /**
   * Spawn the language server and wire up communication.
   */
  start() {
    const { command, args, cwd, env } = this._spawnOpts;

    this._serverProcess = spawn(command, args, {
      cwd,
      env: { ...process.env, ...env },
      stdio: ["pipe", "pipe", "pipe"],
    });

    this._serverProcess.stdout.on("data", (chunk) => this._onData(chunk));
    this._serverProcess.stderr.on("data", (chunk) => {
      log(`${this.name}-STDERR`, chunk.toString().trim());
    });
    this._serverProcess.on("exit", (code) => {
      log("INFO", `[${this.name}] LSP server exited with code ${code}`);
      this._alive = false;
      // Reject all pending requests
      for (const [, entry] of this._pending) {
        entry.reject(new Error(`[${this.name}] LSP server exited (code ${code})`));
        if (entry.timer) clearTimeout(entry.timer);
      }
      this._pending.clear();
    });

    this._alive = true;
    log("INFO", `[${this.name}] LSP server spawned (PID ${this._serverProcess.pid})`);
  }

  /**
   * Send the initialize handshake.
   */
  async initialize() {
    const { rootUri, rootPath, workspaceFolders } = this._initOpts;

    const result = await this.request("initialize", {
      processId: process.pid,
      rootUri,
      rootPath,
      capabilities: {
        textDocument: {
          publishDiagnostics: {
            relatedInformation: true,
          },
          synchronization: {
            didOpen: true,
            didChange: true,
            didClose: true,
          },
        },
        workspace: {
          workspaceFolders: true,
        },
      },
      workspaceFolders,
    });

    this.notify("initialized", {});
    this._initialized = true;
    log("INFO", `[${this.name}] LSP initialized successfully`);
    return result;
  }

  get isAlive() {
    return this._alive && this._initialized;
  }

  /**
   * Get diagnostics for a file. Closes the document if already open, then
   * reopens it to reliably trigger publishDiagnostics from all LSP servers.
   */
  async getDiagnostics(filePath, languageId, timeoutMs = 15000) {
    const absPath = resolve(filePath);
    const uri = `file://${absPath}`;
    const text = readFileSync(absPath, "utf-8");

    // Close if already open — some LSP servers (e.g. typescript-language-server)
    // don't reliably re-publish diagnostics on didChange
    if (this._openDocuments.has(uri)) {
      this.notify("textDocument/didClose", {
        textDocument: { uri },
      });
      this._openDocuments.delete(uri);

      // Wait for the empty diagnostics that the server publishes on close.
      // We set up a waiter and let it resolve (empty or not), then discard it.
      await this._waitForDiagnostics(uri, 2000);
      this._diagnostics.delete(uri);
    }

    // Prepare a waiter for diagnostics on this URI
    const diagnosticPromise = this._waitForDiagnostics(uri, timeoutMs);

    // Open the document fresh
    const version = 1;
    this._openDocuments.set(uri, version);
    this.notify("textDocument/didOpen", {
      textDocument: { uri, languageId, version, text },
    });

    return diagnosticPromise;
  }

  /**
   * Close a document so the LSP can free resources.
   */
  closeDocument(filePath) {
    const uri = `file://${resolve(filePath)}`;
    if (this._openDocuments.has(uri)) {
      this.notify("textDocument/didClose", {
        textDocument: { uri },
      });
      this._openDocuments.delete(uri);
    }
  }

  /**
   * Graceful shutdown per LSP spec.
   */
  async shutdown() {
    if (!this._alive) return;
    try {
      await this.request("shutdown", null, 5000);
      this.notify("exit", null);
    } catch {
      // Force kill if shutdown fails
      if (this._serverProcess) {
        this._serverProcess.kill("SIGKILL");
      }
    }
  }

  // ---- JSON-RPC transport -------------------------------------------------

  request(method, params, timeoutMs = 30000) {
    return new Promise((resolve, reject) => {
      const id = this._nextId++;
      const timer = setTimeout(() => {
        this._pending.delete(id);
        reject(new Error(`[${this.name}] LSP request '${method}' timed out after ${timeoutMs}ms`));
      }, timeoutMs);

      this._pending.set(id, { resolve, reject, timer });
      this._send({ jsonrpc: "2.0", id, method, params });
    });
  }

  notify(method, params) {
    this._send({ jsonrpc: "2.0", method, params });
  }

  _send(msg) {
    const body = JSON.stringify(msg);
    const header = `Content-Length: ${Buffer.byteLength(body)}\r\n\r\n`;
    try {
      this._serverProcess.stdin.write(header + body);
    } catch (err) {
      log("ERROR", `[${this.name}] Failed to write to LSP stdin: ${err.message}`);
    }
  }

  _onData(chunk) {
    this._buffer = Buffer.concat([this._buffer, chunk]);
    this._parseMessages();
  }

  _parseMessages() {
    while (true) {
      if (this._contentLength === -1) {
        // Look for the header boundary
        const headerEnd = this._buffer.indexOf("\r\n\r\n");
        if (headerEnd === -1) return;

        const header = this._buffer.slice(0, headerEnd).toString("ascii");
        const match = header.match(/Content-Length:\s*(\d+)/i);
        if (!match) {
          log("ERROR", `[${this.name}] Invalid LSP header: ${header}`);
          this._buffer = this._buffer.slice(headerEnd + 4);
          continue;
        }
        this._contentLength = parseInt(match[1], 10);
        this._buffer = this._buffer.slice(headerEnd + 4);
      }

      if (this._buffer.length < this._contentLength) return;

      const body = this._buffer.slice(0, this._contentLength).toString("utf-8");
      this._buffer = this._buffer.slice(this._contentLength);
      this._contentLength = -1;

      try {
        const msg = JSON.parse(body);
        this._handleMessage(msg);
      } catch (err) {
        log("ERROR", `[${this.name}] Failed to parse LSP message: ${err.message}`);
      }
    }
  }

  _handleMessage(msg) {
    // Response to a request
    if (msg.id !== undefined && (msg.result !== undefined || msg.error)) {
      const entry = this._pending.get(msg.id);
      if (entry) {
        this._pending.delete(msg.id);
        if (entry.timer) clearTimeout(entry.timer);
        if (msg.error) {
          entry.reject(
            new Error(`[${this.name}] LSP error ${msg.error.code}: ${msg.error.message}`),
          );
        } else {
          entry.resolve(msg.result);
        }
      }
      return;
    }

    // Notification from server
    if (msg.method === "textDocument/publishDiagnostics") {
      const { uri, diagnostics } = msg.params;
      const entry = this._diagnostics.get(uri);
      if (entry) {
        entry.diagnostics = diagnostics;
        // Resolve all waiters
        for (const waiter of entry.waiters) {
          waiter.resolve(diagnostics);
          if (waiter.timer) clearTimeout(waiter.timer);
        }
        entry.waiters = [];
      } else {
        // Cache even if nobody's waiting
        this._diagnostics.set(uri, { diagnostics, waiters: [] });
      }
      return;
    }

    // Log other notifications for debugging
    if (msg.method) {
      log("DEBUG", `[${this.name}] LSP notification: ${msg.method}`);
    }
  }

  _waitForDiagnostics(uri, timeoutMs) {
    return new Promise((resolve) => {
      if (!this._diagnostics.has(uri)) {
        this._diagnostics.set(uri, { diagnostics: null, waiters: [] });
      }

      const entry = this._diagnostics.get(uri);

      const timer = setTimeout(() => {
        // Remove this waiter
        entry.waiters = entry.waiters.filter((w) => w !== waiter);
        // Return whatever we have (might be stale or null)
        resolve(entry.diagnostics || []);
      }, timeoutMs);

      const waiter = { resolve, timer };
      entry.waiters.push(waiter);
    });
  }
}

// ---------------------------------------------------------------------------
// Language routing — built from config at startup
// ---------------------------------------------------------------------------

function getLanguageInfo(filePath) {
  const ext = extname(filePath).toLowerCase();
  return extToLanguage[ext] || null;
}

// ---------------------------------------------------------------------------
// HTTP API Server
// ---------------------------------------------------------------------------

function createApiServer(servers) {
  return createServer(async (req, res) => {
    // Collect request body
    const body = await new Promise((resolve) => {
      const chunks = [];
      req.on("data", (c) => chunks.push(c));
      req.on("end", () => resolve(Buffer.concat(chunks).toString()));
    });

    res.setHeader("Content-Type", "application/json");

    try {
      // --- Health check ---
      if (req.url === "/health") {
        const serverStatus = {};
        for (const [name, client] of Object.entries(servers)) {
          serverStatus[name] = client.isAlive ? "ok" : "down";
        }
        res.end(JSON.stringify({ status: "ok", pid: process.pid, servers: serverStatus }));
        return;
      }

      // --- Diagnostics ---
      if (req.method === "POST" && req.url === "/diagnostics") {
        const { file } = JSON.parse(body);
        if (!file) {
          res.statusCode = 400;
          res.end(JSON.stringify({ error: "Missing 'file' in request body" }));
          return;
        }

        if (!existsSync(file)) {
          res.statusCode = 404;
          res.end(JSON.stringify({ error: `File not found: ${file}` }));
          return;
        }

        const langInfo = getLanguageInfo(file);
        if (!langInfo) {
          res.end(JSON.stringify({ file, diagnostics: [], note: "unsupported file type" }));
          return;
        }

        const client = servers[langInfo.server];
        if (!client || !client.isAlive) {
          res.end(JSON.stringify({
            file,
            diagnostics: [],
            note: `${langInfo.server} LSP not available`,
          }));
          return;
        }

        log("INFO", `Checking diagnostics for: ${file} (server: ${langInfo.server})`);
        const diagnostics = await client.getDiagnostics(file, langInfo.id);

        // Format diagnostics into a useful response
        const formatted = (diagnostics || []).map((d) => ({
          severity: severityName(d.severity),
          message: d.message,
          range: {
            start: { line: d.range.start.line + 1, character: d.range.start.character },
            end: { line: d.range.end.line + 1, character: d.range.end.character },
          },
          source: d.source || langInfo.server,
          code: d.code,
        }));

        res.end(JSON.stringify({ file, diagnostics: formatted }));
        return;
      }

      // --- Shutdown ---
      if (req.method === "POST" && req.url === "/shutdown") {
        res.end(JSON.stringify({ status: "shutting_down" }));
        log("INFO", "Shutdown requested via API");
        await cleanup();
        return;
      }

      // --- 404 ---
      res.statusCode = 404;
      res.end(JSON.stringify({ error: "Not found" }));
    } catch (err) {
      log("ERROR", `API error: ${err.message}`);
      res.statusCode = 500;
      res.end(JSON.stringify({ error: err.message }));
    }
  });
}

function severityName(severity) {
  switch (severity) {
    case 1:
      return "error";
    case 2:
      return "warning";
    case 3:
      return "information";
    case 4:
      return "hint";
    default:
      return "unknown";
  }
}

// ---------------------------------------------------------------------------
// Server factory — creates LSP clients from config
// ---------------------------------------------------------------------------

async function createServers(config) {
  const servers = {};

  for (const [name, serverConfig] of Object.entries(config.servers || {})) {
    // Check requirements
    if (!checkRequirements(serverConfig)) {
      log("INFO", `[${name}] Skipped (requirements not met)`);
      continue;
    }

    // Resolve command binary
    const command = resolveCommand(serverConfig);
    if (!command) {
      log("INFO", `[${name}] Skipped (command not found: ${serverConfig.command})`);
      continue;
    }

    const cwd = serverConfig.cwd ? join(PROJECT_DIR, serverConfig.cwd) : PROJECT_DIR;
    const root = serverConfig.root ? join(PROJECT_DIR, serverConfig.root) : cwd;
    const rootUri = `file://${root}`;

    const client = new LSPClient(
      name,
      {
        command,
        args: serverConfig.args || [],
        cwd,
        env: interpolateEnv(serverConfig.env),
      },
      {
        rootUri,
        rootPath: root,
        workspaceFolders: [{ uri: rootUri, name }],
      },
    );

    try {
      client.start();
      await client.initialize();
      servers[name] = client;
      log("INFO", `[${name}] LSP server ready`);
    } catch (err) {
      log("ERROR", `[${name}] Failed to start: ${err.message}`);
    }
  }

  return servers;
}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

let httpServer;
let servers = {};

async function cleanup() {
  log("INFO", "Cleaning up...");

  for (const [name, client] of Object.entries(servers)) {
    try {
      await client.shutdown();
      log("INFO", `[${name}] shut down`);
    } catch (err) {
      log("ERROR", `[${name}] shutdown error: ${err.message}`);
    }
  }

  try {
    if (httpServer) httpServer.close();
  } catch {
    // ignore
  }

  // Remove runtime files
  for (const f of [PID_FILE, SOCKET_FILE, SOCKET_PATH]) {
    try {
      if (existsSync(f)) unlinkSync(f);
    } catch {
      // ignore
    }
  }

  process.exit(0);
}

async function main() {
  // Ensure state directory exists
  if (!existsSync(STATE_DIR)) {
    mkdirSync(STATE_DIR, { recursive: true });
  }

  // Clean up stale socket file
  if (existsSync(SOCKET_PATH)) {
    try {
      unlinkSync(SOCKET_PATH);
    } catch {
      // ignore
    }
  }

  // Truncate old log
  try {
    writeFileSync(LOG_FILE, "");
  } catch {
    // ignore
  }

  log("INFO", `LSP Bridge starting (project: ${PROJECT_DIR})`);

  // Load config and start LSP servers
  const config = loadConfig();
  extToLanguage = buildExtensionMap(config);
  log("INFO", `Loaded config with servers: ${Object.keys(config.servers || {}).join(", ")}`);
  log("INFO", `Supported extensions: ${Object.keys(extToLanguage).join(", ")}`);

  servers = await createServers(config);

  const serverNames = Object.keys(servers);
  if (serverNames.length === 0) {
    log("ERROR", "No LSP servers could be started. Exiting.");
    process.exit(1);
  }

  log("INFO", `Active LSP servers: ${serverNames.join(", ")}`);

  // Start HTTP server on Unix socket
  httpServer = createApiServer(servers);

  await new Promise((resolve, reject) => {
    httpServer.on("error", reject);
    httpServer.listen(SOCKET_PATH, () => {
      log("INFO", `HTTP API listening on ${SOCKET_PATH}`);
      resolve();
    });
  });

  // Write PID and socket path for hooks to discover
  writeFileSync(PID_FILE, process.pid.toString());
  writeFileSync(SOCKET_FILE, SOCKET_PATH);

  log("INFO", `LSP Bridge running (PID ${process.pid}, socket ${SOCKET_PATH})`);

  // Handle signals
  process.on("SIGTERM", cleanup);
  process.on("SIGINT", cleanup);
  process.on("SIGHUP", cleanup);
}

main().catch((err) => {
  log("ERROR", `Fatal: ${err.message}`);
  process.exit(1);
});
