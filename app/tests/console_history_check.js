// After a restart the page must show every assistant's own history,
// and the older messages must be reachable from the top of each pane.
//
// Usage: node console_history_check.js <port> <token>

const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");

const PORT = Number(process.argv[2] || 8796);
const TOKEN = process.argv[3] || "history-token";
const EDGE = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge";
const DEBUG_PORT = 9342;

const failures = [];
function check(name, condition, detail) {
  const ok = !!condition;
  console.log((ok ? "  ok   " : "  FAIL ") + name +
    (!ok && detail !== undefined ? " -- " + JSON.stringify(detail) : ""));
  if (!ok) failures.push(name);
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

let nextId = 1;
const pending = new Map();
function cdp(socket, method, params) {
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
    const ex = result.exceptionDetails;
    const detail = (ex.exception && (ex.exception.description || ex.exception.value))
      || ex.text || "evaluate failed";
    throw new Error(detail);
  }
  return result.result.value;
}
async function waitFor(socket, expression, wanted, tries = 60) {
  let value;
  for (let i = 0; i < tries; i++) {
    await sleep(300);
    value = await evaluate(socket, expression);
    if (wanted(value)) return value;
  }
  return value;
}

async function connect() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "edge-history-cdp-"));
  const child = spawn(EDGE, [
    "--headless=new", "--disable-gpu", "--no-sandbox",
    "--remote-debugging-port=" + DEBUG_PORT,
    "--user-data-dir=" + dir,
    "--window-size=1280,900", "about:blank"
  ], { stdio: "ignore" });

  let target = null;
  for (let attempt = 0; attempt < 80; attempt++) {
    await sleep(250);
    try {
      const list = await (await fetch(
        "http://127.0.0.1:" + DEBUG_PORT + "/json/list")).json();
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

async function main() {
  const { socket, child, dir } = await connect();
  try {
    await cdp(socket, "Page.enable");
    await cdp(socket, "Runtime.enable");
    await cdp(socket, "Page.navigate", {
      url: "http://127.0.0.1:" + PORT + "/console?token=" + TOKEN});

    const stateExpr = `(() => ({
      ready: document.getElementById('thread') &&
             Object.keys(threads).length > 0,
      panes: Object.keys(threads).map(k => ({
        k, n: threads[k].items.length, total: threads[k].total,
        hasMore: threads[k].hasMore,
        olderShown: threads[k].olderWrap.style.display !== 'none',
        first: threads[k].items.length ? threads[k].items[0].message : ''
      })),
      banner: document.getElementById('banner').style.display === 'block'
              ? document.getElementById('banner').innerText : '',
      hasAllView: document.body.innerText.includes('全部对话')
    }))()`;
    const state = await waitFor(socket, stateExpr, v => v && v.ready);
    check("the page opens after a restart", state.ready, state);
    check("there is no merged 'all conversations' view", !state.hasAllView);
    check("the page has no error banner", !state.banner, state.banner);

    const byKey = Object.fromEntries(state.panes.map(p => [p.k, p]));
    check("the fish's one line came back", byKey.dsh && byKey.dsh.n === 1,
      byKey.dsh);
    check("Codex's one line came back", byKey.codex && byKey.codex.n === 1,
      byKey.codex);
    check("Hermes's long history came back", byKey.hermes && byKey.hermes.n === 50,
      byKey.hermes);
    check("Hermes reports the full count on disk",
      byKey.hermes && byKey.hermes.total === 360, byKey.hermes);
    check("Hermes offers earlier messages",
      byKey.hermes && byKey.hermes.hasMore && byKey.hermes.olderShown,
      byKey.hermes);
    check("the fish has no earlier messages to load",
      byKey.dsh && !byKey.dsh.hasMore, byKey.dsh);

    // Click the button and see the older lines arrive at the top.
    await evaluate(socket, `loadOlder('hermes')`);
    await waitFor(socket, `threads.hermes ? threads.hermes.items.length : 0`,
      n => n > 50);
    const after = await evaluate(socket, `(() => {
      const t = threads.hermes;
      const seqs = t.items.map(e => Number(e.seq || 0));
      return {
        n: t.items.length,
        first: t.items[0].message,
        last: t.items[t.items.length - 1].message,
        ordered: seqs.every((s, i) => i === 0 || seqs[i-1] < s),
        unique: new Set(seqs).size === seqs.length,
        hasMore: t.hasMore,
        label: t.olderBtn.textContent,
        domFirst: t.list.querySelector('.msg .bubble')
          ? t.list.querySelector('.msg .bubble').textContent : null
      };
    })()`);
    check("older lines were loaded", after.n > 50, after);
    check("a whole page of 50 older lines arrived", after.n === 100, after.n);
    check("the oldest of that page is now at the top",
      after.first === "小赫第260句", after.first);
    check("the new page is inserted above, in the right order", after.ordered,
      after);
    check("no line appears twice", after.unique, after);
    check("the DOM matches the in-memory order", after.domFirst === after.first,
      after);
    check("the newest line is still at the bottom",
      after.last === "小赫第359句", after.last);
    check("there is still more to load", after.hasMore, after);
    check("the button shows how many are left", /还有/.test(after.label),
      after.label);

    // Load all the way back and make sure the very first line is there.
    for (let i = 0; i < 10; i++) {
      if (!(await evaluate(socket, "threads.hermes.hasMore"))) break;
      await evaluate(socket, `loadOlder('hermes')`);
      await sleep(350);
    }
    const end = await evaluate(socket, `(() => ({
      n: threads.hermes.items.length,
      first: threads.hermes.items[0].message,
      hasMore: threads.hermes.hasMore
    }))()`);
    check("the conversation can be read all the way back",
      end.first === "小赫第0句", end);
    check("all 360 lines are reachable", end.n === 360, end);
    check("the button stops when there is nothing more", !end.hasMore, end);
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
  console.log("the conversation survives a restart");
}

main().catch(error => {
  console.error("the check itself failed: " + error.message);
  process.exit(2);
});
