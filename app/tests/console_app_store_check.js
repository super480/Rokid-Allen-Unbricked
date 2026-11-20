// 应用商店：隔离主机与真实浏览器；不连接若琪，不播报。
const fs = require("fs"), os = require("os"), path = require("path"), { spawn } = require("child_process");
const PORT = Number(process.argv[2]), TOKEN = process.argv[3];
const EDGE = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", DBG = 9365;
const sleep = ms => new Promise(r => setTimeout(r, ms));
let id = 1; const pend = new Map();
(async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "edge-app-store-"));
  const child = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-sandbox",
    "--remote-debugging-port=" + DBG, "--user-data-dir=" + dir,
    "--window-size=1280,650", "about:blank"], { stdio: "ignore" });
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
  await send("Page.enable"); await send("Runtime.enable");
  await send("Page.navigate", {url:"http://127.0.0.1:"+PORT+"/console?token="+TOKEN});
  await sleep(1800);
  await ev(`document.getElementById('btnsettings').click()`);
  await ev(`document.querySelector('[data-tab="miniapps"]').click()`);
  await sleep(800);
  const catalog = await ev(`(()=>{
    const tiles=[...document.querySelectorAll('.store-tile')];
    return {title:document.getElementById('panetitle').textContent, count:tiles.length,
      cols:getComputedStyle(document.getElementById('appstore-grid')).gridTemplateColumns.split(' ').length,
      ready:tiles.every(t=>t.querySelector('img').naturalWidth>0 && t.querySelectorAll('button').length===2),
      scroll:document.querySelector('.settings-content').scrollHeight > document.querySelector('.settings-content').clientHeight,
      rows:tiles.map(t=>Math.round(t.getBoundingClientRect().top))};
  })()`);
  check("应用商店三列小卡片、截图及双按钮", catalog.title==='应用商店' && catalog.count===6
    && catalog.cols===3 && catalog.ready && catalog.rows[0]===catalog.rows[2] && catalog.rows[3]>catalog.rows[0], catalog);
  check("应用目录可以纵向滚动",catalog.scroll,catalog);
  await ev(`document.querySelector('[data-store-settings="network_speed"]').click()`);
  const detail=await ev(`({title:document.getElementById('panetitle').textContent,
    panels:[...document.querySelectorAll('[data-app-settings]')].filter(e=>!e.hidden).map(e=>e.dataset.appSettings),
    input:!!document.getElementById('minivoice-network_speed')})`);
  check("设置只进入对应应用并保留原有表单", detail.title==='网络速度' && detail.panels.join(',')==='network_speed' && detail.input, detail);
  await ev(`document.getElementById('minivoice-network_speed').value='看看宽带';document.querySelector('[data-voice-save="network_speed"]').click()`);
  await sleep(500);
  await ev(`document.getElementById('appstore-back').click();document.querySelector('[data-store-toggle="network_speed"]').click()`);
  await sleep(500);
  const removed=await ev(`apiGet('/api/app-store')`);
  const blocked=await ev(`apiPost('/api/miniapps/query',{app_id:'network_speed'}).then(()=>false).catch(()=>true)`);
  const saved=await ev(`apiGet('/api/miniapps')`);
  check("删除真正禁用查询且保留语音配置", removed.apps.find(a=>a.id==='network_speed').installed===false
    && blocked && saved.voice_phrases.network_speed.includes('看看宽带'), {removed,blocked,saved});
  await send('Page.reload',{ignoreCache:true});await sleep(1600);
  await ev(`document.getElementById('btnsettings').click();document.querySelector('[data-tab="miniapps"]').click()`);
  await sleep(500);
  const add=await ev(`document.querySelector('[data-store-toggle="network_speed"]').textContent`);
  check("刷新后删除状态保持",add==='添加',add);
  await ev(`document.querySelector('[data-store-toggle="network_speed"]').click()`);await sleep(500);
  const restored=await ev(`apiGet('/api/app-store')`);
  check("添加恢复应用",restored.apps.find(a=>a.id==='network_speed').installed===true,restored);
  await ev(`document.querySelector('[data-store-settings="weather"]').click()`);
  check("天气设置入口可用",await ev(`!document.querySelector('[data-app-settings="weather"]').hidden`));
  await ev(`document.getElementById('appstore-back').click();document.querySelector('[data-store-settings="clock"]').click()`);
  check("时钟设置入口可用",await ev(`!document.querySelector('[data-app-settings="clock"]').hidden`));

  await ev(`document.getElementById('appstore-back').click();
    document.querySelector('[data-store-settings="bambu_print"]').click()`);
  await sleep(300);
  await ev(`document.getElementById('bambu-host').value='192.168.1.50';
    document.getElementById('bambu-serial').value='TESTSERIAL1';
    document.getElementById('bambu-code').value='FAKE2468';
    document.getElementById('bambu-form').requestSubmit()`);
  await sleep(400);
  const printer = await ev(`apiGet('/api/bambu-print')`);
  const passwordCleared = await ev(`document.getElementById('bambu-code').value === ''`);
  check("打印机设置保存成功且访问码不回显", printer.host==='192.168.1.50'
    && printer.serial==='TESTSERIAL1' && printer.has_access_code === true
    && !('access_code' in printer) && passwordCleared, printer);

  try { ws.close(); } catch (e) {}
  try { child.kill(); } catch (e) {}
  try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) {}
  const failed = checks.filter(c => !c.ok);
  console.log(failed.length ? "FAILED: " + failed.map(c => c.name).join(", ") : "all checks passed");
  process.exit(failed.length ? 1 : 0);
})().catch(e => { console.error("ERR", e.message); process.exit(1); });
