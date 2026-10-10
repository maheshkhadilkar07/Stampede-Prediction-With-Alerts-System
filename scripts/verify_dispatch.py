"""
scripts/verify_dispatch.py — verification for the officer-dispatch feature.

Run it with nothing installed but Python:

    python scripts/verify_dispatch.py

Two independent parts, because this deliberately does not need Flask or
SQLAlchemy to be importable:

  PART A — structural assertions against the REAL dispatch/engine.py, via
           `ast`. This reads the shipped bytes, so it proves the locking,
           the transition allow-list, the escalate-flag plumbing and the
           distinct-reason contract are actually present in the file.

  PART B — a re-typed simulation of the same state machine over stdlib
           sqlite3, to exercise the DYNAMICS (offer ordering, escalation,
           expiry, dedupe) that no static check can reach.

Honest limitation of Part B: it tests the ALGORITHM as transcribed here, not
the shipped code. A divergence between this transcription and engine.py
would not be caught — which is exactly why Part A exists, and why a real
end-to-end run on the live app is still worth doing. dispatch/geo.py IS
imported for real in Part B (it is stdlib-only by design).
"""

import ast
import os
import sqlite3
import sys
from datetime import datetime, timedelta

# Works from anywhere: this file lives in scripts/, the project is its parent.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "dispatch"))
import geo  # real shipped module, zero third-party imports

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -> {detail}" if detail and not condition else ""))


# ===========================================================================
# PART A — structural assertions on the real engine.py
# ===========================================================================
print("\nPART A  structural checks on the shipped dispatch/engine.py")

src = open(os.path.join(ROOT, "dispatch", "engine.py"), encoding="utf-8").read()
tree = ast.parse(src)
engine_cls = next(
    n for n in ast.walk(tree)
    if isinstance(n, ast.ClassDef) and n.name == "DispatchEngine"
)
methods = {n.name: n for n in engine_cls.body if isinstance(n, ast.FunctionDef)}


def holds_lock(fn):
    """True if the method body contains `with self._lock:`."""
    for node in ast.walk(fn):
        if isinstance(node, ast.With):
            for item in node.items:
                ctx = item.context_expr
                if (isinstance(ctx, ast.Attribute) and ctx.attr == "_lock"
                        and isinstance(ctx.value, ast.Name) and ctx.value.id == "self"):
                    return True
    return False


# Every method that mutates dispatch/offer state must serialise on the lock.
MUTATORS = [
    "create_dispatch_for_alert", "create_manual_dispatch", "accept_offer",
    "decline_offer", "update_status", "cancel_dispatch", "retry_dispatch",
    "_expire_and_escalate",
]
for name in MUTATORS:
    check(f"{name}() serialises on self._lock", name in methods and holds_lock(methods[name]))

# The transition allow-list, read out of the source rather than assumed.
us = methods["update_status"]
allowed_node = next(
    (n.value for n in ast.walk(us)
     if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) == "allowed"),
    None,
)
allowed = ast.literal_eval(allowed_node) if allowed_node else None
expected_allowed = {
    "EN_ROUTE": {"ACCEPTED"},
    "ARRIVED": {"ACCEPTED", "EN_ROUTE"},
    "RESOLVED": {"ACCEPTED", "EN_ROUTE", "ARRIVED"},
}
check("update_status allow-list is exactly the forward-only set",
      allowed == expected_allowed, f"got {allowed}")
check("update_status cannot resolve a never-accepted incident",
      allowed and "PENDING" not in allowed["RESOLVED"] and "OFFERED" not in allowed["RESOLVED"])
check("update_status cannot walk ARRIVED back to EN_ROUTE",
      allowed and "ARRIVED" not in allowed["EN_ROUTE"])

# Identity checks must exist before any mutation.
def calls_with_kw(fn, func_attr, kw):
    out = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == func_attr):
            out.append({k.arg for k in node.keywords})
    return out

offer_calls = calls_with_kw(methods["_offer_to_next_officer"], "_mark_unassigned", "escalate")
check("_offer_to_next_officer has 2 _mark_unassigned paths", len(offer_calls) == 2,
      f"found {len(offer_calls)}")
check("both _mark_unassigned paths forward the escalate flag",
      all("escalate" in kws for kws in offer_calls), f"{offer_calls}")

reoffer_calls = calls_with_kw(methods["_reoffer_unassigned"], "_offer_to_next_officer", "")
check("_reoffer_unassigned suppresses the duplicate escalation email",
      any("escalate_if_unassigned" in kws for kws in reoffer_calls), f"{reoffer_calls}")
check("_reoffer_unassigned keeps prior offers (never deletes them)",
      "session.delete" not in ast.get_source_segment(src, methods["_reoffer_unassigned"]))
check("retry_dispatch DOES clear prior offers (admin asks everyone again)",
      "delete" in ast.get_source_segment(src, methods["retry_dispatch"]))

# accept_offer must distinguish every rejection path.
accept_src = ast.get_source_segment(src, methods["accept_offer"])
for reason in ["not_found", "not_your_offer", "already_taken", "closed", "expired"]:
    check(f"accept_offer returns a distinct '{reason}' reason", f'"{reason}"' in accept_src)
check("accept_offer verifies offer.officer_id before mutating",
      accept_src.index("officer_id !=") < accept_src.index('offer.status = "ACCEPTED"'))
check("update_status verifies accepted_by_id before mutating",
      ast.get_source_segment(src, us).index("accepted_by_id !=")
      < ast.get_source_segment(src, us).index("dispatch.status = new_status"))

# Dedupe must block reuse on exactly the two human-decided terminal states.
dedupe_src = ast.get_source_segment(src, methods["_find_reusable_dispatch_near"])
check("dedupe blocks reuse only for RESOLVED and CANCELLED",
      '"RESOLVED", "CANCELLED"' in dedupe_src and "UNASSIGNED" not in dedupe_src.split('"""')[2])

# The reloader guard, so two monitors never run.
check("monitor is guarded against the Flask reloader double-import",
      "WERKZEUG_RUN_MAIN" in ast.get_source_segment(src, methods["_start_monitor"]))


# ===========================================================================
# PART B — lifecycle simulation over stdlib sqlite3
# ===========================================================================
print("\nPART B  lifecycle dynamics (re-typed state machine, real geo.py)")

OFFER_TIMEOUT = 25
MAX_OFFERS = 5
RADIUS_KM = 15.0
MAX_LOC_AGE = 300
DEDUPE_WINDOW = 600

db = sqlite3.connect(":memory:")
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, role TEXT,
  on_duty INT, is_verified INT, last_latitude REAL, last_longitude REAL,
  last_location_at TEXT);
CREATE TABLE alerts (id INTEGER PRIMARY KEY, acknowledged INT DEFAULT 0);
CREATE TABLE dispatches (id INTEGER PRIMARY KEY, alert_id INT, status TEXT,
  latitude REAL, longitude REAL, offers_made INT DEFAULT 0,
  accepted_by_id INT, created_at TEXT, close_reason TEXT);
CREATE TABLE offers (id INTEGER PRIMARY KEY, dispatch_id INT, officer_id INT,
  status TEXT, sequence INT, distance_km REAL, offered_at TEXT, expires_at TEXT);
""")

EMAILS = []          # escalation emails that would have been sent
NOW = datetime(2026, 10, 2, 12, 0, 0)


def ts(dt):
    return dt.isoformat()


class Row:
    """Duck-type for geo.rank_by_distance."""
    def __init__(self, r):
        self.id = r["id"]
        self.last_latitude = r["last_latitude"]
        self.last_longitude = r["last_longitude"]


def fresh(r, now):
    if r["last_location_at"] is None:
        return False
    age = (now - datetime.fromisoformat(r["last_location_at"])).total_seconds()
    return age <= MAX_LOC_AGE


def rank_available(d, now):
    cands = db.execute(
        "SELECT * FROM users WHERE role='officer' AND on_duty=1 AND is_verified=1"
    ).fetchall()
    asked = {r["officer_id"] for r in db.execute(
        "SELECT officer_id FROM offers WHERE dispatch_id=?", (d["id"],))}
    busy = {r["accepted_by_id"] for r in db.execute(
        "SELECT accepted_by_id FROM dispatches WHERE status IN "
        "('ACCEPTED','EN_ROUTE','ARRIVED') AND accepted_by_id IS NOT NULL")}
    eligible = [Row(r) for r in cands
                if r["id"] not in (asked | busy) and fresh(r, now)]
    return geo.rank_by_distance(d["latitude"], d["longitude"], eligible,
                                max_radius_km=RADIUS_KM)


def get_d(did):
    return db.execute("SELECT * FROM dispatches WHERE id=?", (did,)).fetchone()


def mark_unassigned(did, reason, escalate=True):
    db.execute("UPDATE dispatches SET status='UNASSIGNED', close_reason=? WHERE id=?",
               (reason, did))
    if escalate:
        EMAILS.append((did, reason))


def offer_next(did, now, escalate_if_unassigned=True):
    d = get_d(did)
    if d["status"] in ("RESOLVED", "CANCELLED", "UNASSIGNED",
                       "ACCEPTED", "EN_ROUTE", "ARRIVED"):
        return False
    if d["offers_made"] >= MAX_OFFERS:
        mark_unassigned(did, f"No response after asking {d['offers_made']} officers.",
                        escalate=escalate_if_unassigned)
        return False
    ranked = rank_available(d, now)
    if not ranked:
        mark_unassigned(did, "nobody in range", escalate=escalate_if_unassigned)
        return False
    officer, dist = ranked[0]
    seq = d["offers_made"] + 1
    db.execute("INSERT INTO offers (dispatch_id, officer_id, status, sequence,"
               " distance_km, offered_at, expires_at) VALUES (?,?,?,?,?,?,?)",
               (did, officer.id, "PENDING", seq, round(dist, 3), ts(now),
                ts(now + timedelta(seconds=OFFER_TIMEOUT))))
    db.execute("UPDATE dispatches SET status='OFFERED', offers_made=? WHERE id=?",
               (seq, did))
    return True


def find_reusable(lat, lon, now):
    start = now - timedelta(seconds=DEDUPE_WINDOW)
    for d in db.execute("SELECT * FROM dispatches WHERE status NOT IN "
                        "('RESOLVED','CANCELLED')").fetchall():
        if datetime.fromisoformat(d["created_at"]) < start:
            continue
        if geo.haversine_km(lat, lon, d["latitude"], d["longitude"]) <= 0.15:
            return d
    return None


def create_for_alert(alert_id, lat, lon, now):
    existing = find_reusable(lat, lon, now)
    if existing is not None:
        if existing["status"] == "UNASSIGNED":
            if existing["offers_made"] < MAX_OFFERS:
                db.execute("UPDATE dispatches SET status='PENDING', close_reason=NULL"
                           " WHERE id=?", (existing["id"],))
                offer_next(existing["id"], now, escalate_if_unassigned=False)
        return existing["id"]
    cur = db.execute("INSERT INTO dispatches (alert_id, status, latitude, longitude,"
                     " created_at) VALUES (?,?,?,?,?)",
                     (alert_id, "PENDING", lat, lon, ts(now)))
    did = cur.lastrowid
    offer_next(did, now)
    return did


def accept(offer_id, officer_id, now):
    o = db.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
    if o is None:
        return "not_found"
    if o["officer_id"] != officer_id:
        return "not_your_offer"
    d = get_d(o["dispatch_id"])
    if d["status"] in ("ACCEPTED", "EN_ROUTE", "ARRIVED",
                       "RESOLVED", "CANCELLED", "UNASSIGNED"):
        return "already_taken"
    if o["status"] != "PENDING":
        return "closed"
    if (datetime.fromisoformat(o["expires_at"]) - now).total_seconds() <= 0:
        return "expired"
    db.execute("UPDATE offers SET status='ACCEPTED' WHERE id=?", (offer_id,))
    db.execute("UPDATE dispatches SET status='ACCEPTED', accepted_by_id=? WHERE id=?",
               (officer_id, o["dispatch_id"]))
    return "ok"


def decline(offer_id, officer_id, now):
    o = db.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
    if o is None or o["officer_id"] != officer_id:
        return "not_found"
    if o["status"] != "PENDING":
        return "closed"
    db.execute("UPDATE offers SET status='DECLINED' WHERE id=?", (offer_id,))
    offer_next(o["dispatch_id"], now)
    return "ok"


def update_status(did, officer_id, new, now):
    allowed = {"EN_ROUTE": {"ACCEPTED"}, "ARRIVED": {"ACCEPTED", "EN_ROUTE"},
               "RESOLVED": {"ACCEPTED", "EN_ROUTE", "ARRIVED"}}
    if new not in allowed:
        return "unknown_status"
    d = get_d(did)
    if d is None:
        return "not_found"
    if d["accepted_by_id"] != officer_id:
        return "not_yours"
    if d["status"] not in allowed[new]:
        return "bad_transition"
    db.execute("UPDATE dispatches SET status=? WHERE id=?", (new, did))
    if new == "RESOLVED" and d["alert_id"] is not None:
        db.execute("UPDATE alerts SET acknowledged=1 WHERE id=?", (d["alert_id"],))
    return "ok"


def expire_and_escalate(now):
    lapsed = [r for r in db.execute("SELECT * FROM offers WHERE status='PENDING'")
              if datetime.fromisoformat(r["expires_at"]) <= now]
    for o in lapsed:
        db.execute("UPDATE offers SET status='TIMEOUT' WHERE id=?", (o["id"],))
    for o in lapsed:
        if get_d(o["dispatch_id"])["status"] in ("PENDING", "OFFERED"):
            offer_next(o["dispatch_id"], now)


def pending_count(did):
    return db.execute("SELECT COUNT(*) c FROM offers WHERE dispatch_id=? AND"
                      " status='PENDING'", (did,)).fetchone()["c"]


# --- scenario 1: escalation chain, single-PENDING invariant ----------------
db.execute("INSERT INTO alerts (id) VALUES (1)")
# Six officers at increasing distance from (19.0760, 72.8777).
for i, dlat in enumerate([0.001, 0.004, 0.010, 0.020, 0.030, 0.040], start=1):
    db.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?,?)",
               (i, f"officer{i}", "officer", 1, 1,
                19.0760 + dlat, 72.8777, ts(NOW)))

did = create_for_alert(1, 19.0760, 72.8777, NOW)
check("offer goes to the NEAREST officer first",
      db.execute("SELECT officer_id FROM offers WHERE dispatch_id=? AND sequence=1",
                 (did,)).fetchone()["officer_id"] == 1)
check("exactly one PENDING offer after raising", pending_count(did) == 1)

invariant_held = True
t = NOW
for round_no in range(1, 6):
    if pending_count(did) != 1 and get_d(did)["status"] == "OFFERED":
        invariant_held = False
    t = t + timedelta(seconds=OFFER_TIMEOUT + 1)
    expire_and_escalate(t)
    if pending_count(did) > 1:
        invariant_held = False

check("never more than one PENDING offer during a full escalation chain",
      invariant_held)
check("escalated to each officer in distance order",
      [r["officer_id"] for r in db.execute(
          "SELECT officer_id FROM offers WHERE dispatch_id=? ORDER BY sequence", (did,))]
      == [1, 2, 3, 4, 5])
check("gave up at DISPATCH_MAX_OFFERS, not at the 6th officer",
      get_d(did)["offers_made"] == MAX_OFFERS and get_d(did)["status"] == "UNASSIGNED")
check("admins emailed exactly once when nobody took it", len(EMAILS) == 1, f"{EMAILS}")

# --- scenario 2: the dedupe fix -------------------------------------------
emails_before = len(EMAILS)
rows_before = db.execute("SELECT COUNT(*) c FROM dispatches").fetchone()["c"]
# Three more alerts from the same crowd, 90 m away, while still UNASSIGNED.
for aid in (2, 3, 4):
    db.execute("INSERT INTO alerts (id) VALUES (?)", (aid,))
    create_for_alert(aid, 19.0768, 72.8777, t + timedelta(seconds=30))
check("repeat alerts at the same spot raise NO duplicate dispatch",
      db.execute("SELECT COUNT(*) c FROM dispatches").fetchone()["c"] == rows_before)
check("repeat alerts do NOT re-email admins (the bug just fixed)",
      len(EMAILS) == emails_before, f"{len(EMAILS) - emails_before} extra emails")

# A 7th officer comes on duty right next to the incident -> must be re-offered.
db.execute("INSERT INTO users VALUES (7,'officer7','officer',1,1,19.0761,72.8777,?)",
           (ts(t + timedelta(seconds=40)),))
db.execute("UPDATE dispatches SET offers_made=0 WHERE id=?", (did,))  # admin Retry
db.execute("DELETE FROM offers WHERE dispatch_id=?", (did,))
db.execute("INSERT INTO alerts (id) VALUES (5)")
create_for_alert(5, 19.0768, 72.8777, t + timedelta(seconds=45))
check("a newly-available officer IS paged on the existing record",
      get_d(did)["status"] == "OFFERED"
      and db.execute("SELECT officer_id FROM offers WHERE dispatch_id=?",
                     (did,)).fetchone()["officer_id"] == 7)

# --- scenario 3: accept/decline races and identity -------------------------
live = db.execute("SELECT * FROM offers WHERE dispatch_id=? AND status='PENDING'",
                  (did,)).fetchone()
tn = t + timedelta(seconds=50)
check("an officer cannot accept someone else's offer",
      accept(live["id"], 3, tn) == "not_your_offer")
check("the right officer accepts cleanly", accept(live["id"], 7, tn) == "ok")
check("a second accept on a claimed incident is 'already_taken'",
      accept(live["id"], 7, tn) == "already_taken")
check("accepted dispatch records who took it", get_d(did)["accepted_by_id"] == 7)
check("a busy officer is excluded from the next incident's ranking",
      7 not in [c.id for c, _ in rank_available(
          {"id": -1, "latitude": 19.0760, "longitude": 72.8777}, tn)])

# --- scenario 4: expiry beats a late accept -------------------------------
# 19.0900 is ~1.5 km from the first crowd: far enough not to be deduped into
# it, close enough that officers are still inside the 15 km search radius.
db.execute("INSERT INTO alerts (id) VALUES (6)")
did2 = create_for_alert(6, 19.0900, 72.8777, tn)
check("a second, genuinely distant incident is its own dispatch", did2 != did)
o2 = db.execute("SELECT * FROM offers WHERE dispatch_id=? AND status='PENDING'",
                (did2,)).fetchone()
check("the distant incident did find an officer in range", o2 is not None,
      f"dispatch {did2} is {get_d(did2)['status']}")
check("a late accept is refused as 'expired', not silently honoured",
      o2 is not None and
      accept(o2["id"], o2["officer_id"], tn + timedelta(seconds=OFFER_TIMEOUT + 5))
      == "expired")

# --- scenario 5: status transitions ---------------------------------------
check("EN_ROUTE is allowed from ACCEPTED", update_status(did, 7, "EN_ROUTE", tn) == "ok")
check("ARRIVED is allowed from EN_ROUTE", update_status(did, 7, "ARRIVED", tn) == "ok")
check("EN_ROUTE from ARRIVED is rejected as a bad transition",
      update_status(did, 7, "EN_ROUTE", tn) == "bad_transition")
check("a non-owner cannot move someone else's incident",
      update_status(did, 3, "RESOLVED", tn) == "not_yours")
check("an unknown status is rejected", update_status(did, 7, "DONE", tn) == "unknown_status")
check("RESOLVED is allowed from ARRIVED", update_status(did, 7, "RESOLVED", tn) == "ok")
check("resolving acknowledges the parent alert",
      db.execute("SELECT acknowledged FROM alerts WHERE id=1").fetchone()[0] == 1)
check("a resolved incident cannot be moved again",
      update_status(did, 7, "ARRIVED", tn) == "bad_transition")

# --- scenario 6: stale GPS excludes an officer ----------------------------
db.execute("UPDATE users SET last_location_at=? WHERE id=3",
           (ts(tn - timedelta(seconds=MAX_LOC_AGE + 60)),))
check("an officer whose phone went quiet is not offered incidents",
      3 not in [c.id for c, _ in rank_available(
          {"id": -2, "latitude": 19.0760, "longitude": 72.8777}, tn)])

# --- scenario 7: a resolved incident does not block a genuinely new one ----
db.execute("INSERT INTO alerts (id) VALUES (7)")
db.execute("UPDATE users SET on_duty=1, last_location_at=? WHERE id=4", (ts(tn),))
did3 = create_for_alert(7, 19.0760, 72.8777, tn + timedelta(seconds=5))
check("a new alert after RESOLVED opens a fresh incident", did3 != did)

# --- scenario 8: the email flood, with an empty roster --------------------
# The real-world shape of the bug just fixed: a 30-minute surge at a venue
# with nobody on duty. Every repeat alert used to open its own dispatch and
# send its own "NO OFFICER ASSIGNED" email.
db.execute("UPDATE users SET on_duty=0")
rows_before = db.execute("SELECT COUNT(*) c FROM dispatches").fetchone()["c"]
emails_before = len(EMAILS)
t8 = tn + timedelta(seconds=10)
for aid in range(10, 20):           # ten alerts from one sustained crowd
    db.execute("INSERT INTO alerts (id) VALUES (?)", (aid,))
    create_for_alert(aid, 19.2000, 72.8777, t8 + timedelta(seconds=aid * 20))

new_rows = db.execute("SELECT COUNT(*) c FROM dispatches").fetchone()["c"] - rows_before
new_mail = len(EMAILS) - emails_before
check("a 10-alert surge with an empty roster opens ONE dispatch",
      new_rows == 1, f"opened {new_rows}")
check("a 10-alert surge with an empty roster sends ONE escalation email",
      new_mail == 1, f"sent {new_mail}")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("\nFAILED:")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
