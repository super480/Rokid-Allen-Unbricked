// New lines have to appear on their own, without a reload.
//
// The console polls the host, so a line that is spoken or typed on the
// speaker shows up in the browser on its own. This drives the real page
// in a real browser: it opens the console, a new line is written on the
// host behind its back, and the page has to show it by itself.
//
// Usage: node console_live_check.js <port> <token> <marker-file>

const fs = require("fs");
const PORT = Number(process.argv[2] || 8797);
const TOKEN = process.argv[3] || "page-token";
const MARKER = process.argv[4] || "";
const EDGE = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge";
const DEBUG_PORT = 9334;

const failures = [];

function check(name, condition, detail) {
  const ok = !!condition;
  console.log((ok ? "  ok   " : "  FAIL ") + name +
    (!ok && detail ? " -- " + detail : ""));
  if (!ok) failures.push(name);
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

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

async function main() {
  const { socket, child, dir } = await connect();
  try {
    const url = "http://127.0.0.1:" + PORT + "/console?token=" + TOKEN;
    await cdp(socket, "Page.enable");
    await cdp(socket, "Runtime.enable");
    await cdp(socket, "Page.navigate", { url });

    let drawn = "";
    for (let attempt = 0; attempt < 60; attempt++) {
      await sleep(300);
      drawn = await evaluate(socket,
        "document.getElementById('thread').innerText");
      if (drawn && drawn.includes("开场白")) break;
    }
    check("the page opens with the conversation",
      drawn.includes("开场白"), drawn.slice(0, 120));

    // Tell the host to write a new line.
    if (MARKER) fs.writeFileSync(MARKER, "write");
    console.log("  (a new line is written on the host)");

    let text = "";
    for (let attempt = 0; attempt < 60; attempt++) {
      await sleep(300);
      text = await evaluate(socket,
        "document.getElementById('thread').innerText");
      if (text && text.includes("新出现的一句")) break;
    }
    check("the new line shows up without a reload",
      text.includes("新出现的一句"), text.slice(0, 200));
    check("the line before it is still there",
      text.includes("开场白"), text.slice(0, 200));
    const banner = await evaluate(socket,
      "document.getElementById('banner').style.display === 'block' ? " +
      "document.getElementById('banner').innerText : ''");
    check("the page is not in a broken state", !banner, banner);

    // The badge is a notification counter: only NEW assistant replies count.
    // A switch notice, the user's own line and first-paint history do not.
    const counters = await evaluate(socket, `(() => {
      const key = activeThreadKey() === 'dsh' ? 'hermes' : 'dsh';
      const t = threadOf(key);
      const at = Date.now() / 1000;
      t.unread = 0;
      addEntry({seq: 90001, role: 'system', agent: key,
                message: '已切换到 X', at: at}, true);
      addEntry({seq: 90002, role: 'user', agent: key,
                message: '我说的话', at: at}, true);
      const afterNoise = t.unread;
      addEntry({seq: 90003, role: 'agent', agent: key,
                message: '它回的一句', at: at}, true);
      const afterAgent = t.unread;
      addEntry({seq: 90004, role: 'agent', agent: key,
                message: '首屏历史', at: at}, false);
      const afterHistory = t.unread;
      const before = Number(speechCounts[key] || 0);
      renderEntries([{seq: 90005, role: 'system', agent: key,
                       message: '又是切换提示', at: at}], true);
      const afterSystemCount = Number(speechCounts[key] || 0);
      renderEntries([{seq: 90006, role: 'agent', agent: key,
                       message: '又回一句', at: at}], true);
      const afterSpeechCount = Number(speechCounts[key] || 0);
      renderAgents();
      return {active: activeThreadKey(), key: key, afterNoise,
              afterAgent, afterHistory, before,
              afterSystemCount, afterSpeechCount,
              badges: [...document.querySelectorAll('#agentlist .badge')]
                .map(b => b.textContent)};
    })()`);
    check("switch notices and user lines do not create a badge",
      counters.afterNoise === 0, counters);
    check("one new agent reply creates exactly one badge",
      counters.afterAgent === 1, counters);
    check("first-paint history creates no badge",
      counters.afterHistory === 1, counters);
    check("the side counter ignores switch notices",
      counters.afterSystemCount === counters.before, counters);
    check("the side counter grows only on agent replies",
      counters.afterSpeechCount === counters.before + 1, counters);

    // The user's own line is a blue bubble, right-aligned, and visibly
    // different from the assistant's neutral bubble. Compare against a
    // probe element so the assertion holds in both themes.
    const style = await evaluate(socket, `(() => {
      const at = Date.now() / 1000;
      const u = buildEntry({role: 'user', agent: 'dsh', message: '我说的话',
                            at: at, seq: 91001});
      const a = buildEntry({role: 'agent', agent: 'dsh', message: '它回的话',
                            at: at, seq: 91002});
      const probe = document.createElement('div');
      probe.style.background = 'var(--accent)';
      document.body.appendChild(u);
      document.body.appendChild(a);
      document.body.appendChild(probe);
      const ub = getComputedStyle(u.querySelector('.bubble'));
      const out = {
        align: getComputedStyle(u).alignItems,
        justify: getComputedStyle(u.querySelector('.head')).justifyContent,
        padding: ub.padding,
        bg: ub.backgroundColor,
        color: ub.color,
        agentBg: getComputedStyle(a.querySelector('.bubble')).backgroundColor,
        accent: getComputedStyle(probe).backgroundColor,
        onAccent: getComputedStyle(document.documentElement)
          .getPropertyValue('--on-accent').trim()
      };
      u.remove(); a.remove(); probe.remove();
      return out;
    })()`);
    check("the user's line is right-aligned",
      style.align === 'flex-end' && style.justify === 'flex-end', style);
    check("the user's line has a real bubble",
      style.padding !== '0px' && style.bg !== 'rgba(0, 0, 0, 0)', style);
    check("the bubble uses the accent blue",
      style.bg === style.accent, style);
    check("the bubble differs from the assistant's",
      style.bg !== style.agentBg, style);

    // 若琪直连/原厂播报（天气卡提示等）用空 agent id 记录。它们不是
    // 注册助手，但仍要有若琪头像，不能退化成首字「若」。
    const face = await evaluate(socket, `(() => {
      const at = Date.now() / 1000;
      const out = {};
      [['empty', ''], ['direct', 'direct']].forEach(([k, id]) => {
        const el = buildEntry({role: 'agent', agent: id,
                               message: '（天气卡片已打开，原厂播报）', at: at});
        const av = el.querySelector('.avatar');
        out[k] = {bg: av.style.backgroundImage, text: av.textContent};
      });
      return out;
    })()`);
    check("an empty-agent notice shows the Rokid face, not a letter",
      /rokid\.png/.test(face.empty.bg) && face.empty.text === '', face);
    check("a direct notice shows the same Rokid face",
      /rokid\.png/.test(face.direct.bg) && face.direct.text === '', face);

    // A run of identical switch notices must collapse to one line with ×N.
    // A real message breaks the run; the next identical notices form a new
    // group. The underlying entries are still all kept.
    const fold = await evaluate(socket, `(() => {
      const key = 'foldprobe';
      const t = threadOf(key);
      const at = Date.now() / 1000;
      const rows = [
        {role: 'system', agent: key, message: '已切换到 巴蒂', at: at, seq: 92001},
        {role: 'system', agent: key, message: '已切换到 巴蒂', at: at, seq: 92002},
        {role: 'system', agent: key, message: '已切换到 巴蒂', at: at, seq: 92003},
        {role: 'system', agent: key, message: '已切换到 巴蒂', at: at, seq: 92004},
        {role: 'user',   agent: key, message: '我说的话',     at: at, seq: 92005},
        {role: 'system', agent: key, message: '已切换到 巴蒂', at: at, seq: 92006},
        {role: 'system', agent: key, message: '已切换到 巴蒂', at: at, seq: 92007}
      ];
      rows.forEach(r => addEntry(r, false));
      const groups = [...t.list.querySelectorAll('.msg.system-group')];
      const texts = groups.map(g => g.querySelector('.bubble').textContent);
      const firstCount = groups[0] ? groups[0].dataset.count : '';
      const itemsBeforePaging = t.items.length;
      // Paging older entries in must fold into the group at the front too.
      prependEntry({role: 'system', agent: key, message: '已切换到 巴蒂',
                    at: at, seq: 91999}, t);
      const front = t.list.querySelector('.msg.system-group');
      return {
        groups: groups.length, texts: texts,
        firstCount: firstCount,
        frontCount: front ? front.dataset.count : '',
        frontText: front ? front.querySelector('.bubble').textContent : '',
        items: itemsBeforePaging,
        userLines: t.list.querySelectorAll('.msg.user').length
      };
    })()`);
    check("four identical notices become one line marked ×4",
      fold.groups === 2 && fold.firstCount === '4' &&
      /×4$/.test(fold.texts[0]), fold);
    check("a real message breaks the run, next pair is ×2",
      /×2$/.test(fold.texts[1]) && fold.userLines === 1, fold);
    check("all entries are still kept in memory",
      fold.items === 7, fold);
    check("paging older notices folds into the front group",
      fold.frontCount === '5' && /×5$/.test(fold.frontText), fold);
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
