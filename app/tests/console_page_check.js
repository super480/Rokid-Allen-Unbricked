// The console page has to recover on its own after the host restarts.
//
// The page keeps the number of the last line it drew. After a restart
// the host counts from 1 again, so the old number points past the end
// and every poll would ask for lines that do not exist -- the page
// would look as if it had stopped receiving messages, and only a manual
// reload would help.
//
// So the page has to notice the new host (by its identity) and redraw
// the conversation. This drives the real page in a real browser through
// the debugging protocol: it opens the console, the host is restarted
// behind its back, and the page has to catch up on its own.
//
// Usage: node console_page_check.js <port> <token> <marker-file>

const fs = require("fs");
const PORT = Number(process.argv[2] || 8797);
const TOKEN = process.argv[3] || "page-token";
const MARKER = process.argv[4] || "";
const EDGE = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge";
const DEBUG_PORT = 9333;

const failures = [];

function check(name, condition, detail) {
  const ok = !!condition;
  console.log((ok ? "  ok   " : "  FAIL ") + name +
    (!ok && detail ? " -- " + detail : ""));
  if (!ok) failures.push(name);
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

// -- the browser, driven through the debugging protocol ---------------

let nextId = 1;
const pending = new Map();

async function cdp(socket, method, params) {
  const id = nextId++;
  socket.send(JSON.stringify({ id, method, params: params || {} }));
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    setTimeout(() => {
      if (pending.has(id)) {
        pending.delete(id);
        reject(new Error("timeout: " + method));
      }
    }, 20000);
  });
}

async function evaluate(socket, expression) {
  const result = await cdp(socket, "Runtime.evaluate", {
    expression, returnByValue: true, awaitPromise: true});
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.text || "evaluate failed");
  }
  return result.result.value;
}

async function connect() {
  const os = require("os");
  const path = require("path");
  const { spawn } = require("child_process");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "edge-cdp-"));
  const child = spawn(EDGE, [
    "--headless=new", "--disable-gpu", "--no-sandbox",
    "--remote-debugging-port=" + DEBUG_PORT,
    "--user-data-dir=" + dir,
    "--window-size=1280,900",
    "about:blank"
  ], { stdio: "ignore" });

  let target = null;
  for (let attempt = 0; attempt < 80; attempt++) {
    await sleep(250);
    try {
      const response = await fetch(
        "http://127.0.0.1:" + DEBUG_PORT + "/json/list");
      const list = await response.json();
      target = list.find(item => item.type === "page");
      if (target) break;
    } catch (e) { /* the browser is not up yet */ }
  }
  if (!target) throw new Error("the browser did not come up");

  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.onopen = resolve;
    socket.onerror = reject;
  });
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const entry = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) entry.reject(new Error(message.error.message));
      else entry.resolve(message.result);
    }
  };
  return { socket, child, dir };
}

// -- the check ---------------------------------------------------------

async function main() {
  const { socket, child, dir } = await connect();
  try {
    const url = "http://127.0.0.1:" + PORT + "/console?token=" + TOKEN;
    await cdp(socket, "Page.enable");
    await cdp(socket, "Runtime.enable");
    await cdp(socket, "Page.navigate", { url });

    // Wait for the page to have drawn the conversation.
    let drawn = "";
    for (let attempt = 0; attempt < 60; attempt++) {
      await sleep(300);
      drawn = await evaluate(socket,
        "document.getElementById('thread').innerText");
      if (drawn && drawn.includes("重启前的一句话")) break;
    }
    check("the page opens with the conversation",
      drawn.includes("重启前的一句话"), drawn.slice(0, 120));

    const before = await evaluate(socket, "hostInstance");
    check("the page remembers the host it is talking to",
      typeof before === "string" && before.length > 0, String(before));

    // Tell the host to restart now.
    if (MARKER) fs.writeFileSync(MARKER, "restart");
    console.log("  (the host is restarting)");

    // The page has to notice the new host by itself.
    let after = before;
    for (let attempt = 0; attempt < 120; attempt++) {
      await sleep(300);
      after = await evaluate(socket, "hostInstance");
      if (after && after !== before) break;
    }
    check("the page noticed that the host changed",
      after && after !== before, after + " == " + before);

    // Give the page a moment to redraw, then look at the conversation.
    let text = "";
    for (let attempt = 0; attempt < 80; attempt++) {
      await sleep(300);
      text = await evaluate(socket,
        "document.getElementById('thread').innerText");
      if (text && text.includes("重启后的一句")) break;
    }
    check("the conversation from before the restart is still there",
      text.includes("重启前的一句话"), text.slice(0, 200));
    check("the line from after the restart shows up on its own",
      text.includes("重启后的一句"), text.slice(0, 200));
    const banner = await evaluate(socket,
      "document.getElementById('banner').style.display === 'block' ? " +
      "document.getElementById('banner').innerText : ''");
    check("the page is not in a broken state", !banner, banner);
  } finally {
    try { socket.close(); } catch (e) {}
    try { child.kill(); } catch (e) {}
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) {}
  }

  console.log();
  if (failures.length) {
    console.log("FAILED: " + failures.join(", "));
    process.exit(1);
  }
  console.log("all checks passed");
}

main().catch(error => {
  console.error("the check itself failed: " + error.message);
  process.exit(2);
});
