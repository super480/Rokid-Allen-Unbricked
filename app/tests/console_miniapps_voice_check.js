// 小应用 · 自定义语音：真实浏览器里输入 -> 点保存 -> 立即识别 -> 重载回填。
// Started by console_miniapps_voice_check.py against a throwaway host.
const fs = require("fs"), os = require("os"), path = require("path"), { spawn } = require("child_process");
const PORT = Number(process.argv[2]), TOKEN = process.argv[3];
const EDGE = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", DBG = 9357;
const sleep = ms => new Promise(r => setTimeout(r, ms));
let id = 1; const pend = new Map();
(async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "edge-minivoice-"));
  const child = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-sandbox",
    "--remote-debugging-port=" + DBG, "--user-data-dir=" + dir,
    "--window-size=1280,900", "about:blank"], { stdio: "ignore" });
  let target = null;
  for (let i = 0; i < 80; i++) {
    await sleep(250);
    try {
      const list = await (await fetch("http://127.0.0.1:" + DBG + "/json/list")).json();
      target = list.find(x => x.type === "page");
      if (target) break;
    } catch (e) {}
  }
  if (!target) throw new Error("edge did not start");
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  ws.onmessage = e => {
    const m = JSON.parse(e.data);
    if (m.id && pend.has(m.id)) { const f = pend.get(m.id); pend.delete(m.id); f(m.result); }
  };
  const send = (method, params) => {
    const i = id++;
    ws.send(JSON.stringify({ id: i, method, params: params || {} }));
    return new Promise(r => pend.set(i, r));
  };
  const ev = async expr => {
    const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) {
      const ex = r.exceptionDetails;
      throw new Error(String((ex.exception && (ex.exception.description || ex.exception.value)) || ex.text));
    }
    return r.result && r.result.value;
  };
  const checks = [];
  const check = (name, ok, detail) => {
    checks.push({ name, ok, detail });
    console.log((ok ? "  ok  " : "  FAIL") + " " + name + (ok ? "" : " -> " + JSON.stringify(detail)));
  };
  const openMiniapps = async () => {
    await ev(`document.getElementById('btnsettings').click()`);
    await sleep(250);
    await ev(`[...document.querySelectorAll('#settingstabs .nav-item')].find(b=>b.dataset.tab==='miniapps').click()`);
    await sleep(900);
    await ev(`document.querySelector('[data-store-settings="network_speed"]').click()`);
  };

  await send("Page.enable"); await send("Runtime.enable");
  await send("Page.navigate", { url: "http://127.0.0.1:" + PORT + "/console?token=" + TOKEN });
  await sleep(3200);
  await openMiniapps();

  const surface = await ev(`(()=>{
    const ids = ['codex_usage','network_speed','system_resources','bambu_print'];
    return {
      title: document.getElementById('panetitle').textContent,
      inputs: ids.filter(x => !!document.getElementById('minivoice-'+x)).length,
      saves: document.querySelectorAll('[data-voice-save]').length,
      netBuiltin: document.getElementById('minibuiltin-network_speed').textContent,
      actions: document.getElementById('miniappcardactions').textContent
    };
  })()`);
  check("四张卡片各有一个自定义语音输入框", surface.inputs === 4, surface);
  check("四张卡片各有一个保存按钮", surface.saves === 4, surface);
  check("内置说法与卡片动作都渲染出来",
    surface.netBuiltin.includes("查询网速") && surface.actions.includes("关闭卡片"),
    surface);

  // Type a phrase and press THAT card's save button.
  await ev(`(()=>{
    const input = document.getElementById('minivoice-network_speed');
    input.value = '看看宽带，网速快不快';
    document.querySelector('[data-voice-save="network_speed"]').click();
    return true;
  })()`);
  await sleep(900);
  const saved = await ev(`(()=>({
    status: document.getElementById('minivoicestatus-network_speed').textContent,
    value: document.getElementById('minivoice-network_speed').value,
    result: document.getElementById('miniresult').textContent
  }))()`);
  check("点保存后状态显示已保存且立即生效",
    saved.status.includes("已保存") && saved.status.includes("无需重启") &&
    saved.value.includes("看看宽带"), saved);

  const hit = await ev(`apiPost('/api/text', {text:'若琪，看看宽带', request_id:'voice-e2e-1'})`);
  check("保存后立刻能识别（命中网速卡、不播报）",
    hit && hit.intent === "miniapp" && hit.app_id === "network_speed" &&
    hit.operation === "speed" && !("speak" in hit), hit);
  const miss = await ev(`apiPost('/api/text', {text:'随便聊点别的', request_id:'voice-e2e-2'})`);
  check("无关句子不会误打开卡片", miss && miss.intent !== "miniapp", miss);

  // Reload: the phrase must come back from the persisted config.
  await send("Page.reload", { ignoreCache: true });
  await sleep(3200);
  await openMiniapps();
  const reloaded = await ev(`document.getElementById('minivoice-network_speed').value`);
  check("重载页面后自定义说法回填", reloaded.includes("看看宽带"), reloaded);

  // Clearing the box removes that card's phrases only.
  await ev(`(()=>{
    document.getElementById('minivoice-network_speed').value = '';
    document.querySelector('[data-voice-save="network_speed"]').click();
    return true;
  })()`);
  await sleep(800);
  const cleared = await ev(`document.getElementById('minivoicestatus-network_speed').textContent`);
  check("留空保存可清空自定义说法", cleared.includes("已清空"), cleared);

  try { ws.close(); } catch (e) {}
  try { child.kill(); } catch (e) {}
  try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) {}
  const failed = checks.filter(c => !c.ok);
  console.log(failed.length ? "FAILED: " + failed.map(c => c.name).join(", ") : "all checks passed");
  process.exit(failed.length ? 1 : 0);
})().catch(e => { console.error("ERR", e.message); process.exit(1); });
