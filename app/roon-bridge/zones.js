#!/usr/bin/env node
"use strict";
/*
 * Pure helpers for the zone state the sidecar exposes over /status.
 *
 * Kept separate from index.js so the shape can be unit tested without a
 * live Roon Core. The classic mistake here is mapping Object.keys(zoneMap)
 * straight into simplify(): that hands the function a string id, not the
 * zone object, and every zone comes back blank. Always use Object.values().
 */
function simplify(zone) {
    const z = zone && typeof zone === "object" ? zone : {};
    const np = z.now_playing || {};
    const three = np.three_line || {};
    const two = np.two_line || {};
    const one = np.one_line || {};
    return {
        zone_id: z.zone_id || "",
        display_name: z.display_name || "",
        state: z.state || "stopped",
        title: three.line1 || two.line1 || one.line1 || "",
        artist: three.line2 || two.line2 || "",
        album: three.line3 || "",
        seek_position: Number(np.seek_position || 0),
        length: Number(np.length || 0),
        image_key: np.image_key || "",
        is_play_allowed: !!z.is_play_allowed,
        is_pause_allowed: !!z.is_pause_allowed,
        is_next_allowed: !!z.is_next_allowed,
        is_previous_allowed: !!z.is_previous_allowed,
        outputs: (z.outputs || []).map(o => ({
            output_id: o.output_id || "",
            display_name: o.display_name || "",
            volume: o.volume || null
        }))
    };
}

function listZones(zoneMap) {
    return Object.values(zoneMap || {}).map(simplify);
}

function pickZone(zoneMap, wanted) {
    const all = listZones(zoneMap);
    if (!all.length) return null;
    if (wanted) {
        const exact = all.find(z => z.zone_id === wanted);
        if (exact) return exact;
    }
    return all.find(z => z.state === "playing") || all[0];
}

/** Apply one subscribe_zones event in place. Returns an error string or "". */
function applyZoneEvent(zoneMap, cmd, msg) {
    const body = msg || {};
    if (cmd === "Subscribed") {
        Object.keys(zoneMap).forEach(id => { delete zoneMap[id]; });
        (body.zones || []).forEach(z => { zoneMap[z.zone_id] = z; });
        return "";
    }
    if (cmd === "Changed") {
        (body.zones_removed || []).forEach(id => { delete zoneMap[id]; });
        (body.zones_added || []).forEach(z => { zoneMap[z.zone_id] = z; });
        (body.zones_changed || []).forEach(z => { zoneMap[z.zone_id] = z; });
        (body.zones_seek_changed || []).forEach(e => {
            const z = zoneMap[e.zone_id];
            // Some seek events only carry queue_time_remaining. Writing an
            // undefined position would reset the on-screen progress to 0.
            if (z && z.now_playing && e.seek_position != null) {
                z.now_playing.seek_position = e.seek_position;
            }
        });
        return "";
    }
    if (cmd && cmd !== "NetworkError" && cmd !== "Unsubscribed") {
        return String(cmd);
    }
    return "";
}

module.exports = { simplify, listZones, pickZone, applyZoneEvent };
