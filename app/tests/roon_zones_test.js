"use strict";
/*
 * Regression test for the sidecar's zone shape. Run directly with node, or
 * through unittest via roon_test.RoonZonesJsTest.
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const lib = require(path.join(__dirname, "..", "roon-bridge", "zones.js"));

let checks = 0;
function check(name, fn) {
    fn();
    checks += 1;
    console.log("ok - " + name);
}

function rawZone(id, name, state, extra) {
    return Object.assign({
        zone_id: id,
        display_name: name,
        state: state,
        now_playing: {
            three_line: { line1: "不信邪", line2: "任贤齐", line3: "专辑" },
            seek_position: 16,
            length: 192,
            image_key: "cover-" + id
        },
        is_next_allowed: true,
        outputs: [{ output_id: "out-" + id, display_name: name,
                    volume: { min: 0, max: 100, step: 2, value: 40 } }]
    }, extra || {});
}

check("listZones maps zone objects, not map keys", () => {
    const map = { "z1": rawZone("z1", "Super Apple TV", "playing"),
                  "z2": rawZone("z2", "HQPlayer WIN", "stopped") };
    const list = lib.listZones(map);
    assert.equal(list.length, 2);
    assert.equal(list[0].zone_id, "z1");
    assert.equal(list[0].display_name, "Super Apple TV");
    assert.equal(list[0].title, "不信邪");
    assert.equal(list[0].artist, "任贤齐");
    assert.equal(list[0].album, "专辑");
    assert.equal(list[0].state, "playing");
    assert.equal(list[0].image_key, "cover-z1");
    assert.equal(list[0].seek_position, 16);
    assert.equal(list[0].length, 192);
    assert.equal(list[0].outputs[0].display_name, "Super Apple TV");
    assert.equal(list[1].state, "stopped");
});

check("pickZone prefers the playing zone", () => {
    const map = { a: rawZone("a", "HQPlayer WIN", "stopped"),
                  b: rawZone("b", "Super Apple TV", "playing") };
    assert.equal(lib.pickZone(map, "").zone_id, "b");
});

check("pickZone honours an explicit target and falls back safely", () => {
    const map = { a: rawZone("a", "客厅", "stopped"),
                  b: rawZone("b", "Apple TV", "playing") };
    assert.equal(lib.pickZone(map, "a").zone_id, "a");
    assert.equal(lib.pickZone(map, "missing").zone_id, "b");
    assert.equal(lib.pickZone({}, ""), null);
});

check("Subscribed replaces the map, Changed adds/removes/updates", () => {
    const map = { stale: rawZone("stale", "旧区域", "stopped") };
    lib.applyZoneEvent(map, "Subscribed",
        { zones: [rawZone("z1", "Apple TV", "playing")] });
    assert.deepEqual(Object.keys(map), ["z1"]);

    lib.applyZoneEvent(map, "Changed", {
        zones_added: [rawZone("z2", "Denon", "paused")],
        zones_changed: [rawZone("z1", "Apple TV", "paused")],
        zones_removed: ["stale"]
    });
    assert.equal(lib.listZones(map).length, 2);
    assert.equal(map.z1.state, "paused");

    lib.applyZoneEvent(map, "Changed", { zones_removed: ["z2"] });
    assert.deepEqual(Object.keys(map), ["z1"]);
});

check("seek events never overwrite the position with undefined", () => {
    const map = { z1: rawZone("z1", "Apple TV", "playing") };
    lib.applyZoneEvent(map, "Changed",
        { zones_seek_changed: [{ zone_id: "z1" }] });
    assert.equal(map.z1.now_playing.seek_position, 16);
    lib.applyZoneEvent(map, "Changed",
        { zones_seek_changed: [{ zone_id: "z1", seek_position: 88 }] });
    assert.equal(map.z1.now_playing.seek_position, 88);
});

check("unexpected commands surface as errors, network noise does not", () => {
    const map = {};
    assert.equal(lib.applyZoneEvent(map, "Subscribed", { zones: [] }), "");
    assert.equal(lib.applyZoneEvent(map, "NetworkError", {}), "");
    assert.equal(lib.applyZoneEvent(map, "Unsubscribed", {}), "");
    assert.equal(lib.applyZoneEvent(map, "Changed", {}), "");
    assert.equal(lib.applyZoneEvent(map, "Invalid", {}), "Invalid");
});

check("simplify tolerates missing tags and malformed input", () => {
    const bare = lib.simplify({ zone_id: "bare", state: "stopped" });
    assert.equal(bare.title, "");
    assert.equal(bare.outputs.length, 0);
    assert.equal(bare.seek_position, 0);
    const two = lib.simplify({ zone_id: "two", now_playing: {
        two_line: { line1: "晴天", line2: "周杰伦" } } });
    assert.equal(two.title, "晴天");
    assert.equal(two.artist, "周杰伦");
    const fallback = lib.simplify("z1");
    assert.equal(fallback.display_name, "");
});

console.log("roon zones: " + checks + " checks passed");
