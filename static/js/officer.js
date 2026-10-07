/* =========================================================
   officer.js
   Field-officer client: duty state, GPS reporting, incoming
   dispatch offers, and the accept/navigate flow.

   Two channels deliver an offer, on purpose:
     1. Web Push  - wakes the phone with the app closed.
     2. Polling   - works even if notification permission was
                    denied, the push service is unreachable, or
                    the VAPID keys were never configured.
   Push is the better experience; polling is the one that must
   never fail, because a responder screen cannot depend on a
   permission the officer might have declined.
   ========================================================= */

(function () {
  "use strict";

  const root = document.getElementById("officer-root");
  if (!root) return;

  const PING_SECONDS = parseInt(root.dataset.pingSeconds || "20", 10);
  const POLL_MS = 4000;
  const RING_RADIUS = 90;
  const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;

  const state = {
    onDuty: root.dataset.onDuty === "true",
    offerId: null,
    offerDeadline: null,
    offerWindow: 25,
    activeDispatchId: null,
    watchId: null,
    pingTimer: null,
    tickTimer: null,
    lastFix: null,
    audio: null,
    busy: false,
  };

  // -------------------------------------------------------
  // SMALL HELPERS
  // -------------------------------------------------------
  function $(id) { return document.getElementById(id); }

  // The app installs a global CSRFProtect, so every POST here must carry
  // the token. Flask-WTF accepts it in an X-CSRFToken header exactly as it
  // accepts a hidden form field, which keeps these endpoints protected
  // instead of having to exempt them.
  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  async function postJson(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken(),
      },
      credentials: "same-origin",
      body: JSON.stringify(body || {}),
    });
    let data = {};
    try { data = await response.json(); } catch (_) { /* empty body */ }

    // A session that expired while the phone sat in a pocket comes back as
    // a login redirect, not JSON. Say so plainly rather than letting it
    // surface as a mystery failure mid-incident.
    if (response.status === 401 || response.status === 403) {
      toast("Your session expired. Reload and sign in again.", 9000);
    }
    return { status: response.status, data: data };
  }

  function toast(message, holdMs) {
    const el = $("toast");
    if (!el) return;
    el.textContent = message;
    el.hidden = false;
    clearTimeout(toast._timer);
    toast._timer = setTimeout(function () { el.hidden = true; }, holdMs || 4200);
  }

  // -------------------------------------------------------
  // ALARM
  // A synthesised two-tone alert rather than an audio file: no
  // asset to ship, and it cannot fail to load mid-incident.
  // The AudioContext is unlocked by the duty tap, which is the
  // user gesture browsers require before any sound can play.
  // -------------------------------------------------------
  function unlockAudio() {
    if (state.audio) return;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    try {
      state.audio = new Ctx();
      if (state.audio.state === "suspended") state.audio.resume();
    } catch (_) {
      state.audio = null;
    }
  }

  function sound() {
    if (!state.audio) return;
    const ctx = state.audio;
    const now = ctx.currentTime;
    // Three rising pairs — reads as an alert, not a notification chime.
    [0, 0.42, 0.84].forEach(function (offset) {
      [740, 988].forEach(function (freq, index) {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        const start = now + offset + index * 0.16;
        osc.type = "square";
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, start);
        gain.gain.exponentialRampToValueAtTime(0.16, start + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.15);
        osc.connect(gain).connect(ctx.destination);
        osc.start(start);
        osc.stop(start + 0.17);
      });
    });
  }

  function buzz() {
    if (navigator.vibrate) navigator.vibrate([400, 120, 400, 120, 700]);
  }

  // -------------------------------------------------------
  // GEOLOCATION
  // -------------------------------------------------------
  function readPosition() {
    return new Promise(function (resolve, reject) {
      if (!navigator.geolocation) {
        reject(new Error("This device has no location support."));
        return;
      }
      navigator.geolocation.getCurrentPosition(
        function (pos) {
          resolve({ latitude: pos.coords.latitude, longitude: pos.coords.longitude });
        },
        function (err) { reject(err); },
        { enableHighAccuracy: true, timeout: 12000, maximumAge: 5000 }
      );
    });
  }

  function startReporting() {
    stopReporting();
    // watchPosition catches movement; the interval guarantees a fix
    // lands even while standing still, so the server never ages the
    // officer out of the dispatchable pool for being stationary.
    if (navigator.geolocation) {
      state.watchId = navigator.geolocation.watchPosition(
        function (pos) {
          state.lastFix = { latitude: pos.coords.latitude, longitude: pos.coords.longitude };
          paintGps("ok", "Location sharing is on.");
        },
        function () { paintGps("warn", "Location signal is weak."); },
        { enableHighAccuracy: true, maximumAge: 10000, timeout: 20000 }
      );
    }
    state.pingTimer = setInterval(sendFix, PING_SECONDS * 1000);
    sendFix();
  }

  function stopReporting() {
    if (state.watchId !== null && navigator.geolocation) {
      navigator.geolocation.clearWatch(state.watchId);
      state.watchId = null;
    }
    clearInterval(state.pingTimer);
    state.pingTimer = null;
  }

  async function sendFix() {
    if (!state.onDuty) return;
    try {
      const fix = state.lastFix || (await readPosition());
      state.lastFix = fix;
      await postJson("/api/officer/location", fix);
    } catch (_) {
      paintGps("bad", "Location unavailable — dispatch cannot reach you.");
    }
  }

  function paintGps(level, message) {
    const dot = $("gps-dot");
    const text = $("gps-text");
    if (dot) dot.className = "dot " + level;
    if (text) text.textContent = message;
  }

  // -------------------------------------------------------
  // DUTY
  // -------------------------------------------------------
  async function setDuty(goingOn) {
    const toggle = $("duty-toggle");
    if (toggle) toggle.disabled = true;
    unlockAudio();

    try {
      let body = { on_duty: goingOn };
      if (goingOn) {
        paintGps("warn", "Getting your location…");
        const fix = await readPosition();
        state.lastFix = fix;
        body.latitude = fix.latitude;
        body.longitude = fix.longitude;
      }

      const result = await postJson("/api/officer/duty", body);
      if (!result.data.ok) {
        toast(result.data.message || "Could not change duty status.");
        paintGps("bad", "Location permission is needed to go on duty.");
        return;
      }

      state.onDuty = result.data.on_duty;
      paintDuty();
      if (state.onDuty) {
        startReporting();
        toast("You are on duty. Dispatch can now reach you.");
      } else {
        stopReporting();
        paintGps("", "Location sharing is off.");
        toast("You are off duty.");
      }
    } catch (err) {
      const denied = err && err.code === 1;
      toast(denied
        ? "Location permission was blocked. Allow it in your browser settings to go on duty."
        : "Could not read your location. Move somewhere with a clearer signal and try again.");
      paintGps("bad", "No location fix.");
    } finally {
      if (toggle) toggle.disabled = false;
    }
  }

  function paintDuty() {
    const toggle = $("duty-toggle");
    const panel = $("duty-panel");
    const label = $("duty-state");
    if (toggle) toggle.setAttribute("aria-checked", state.onDuty ? "true" : "false");
    if (panel) panel.className = "duty " + (state.onDuty ? "is-on" : "is-off");
    if (label) label.textContent = state.onDuty ? "On duty" : "Off duty";
  }

  // -------------------------------------------------------
  // OFFER TAKEOVER
  // -------------------------------------------------------
  function showOffer(offer) {
    const isNew = state.offerId !== offer.offer_id;
    state.offerId = offer.offer_id;
    state.offerWindow = offer.timeout_seconds || 25;
    state.offerDeadline = Date.now() + offer.seconds_remaining * 1000;

    const takeover = $("takeover");
    if (!takeover) return;

    takeover.className = "takeover level-" + String(offer.risk_level || "critical").toLowerCase();
    takeover.hidden = false;

    $("offer-level").textContent = offer.risk_level + " crowd risk";
    $("offer-seq").textContent = "Officer " + offer.sequence + " contacted";
    $("offer-distance").textContent = offer.distance_text || "—";
    $("offer-eta").textContent = offer.eta_minutes ? "about " + offer.eta_minutes + " min out" : "";

    const place = offer.location || {};
    $("offer-name").textContent = place.name || "Unknown camera";
    $("offer-venue").textContent = place.venue || "";
    const where = $("offer-where");
    if (place.address) {
      where.textContent = place.address;
      where.hidden = false;
    } else {
      where.hidden = true;
    }
    $("offer-message").textContent = offer.message || "";

    const ring = $("ring-bar");
    if (ring) {
      ring.style.strokeDasharray = String(RING_CIRCUMFERENCE);
      ring.style.strokeDashoffset = "0";
    }

    if (isNew) {
      sound();
      buzz();
    }
    startTicking();
  }

  function hideOffer() {
    const takeover = $("takeover");
    if (takeover) takeover.hidden = true;
    state.offerId = null;
    state.offerDeadline = null;
    clearInterval(state.tickTimer);
    state.tickTimer = null;
  }

  function startTicking() {
    clearInterval(state.tickTimer);
    tick();
    state.tickTimer = setInterval(tick, 250);
  }

  function tick() {
    if (!state.offerDeadline) return;
    const remainingMs = Math.max(0, state.offerDeadline - Date.now());
    const seconds = Math.ceil(remainingMs / 1000);

    const label = $("offer-seconds");
    if (label) label.textContent = String(seconds);

    const ring = $("ring-bar");
    if (ring) {
      const spent = 1 - Math.min(1, remainingMs / (state.offerWindow * 1000));
      ring.style.strokeDashoffset = String(RING_CIRCUMFERENCE * spent);
    }

    if (remainingMs <= 0) {
      hideOffer();
      toast("That assignment moved to another officer.");
      refresh();
    }
  }

  async function respond(accepting) {
    if (!state.offerId || state.busy) return;
    state.busy = true;
    const acceptBtn = $("btn-accept");
    const passBtn = $("btn-pass");
    if (acceptBtn) acceptBtn.disabled = true;
    if (passBtn) passBtn.disabled = true;

    const verb = accepting ? "accept" : "decline";
    try {
      const result = await postJson("/api/officer/offer/" + state.offerId + "/" + verb, {});
      if (result.data.ok) {
        hideOffer();
        if (accepting && result.data.redirect) {
          window.location.href = result.data.redirect;
          return;
        }
        toast(accepting ? "Assignment accepted." : "Passed to the next officer.");
      } else {
        hideOffer();
        toast(result.data.message || "That assignment is no longer available.");
      }
    } catch (_) {
      toast("Network problem. Check your connection and try again.");
    } finally {
      state.busy = false;
      if (acceptBtn) acceptBtn.disabled = false;
      if (passBtn) passBtn.disabled = false;
      refresh();
    }
  }

  // -------------------------------------------------------
  // POLLING
  // -------------------------------------------------------
  async function refresh() {
    try {
      const response = await fetch("/api/officer/state", { credentials: "same-origin" });
      if (!response.ok) return;
      const data = await response.json();
      if (!data.ok) return;

      if (data.on_duty !== state.onDuty) {
        state.onDuty = data.on_duty;
        paintDuty();
        if (state.onDuty && !state.pingTimer) startReporting();
        if (!state.onDuty) stopReporting();
      }

      if (data.offer) {
        showOffer(data.offer);
      } else if (state.offerId) {
        hideOffer();
      }

      // An assignment accepted on another device should pull this one
      // to the incident screen too, so both surfaces agree.
      if (data.active && data.active.dispatch_id !== state.activeDispatchId) {
        state.activeDispatchId = data.active.dispatch_id;
        if (!document.body.dataset.dispatchPage) {
          window.location.href = data.active.detail_url;
        }
      }

      if (state.onDuty && !data.location_fresh) {
        paintGps("warn", "Your last location is stale. Keep this screen open.");
      }
    } catch (_) {
      /* offline — the next tick retries */
    }
  }

  // -------------------------------------------------------
  // WEB PUSH
  // -------------------------------------------------------
  function urlBase64ToUint8Array(base64String) {
    const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    const raw = window.atob(base64);
    const output = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i += 1) output[i] = raw.charCodeAt(i);
    return output;
  }

  async function enablePush() {
    const button = $("push-enable");
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
      toast("This browser cannot deliver background alerts. Keep this screen open instead.");
      return;
    }
    if (button) button.disabled = true;

    try {
      const keyResponse = await fetch("/api/push/vapid-key", { credentials: "same-origin" });
      const keyData = await keyResponse.json();
      if (!keyData.configured) {
        toast("Background alerts are not set up on the server yet.");
        return;
      }

      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        toast("Notifications are blocked. Allow them to be alerted with the app closed.");
        return;
      }

      const registration = await navigator.serviceWorker.register("/officer/sw.js", {
        scope: "/officer/",
      });
      await navigator.serviceWorker.ready;

      let subscription = await registration.pushManager.getSubscription();
      if (!subscription) {
        subscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(keyData.public_key),
        });
      }

      const result = await postJson("/api/push/subscribe", subscription.toJSON());
      if (result.data.ok) {
        const notice = $("push-notice");
        if (notice) notice.hidden = true;
        toast("Background alerts are on for this device.");
      } else {
        toast("Could not register this device for alerts.");
      }
    } catch (err) {
      toast("Could not turn on background alerts: " + (err && err.message ? err.message : "unknown error"));
    } finally {
      if (button) button.disabled = false;
    }
  }

  // -------------------------------------------------------
  // INCIDENT STATUS (detail page)
  // -------------------------------------------------------
  async function updateStatus(button) {
    const dispatchId = button.dataset.dispatchId;
    const next = button.dataset.status;
    button.disabled = true;
    try {
      const result = await postJson("/api/officer/dispatch/" + dispatchId + "/status", { status: next });
      if (result.data.ok) {
        window.location.reload();
      } else {
        toast("Could not update: " + (result.data.reason || "unknown"));
        button.disabled = false;
      }
    } catch (_) {
      toast("Network problem. Try again.");
      button.disabled = false;
    }
  }

  // -------------------------------------------------------
  // WIRING
  // -------------------------------------------------------
  const dutyToggle = $("duty-toggle");
  if (dutyToggle) {
    dutyToggle.addEventListener("click", function () { setDuty(!state.onDuty); });
  }

  const acceptBtn = $("btn-accept");
  if (acceptBtn) acceptBtn.addEventListener("click", function () { respond(true); });

  const passBtn = $("btn-pass");
  if (passBtn) passBtn.addEventListener("click", function () { respond(false); });

  const pushBtn = $("push-enable");
  if (pushBtn) pushBtn.addEventListener("click", enablePush);

  document.querySelectorAll("[data-status]").forEach(function (button) {
    button.addEventListener("click", function () { updateStatus(button); });
  });

  // The service worker posts here when the officer taps "Accept" straight
  // from the notification, so the open page can follow along.
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.addEventListener("message", function (event) {
      const payload = event.data || {};
      if (payload.type === "dispatch_offer" || payload.type === "refresh") refresh();
    });
  }

  paintDuty();
  if (state.onDuty) startReporting();
  refresh();
  setInterval(refresh, POLL_MS);

  // Coming back from a locked screen should show the truth immediately,
  // not wait out the remainder of the poll interval.
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refresh();
  });
})();
