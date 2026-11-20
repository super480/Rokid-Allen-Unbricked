// The console's wake-word settings page, checked in a real browser.
// Started by console_wake_check.py, which runs its own temporary host.
const fs = require("fs"), os = require("os"), path = require("path"), { spawn } = require("child_process");
const PORT = Number(process.argv[2]), TOKEN = process.argv[3], OUT = process.argv[4];
const EDGE = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", DBG = 9351;
const sleep = ms => new Promise(r => setTimeout(r, ms));
let id = 1; const pend = new Map();
(async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "edge-wake-"));
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
  await send("Page.enable"); await send("Runtime.enable");
  await send("Page.navigate", { url: "http://127.0.0.1:" + PORT + "/console?token=" + TOKEN });
  await sleep(3500);

  const checks = [];
  const check = (name, ok, detail) => { checks.push({ name, ok, detail }); console.log((ok ? "  ok  " : "  FAIL") + " " + name + (ok ? "" : " -> " + JSON.stringify(detail))); };

  // Open the settings dialog and switch to the wake-word tab.
  await ev(`document.getElementById('btnsettings').click()`);
  await sleep(300);
  await ev(`[...document.querySelectorAll('#settingstabs .nav-item')].find(b=>b.dataset.tab==='wake').click()`);
  await sleep(1000);

  const initial = await ev(`(()=>{
    const hint = document.querySelector('#tab-wake .hintline');
    return {
      title: document.getElementById('panetitle').textContent,
      hasTab: !!document.getElementById('tab-wake'),
      master: document.getElementById('wakeenable').checked,
      cardHidden: document.getElementById('wakecard').style.display === 'none',
      hint: hint ? hint.innerText : '',
      saveHidden: document.getElementById('settingssave').style.display === 'none'
    };
  })()`);
  check("唤醒词分类出现在设置导航里", initial.hasTab && initial.title === "唤醒词", initial);
  check("总开关默认关闭，各行默认隐藏", initial.master === false && initial.cardHidden, initial);
  check("说明里提示带声调拼音与示例", initial.hint.includes("带声调") && initial.hint.includes("da4fei2yu2"), initial.hint);
  check("该分类不显示底部统一保存按钮", initial.saveHidden, initial);

  // Turn the master switch on: the rows must appear with the agreed words.
  await ev(`(()=>{const s=document.getElementById('wakeenable'); s.checked=true; s.dispatchEvent(new Event('change'));})()`);
  await sleep(1000);
  const rows = await ev(`(()=>({
    cardHidden: document.getElementById('wakecard').style.display === 'none',
    rows: [...document.querySelectorAll('#wakerows .row')].map(r=>({
      name: r.querySelector('.t').textContent,
      value: r.querySelector('input[type=text]').value,
      checked: r.querySelector('input[type=checkbox]').checked,
      hasSave: !!r.querySelector('button') && r.querySelector('button').textContent === '保存'
    }))
  }))()`);
  check("打开总开关后 7 个助手各一行", !rows.cardHidden && rows.rows.length === 7, rows);
  const wanted = { "大肥鱼": "da4fei2yu2", "巴蒂": "ba1di4", "GPT": "ji4pi4ti4", "小赫": "xiao3he4", "猫娘": "mao1niang2", "小家": "xiao3jia1", "克劳德": "ke4lao2de3" };
  for (const [name, value] of Object.entries(wanted)) {
    const row = rows.rows.find(r => r.name === name);
    check(name + " 默认唤醒词 " + value, !!row && row.value === value && row.checked && row.hasSave, row);
  }

  // Type a new word and press this row's save button.
  await ev(`(()=>{
    const r=[...document.querySelectorAll('#wakerows .row')].find(x=>x.querySelector('.t').textContent==='大肥鱼');
    const i=r.querySelector('input[type=text]'); i.value='xiao3yu2'; i.dispatchEvent(new Event('input'));
    r.querySelector('button').click();
  })()`);
  await sleep(900);
  const saved = await ev(`(()=>{
    const r=[...document.querySelectorAll('#wakerows .row')].find(x=>x.querySelector('.t').textContent==='大肥鱼');
    return r ? r.querySelector('input[type=text]').value : '(missing)';
  })()`);
  check("改拼音点保存后生效", saved === "xiao3yu2", saved);
  const apiValue = await ev(`fetch('/api/wake',{headers:{Authorization:'Bearer '+TOKEN}}).then(r=>r.json()).then(d=>d.agents.dsh.pinyin)`);
  check("保存结果到达主机接口", apiValue === "xiao3yu2", apiValue);

  // A reload must keep the word: the config was written to disk.
  await send("Page.reload");
  await sleep(3200);
  await ev(`document.getElementById('btnsettings').click()`);
  await sleep(300);
  await ev(`[...document.querySelectorAll('#settingstabs .nav-item')].find(b=>b.dataset.tab==='wake').click()`);
  await sleep(900);
  const afterReload = await ev(`(()=>{
    const r=[...document.querySelectorAll('#wakerows .row')].find(x=>x.querySelector('.t').textContent==='大肥鱼');
    return { master: document.getElementById('wakeenable').checked, value: r ? r.querySelector('input[type=text]').value : '(missing)' };
  })()`);
  check("刷新页面后设置仍在", afterReload.master === true && afterReload.value === "xiao3yu2", afterReload);

  // Batch alias editor: one card, one save button for every assistant.
  const aliasUI = await ev(`(()=>({
    card: !!document.getElementById('wakealiascard'),
    rows: [...document.querySelectorAll('#wakealiasrows input[data-agent]')].map(i => i.dataset.agent),
    hasSave: !!document.getElementById('wakealiasesave'),
    hint: document.getElementById('wakealiascard').textContent
  }))()`);
  check("唤醒词页有全助手别名卡片与统一保存按钮",
    aliasUI.card && aliasUI.hasSave && aliasUI.rows.length === 7 &&
    aliasUI.rows.includes('xiaojia') && aliasUI.rows.includes('claude') &&
    aliasUI.hint.includes('不需要重启') && aliasUI.hint.includes('activ fake'), aliasUI);

  // 克劳德 must appear in the local-CLI auto-detection card with the
  // agreed default pinyin, so a one-click add works like every other agent.
  const claudeScan = await ev(`fetch('/api/wake/scan',{headers:{Authorization:'Bearer '+TOKEN}}).then(r=>r.json()).then(d=>d.claude||null)`);
  check("自动检测列出克劳德并建议 ke4lao2de3",
    !!claudeScan && claudeScan.name === '克劳德' &&
    claudeScan.suggested === 'ke4lao2de3', claudeScan);
  const claudeDetectText = await ev(`(document.getElementById('wakedetectcard')||{}).textContent||''`);
  check("本机检测卡片显示克劳德", claudeDetectText.includes('克劳德'), claudeDetectText.slice(0,200));

  // 小家连的是局域网 HA，不是本机命令行：唤醒词页可按 IP 自定义连接。
  const haUI = await ev(`(()=>{
    const c = document.getElementById('wakehacard');
    return {
      card: !!c,
      text: c ? c.textContent : '',
      host: !!document.getElementById('wakehahost'),
      token: !!document.getElementById('wakehatoken'),
      save: !!document.getElementById('wakehasave'),
      probe: !!document.getElementById('wakehatest'),
      detect: (document.getElementById('wakedetectcard')||{}).textContent || ''
    };
  })()`);
  check("唤醒词页有小家局域网 HA 连接卡片（IP/令牌/保存/检测）",
    haUI.card && haUI.host && haUI.token && haUI.save && haUI.probe, haUI);
  check("卡片说明按 IP 自定义、小家检测局域网 HA",
    haUI.text.includes('192.0.2.4:8123') && haUI.text.includes('局域网')
    && haUI.detect.includes('小家是例外'), haUI);
  const ipUrl = await ev(`wakeHaUrl('192.168.1.77:8123')`);
  check("纯 IP 输入自动补 http:// 前缀",
    ipUrl === 'http://192.168.1.77:8123', ipUrl);
  await ev(`document.getElementById('wakehatest').click()`);
  await sleep(800);
  const probeStatus = await ev(`document.getElementById('wakehastatus').textContent`);
  check("仅检测按钮会刷新连接状态（不写配置）",
    probeStatus.includes('状态：') && !probeStatus.includes('检测中'), probeStatus);

  // 保存链路：换 IP 后接口立刻拿到新地址；用本地拒绝端口避免等网络超时，
  // 测完把原配置写回去，不影响后面几组浏览器用例。
  const haBefore = await ev(`fetch('/api/homeassistant',{headers:{Authorization:'Bearer '+TOKEN}}).then(r=>r.json()).then(d=>d.settings.base_url)`);
  await ev(`(()=>{
    const i=document.getElementById('wakehahost');
    i.value='127.0.0.1:1'; i.dispatchEvent(new Event('input'));
    document.getElementById('wakehasave').click();
  })()`);
  await sleep(1400);
  const haAfter = await ev(`fetch('/api/homeassistant',{headers:{Authorization:'Bearer '+TOKEN}}).then(r=>r.json()).then(d=>d.settings.base_url)`);
  check("保存并检测把自定义 IP 写进 HA 配置",
    haAfter === 'http://127.0.0.1:1', {before: haBefore, after: haAfter});
  await ev(`fetch('/api/homeassistant/settings',{method:'POST',headers:{Authorization:'Bearer '+TOKEN,'Content-Type':'application/json'},body:JSON.stringify({base_url:${JSON.stringify(haBefore)},keep_token:true})}).then(r=>r.json())`);
  const haRestored = await ev(`fetch('/api/homeassistant',{headers:{Authorization:'Bearer '+TOKEN}}).then(r=>r.json()).then(d=>d.settings.base_url)`);
  check("自定义 IP 用例结束后恢复原 HA 地址",
    haRestored === haBefore, {before: haBefore, after: haRestored});
  await ev(`(()=>{
    const i=[...document.querySelectorAll('#wakealiasrows input[data-agent]')].find(x=>x.dataset.agent==='workbuddy');
    i.value='八八|八弟|测试别名'; i.dispatchEvent(new Event('input'));
    document.getElementById('wakealiasesave').click();
  })()`);
  await sleep(900);
  const aliasApi = await ev(`fetch('/api/wake',{headers:{Authorization:'Bearer '+TOKEN}}).then(r=>r.json()).then(d=>d.agents.workbuddy.aliases)`);
  check("一次保存把所有别名写到主机接口",
    Array.isArray(aliasApi) && aliasApi.includes('测试别名'), aliasApi);
  const aliasStatus = await ev(`document.getElementById('wakealiasstatus').textContent`);
  check("保存后明确提示无需重启", aliasStatus.includes('无需重启'), aliasStatus);

  // Proactive-chat settings for the fifth assistant.
  await ev(`[...document.querySelectorAll('#settingstabs .nav-item')].find(b=>b.dataset.tab==='neko').click()`);
  await sleep(900);
  const neko = await ev(`(()=>({
    hasTab: !!document.getElementById('tab-neko'),
    on: document.getElementById('tab-neko').classList.contains('on'),
    title: document.getElementById('panetitle').textContent,
    enabled: document.getElementById('nekoenable').checked,
    interval: document.getElementById('nekointerval').value,
    quietStart: document.getElementById('nekoquietstart').value,
    quietEnd: document.getElementById('nekoquietend').value,
    hasNow: !!document.getElementById('nekonow'),
    hasFresh: !!document.getElementById('nekofresh'),
    freshLabel: (document.getElementById('nekofresh') || {}).textContent || '',
    hasSave: !!document.getElementById('nekosave'),
    hasEarOnly: !!document.getElementById('nekoearonly'),
    status: document.getElementById('nekostatus').textContent,
    saveHidden: document.getElementById('settingssave').style.display === 'none',
    tabs: [...document.querySelectorAll('#settingstabs .nav-item')].map(b => b.dataset.tab)
  }))()`);
  check("猫娘分类出现在设置导航里", neko.hasTab && neko.on && neko.title.includes("主动说话"), neko);
  check("WorkBuddy 授权排在第二位", neko.tabs[1] === "auth", neko.tabs);
  check("默认开启 / 10 分钟 / 静默 00:00-11:00",
    neko.enabled === true && neko.interval === "10" && neko.quietStart === "00:00" && neko.quietEnd === "11:00", neko);
  check("有「保存」与「立即说一句」且不显示统一保存",
    neko.hasNow && neko.hasSave && neko.saveHidden && neko.status === "设置已保存", neko);
  check("重播之外还有「生成新话」入口",
    neko.hasFresh && neko.freshLabel === "说句新的", neko);
  check("猫娘页已移除耳朵模式开关（后端端点保留）",
    neko.hasEarOnly === false, neko);

  await ev(`(()=>{const i=document.getElementById('nekointerval'); i.value='7'; i.dispatchEvent(new Event('change'));})()`);
  await sleep(700);
  const beforeSave = await ev(`fetch('/api/neko/proactive',{headers:{Authorization:'Bearer '+TOKEN}}).then(r=>r.json()).then(d=>d.interval_minutes)`);
  check("改间隔后未点保存不写配置", beforeSave === 10, beforeSave);
  const dirty = await ev(`document.getElementById('nekostatus').textContent`);
  check("未保存状态有可见提示", dirty === "有未保存的改动", dirty);
  await ev(`document.getElementById('nekosave').click()`);
  await sleep(900);
  const afterSave = await ev(`fetch('/api/neko/proactive',{headers:{Authorization:'Bearer '+TOKEN}}).then(r=>r.json()).then(d=>d.interval_minutes)`);
  check("点保存后间隔到达主机接口", afterSave === 7, afterSave);
  const clean = await ev(`document.getElementById('nekostatus').textContent`);
  check("保存后状态回到已保存", clean === "设置已保存", clean);

  // S3: the music panel. Only DOM and read-only endpoints are exercised;
  // no clip is queued on the real speaker from this test host.
  await ev(`[...document.querySelectorAll('#settingstabs .nav-item')].find(b=>b.dataset.tab==='music').click()`);
  await sleep(900);
  const music = await ev(`(()=>({
    hasTab: !!document.getElementById('tab-music'),
    on: document.getElementById('tab-music').classList.contains('on'),
    title: document.getElementById('panetitle').textContent,
    tabs: [...document.querySelectorAll('#settingstabs .nav-item')].map(b=>b.dataset.tab),
    hasTest: !!document.getElementById('musictest'),
    hasStop: !!document.getElementById('musicstop'),
    hasSearch: !!document.getElementById('musicsearch'),
    hasFolders: !!document.getElementById('musicfolders'),
    hasFolderInput: !!document.getElementById('musicfolderinput'),
    hasFolderSave: !!document.getElementById('musicfolderssave'),
    hasReindex: !!document.getElementById('musicreindex'),
    hasShuffle: !!document.getElementById('musicshuffle'),
    hasNext: !!document.getElementById('musicnext'),
    hasPrev: !!document.getElementById('musicprev'),
    hasPause: !!document.getElementById('musicpause'),
    hasParseInput: !!document.getElementById('musicparseinput'),
    hasParseButton: !!document.getElementById('musicparsebtn'),
    hasVolUp: !!document.getElementById('musicvolup'),
    hasVolDown: !!document.getElementById('musicvoldown'),
    hasVolMute: !!document.getElementById('musicvolmute'),
    hasPick: !!document.getElementById('musicfolderpick'),
    pickLabel: (document.getElementById('musicfolderpick') || {}).textContent || '',
    hasPicker: !!document.getElementById('folderpicker'),
    hasPickerList: !!document.getElementById('folderlist'),
    hasPickerNative: !!document.getElementById('foldernative'),
    hasPickerChoose: !!document.getElementById('folderchoose'),
    hasNetease: !!document.getElementById('neteasestatus'),
    hasNeteaseCookie: !!document.getElementById('neteasecookie'),
    hasNeteaseLogin: !!document.getElementById('neteaselogin'),
    hasNeteaseLogout: !!document.getElementById('neteaselogout'),
    hasQQStatus: !!document.getElementById('qqmusicstatus'),
    hasQQCookie: !!document.getElementById('qqmusiccookie'),
    hasQQLogin: !!document.getElementById('qqmusiclogin'),
    hasQQLogout: !!document.getElementById('qqmusiclogout'),
    hasQQSearch: !!document.getElementById('qqmusicsearch'),
    qqTitle: [...document.querySelectorAll('#tab-music .card-title')]
      .map(x => x.textContent).join(' | '),
    hasTranscodeHelp: !!document.getElementById('transcodehelp'),
    transcodeHidden: document.getElementById('musictranscodehint').style.display === 'none',
    hasRoonPower: !!document.getElementById('roonpower'),
    hasRoonStatus: !!document.getElementById('roonstatus'),
    hasRoonZone: !!document.getElementById('roonzone'),
    hasRoonTarget: !!document.getElementById('roontarget'),
    hasRoonPlay: !!document.getElementById('roonplay'),
    hasRoonRefresh: !!document.getElementById('roonrefresh'),
    hasRoonLyricsRoot: !!document.getElementById('roonlyricsroot'),
    hasRoonLyricsPick: !!document.getElementById('roonlyricspick'),
    hasRoonLyricsSave: !!document.getElementById('roonlyricssave'),
    hasLocalSearch: !!document.getElementById('musiclocalquery'),
    hasLocalButton: !!document.getElementById('musiclocalsearch'),
    hasLocalResults: !!document.getElementById('musiclocalresults'),
    neteaseTitle: [...document.querySelectorAll('#tab-music .card-title')]
      .map(x => x.textContent).join(' | '),
    folderHint: [...document.querySelectorAll('#tab-music .hintline')]
      .map(x => x.textContent).join(' | '),
    transcodeHint: (document.getElementById('musictranscodehint') || {}).textContent || '',
    noLibraryList: !document.getElementById('musiclibrary'),
    saveHidden: document.getElementById('settingssave').style.display === 'none'
  }))()`);
  check("音乐分类出现在设置导航里且不影响授权排第二",
    music.hasTab && music.on && music.title.includes("音乐") &&
    music.tabs[1] === "auth" && music.tabs.indexOf("music") ===
      music.tabs.indexOf("neko") + 1, music);
  check("试音 / 停止 / 搜索三个入口齐备",
    music.hasTest && music.hasStop && music.hasSearch, music);
  check("只管多个文件夹路径，不列曲目",
    music.hasFolders && music.hasFolderInput && music.hasFolderSave &&
    music.hasReindex && music.noLibraryList && music.saveHidden, music);
  check("猫娘随机连播有开关与换一首", music.hasShuffle && music.hasNext, music);
  check("播放控制四个按钮齐备并说明语音控制",
    music.hasPrev && music.hasPause && music.hasNext && music.hasStop &&
    music.folderHint.includes('暂停') && music.folderHint.includes('已暂停'), music);
  check("音量控制按钮齐备且说明语音说法",
    music.hasVolUp && music.hasVolDown && music.hasVolMute &&
    music.folderHint.includes('调大音量') && music.folderHint.includes('静音'), music);
  check("语音点歌卡片写明本地优先并有解析自测框",
    music.hasParseInput && music.hasParseButton &&
    music.folderHint.includes('我想听晴天') &&
    music.folderHint.includes('优先本地曲库'), music);
  const parsed = await ev(`fetch('/api/music/parse',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+TOKEN},body:JSON.stringify({text:'我想听周杰伦的晴天'})}).then(r=>r.json())`);
  check("语音点歌解析只读：抽得出歌手与歌名且不排队音频",
    parsed.ok && parsed.parsed && parsed.parsed.artist === '周杰伦' &&
    parsed.parsed.title === '晴天', parsed);
  const folders = await ev(`fetch('/api/music/folders',{headers:{Authorization:'Bearer '+TOKEN}}).then(r=>r.json()).then(d=>({list:d.folders,index:d.index,shuffle:d.shuffle}))`);
  check("文件夹接口返回列表与索引状态",
    Array.isArray(folders.list) && folders.list.length >= 1 &&
    folders.index && typeof folders.index.count === 'number' &&
    typeof folders.shuffle === 'boolean', folders);
  check("文件夹卡片说明 5 层扫描深度", music.folderHint.includes('5 层'), music.folderHint);
  check("文件夹卡片写明转码原因/判定标准/原始文件只读",
    music.transcodeHint.includes('Android 4.4') &&
    music.transcodeHint.includes('16-bit') &&
    music.transcodeHint.includes('24 MB') &&
    music.transcodeHint.includes('44.1 kHz') &&
    music.transcodeHint.includes('原始文件') &&
    music.transcodeHint.includes('只读'), music.transcodeHint);
  check("转码说明默认隐藏且点「?」才展开",
    music.hasTranscodeHelp && music.transcodeHidden, music);
  const toggled = await ev(`(()=>{document.getElementById('transcodehelp').click(); const v=document.getElementById('musictranscodehint').style.display; document.getElementById('transcodehelp').click(); return v;})()`);
  check("点「?」后转码说明显示，再点收起", toggled === 'block', toggled);
  check("添加按钮前有「选择…」按钮与目录选择器",
    music.hasPick && music.pickLabel.includes('选择') && music.hasPicker &&
    music.hasPickerList && music.hasPickerNative && music.hasPickerChoose, music);
  check("网易云音乐单独成卡片并可登录",
    music.neteaseTitle.includes('网易云音乐') && music.hasNetease &&
    music.hasNeteaseCookie && music.hasNeteaseLogin && music.hasNeteaseLogout,
    music.neteaseTitle);
  check("QQ 音乐单独成卡片并可登录",
    music.qqTitle.includes('QQ 音乐') && music.hasQQStatus &&
    music.hasQQCookie && music.hasQQLogin && music.hasQQLogout,
    music.qqTitle);
  check("Roon 元素存在（已拆到独立页签，不阻塞音乐页）",
    music.hasRoonPower && music.hasRoonStatus && music.hasRoonZone &&
    music.hasRoonTarget && music.hasRoonPlay && music.hasRoonRefresh &&
    music.hasRoonLyricsRoot && music.hasRoonLyricsPick &&
    music.hasRoonLyricsSave, music);
  const roon = await ev(`fetch('/api/roon/status',{headers:{Authorization:'Bearer '+TOKEN}}).then(r=>r.json())`);
  check("Roon 状态接口可读且带开关与目标字段",
    roon && typeof roon.online === 'boolean' &&
    typeof roon.stage === 'string' && typeof roon.target === 'string' &&
    typeof roon.enabled === 'boolean', roon);
  // 「外观」已改名「其它」，这一页还放着只管到期提示的原厂语音开关。
  await ev(`[...document.querySelectorAll('#settingstabs .nav-item')].find(b=>b.dataset.tab==='appearance').click()`);
  await sleep(900);
  const miscUI = await ev(`(()=>({
    nav: [...document.querySelectorAll('#settingstabs .nav-item')].find(b=>b.dataset.tab==='appearance').textContent,
    title: document.getElementById('panetitle').textContent,
    on: document.getElementById('tab-appearance').classList.contains('on'),
    sw: !!document.getElementById('factorynoticemute'),
    checked: document.getElementById('factorynoticemute').checked,
    hint: [...document.querySelectorAll('#tab-appearance .hintline')].map(x=>x.textContent).join(' | ')
  }))()`);
  check("外观分类已改名为「其它」",
    miscUI.nav === '其它' && miscUI.title === '其它' && miscUI.on, miscUI);
  check("其它页有只遮蔽到期提示的开关",
    miscUI.sw && miscUI.checked && miscUI.hint.includes('只遮蔽到期提示') &&
    miscUI.hint.includes('不会屏蔽闹钟'), miscUI);
  // Roon 现在是一个独立页签，位于「音乐」与「其它」之间。
  await ev(`[...document.querySelectorAll('#settingstabs .nav-item')].find(b=>b.dataset.tab==='roon').click()`);
  await sleep(900);
  const roonUI = await ev(`(()=>({
    hasTab: !!document.getElementById('tab-roon'),
    on: document.getElementById('tab-roon').classList.contains('on'),
    title: document.getElementById('panetitle').textContent,
    tabs: [...document.querySelectorAll('#settingstabs .nav-item')].map(b=>b.dataset.tab),
    roonIdx: [...document.querySelectorAll('#settingstabs .nav-item')].map(b=>b.dataset.tab).indexOf('roon'),    musicIdx: [...document.querySelectorAll('#settingstabs .nav-item')].map(b=>b.dataset.tab).indexOf('music'),
    appearanceIdx: [...document.querySelectorAll('#settingstabs .nav-item')].map(b=>b.dataset.tab).indexOf('appearance'),
    hint: [...document.querySelectorAll('#tab-roon .hintline')].map(x=>x.textContent).join(' | ')
  }))()`);
  check("Roon 独立分类出现在设置导航里",
    roonUI.hasTab && roonUI.on && roonUI.title.includes("Roon"), roonUI);
  check("Roon 页签紧跟音乐、其后是家居与其它",
    roonUI.roonIdx === roonUI.musicIdx + 1 &&
    roonUI.tabs[roonUI.roonIdx + 1] === 'home' &&
    roonUI.appearanceIdx === roonUI.roonIdx + 2, roonUI.tabs);
  check("Roon 页签写明 AirPlay / 桥名 / 关掉说明 / 内嵌歌词",
    roonUI.hint.includes('AirPlay') &&
    roonUI.hint.includes('Rokid Allen Bridge') &&
    roonUI.hint.includes('没有 Roon 时关掉') &&
    roonUI.hint.includes('读它内嵌的 LRC'), roonUI.hint);

  // 智能家居（Home Assistant）页签：连接设置 + 门控额外词，只读。
  await ev(`[...document.querySelectorAll('#settingstabs .nav-item')].find(b=>b.dataset.tab==='home').click()`);
  await sleep(700);
  const homeUI = await ev(`(()=>({
    on: document.getElementById('tab-home').classList.contains('on'),
    title: document.getElementById('panetitle').textContent,
    enabled: !!document.getElementById('haenabled'),
    base: !!document.getElementById('habase'),
    token: !!document.getElementById('hatoken'),
    test: !!document.getElementById('hatest'),
    gateExtra: !!document.getElementById('hagateextra')
  }))()`);
  check("家居分类出现在设置导航里",
    homeUI.on && homeUI.title.includes("家居"), homeUI);
  check("家居页有连接开关/地址/令牌/测试",
    homeUI.enabled && homeUI.base && homeUI.token && homeUI.test, homeUI);
  check("家居页有门控额外词输入",
    homeUI.gateExtra, homeUI);

  // The picker really browses this computer one level at a time.
  await ev(`document.getElementById('musicfolderpick').click()`);
  await sleep(900);
  const picked = await ev(`(()=>({
    open: document.getElementById('folderpicker').classList.contains('open'),
    crumb: document.getElementById('foldercrumb').textContent,
    rows: [...document.querySelectorAll('#folderlist [data-folder-dir]')].length,
    shortcuts: document.querySelectorAll('#foldershortcuts button').length,
    hint: document.getElementById('folderhint').textContent
  }))()`);
  check("选择弹窗打开并列出本机目录",
    picked.open && picked.crumb.startsWith('/') && picked.rows > 0 &&
    picked.shortcuts > 0, picked);
  check("目录弹窗显示扫描深度", picked.hint.includes('5 层'), picked.hint);
  const beforeCrumb = picked.crumb;
  if (picked.rows > 0) {
    await ev(`document.querySelector('#folderlist [data-folder-dir]').click()`);
    await sleep(700);
    const deeper = await ev(`document.getElementById('foldercrumb').textContent`);
    check("点子文件夹进入下一层", deeper !== beforeCrumb && deeper.startsWith(beforeCrumb),
      { before: beforeCrumb, after: deeper });
    await ev(`document.getElementById('folderup').click()`);
    await sleep(700);
    const back = await ev(`document.getElementById('foldercrumb').textContent`);
    check("上一级能退回原目录", back === beforeCrumb, back);
  }
  await ev(`document.getElementById('folderchoose').click()`);
  await sleep(300);
  const added = await ev(`(()=>({
    closed: !document.getElementById('folderpicker').classList.contains('open'),
    rows: document.querySelectorAll('#musicfolders [data-folder-index]').length,
    status: document.getElementById('musicfolderstatus').textContent
  }))()`);
  check("选中的文件夹进入待保存列表",
    added.closed && added.rows >= 2 && added.status.includes('保存文件夹'), added);
  const netease = await ev(`fetch('/api/music/netease',{headers:{Authorization:'Bearer '+TOKEN}}).then(r=>r.json())`);
  check("网易云账号状态接口可读",
    typeof netease.logged_in === 'boolean' && !!netease.credential_page, netease);
  check("本地曲库有独立搜索框",
    music.hasLocalSearch && music.hasLocalButton && music.hasLocalResults, music);
  const localApi = await ev(`fetch('/api/music/search-local?q=zzzz-no-match',{headers:{Authorization:'Bearer '+TOKEN}}).then(r=>r.json())`);
  check("本地搜索接口只返回有界的一页",
    Array.isArray(localApi.items) && typeof localApi.total === 'number' &&
    localApi.items.length === 0, localApi);
  await ev(`(()=>{document.getElementById('musiclocalquery').value='zzzz-no-match'; document.getElementById('musiclocalsearch').click();})()`);
  await sleep(700);
  const localUi = await ev(`(()=>({
    hint: document.getElementById('musiclocalhint').textContent,
    rows: document.querySelectorAll('#musiclocalresults [data-music-index]').length
  }))()`);
  check("本地搜索框可输入且不把整个曲库铺出来",
    localUi.rows === 0 && localUi.hint.includes('匹配'), localUi);

  // 中文输入法按回车确认候选只上屏，不能把半句话发给助手。
  // 先把 submit 换成计数桩，避免真发消息；换不掉就直接判失败，不误发。
  const fixture = await ev(`(()=>{
    const orig = window.submit;
    if (typeof orig !== 'function') return false;
    window.__origSubmit = orig; window.__sent = 0;
    window.submit = () => { window.__sent++; };
    const ok = window.submit !== orig;
    if (!ok) window.submit = orig;
    return ok;
  })()`);
  check("输入框测试夹具可替换 submit", fixture, fixture);
  const ime = await ev(`(()=>{
    const ta = document.getElementById('input');
    ta.value = 'hello';
    ta.dispatchEvent(new CompositionEvent('compositionstart', {bubbles:true}));
    const a = new KeyboardEvent('keydown', {key:'Enter', bubbles:true, cancelable:true});
    ta.dispatchEvent(a);
    const composing = {sent: window.__sent, prevented: a.defaultPrevented, value: ta.value};
    const b = new KeyboardEvent('keydown', {key:'Enter', isComposing:true, bubbles:true, cancelable:true});
    ta.dispatchEvent(b);
    const flagged = {sent: window.__sent, prevented: b.defaultPrevented, value: ta.value};
    ta.dispatchEvent(new CompositionEvent('compositionend', {bubbles:true}));
    return {composing, flagged};
  })()`);
  check("输入法组合中的回车只上屏、不发送",
    ime.composing.sent === 0 && !ime.composing.prevented &&
    ime.flagged.sent === 0 && !ime.flagged.prevented &&
    ime.flagged.value === 'hello', ime);
  await sleep(60);
  const keys = await ev(`(()=>{
    const ta = document.getElementById('input');
    const fire = init => {
      const e = new KeyboardEvent('keydown',
        Object.assign({key:'Enter', bubbles:true, cancelable:true}, init));
      ta.dispatchEvent(e);
      return window.__sent;
    };
    const plain = fire({});
    const accel = fire({metaKey:true});
    const ctrl = fire({ctrlKey:true});
    const shift = fire({shiftKey:true});
    const out = {plain, accel, ctrl, shift};
    ta.value = ''; window.submit = window.__origSubmit;
    return out;
  })()`);
  check("Enter / ⌘Enter / Ctrl+Enter 发送，Shift+Enter 换行",
    keys.plain === 1 && keys.accel === 2 && keys.ctrl === 3 &&
    keys.shift === 3, keys);

  if (OUT) {
    const shot = await send("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(OUT, Buffer.from(shot.data, "base64"));
  }
  try { ws.close(); } catch (e) {}
  try { child.kill(); } catch (e) {}
  try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) {}
  const failed = checks.filter(c => !c.ok);
  console.log(failed.length ? "FAILED: " + failed.map(c => c.name).join(", ") : "all checks passed");
  process.exit(failed.length ? 1 : 0);
})().catch(e => { console.error("ERR", e.message); process.exit(1); });
