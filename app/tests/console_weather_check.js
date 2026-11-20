// 天气查询卡：真实浏览器里验证 开关翻转 / factory_owns 联动 / 自定义说法保存。
// Started by console_weather_check.py against a throwaway host.
// 注意：绝不点「查询天气」按钮——那会在音箱上拉起原厂卡片并播报（音频约束）。
const fs = require("fs"), os = require("os"), path = require("path"), { spawn } = require("child_process");
const PORT = Number(process.argv[2]), TOKEN = process.argv[3];
const EDGE = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", DBG = 9358;
const sleep = ms => new Promise(r => setTimeout(r, ms));
let id = 1; const pend = new Map();
(async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "edge-weather-"));
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
    await ev(`document.querySelector('[data-store-settings="weather"]').click()`);
  };

  await send("Page.enable"); await send("Runtime.enable");
  await send("Page.navigate", { url: "http://127.0.0.1:" + PORT + "/console?token=" + TOKEN });
  await sleep(3200);
  await openMiniapps();

  // 1. The card is on the tab, with all its moving parts.
  const surface = await ev(`(()=>{
    return {
      title: document.getElementById('panetitle').textContent,
      hasSwitch: !!document.getElementById('weatheroverride'),
      hasButton: !!document.getElementById('minweather'),
      hasFix: !!document.getElementById('weatherfix'),
      hasInput: !!document.getElementById('minivoice-weather'),
      hasSave: !!document.getElementById('weathervoicesave'),
      hasCity: !!document.getElementById('weathercity'),
      hasApiUrl: !!document.getElementById('weatherapiurl'),
      hasWttrUrl: !!document.getElementById('weatherwttrurl'),
      hasSettingsSave: !!document.getElementById('weathersettingsave'),
      builtin: document.getElementById('minibuiltin-weather').textContent,
      hint: document.getElementById('weatheroverridehint').textContent,
      checked: document.getElementById('weatheroverride').checked
    };
  })()`);
  check("天气卡在位：开关 / 查询按钮 / 修复按钮 / 自定义说法 / 城市 / 数据源 / 保存按钮",
    surface.hasSwitch && surface.hasButton && surface.hasFix && surface.hasInput
    && surface.hasSave && surface.hasCity && surface.hasApiUrl && surface.hasWttrUrl
    && surface.hasSettingsSave,
    surface);
  check("默认关闭：开关未勾选、提示为已关闭", !surface.checked && surface.hint.includes("已关闭"),
    surface);
  check("内置说法是关键词列表", surface.builtin.includes("天气") && surface.builtin.includes("关键词"),
    surface);

  // 2. OFF: the factory rule regains the weather words.
  const offRule = await ev(`apiGet('/api/status').then(s => s.direct.factory_owns)`);
  check("开关关闭时 factory_owns 运行时含天气词", /天气/.test(offRule) && /下雨/.test(offRule),
    offRule);

  // 3. Flip ON through the real switch; the rule must lose the words.
  await ev(`document.getElementById('weatheroverride').click()`);
  await sleep(800);
  const afterOn = await ev(`(()=>({
    checked: document.getElementById('weatheroverride').checked,
    hint: document.getElementById('weatheroverridehint').textContent
  }))()`);
  const onRule = await ev(`apiGet('/api/status').then(s => s.direct.factory_owns)`);
  check("点开关后翻转为已开启", afterOn.checked && afterOn.hint.includes("已开启"), afterOn);
  check("开关开启时 factory_owns 不再含天气词", !/天气/.test(onRule) && !/下雨/.test(onRule),
    onRule);

  // 4. A custom phrase (no builtin keyword) saves and recognises at once.
  await ev(`(()=>{
    document.getElementById('minivoice-weather').value = '外面啥情况';
    document.getElementById('weathervoicesave').click();
    return true;
  })()`);
  await sleep(900);
  const saved = await ev(`(()=>({
    status: document.getElementById('minivoicestatus-weather').textContent,
    value: document.getElementById('minivoice-weather').value
  }))()`);
  check("保存自定义说法后显示已保存且立即生效",
    saved.status.includes("已保存") && saved.status.includes("无需重启") &&
    saved.value.includes("外面啥情况"), saved);
  const hit = await ev(`apiPost('/api/text', {text:'若琪，外面啥情况', request_id:'weather-e2e-1'})`);
  check("保存后自定义说法立刻被天气技能接管",
    hit && hit.intent === "weather" && hit.launched, hit);
  const miss = await ev(`apiPost('/api/text', {text:'随便聊点别的', request_id:'weather-e2e-2'})`);
  check("无关句子不会被天气技能误接管", miss && miss.intent !== "weather", miss);

  // 4b. The city + source URLs save and echo back.
  await ev(`(()=>{
    document.getElementById('weathercity').value = '北京市';
    document.getElementById('weatherapiurl').value = 'https://mirror.example/v1';
    document.getElementById('weatherwttrurl').value = '';
    document.getElementById('weathersettingsave').click();
    return true;
  })()`);
  await sleep(900);
  const settings = await ev(`(()=>({
    status: document.getElementById('weathersettingsstatus').textContent,
    city: document.getElementById('weathercity').value,
    url: document.getElementById('weatherapiurl').value
  }))()`);
  check("保存城市与数据源后状态显示已生效",
    settings.status.includes("北京市") && settings.status.includes("已保存")
    && settings.city === "北京市" && settings.url === "https://mirror.example/v1",
    settings);
  const apiState = await ev(`apiGet('/api/weather')`);
  check("主机配置里城市与主源 URL 已持久化",
    apiState.city === "北京市"
    && apiState.api_url === "https://mirror.example/v1"
    && apiState.wttr_url === "",
    apiState);
  const badUrl = await ev(`apiPost('/api/weather/settings', {api_url: 'mirror.example/v1'})
    .then(() => 'accepted').catch(e => 'rejected:' + e.message)`);
  check("不带 http(s) 的地址被拒绝",
    String(badUrl).startsWith("rejected"), badUrl);

  // 5. Reload: the switch, the phrase and the settings come back.
  await send("Page.reload", { ignoreCache: true });
  await sleep(3200);
  await openMiniapps();
  const reloaded = await ev(`(()=>({
    checked: document.getElementById('weatheroverride').checked,
    value: document.getElementById('minivoice-weather').value,
    city: document.getElementById('weathercity').value,
    url: document.getElementById('weatherapiurl').value
  }))()`);
  check("重载页面后开关 / 自定义说法 / 城市 / 数据源都回填",
    reloaded.checked && reloaded.value.includes("外面啥情况")
    && reloaded.city === "北京市"
    && reloaded.url === "https://mirror.example/v1",
    reloaded);

  // 5b. Clearing the settings follows the device / defaults again.
  await ev(`(()=>{
    document.getElementById('weathercity').value = '';
    document.getElementById('weatherapiurl').value = '';
    document.getElementById('weathersettingsave').click();
    return true;
  })()`);
  await sleep(800);
  const cleared = await ev(`(()=>{
    const st = document.getElementById('weathersettingsstatus').textContent;
    return {status: st, city: document.getElementById('weathercity').value};
  })()`);
  check("留空保存后城市恢复跟随设备",
    cleared.city === "" && cleared.status.includes("跟随设备"), cleared);

  // 6. Flip back OFF: reversible, and the rule regains the words.
  await ev(`document.getElementById('weatheroverride').click()`);
  await sleep(800);
  const backRule = await ev(`apiGet('/api/status').then(s => s.direct.factory_owns)`);
  const backOff = await ev(`document.getElementById('weatheroverride').checked`);
  check("翻回关闭后 factory_owns 恢复含天气词", !backOff && /天气/.test(backRule) && /下雨/.test(backRule),
    backRule);

  try { ws.close(); } catch (e) {}
  try { child.kill(); } catch (e) {}
  try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) {}
  const failed = checks.filter(c => !c.ok);
  console.log(failed.length ? "FAILED: " + failed.map(c => c.name).join(", ") : "all checks passed");
  process.exit(failed.length ? 1 : 0);
})().catch(e => { console.error("ERR", e.message); process.exit(1); });
