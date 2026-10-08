// Dcode for VS Code. Plain JavaScript, no dependencies, no build step, no network beyond 127.0.0.1.
//
// The extension does not run a model or an agent itself. It starts the `dcode` program in the
// integrated terminal and talks to Dcode's own localhost hub (the one the web UI uses) to send
// code to a running session.
"use strict";

const vscode = require("vscode");
const http = require("http");
const path = require("path");
const { execFile } = require("child_process");

function program() {
  return vscode.workspace.getConfiguration("dcode").get("path") || "dcode";
}

function folder() {
  const editor = vscode.window.activeTextEditor;
  const owner = editor && vscode.workspace.getWorkspaceFolder(editor.document.uri);
  const first = (vscode.workspace.workspaceFolders || [])[0];
  return (owner || first || { uri: { fsPath: undefined } }).uri.fsPath;
}

function quote(arg) {
  return /^[\w@%+=:,./-]+$/.test(arg) ? arg : `'${arg.replace(/'/g, `'\\''`)}'`;
}

function terminal(args, name) {
  const term = vscode.window.createTerminal({ name: name || "Dcode", cwd: folder() });
  term.show();
  term.sendText([program(), ...args].map(quote).join(" "));
  return term;
}

/** `dcode ui` prints the hub link (with the access token) and one line per running session. */
function hubInfo() {
  return new Promise((resolve, reject) => {
    execFile(program(), ["ui"], { timeout: 10000 }, (error, stdout) => {
      const link = /(https?:\/\/[^\s/]+)\/\?token=([\w-]+)/.exec(stdout || "");
      if (!link) {
        return reject(new Error(error && error.code === "ENOENT"
          ? `Can't find "${program()}". Set "dcode.path" in Settings.`
          : "No Dcode session is running. Start one with “Dcode: Start in Terminal”."));
      }
      const sessions = [];
      const row = /^\s*session\s+(\S+)\s+(.*)$/gm;
      let match;
      while ((match = row.exec(stdout)) !== null) sessions.push({ id: match[1], cwd: match[2].trim() });
      resolve({ base: link[1], token: link[2], sessions });
    });
  });
}

function post(info, route, body) {
  return new Promise((resolve, reject) => {
    const url = new URL(info.base + route);
    const data = JSON.stringify(body);
    const request = http.request({
      host: url.hostname, port: url.port, path: url.pathname, method: "POST",
      headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data),
        Cookie: `dcode_token=${info.token}`, "X-Dcode": "1" },
    }, (response) => {
      let text = "";
      response.on("data", (chunk) => (text += chunk));
      response.on("end", () => (response.statusCode < 300 ? resolve(text)
        : reject(new Error(`Dcode answered ${response.statusCode}: ${text.slice(0, 200)}`))));
    });
    request.on("error", reject);
    request.end(data);
  });
}

/** The session working in this folder, else the only one, else ask. */
async function pickSession(info) {
  if (!info.sessions.length) throw new Error("No Dcode session is running. Start one with “Dcode: Start in Terminal”.");
  const here = folder();
  const local = info.sessions.filter((s) => here && (s.cwd === here || here.startsWith(s.cwd + path.sep)));
  if (local.length === 1) return local[0];
  if (info.sessions.length === 1) return info.sessions[0];
  const picked = await vscode.window.showQuickPick(
    info.sessions.map((s) => ({ label: s.id, description: s.cwd, session: s })), { placeHolder: "Send to which Dcode session?" });
  if (!picked) throw new Error("");
  return picked.session;
}

/** What to send for the current editor: "@file" for a whole file, the lines themselves for a selection. */
function describe(editor, wholeFile) {
  const file = vscode.workspace.asRelativePath(editor.document.uri, false);
  const selection = editor.selection;
  if (wholeFile || selection.isEmpty) return `@${file}`;
  const code = editor.document.getText(selection);
  const fence = "`".repeat(Math.max(3, ...(code.match(/`+/g) || [""]).map((run) => run.length + 1)));
  return `${file} lines ${selection.start.line + 1}-${selection.end.line + 1}:\n${fence}\n${code}\n${fence}`;
}

async function send(wholeFile) {
  const editor = vscode.window.activeTextEditor;
  if (!editor) return vscode.window.showInformationMessage("Open a file first.");
  const question = await vscode.window.showInputBox({
    prompt: wholeFile ? "What should Dcode do with this file?" : "What should Dcode do with the selection?",
    placeHolder: "explain this · find the bug · write tests for it",
  });
  if (question === undefined) return undefined;
  try {
    const info = await hubInfo();
    const session = await pickSession(info);
    await post(info, `/api/s/${session.id}/message`, { text: `${question || "Look at this."}\n\n${describe(editor, wholeFile)}` });
    vscode.window.setStatusBarMessage(`Sent to Dcode session ${session.id}`, 4000);
  } catch (error) {
    if (error.message) vscode.window.showErrorMessage(error.message);
  }
  return undefined;
}

async function openUi() {
  try {
    const info = await hubInfo();
    const session = info.sessions.length ? await pickSession(info) : null;
    const url = `${info.base}/?token=${info.token}${session ? "#s=" + session.id : ""}`;
    const inside = vscode.workspace.getConfiguration("dcode").get("openUiInside");
    if (inside) {
      try { return await vscode.commands.executeCommand("simpleBrowser.show", url); } catch (_) { /* no Simple Browser here */ }
    }
    return await vscode.env.openExternal(vscode.Uri.parse(url));
  } catch (error) {
    if (error.message) vscode.window.showErrorMessage(error.message);
  }
  return undefined;
}

function activate(context) {
  const add = (name, run) => context.subscriptions.push(vscode.commands.registerCommand(name, run));
  add("dcode.start", () => terminal([]));
  add("dcode.continue", () => terminal(["--continue"]));
  add("dcode.attach", () => terminal(["attach"], "Dcode (attached)"));
  add("dcode.openUi", openUi);
  add("dcode.sendSelection", () => send(false));
  add("dcode.sendFile", () => send(true));
}

function deactivate() {}

module.exports = { activate, deactivate, describe, quote, hubInfo, pickSession };
