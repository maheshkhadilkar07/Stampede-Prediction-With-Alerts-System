"""
scripts/verify_simulation.py — verification for the 3D dispatch simulation.

    python scripts/verify_simulation.py

Needs nothing installed but Python. Three parts:

  PART A — AST assertions against the REAL dispatch/simulation.py. The
           important one: every endpoint that acts *as* an officer must go
           through _sim_officer_or_error first, because accepting an incident
           on a real officer's behalf would falsify that person's response
           record. Also checks every mutating endpoint is admin-gated and
           JSON-only.

  PART B — the purge, re-typed over stdlib sqlite3 against a database
           deliberately mixing real and simulated rows. A purge that takes
           one real row with it is worse than no purge at all, so this also
           runs a NEGATIVE CONTROL: a naive "delete the officers that look
           like officers" purge, to show the markers are doing real work.

  PART C — the seeded venue's geometry, checked against dispatch/geo.py's
           own haversine (imported for real - it is stdlib-only by design).

Part B tests the logic as transcribed here, not the shipped bytes; Part A is
what covers the shipped file. Neither replaces running the app.
"""

import ast
import os
import sqlite3
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "dispatch"))
import geo  # real shipped module

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(f"  [{'ok  ' if condition else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail and not condition else ""))


# ===========================================================================
# PART A — structural checks on the shipped simulation.py
# ===========================================================================
print("\nPART A  structural checks on dispatch/simulation.py")

src = open(os.path.join(ROOT, "dispatch", "simulation.py"), encoding="utf-8").read()
tree = ast.parse(src)

views = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def decorator_names(fn):
    out = set()
    for d in fn.decorator_list:
        if isinstance(d, ast.Name):
            out.add(d.id)
        elif isinstance(d, ast.Attribute):
            out.add(d.attr)
        elif isinstance(d, ast.Call):
            f = d.func
            out.add(getattr(f, "attr", getattr(f, "id", "")))
    return out


def body_calls(fn):
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            out.add(getattr(f, "attr", getattr(f, "id", "")))
    return out


# Endpoints that act as an officer. Each MUST verify the account is one the
# simulation created before doing anything.
ACTS_AS_OFFICER = [
    "api_move_officer", "api_toggle_duty", "api_delete_officer",
    "api_accept", "api_decline", "api_status",
]
for name in ACTS_AS_OFFICER:
    present = name in views
    guarded = present and "_sim_officer_or_error" in body_calls(views[name])
    check(f"{name}() refuses non-simulated accounts", guarded,
          "missing _sim_officer_or_error" if present else "endpoint not found")

# Every mutating endpoint is admin-only and JSON-only.
MUTATORS = ACTS_AS_OFFICER + [
    "api_raise", "api_spawn_officer", "api_heartbeat",
    "api_seed_venue", "api_purge",
]
for name in MUTATORS:
    decs = decorator_names(views[name]) if name in views else set()
    check(f"{name}() is @admin_required and @json_only",
          {"admin_required", "json_only"} <= decs, f"has {sorted(decs)}")

check("the read-only world endpoint is admin-gated",
      "admin_required" in decorator_names(views["api_world"]))
check("the page itself is admin-gated",
      "admin_required" in decorator_names(views["home"]))

# Incidents raised here must be marked, or they pollute the response-time
# average on the dispatch board.
raise_src = ast.get_source_segment(src, views["api_raise"])
check("api_raise marks the dispatch is_simulated=True",
      "is_simulated=True" in raise_src.replace(" ", ""))
check("api_raise goes through the real engine, not its own insert",
      "create_manual_dispatch" in raise_src and "Dispatch(" not in raise_src)

purge_src = ast.get_source_segment(src, views["api_purge"])
check("purge scopes dispatches by is_simulated",
      "is_simulated=True" in purge_src.replace(" ", ""))
check("purge scopes officers by the configured prefix, not a guessed name",
      "_sim_officers()" in purge_src)
check("purge scopes locations by the configured prefix",
      "SIMULATION_LOCATION_PREFIX" in purge_src)
check("purge refuses to orphan a real incident (officer guard)",
      "is_simulated for d in officer.dispatches_accepted" in purge_src
      or "not d.is_simulated" in purge_src)

spawn_src = ast.get_source_segment(src, views["api_spawn_officer"])
check("demo officers get a random password, never a known one",
      "secrets.token_urlsafe" in spawn_src)
check("demo officer addresses use the reserved .invalid domain",
      "SIMULATION_EMAIL_DOMAIN" in spawn_src)
check("demo officer creation is capped",
      "SIMULATION_MAX_OFFICERS" in spawn_src)


# ===========================================================================
# PART B — purge scoping over sqlite3
# ===========================================================================
print("\nPART B  purge scoping (re-typed, against mixed real/simulated data)")

PREFIX_OFFICER = "sim-officer-"
PREFIX_LOCATION = "[Demo] "


def build_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, role TEXT);
    CREATE TABLE camera_locations (id INTEGER PRIMARY KEY, name TEXT);
    CREATE TABLE dispatches (id INTEGER PRIMARY KEY, status TEXT,
      is_simulated INT NOT NULL DEFAULT 0, accepted_by_id INT, location_id INT);
    CREATE TABLE dispatch_offers (id INTEGER PRIMARY KEY, dispatch_id INT,
      officer_id INT);
    """)
    # --- real things that must survive ---
    db.execute("INSERT INTO users VALUES (1,'Mahesh','admin')")
    db.execute("INSERT INTO users VALUES (2,'Tulip','admin')")
    db.execute("INSERT INTO users VALUES (3,'officer_raj','officer')")
    db.execute("INSERT INTO camera_locations VALUES (1,'Main Platform')")
    db.execute("INSERT INTO camera_locations VALUES (2,'Gate 4')")
    db.execute("INSERT INTO dispatches VALUES (100,'RESOLVED',0,3,1)")
    db.execute("INSERT INTO dispatches VALUES (101,'UNASSIGNED',0,NULL,2)")
    db.execute("INSERT INTO dispatch_offers VALUES (900,100,3)")
    # --- simulated things that must go ---
    db.execute(f"INSERT INTO users VALUES (10,'{PREFIX_OFFICER}1','officer')")
    db.execute(f"INSERT INTO users VALUES (11,'{PREFIX_OFFICER}2','officer')")
    for i, n in enumerate(["Main Gate", "North Concourse", "East Stand",
                           "West Stand", "Platform 1", "Exit C"], start=10):
        db.execute("INSERT INTO camera_locations VALUES (?,?)",
                   (i, f"{PREFIX_LOCATION}{n}"))
    db.execute("INSERT INTO dispatches VALUES (200,'RESOLVED',1,10,10)")
    db.execute("INSERT INTO dispatches VALUES (201,'OFFERED',1,NULL,11)")
    db.execute("INSERT INTO dispatch_offers VALUES (910,200,10)")
    db.execute("INSERT INTO dispatch_offers VALUES (911,201,11)")
    db.commit()
    return db


def purge(db):
    """The shipped purge, re-typed: markers only, incidents before officers."""
    sims = db.execute("SELECT id FROM dispatches WHERE is_simulated=1").fetchall()
    ids = [r["id"] for r in sims]
    offers = 0
    for did in ids:
        offers += db.execute("SELECT COUNT(*) c FROM dispatch_offers WHERE dispatch_id=?",
                             (did,)).fetchone()["c"]
        db.execute("DELETE FROM dispatch_offers WHERE dispatch_id=?", (did,))
        db.execute("DELETE FROM dispatches WHERE id=?", (did,))

    removed_officers, skipped = 0, []
    for row in db.execute("SELECT id, username FROM users WHERE role='officer' AND "
                          "username LIKE ?", (PREFIX_OFFICER + "%",)).fetchall():
        holds_real = db.execute(
            "SELECT COUNT(*) c FROM dispatches WHERE accepted_by_id=? AND is_simulated=0",
            (row["id"],)).fetchone()["c"]
        if holds_real:
            skipped.append(row["username"]); continue
        db.execute("DELETE FROM dispatch_offers WHERE officer_id=?", (row["id"],))
        db.execute("DELETE FROM users WHERE id=?", (row["id"],))
        removed_officers += 1

    removed_locs, locs_skipped = 0, []
    for row in db.execute("SELECT id, name FROM camera_locations WHERE name LIKE ?",
                          (PREFIX_LOCATION + "%",)).fetchall():
        used = db.execute("SELECT COUNT(*) c FROM dispatches WHERE location_id=?",
                          (row["id"],)).fetchone()["c"]
        if used:
            locs_skipped.append(row["name"]); continue
        db.execute("DELETE FROM camera_locations WHERE id=?", (row["id"],))
        removed_locs += 1

    db.commit()
    return {"dispatches": len(ids), "offers": offers, "officers": removed_officers,
            "locations": removed_locs, "officers_skipped": skipped,
            "locations_skipped": locs_skipped}


db = build_db()
result = purge(db)


def rows(table, where="1=1"):
    return [dict(r) for r in db.execute(f"SELECT * FROM {table} WHERE {where}")]


check("removed both simulated dispatches", result["dispatches"] == 2, str(result))
check("removed their offers", result["offers"] == 2, str(result))
check("removed both demo officers", result["officers"] == 2, str(result))
check("removed all six demo locations", result["locations"] == 6, str(result))

check("real admin accounts untouched",
      {r["username"] for r in rows("users", "role='admin'")} == {"Mahesh", "Tulip"})
check("the real officer account survived",
      len(rows("users", "username='officer_raj'")) == 1)
check("no simulated officer rows remain",
      not rows("users", f"username LIKE '{PREFIX_OFFICER}%'"))
check("both real dispatches survived",
      {r["id"] for r in rows("dispatches")} == {100, 101})
check("the real dispatch kept its officer",
      rows("dispatches", "id=100")[0]["accepted_by_id"] == 3)
check("the real offer row survived", len(rows("dispatch_offers", "id=900")) == 1)
check("real camera locations survived",
      {r["name"] for r in rows("camera_locations")} == {"Main Platform", "Gate 4"})

# --- the guards -----------------------------------------------------------
db2 = build_db()
# A demo officer is holding a REAL incident: deleting them would strip a
# genuine record of who attended.
db2.execute("UPDATE dispatches SET accepted_by_id=10 WHERE id=101 AND is_simulated=0")
db2.execute("UPDATE dispatches SET status='ACCEPTED' WHERE id=101")
db2.commit()
r2 = purge(db2)
check("a demo officer holding a REAL incident is kept, not deleted",
      r2["officers_skipped"] == [f"{PREFIX_OFFICER}1"], str(r2["officers_skipped"]))
check("that real incident still names its officer",
      db2.execute("SELECT accepted_by_id FROM dispatches WHERE id=101")
         .fetchone()["accepted_by_id"] == 10)

db3 = build_db()
# A real incident was raised at a seeded demo location.
db3.execute("UPDATE dispatches SET location_id=11 WHERE id=101 AND is_simulated=0")
db3.commit()
r3 = purge(db3)
check("a demo location used by a REAL incident is kept",
      len(r3["locations_skipped"]) == 1 and r3["locations"] == 5, str(r3))

# --- negative control -----------------------------------------------------
# Prove the markers are load-bearing: a plausible-looking purge that matches
# on role or on "officer" in the name destroys the real roster.
db4 = build_db()
naive_officers = [r["username"] for r in db4.execute(
    "SELECT username FROM users WHERE role='officer'")]
naive_locs = db4.execute("SELECT COUNT(*) c FROM camera_locations").fetchone()["c"]
check("NEGATIVE CONTROL: purging by role alone would delete the real officer",
      "officer_raj" in naive_officers)
check("NEGATIVE CONTROL: purging all locations would delete the real sites",
      naive_locs == 8)


# ===========================================================================
# PART C — seeded venue geometry
# ===========================================================================
print("\nPART C  seeded venue geometry (checked against the real geo.py)")

from math import cos, pi, radians

lat0, lon0, spread = 19.0760, 72.8777, 400.0
m_per_deg_lat = geo.EARTH_RADIUS_KM * 1000 * pi / 180
d_lat = 1.0 / m_per_deg_lat
d_lon = 1.0 / (m_per_deg_lat * max(0.01, cos(radians(lat0))))
layout = [("Main Gate", 0.0, -1.0), ("North Concourse", 0.0, 1.0),
          ("East Stand", 1.0, 0.3), ("West Stand", -1.0, 0.3),
          ("Platform 1", 0.7, -0.8), ("Exit C", -0.7, -0.8)]
seeded = [(n, lat0 + nz * spread * d_lat, lon0 + e * spread * d_lon)
          for n, e, nz in layout]

check("seeds six sites", len(seeded) == 6)

# A pure north offset of `spread` metres must measure as `spread` metres.
north = next(s for s in seeded if s[0] == "North Concourse")
d_north = geo.haversine_km(lat0, lon0, north[1], north[2]) * 1000
check("a 400 m north offset measures 400 m", abs(d_north - 400) < 0.5,
      f"{d_north:.2f} m")

# A pure east offset must too - this is the cos(latitude) correction. Without
# it this would come out ~6% short in Mumbai.
east = next(s for s in seeded if s[0] == "East Stand")
d_east_lon_only = geo.haversine_km(lat0, lon0, lat0, east[2]) * 1000
check("a 400 m east offset measures 400 m (cos-latitude applied)",
      abs(d_east_lon_only - 400) < 0.5, f"{d_east_lon_only:.2f} m")

naive_lon = lon0 + 1.0 * spread * (1.0 / m_per_deg_lat)   # forgetting cos(lat)
d_naive = geo.haversine_km(lat0, lon0, lat0, naive_lon) * 1000
check("NEGATIVE CONTROL: omitting cos(latitude) would be ~6% wrong",
      abs(d_naive - 400) > 15, f"{d_naive:.2f} m")

# Nothing should land outside the dispatch search radius of the centre.
worst = max(geo.haversine_km(lat0, lon0, s[1], s[2]) for s in seeded)
check("every seeded site is well inside the 15 km search radius", worst < 1.0,
      f"{worst:.3f} km")

names = [s[0] for s in seeded]
check("site names are unique", len(set(names)) == len(names))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("\nFAILED:")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
