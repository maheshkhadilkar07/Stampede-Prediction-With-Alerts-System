/* =========================================================
   officer/sw.js  (served from /officer/sw.js so its scope is /officer/)

   The service worker exists for exactly one reason: to wake an officer's
   phone when the app is closed. It deliberately does NOT cache pages.

   An offline cache would be actively dangerous here. A cached officer
   screen could show a stale "standing by" state, or worse, an assignment
   that has already been reassigned to someone else - and the officer would
   have no way to tell. For this app, failing visibly (no network, nothing
   shown) is safer than succeeding with a lie. Caching is reserved for the
   static shell only, and even that is skipped for now in favour of
   correctness.
   ========================================================= */

/* Take over immediately on install rather than waiting for every tab to
   close. A fix to this worker needs to reach an officer's phone today. */
self.addEventListener("install", function (event) {
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(self.clients.claim());
});

/* ---------------------------------------------------------
   INCOMING PUSH
   --------------------------------------------------------- */
self.addEventListener("push", function (event) {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (_) {
    payload = {};
  }

  const title = payload.title || "Dispatch alert";
  const options = {
    body: payload.body || "A crowd incident needs an officer.",
    /* tag keyed to the incident means a re-push REPLACES the previous
       notification instead of stacking duplicates on the lock screen. */
    tag: payload.tag || "dispatch",
    renotify: payload.renotify !== false,
    /* Keep it on screen until the officer deals with it. This is a
       decision with a deadline, not an FYI. */
    requireInteraction: payload.requireInteraction !== false,
    icon: "/static/icons/officer-192.png",
    badge: "/static/icons/officer-96.png",
    vibrate: [400, 120, 400, 120, 700],
    data: payload,
    actions:
      payload.type === "dispatch_offer"
        ? [{ action: "open", title: "Open assignment" }]
        : [],
  };

  event.waitUntil(
    (async function () {
      await self.registration.showNotification(title, options);

      /* If a page is already open, tell it to refresh so the takeover
         appears there too - the officer may be looking at the screen
         when the push lands, and seeing a notification for something the
         page does not show would be baffling. */
      const clients = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      clients.forEach(function (client) {
        client.postMessage({ type: payload.type || "refresh" });
      });
    })()
  );
});

/* ---------------------------------------------------------
   TAPPING THE NOTIFICATION
   --------------------------------------------------------- */
self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/officer/";

  event.waitUntil(
    (async function () {
      const clients = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });

      /* Reuse an existing tab where possible. Accepting is done in the
         page, not here, so that one code path owns the accept call and
         its error handling - the worker only ever navigates. */
      for (const client of clients) {
        if (client.url.indexOf("/officer") !== -1 && "focus" in client) {
          client.postMessage({ type: "refresh" });
          return client.focus();
        }
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow(target);
      }
    })()
  );
});

/* Some browsers expire a subscription and hand us a new one. Re-register
   it silently, or the officer stops receiving pushes with no sign that
   anything changed. */
self.addEventListener("pushsubscriptionchange", function (event) {
  event.waitUntil(
    (async function () {
      const subscription = event.newSubscription;
      if (!subscription) return;
      try {
        await fetch("/api/push/subscribe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify(subscription.toJSON()),
        });
      } catch (_) {
        /* The page will re-subscribe on its next load. */
      }
    })()
  );
});
