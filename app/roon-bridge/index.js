#!/usr/bin/env node
"use strict";
/*
 * Roon extension + local HTTP bridge for the Rokid Allen speaker.
 *
 * Roon 2.x speaks MOO over ws://<core>:9330/api. The extension asks for the
 * transport and image services; Roon shows it in Settings -> Extensions and
 * waits for the user to press Enable. Until then the socket stays open and
 * /status reports "waiting_authorisation".
 *
 * Node 22 ships a global WebSocket that hands binary frames back as Blobs,
 * which the Roon MOO parser cannot read. Force the `ws` polyfill BEFORE the
 * Roon library is required, so its transport sees Buffers instead.
 */
global.WebSocket = require("ws");

const fs = require("fs");
const http = require("http");
const path = require("path");
const RoonApi = require("node-roon-api");
const RoonApiTransport = require("node-roon-api-transport");
const RoonApiImage = require("node-roon-api-image");
const zonesLib = require("./zones");

const ROOT = __dirname;
const STATE_DIR = process.env.ROON_STATE_DIR ||
    path.join(ROOT, "..", "runtime", "roon");
fs.mkdirSync(STATE_DIR, { recursive: true });
// node-roon-api persists its pairing token in ./config.json, so keep the
// working directory inside runtime/ (never version controlled).
process.chdir(STATE_DIR);

const SETTINGS_FILE = path.join(STATE_DIR, "settings.json");
const DEFAULTS = {
    host: "",
    port: 9330,
    zone_id: "",
    http_port: 8766
};

function loadSettings() {
    try {
        const saved = JSON.parse(fs.readFileSync(SETTINGS_FILE, "utf8"));
        return Object.assign({}, DEFAULTS, saved);
    } catch (_) {
        return Object.assign({}, DEFAULTS);
    }
}

function saveSettings() {
    fs.writeFileSync(SETTINGS_FILE, JSON.stringify(settings, null, 2));
}

let settings = loadSettings();
let core = null;
let transport = null;
let image = null;
let zoneMap = {};
let reconnectTimer = null;

const status = {
    online: true,
    socket: false,
    paired: false,
    authorised: false,
    stage: "connecting",
    core: null,
    zones: [],
    zone: null,
    settings: { host: settings.host, port: settings.port,
                zone_id: settings.zone_id },
    updated_at: 0,
    error: ""
};

function stage() {
    if (!status.socket) return "offline";
    if (status.authorised) return "ready";
    if (status.paired) return "waiting_authorisation";
    return "waiting_authorisation";
}

function publish() {
    status.stage = stage();
    status.updated_at = Date.now();
    status.settings = { host: settings.host, port: settings.port,
                        zone_id: settings.zone_id };
    status.zones = zonesLib.listZones(zoneMap);
    status.zone = zonesLib.pickZone(zoneMap, settings.zone_id);
}

function rawZone(wanted) {
    const chosen = zonesLib.pickZone(zoneMap, wanted);
    return chosen ? zoneMap[chosen.zone_id] : null;
}

/* ---------------------------------------------------------------- Roon */

const roon = new RoonApi({
    extension_id: "com.rokid.allen.roon",
    display_name: "Rokid Allen Bridge",
    display_version: "0.1.0",
    publisher: "Rokid Allen",
    email: "noreply@local",
    website: "https://github.com/super480/Rokid-Allen-Unbricked",
    log_level: "none",
    core_paired: onPaired,
    core_unpaired: onUnpaired
});

function onPaired(paired) {
    core = paired;
    status.paired = true;
    status.core = {
        core_id: paired.core_id,
        display_name: paired.display_name,
        display_version: paired.display_version
    };
    transport = paired.services.RoonApiTransport || null;
    image = paired.services.RoonApiImage || null;
    status.authorised = !!transport;
    if (!transport) {
        status.error = "Roon 已连接，但 transport 服务未授权";
        publish();
        return;
    }
    status.error = "";
    zoneMap = {};
    transport.subscribe_zones(onZones);
    publish();
}

function onUnpaired() {
    core = null;
    transport = null;
    image = null;
    zoneMap = {};
    status.paired = false;
    status.authorised = false;
    status.core = null;
    publish();
}

function onZones(cmd, msg) {
    const error = zonesLib.applyZoneEvent(zoneMap, cmd, msg);
    if (error) status.error = error;
    publish();
}

function connect() {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    try {
        roon.ws_connect({
            host: settings.host,
            port: Number(settings.port) || 9330,
            onclose: () => {
                status.socket = false;
                status.paired = false;
                status.authorised = false;
                status.core = null;
                zoneMap = {};
                transport = null;
                image = null;
                publish();
                reconnectTimer = setTimeout(connect, 5000);
            }
        });
        status.socket = true;
        status.error = "";
        publish();
    } catch (error) {
        status.socket = false;
        status.error = String(error && error.message || error);
        publish();
        reconnectTimer = setTimeout(connect, 5000);
    }
}

roon.init_services({ required_services: [RoonApiTransport, RoonApiImage] });
connect();

/* ------------------------------------------------------------- controls */

function volumeStep(output) {
    const v = output && output.volume;
    return v && Number(v.step) > 0 ? Number(v.step) : 1;
}

function applyControl(body, done) {
    if (!transport || !status.authorised) {
        return done({ error: "Roon 扩展尚未启用：Roon → 设置 → 扩展 → Rokid Allen Bridge → Enable" });
    }
    const wanted = body.zone_id || settings.zone_id;
    const zone = rawZone(wanted);
    if (!zone) return done({ error: "没有可用的 Roon 区域" });
    const output = (zone.outputs || [])[0] || null;
    const action = String(body.action || "");
    const value = Number(body.value || 0);
    const finish = err => done(err ? { error: String(err) } : { ok: true });

    switch (action) {
        case "play":      return transport.control(zone, "play", finish);
        case "pause":     return transport.control(zone, "pause", finish);
        case "resume":    return transport.control(zone, "play", finish);
        case "play_pause":return transport.control(zone, "playpause", finish);
        case "next":      return transport.control(zone, "next", finish);
        case "prev":
        case "previous":  return transport.control(zone, "previous", finish);
        case "stop":      return transport.control(zone, "stop", finish);
        case "seek":      return transport.seek(zone, "absolute", value, finish);
        case "volume_up":
        case "volume_down": {
            if (!output) return done({ error: "这个区域没有可用的音量控制" });
            const step = volumeStep(output) * (action === "volume_up" ? 1 : -1);
            return transport.change_volume(output, "relative", step, finish);
        }
        case "volume_set": {
            if (!output || !output.volume) {
                return done({ error: "这个区域没有可用的音量控制" });
            }
            const v = output.volume;
            const min = Number(v.min || 0), max = Number(v.max || 100);
            const target = min + (max - min) * Math.max(0, Math.min(100, value)) / 100;
            return transport.change_volume(output, "absolute", target, finish);
        }
        case "mute":
        case "unmute": {
            if (!output) return done({ error: "这个区域没有可用的音量控制" });
            return transport.mute(output, action, finish);
        }
        default: return done({ error: "未知的控制动作：" + action });
    }
}

/* ------------------------------------------------------------ HTTP API */

function json(res, code, payload) {
    const body = Buffer.from(JSON.stringify(payload));
    res.writeHead(code, { "Content-Type": "application/json; charset=utf-8",
                          "Content-Length": body.length });
    res.end(body);
}

function readBody(req, done) {
    let raw = "";
    req.on("data", chunk => {
        raw += chunk;
        if (raw.length > 65536) req.destroy();
    });
    req.on("end", () => {
        try { done(raw ? JSON.parse(raw) : {}); }
        catch (_) { done(null); }
    });
}

const server = http.createServer((req, res) => {
    let url;
    try { url = new URL(req.url, "http://127.0.0.1"); }
    catch (_) { return json(res, 400, { ok: false, error: "bad url" }); }

    if (url.pathname === "/status" && req.method === "GET") {
        publish();
        return json(res, 200, Object.assign({ ok: true }, status));
    }

    if (url.pathname === "/settings" && req.method === "POST") {
        return readBody(req, body => {
            if (!body) return json(res, 400, { ok: false, error: "bad json" });
            const oldHost = settings.host, oldPort = settings.port;
            if (body.host) settings.host = String(body.host);
            if (body.port) settings.port = Number(body.port) || 9330;
            if (body.zone_id !== undefined) settings.zone_id = String(body.zone_id || "");
            saveSettings();
            publish();
            if (body.host && body.host !== oldHost ||
                body.port && Number(body.port) !== oldPort) {
                onUnpaired();
                connect();
            }
            return json(res, 200, Object.assign({ ok: true }, status));
        });
    }

    if (url.pathname === "/control" && req.method === "POST") {
        return readBody(req, body => {
            if (!body) return json(res, 400, { ok: false, error: "bad json" });
            applyControl(body, result => json(res, 200, result));
        });
    }

    if (url.pathname === "/image" && req.method === "GET") {
        const key = url.searchParams.get("key") || "";
        if (!key || !image) return json(res, 404, { ok: false, error: "no image" });
        return image.get_image(key, {
            scale: "fit",
            width: Number(url.searchParams.get("w") || 480),
            height: Number(url.searchParams.get("h") || 480),
            format: "image/jpeg"
        }, (err, contentType, body) => {
            if (err) return json(res, 502, { ok: false, error: String(err) });
            const buf = Buffer.isBuffer(body) ? body : Buffer.from(body || "");
            res.writeHead(200, { "Content-Type": contentType || "image/jpeg",
                                 "Content-Length": buf.length });
            res.end(buf);
        });
    }

    return json(res, 404, { ok: false, error: "not found" });
});

server.on("error", error => {
    status.error = "HTTP: " + String(error && error.message || error);
});
server.listen(Number(settings.http_port) || 8766, "127.0.0.1", () => {
    console.log("roon-bridge listening on 127.0.0.1:" + settings.http_port +
                " core=" + settings.host + ":" + settings.port);
});
