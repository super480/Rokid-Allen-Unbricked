// 待机时钟卡：真实浏览器里验证 换表盘 / 自定义说法保存 / 回填。
// Started by console_clock_check.py against a throwaway host (device
// serial empty), so the SET_FACE broadcast is a safe no-op and nothing
// on the speaker is touched. No audio is produced either way.
const fs = require("fs"), os = require("os"), path = require("path"), { spawn } = require("child_process");
const PORT = Number(process.argv[2]), TOKEN = process.argv[3];
const EDGE = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", DBG = 9359;
const sleep = ms => new Promise(r => setTimeout(r, ms));
let id = 1; const pend = new Map();
(async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "edge-clock-"));
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
    await ev(`document.querySelector('[data-store-settings="clock"]').click()`);
  };

  await send("Page.enable"); await send("Runtime.enable");
  await send("Page.navigate", { url: "http://127.0.0.1:" + PORT + "/console?token=" + TOKEN });
  await sleep(3200);
  await openMiniapps();

  // 1. The card is on the tab with its face buttons and voice box.
  const surface = await ev(`(()=>{
    return {
      buttons: [...document.querySelectorAll('[data-clock-face]')].map(b=>b.dataset.clockFace),
      hint: document.getElementById('clockfacehint').textContent,
      hasInput: !!document.getElementById('minivoice-clock'),
      hasSave: !!document.getElementById('clockvoicesave'),
      builtin: document.getElementById('minibuiltin-clock').textContent
    };
  })()`);
  check("待机时钟卡在位：十四个表盘按钮 / 提示 / 自定义说法 / 保存按钮",
    surface.buttons.join(",") === "grid,digital,orbit,weekday,minimal,lagoon,milk,forest,roman,factory_sunset,factory_fantasy,factory_bluesky,factory_jellyfish,next"
    && surface.hasInput && surface.hasSave, surface);
  check("默认表盘为 2×2 数字", surface.hint.includes("2×2 数字"), surface);
  check("内置说法是关键词列表", surface.builtin.includes("表盘") && surface.builtin.includes("关键词"),
    surface);

  // 2. Click 电子日历: the hint and the API both follow.
  await ev(`document.querySelector('[data-clock-face="digital"]').click()`);
  await sleep(900);
  const afterDigital = await ev(`(()=>({
    hint: document.getElementById('clockfacehint').textContent,
    out: document.getElementById('miniresult').textContent
  }))()`);
  const digitalState = await ev(`apiGet('/api/clock')`);
  check("点「电子日历」后提示切换且选择持久化",
    afterDigital.hint.includes("电子日历") && digitalState.face === "digital",
    { afterDigital, digitalState });

  // New native face remains selectable even with the original saved face list.
  await ev(`document.querySelector('[data-clock-face="orbit"]').click()`);
  await sleep(900);
  const orbitState = await ev(`apiGet('/api/clock')`);
  const orbitHint = await ev(`document.getElementById('clockfacehint').textContent`);
  check("双旋转刻度表盘可选且回填中文名",
    orbitState.face === "orbit" && orbitState.faces.includes("orbit")
    && orbitHint.includes("双旋转刻度"), {orbitState, orbitHint});

  await ev(`document.querySelector('[data-clock-face="weekday"]').click()`);
  await sleep(900);
  const weekdayState = await ev(`apiGet('/api/clock')`);
  const weekdayHint = await ev(`document.getElementById('clockfacehint').textContent`);
  check("星期环表盘可选且回填中文名",
    weekdayState.face === "weekday" && weekdayState.faces.includes("weekday")
    && weekdayHint.includes("星期环"), {weekdayState, weekdayHint});

  await ev(`document.querySelector('[data-clock-face="minimal"]').click()`);
  await sleep(900);
  const minimalState = await ev(`apiGet('/api/clock')`);
  const minimalHint = await ev(`document.getElementById('clockfacehint').textContent`);
  check("双圆极简表盘可选且回填中文名", minimalState.face === "minimal"
    && minimalState.faces.includes("minimal") && minimalHint.includes("双圆极简"), minimalState);

  await ev(`document.querySelector('[data-clock-face="lagoon"]').click()`);
  await sleep(900);
  const lagoonState = await ev(`apiGet('/api/clock')`);
  const lagoonHint = await ev(`document.getElementById('clockfacehint').textContent`);
  check("蓝紫鳄鱼表盘可选且回填中文名", lagoonState.face === "lagoon"
    && lagoonState.faces.includes("lagoon") && lagoonHint.includes("蓝紫鳄鱼"), lagoonState);

  await ev(`document.querySelector('[data-clock-face="milk"]').click()`);
  await sleep(900);
  const milkState = await ev(`apiGet('/api/clock')`);
  const milkHint = await ev(`document.getElementById('clockfacehint').textContent`);
  check("牛奶派对可选且回填中文名", milkState.face === "milk"
    && milkState.faces.includes("milk") && milkHint.includes("牛奶派对"), milkState);

  await ev(`document.querySelector('[data-clock-face="forest"]').click()`);
  await sleep(900);
  const forestState = await ev(`apiGet('/api/clock')`);
  const forestHint = await ev(`document.getElementById('clockfacehint').textContent`);
  check("林间夜色可选且回填中文名", forestState.face === "forest"
    && forestState.faces.includes("forest") && forestHint.includes("林间夜色"), forestState);

  await ev(`document.querySelector('[data-clock-face="roman"]').click()`);
  await sleep(900);
  const romanState = await ev(`apiGet('/api/clock')`);
  const romanHint = await ev(`document.getElementById('clockfacehint').textContent`);
  check("黑曜罗马可选且回填中文名", romanState.face === "roman"
    && romanState.faces.includes("roman") && romanHint.includes("黑曜罗马"), romanState);

  await ev(`document.querySelector('[data-clock-face="factory_jellyfish"]').click()`);
  await sleep(900);
  const factoryState = await ev(`apiGet('/api/clock')`);
  const factoryHint = await ev(`document.getElementById('clockfacehint').textContent`);
  check("原厂水母可选并回填中文名", factoryState.face === "factory_jellyfish"
    && factoryHint.startsWith("当前表盘：原厂水母"), factoryState);

  // 3. Back to grid through the button.
  await ev(`document.querySelector('[data-clock-face="grid"]').click()`);
  await sleep(900);
  const gridState = await ev(`apiGet('/api/clock')`);
  check("点「2×2 数字」切回 grid", gridState.face === "grid", gridState);

  // 4. Empty face name is rejected.
  const bad = await ev(`apiPost('/api/clock/face', {face: ''})
    .then(() => 'accepted').catch(e => 'rejected:' + e.message)`);
  check("空表盘名被拒绝", String(bad).startsWith("rejected"), bad);

  // 5. A custom phrase saves and the engine recognises it at once.
  await ev(`(()=>{
    document.getElementById('minivoice-clock').value = '换个皮肤';
    document.getElementById('clockvoicesave').click();
    return true;
  })()`);
  await sleep(900);
  const saved = await ev(`(()=>({
    status: document.getElementById('minivoicestatus-clock').textContent,
    value: document.getElementById('minivoice-clock').value
  }))()`);
  check("保存自定义说法后显示已保存且立即生效",
    saved.status.includes("已保存") && saved.status.includes("无需重启")
    && saved.value.includes("换个皮肤"), saved);
  const hit = await ev(`apiPost('/api/text', {text:'若琪，换个皮肤', request_id:'clock-e2e-1'})`);
  check("保存后自定义说法立刻被时钟技能接管",
    hit && hit.intent === "clock" && hit.switched, hit);
  const builtinHit = await ev(`apiPost('/api/text', {text:'换个表盘', request_id:'clock-e2e-2'})`);
  check("内置说法「换个表盘」同样接管", builtinHit && builtinHit.intent === "clock", builtinHit);
  const miss = await ev(`apiPost('/api/text', {text:'随便聊点别的', request_id:'clock-e2e-3'})`);
  check("无关句子不会被时钟技能误接管", miss && miss.intent !== "clock", miss);

  // 6. Reload: face and phrases come back.
  await send("Page.reload", { ignoreCache: true });
  await sleep(3200);
  await openMiniapps();
  const reloaded = await ev(`(()=>({
    hint: document.getElementById('clockfacehint').textContent,
    value: document.getElementById('minivoice-clock').value
  }))()`);
  check("重载页面后表盘提示与自定义说法都回填",
    reloaded.hint.includes("2×2 数字") && reloaded.value.includes("换个皮肤"),
    reloaded);

  // 7. Clearing the phrases restores built-in only.
  await ev(`(()=>{
    document.getElementById('minivoice-clock').value = '';
    document.getElementById('clockvoicesave').click();
    return true;
  })()`);
  await sleep(800);
  const cleared = await ev(`document.getElementById('minivoicestatus-clock').textContent`);
  check("留空保存后清空自定义说法", cleared.includes("已清空"), cleared);

  try { ws.close(); } catch (e) {}
  try { child.kill(); } catch (e) {}
  try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) {}
  const failed = checks.filter(c => !c.ok);
  console.log(failed.length ? "FAILED: " + failed.map(c => c.name).join(", ") : "all checks passed");
  process.exit(failed.length ? 1 : 0);
})().catch(e => { console.error("ERR", e.message); process.exit(1); });
