/**
 * riskmap.js
 * ------------
 * Reusable, resilient Leaflet risk-map used by /map and /responder.
 *
 * Why this exists: loading Leaflet from a single CDN *with an SRI integrity
 * hash* is fragile — if that one CDN is slow/blocked, or the hash doesn't match
 * the served file, the browser refuses to load Leaflet and the page shows an
 * "internet not connected" style error even when you ARE online. This loader:
 *   - tries several CDNs in turn (cdnjs -> jsdelivr -> unpkg), no SRI hash,
 *   - uses VECTOR circle markers (no marker-image files to 404),
 *   - keeps working (positions on a plain backdrop) if only the street TILES
 *     fail to load,
 *   - shows a clear message + Retry button if Leaflet truly can't load.
 *
 * Street tiles still require internet (by design — this is the online map).
 */
(function (global) {
  "use strict";

  const LEAFLET_JS = [
    "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js",
    "https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js",
    "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
  ];
  const LEAFLET_CSS = [
    "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css",
    "https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css",
    "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
  ];
  const RISK_COLORS = {
    SAFE: "#28a745", LOW: "#ffc107", MEDIUM: "#fd7e14", HIGH: "#dc3545", CRITICAL: "#7d0d1b",
  };
  const PROMINENT = ["HIGH", "CRITICAL"];

  function loadCssWithFallback(urls) {
    let i = 0;
    function tryNext() {
      if (i >= urls.length) return;
      const link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = urls[i++];
      link.crossOrigin = "anonymous";
      link.onerror = tryNext;
      document.head.appendChild(link);
    }
    tryNext();
  }

  function loadScriptWithFallback(urls, done) {
    let i = 0;
    (function tryNext() {
      if (global.L) return done(true);
      if (i >= urls.length) return done(!!global.L);
      const s = document.createElement("script");
      s.src = urls[i++];
      s.crossOrigin = "anonymous";
      s.onload = () => done(true);
      s.onerror = tryNext;
      document.head.appendChild(s);
    })();
  }

  function ensureLeaflet(done) {
    if (global.L) return done(true);
    loadCssWithFallback(LEAFLET_CSS);
    loadScriptWithFallback(LEAFLET_JS, done);
  }

  function el(id) { return id ? document.getElementById(id) : null; }

  function showLoadError(mapEl, onRetry) {
    mapEl.innerHTML =
      '<div style="padding:2rem;text-align:center;color:#64748b;">' +
      '<div style="font-size:1.6rem;">📶❌</div>' +
      '<div style="margin:.5rem 0;">The map library could not be loaded. This map needs an ' +
      'internet connection. Check your connection, then retry.</div>' +
      '<button type="button" class="btn btn-sm btn-gradient" id="__riskmap_retry">Retry</button></div>';
    const b = document.getElementById("__riskmap_retry");
    if (b) b.addEventListener("click", onRetry);
  }

  function popupHtml(loc) {
    const color = loc.color || RISK_COLORS.SAFE;
    const liveBadge = loc.is_live
      ? '<span style="color:#dc3545;font-weight:600;">&#9679; LIVE</span><br>' : "";
    const demoBadge = loc.demo
      ? '<span style="background:#eef2ff;color:#2563eb;font-weight:600;padding:1px 6px;border-radius:6px;font-size:0.72rem;">DEMO</span><br>' : "";
    const ts = loc.upload_time ? new Date(loc.upload_time).toLocaleString() : "—";
    let alertBlock = '<div style="color:#64748b;font-size:0.85rem;margin-top:4px;">No alerts raised yet.</div>';
    if (loc.alert) {
      const at = loc.alert.timestamp ? new Date(loc.alert.timestamp).toLocaleString() : "—";
      const lines = (loc.alert.recipients || []).map((r) => {
        const icon = r.status === "sent" ? "✓" : (r.status === "failed" ? "✗" : "…");
        const sc = r.status === "sent" ? "#28a745" : (r.status === "failed" ? "#dc3545" : "#ffc107");
        return '<span style="color:' + sc + '">' + icon + " " + r.name + " (" + r.type + ") — " + r.status + "</span>";
      }).join("<br>");
      alertBlock =
        '<hr style="margin:6px 0;"><b>Latest Alert:</b> ' + loc.alert.risk_level + " at " + at + "<br>" +
        '<span style="font-size:0.85rem;">' + loc.alert.sent_count + " sent, " + loc.alert.failed_count +
        " failed, " + loc.alert.pending_count + " pending</span>" +
        (lines ? '<div style="font-size:0.85rem;margin-top:4px;">' + lines + "</div>" : "");
    }
    const nav = (loc.latitude != null && loc.longitude != null)
      ? '<div style="margin-top:6px;"><a href="https://www.google.com/maps/dir/?api=1&destination=' +
        loc.latitude + ',' + loc.longitude + '" target="_blank" rel="noopener">Navigate here &rarr;</a></div>'
      : "";
    return demoBadge + liveBadge + "<b>" + loc.name + "</b><br>" +
      'Risk: <b style="color:' + color + '">' + loc.risk_label + "</b><br>" +
      "People (current/peak): " + loc.person_count + "<br>" +
      "Density: " + loc.density + " people/cell<br>" +
      "Growth rate: " + ((loc.growth_rate || 0) * 100).toFixed(1) + "%<br>" +
      "Source: " + loc.source_type + " | Last updated: " + ts + alertBlock + nav;
  }

  function init(opts) {
    const mapEl = el(opts.mapElId);
    if (!mapEl) return null;
    const state = { demo: !!opts.demo, map: null, markers: null, halos: [], refit: true, tileErrors: 0, timer: null };

    function buildLegend() {
      const lg = el(opts.legendElId);
      if (!lg) return;
      lg.innerHTML = "";
      Object.entries(RISK_COLORS).forEach(([lvl, col]) => {
        const s = document.createElement("span");
        s.style.marginRight = "1rem";
        s.innerHTML = '<span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:' +
          col + ';margin-right:.4rem;vertical-align:middle;"></span>' + lvl;
        lg.appendChild(s);
      });
    }

    function setCount(n) {
      const c = el(opts.countElId);
      if (!c) return;
      c.textContent = n + (n === 1 ? " active incident" : " active incidents");
      c.className = "risk-badge " + (n > 0 ? "risk-CRITICAL" : "risk-SAFE");
    }

    function build() {
      const L = global.L;
      mapEl.innerHTML = "";
      const map = L.map(mapEl).setView([20.5937, 78.9629], 5);
      state.map = map;
      const tiles = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution: '&copy; OpenStreetMap contributors', maxZoom: 19,
      }).addTo(map);
      tiles.on("tileerror", () => {
        state.tileErrors++;
        if (state.tileErrors === 8) {
          mapEl.style.background = "#e9eef5";
          const note = el(opts.noteElId);
          if (note) { note.style.display = "block"; note.textContent =
            "Street tiles couldn't load (offline?) — showing incident positions on a plain map."; }
        }
      });
      state.markers = L.layerGroup().addTo(map);
      buildLegend();
      setTimeout(() => map.invalidateSize(), 200);
      global.addEventListener("resize", () => map.invalidateSize());

      // gentle pulse for danger halos
      let t = 0;
      (function pulse() {
        t += 0.05;
        const r = 18 + Math.sin(t) * 6, o = 0.22 + Math.sin(t) * 0.1;
        state.halos.forEach((h) => { h.setRadius(r); h.setStyle({ fillOpacity: o }); });
        state._raf = requestAnimationFrame(pulse);
      })();

      refresh();
      state.timer = setInterval(refresh, opts.intervalMs || 5000);
    }

    async function refresh() {
      const L = global.L;
      if (!L || !state.map) return;
      let data;
      try {
        const url = (opts.getUrl ? opts.getUrl(state.demo)
          : "/api/map/locations" + (state.demo ? "?demo=1" : ""));
        const res = await fetch(url);
        data = await res.json();
      } catch (e) { return; }

      state.markers.clearLayers();
      state.halos = [];
      const locs = data.locations || [];
      const emptyEl = el(opts.emptyElId);
      if (emptyEl) emptyEl.style.display = locs.length ? "none" : "block";
      setCount(data.active_incidents || 0);
      const pts = [];
      locs.forEach((loc) => {
        if (loc.latitude == null || loc.longitude == null) return;
        const color = loc.color || RISK_COLORS.SAFE;
        const prominent = PROMINENT.indexOf(loc.risk_level) >= 0;
        if (prominent) {
          const halo = L.circleMarker([loc.latitude, loc.longitude], {
            radius: 18, color: color, fillColor: color, fillOpacity: 0.18, weight: 1, interactive: false,
          }).addTo(state.markers);
          state.halos.push(halo);
        }
        const m = L.circleMarker([loc.latitude, loc.longitude], {
          radius: loc.is_live ? 13 : (prominent ? 11 : 9),
          color: color, fillColor: color, fillOpacity: 0.9,
          weight: loc.is_live ? 3 : (prominent ? 2.5 : 1.5),
        });
        m.bindPopup(popupHtml(loc), { maxWidth: 320 });
        m.addTo(state.markers);
        pts.push([loc.latitude, loc.longitude]);
      });
      if (state.refit && pts.length) {
        state.map.fitBounds(pts, { padding: [40, 40], maxZoom: 14 });
        state.refit = false;
      }
      if (typeof opts.onData === "function") opts.onData(data);
    }

    function start() {
      ensureLeaflet((ok) => {
        if (!ok || !global.L) { showLoadError(mapEl, start); return; }
        build();
      });
    }
    start();

    return {
      refresh: refresh,
      setDemo: (v) => { state.demo = !!v; state.refit = true; refresh(); },
      destroy: () => {
        if (state.timer) clearInterval(state.timer);
        if (state._raf) cancelAnimationFrame(state._raf);
        if (state.map) state.map.remove();
      },
    };
  }

  global.RiskMap = { init: init, RISK_COLORS: RISK_COLORS };
})(typeof window !== "undefined" ? window : this);
