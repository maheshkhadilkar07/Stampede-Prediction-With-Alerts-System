"""
dispatch/simulation.py
========================
An admin-only 3D view of the dispatch system, and the endpoints behind it.

WHAT THIS IS
------------
A rendered map of the things dispatch actually reasons about: registered
camera locations, on-duty officers and their last reported positions, and
live incidents with the offer currently sitting on somebody's phone.

It is wired to the real engine. Raising an incident here calls the same
create_manual_dispatch() the admin board calls, which runs the same
nearest-first ranking and sends the same Web Push notification. An officer
holding a real phone sees a real offer and taps a real Accept button. There
is no parallel "simulation mode" inside the engine, because a simulation that
exercises different code from production tells you nothing about production.

WHAT THAT COSTS, AND HOW IT IS CONTAINED
----------------------------------------
Driving the real engine means leaving real rows behind. Two mitigations,
both deliberate:

  1. Every incident raised here is marked Dispatch.is_simulated = True. The
     dispatch board excludes those rows from its response-time average,
     which is the one number someone would quote as evidence the system
     works, and would be nonsense if a demo officer accepting in two seconds
     were averaged into it.

  2. Demo officers are real officer accounts, because the engine must see
     them exactly as it sees anyone else - but they are created with a
     random unusable password and an address at the RFC 2606 reserved
     .invalid TLD. They cannot be logged into and cannot receive mail.

Both are purgeable in one action from the page, and the purge is scoped by
those two markers rather than by guessing from names or timestamps.

WHY DEMO OFFICERS EXIST AT ALL
------------------------------
Without them the honest behaviour of an empty roster takes over: every
incident goes straight to UNASSIGNED, which is correct but demonstrates
nothing. Placing officers on the map is what lets the sequential
nearest-first escalation actually be seen happening.

Save this file at: Stampede-Prediction-System/dispatch/simulation.py
"""

import secrets
from datetime import datetime

from flask import Blueprint, jsonify, render_template, request

import config
from database.database import db
from database.models import CameraLocation, Dispatch, DispatchOffer, User
from utils.logger import get_logger

from dispatch import geo, push
from dispatch.engine import dispatch_engine

logger = get_logger(__name__)

simulation_bp = Blueprint("simulation", __name__, url_prefix="/admin/simulation")


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def _is_sim_officer(user) -> bool:
    return bool(user.username and user.username.startswith(config.SIMULATION_OFFICER_PREFIX))


def _sim_officers():
    """Every demo officer account, oldest first so numbering stays stable."""
    return (
        User.query
        .filter(User.role == "officer")
        .filter(User.username.like(f"{config.SIMULATION_OFFICER_PREFIX}%"))
        .order_by(User.id)
        .all()
    )


def _next_sim_index() -> int:
    """
    Lowest unused suffix, so deleting #2 of three and adding one reuses 2
    rather than creating a confusing sim-officer-4 next to 1 and 3.
    """
    used = set()
    for officer in _sim_officers():
        suffix = officer.username[len(config.SIMULATION_OFFICER_PREFIX):]
        if suffix.isdigit():
            used.add(int(suffix))
    index = 1
    while index in used:
        index += 1
    return index


def _serialise_officer(officer) -> dict:
    """
    One officer as the 3D view needs them.

    `dispatchable` is computed with the engine's own predicate rather than
    re-derived here, so a marker that looks available on the map is
    available to the engine - the view must not imply eligibility rules
    that differ from the ones actually used to rank.
    """
    age = geo.location_age_seconds(officer.last_location_at)
    return {
        "id": officer.id,
        "username": officer.username,
        "badge_number": officer.badge_number,
        "is_simulated": _is_sim_officer(officer),
        "on_duty": bool(officer.on_duty),
        "is_verified": bool(officer.is_verified),
        "latitude": officer.last_latitude,
        "longitude": officer.last_longitude,
        "location_age_seconds": int(age) if age is not None else None,
        "location_fresh": officer.has_fresh_location(
            config.OFFICER_LOCATION_MAX_AGE_SECONDS
        ),
        "dispatchable": officer.is_dispatchable(
            config.OFFICER_LOCATION_MAX_AGE_SECONDS
        ),
    }


def _world_payload() -> dict:
    """
    Everything the scene draws, from one consistent read.

    Assembled in a single response rather than three endpoints because the
    view has to be internally coherent: an officer shown holding an offer
    must be the same officer the offer says it went to, and two round trips
    can straddle an escalation and disagree.
    """
    locations = (
        CameraLocation.query
        .filter_by(is_active=True)
        .order_by(CameraLocation.name)
        .all()
    )
    officers = User.query.filter_by(role="officer").order_by(User.username).all()

    # Closed incidents are included but capped: the scene fades them out, and
    # an admin wants to see what just happened without loading a year of
    # history into a WebGL context.
    live = (
        Dispatch.query
        .filter(Dispatch.status.in_(
            ["PENDING", "OFFERED", "ACCEPTED", "EN_ROUTE", "ARRIVED"]
        ))
        .order_by(Dispatch.created_at.desc())
        .all()
    )
    recent_closed = (
        Dispatch.query
        .filter(Dispatch.status.in_(["RESOLVED", "CANCELLED", "UNASSIGNED"]))
        .order_by(Dispatch.created_at.desc())
        .limit(12)
        .all()
    )

    return {
        "ok": True,
        "server_time": datetime.utcnow().isoformat() + "Z",
        "locations": [
            {
                "id": loc.id,
                "name": loc.name,
                "description": loc.description,
                "latitude": loc.latitude,
                "longitude": loc.longitude,
                "is_demo": bool(
                    loc.name and loc.name.startswith(config.SIMULATION_LOCATION_PREFIX)
                ),
            }
            for loc in locations
        ],
        "officers": [_serialise_officer(o) for o in officers],
        "dispatches": [d.to_dict() for d in live],
        "closed": [d.to_dict() for d in recent_closed],
        "settings": {
            "offer_timeout": config.DISPATCH_OFFER_TIMEOUT_SECONDS,
            "max_offers": config.DISPATCH_MAX_OFFERS,
            "search_radius_km": config.DISPATCH_SEARCH_RADIUS_KM,
            "location_max_age": config.OFFICER_LOCATION_MAX_AGE_SECONDS,
            "dispatch_enabled": config.DISPATCH_ENABLED,
            "push_configured": push.push_is_configured(),
            "max_sim_officers": config.SIMULATION_MAX_OFFICERS,
            "average_speed_kmh": config.DISPATCH_AVERAGE_SPEED_KMH,
            "default_lat": config.SIMULATION_DEFAULT_LAT,
            "default_lon": config.SIMULATION_DEFAULT_LON,
            "trigger_levels": sorted(config.DISPATCH_TRIGGER_LEVELS),
        },
    }


def _sim_officer_or_error(officer_id):
    """
    Resolve a demo officer, refusing anything that is not one.

    This guard is the important one in this module. Every action below acts
    *as* an officer - accepting, declining, moving, going off duty - and
    doing that to a real account would put words in a real person's mouth:
    their response record would show them accepting an incident they never
    saw. The simulation may only ever drive accounts it created itself.
    """
    officer = db.session.get(User, int(officer_id))
    if officer is None:
        return None, ({"ok": False, "reason": "not_found",
                       "message": "No such officer."}, 404)
    if not _is_sim_officer(officer):
        return None, ({
            "ok": False, "reason": "not_simulated",
            "message": (
                f"{officer.username} is a real account. The simulation can "
                "only act on officers it created, because accepting on "
                "someone's behalf would falsify their response record."
            ),
        }, 403)
    return officer, None


# ---------------------------------------------------------------------------
# PAGE
# ---------------------------------------------------------------------------
def _register_views():
    """
    Routes are attached inside a function so the decorators can be imported
    from dispatch.routes without a circular import at module load: routes.py
    imports this module only from inside register_dispatch_blueprints(),
    by which point routes.py itself is fully imported.
    """
    from dispatch.routes import admin_required, json_only

    @simulation_bp.route("/")
    @admin_required
    def home():
        world = _world_payload()
        # The initial payload is embedded so the scene can draw immediately
        # rather than showing an empty map for one poll interval - the first
        # impression of this page is the whole point of it existing.
        return render_template(
            "simulation.html",
            world=world,
            settings=world["settings"],
            simulation_enabled=config.SIMULATION_ENABLED,
            location_count=len(world["locations"]),
            officer_count=len(world["officers"]),
        )

    @simulation_bp.route("/api/world")
    @admin_required
    def api_world():
        return jsonify(_world_payload())

    # -----------------------------------------------------------------------
    # RAISING AN INCIDENT
    # -----------------------------------------------------------------------
    @simulation_bp.route("/api/raise", methods=["POST"])
    @admin_required
    @json_only
    def api_raise():
        """
        Raise a real incident at a registered location.

        Goes through the same create_manual_dispatch the admin board uses, so
        what happens next is not simulated at all: real ranking, real push,
        a real countdown.
        """
        if not config.SIMULATION_ENABLED:
            return jsonify({"ok": False, "reason": "disabled",
                            "message": "SIMULATION_ENABLED is false."}), 403

        payload = request.get_json(silent=True) or {}
        location_id = payload.get("location_id")
        risk_level = (payload.get("risk_level") or "HIGH").upper()

        if location_id is None:
            return jsonify({"ok": False, "reason": "no_location",
                            "message": "Pick a camera location first."}), 400

        location = db.session.get(CameraLocation, int(location_id))
        if location is None:
            return jsonify({"ok": False, "reason": "not_found",
                            "message": "No such location."}), 404

        dispatch_id = dispatch_engine.create_manual_dispatch(
            location_id=location.id,
            risk_level=risk_level,
            message=f"[SIMULATION] {risk_level} crowd risk at {location.name}.",
            is_simulated=True,
        )
        if dispatch_id is None:
            return jsonify({
                "ok": False, "reason": "not_created",
                "message": (
                    "The engine declined to raise this. Usually that means "
                    "DISPATCH_ENABLED is false, or the location has no "
                    "coordinates."
                ),
            }), 400

        dispatch = db.session.get(Dispatch, dispatch_id)
        logger.info("Simulation raised dispatch %s at %s.", dispatch_id, location.name)
        return jsonify({"ok": True, "dispatch": dispatch.to_dict()})

    # -----------------------------------------------------------------------
    # DEMO OFFICERS
    # -----------------------------------------------------------------------
    @simulation_bp.route("/api/officers", methods=["POST"])
    @admin_required
    @json_only
    def api_spawn_officer():
        """Create a demo officer, on duty, at the given position."""
        payload = request.get_json(silent=True) or {}
        latitude, longitude = payload.get("latitude"), payload.get("longitude")

        if not geo.is_valid_coordinate(latitude, longitude):
            return jsonify({"ok": False, "reason": "bad_coordinates",
                            "message": "Need a valid latitude and longitude."}), 400

        existing = _sim_officers()
        if len(existing) >= config.SIMULATION_MAX_OFFICERS:
            return jsonify({
                "ok": False, "reason": "limit",
                "message": (
                    f"Already {len(existing)} demo officers "
                    f"(SIMULATION_MAX_OFFICERS={config.SIMULATION_MAX_OFFICERS})."
                ),
            }), 400

        index = _next_sim_index()
        username = f"{config.SIMULATION_OFFICER_PREFIX}{index}"
        officer = User(
            username=username,
            email=f"{username}@{config.SIMULATION_EMAIL_DOMAIN}",
            role="officer",
            # Verified so the engine's eligibility check passes; there is no
            # inbox to confirm an OTP from, and these accounts exist only to
            # be dispatched to.
            is_verified=True,
            badge_number=f"SIM-{index:03d}",
            on_duty=True,
            last_latitude=float(latitude),
            last_longitude=float(longitude),
            last_location_at=datetime.utcnow(),
        )
        # Random and never revealed: these accounts are dispatch targets, not
        # logins. Nobody - including whoever is running the demo - can sign
        # in as one.
        officer.set_password(secrets.token_urlsafe(32))

        db.session.add(officer)
        db.session.commit()
        logger.info("Simulation created demo officer %s.", username)
        return jsonify({"ok": True, "officer": _serialise_officer(officer)})

    @simulation_bp.route("/api/officers/<int:officer_id>/move", methods=["POST"])
    @admin_required
    @json_only
    def api_move_officer(officer_id):
        """
        Reposition a demo officer.

        Writes last_location_at as well as the coordinates, because a
        position without a timestamp is exactly what the engine refuses to
        trust - moving a marker has to make it dispatchable, or the map
        would show an officer somewhere the engine will not send anyone.
        """
        officer, error = _sim_officer_or_error(officer_id)
        if error:
            return jsonify(error[0]), error[1]

        payload = request.get_json(silent=True) or {}
        latitude, longitude = payload.get("latitude"), payload.get("longitude")
        if not geo.is_valid_coordinate(latitude, longitude):
            return jsonify({"ok": False, "reason": "bad_coordinates",
                            "message": "Need a valid latitude and longitude."}), 400

        officer.last_latitude = float(latitude)
        officer.last_longitude = float(longitude)
        officer.last_location_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"ok": True, "officer": _serialise_officer(officer)})

    @simulation_bp.route("/api/officers/<int:officer_id>/duty", methods=["POST"])
    @admin_required
    @json_only
    def api_toggle_duty(officer_id):
        officer, error = _sim_officer_or_error(officer_id)
        if error:
            return jsonify(error[0]), error[1]

        payload = request.get_json(silent=True) or {}
        officer.on_duty = bool(payload.get("on_duty", not officer.on_duty))
        if officer.on_duty:
            officer.last_location_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"ok": True, "officer": _serialise_officer(officer)})

    @simulation_bp.route("/api/officers/<int:officer_id>/delete", methods=["POST"])
    @admin_required
    @json_only
    def api_delete_officer(officer_id):
        officer, error = _sim_officer_or_error(officer_id)
        if error:
            return jsonify(error[0]), error[1]

        # A demo officer holding a real incident must not vanish from under
        # it: deleting the row would null the dispatch's accepted_by_id and
        # leave a genuine record claiming nobody attended.
        real_holds = [
            d for d in officer.dispatches_accepted if not d.is_simulated
        ]
        if real_holds:
            return jsonify({
                "ok": False, "reason": "holds_real_dispatch",
                "message": (
                    f"{officer.username} is attached to {len(real_holds)} "
                    "non-simulated incident(s). Purge or resolve those first."
                ),
            }), 409

        username = officer.username
        db.session.delete(officer)
        db.session.commit()
        logger.info("Simulation deleted demo officer %s.", username)
        return jsonify({"ok": True})

    @simulation_bp.route("/api/heartbeat", methods=["POST"])
    @admin_required
    @json_only
    def api_heartbeat():
        """
        Keep demo officers' positions fresh while the page is open.

        A real officer's phone pings every OFFICER_LOCATION_PING_SECONDS; a
        marker on a map has nothing to ping with, so without this it would
        silently stop being dispatchable after
        OFFICER_LOCATION_MAX_AGE_SECONDS and the demo would mysteriously
        start reporting nobody available. Tying the refresh to this page
        being open gives demo officers exactly the right lifetime: they are
        standing by while someone is watching, and go stale afterwards.
        """
        now = datetime.utcnow()
        refreshed = 0
        for officer in _sim_officers():
            if officer.on_duty and officer.last_latitude is not None:
                officer.last_location_at = now
                refreshed += 1
        if refreshed:
            db.session.commit()
        return jsonify({"ok": True, "refreshed": refreshed})

    # -----------------------------------------------------------------------
    # ACTING AS A DEMO OFFICER
    # -----------------------------------------------------------------------
    @simulation_bp.route("/api/offers/<int:offer_id>/accept", methods=["POST"])
    @admin_required
    @json_only
    def api_accept(offer_id):
        """Accept on a demo officer's behalf, through the real engine."""
        offer = db.session.get(DispatchOffer, offer_id)
        if offer is None:
            return jsonify({"ok": False, "reason": "not_found",
                            "message": "That offer no longer exists."}), 404

        _, error = _sim_officer_or_error(offer.officer_id)
        if error:
            return jsonify(error[0]), error[1]

        result = dispatch_engine.accept_offer(offer.id, offer.officer_id)
        return jsonify(result), (200 if result.get("ok") else 409)

    @simulation_bp.route("/api/offers/<int:offer_id>/decline", methods=["POST"])
    @admin_required
    @json_only
    def api_decline(offer_id):
        offer = db.session.get(DispatchOffer, offer_id)
        if offer is None:
            return jsonify({"ok": False, "reason": "not_found",
                            "message": "That offer no longer exists."}), 404

        _, error = _sim_officer_or_error(offer.officer_id)
        if error:
            return jsonify(error[0]), error[1]

        result = dispatch_engine.decline_offer(offer.id, offer.officer_id)
        return jsonify(result), (200 if result.get("ok") else 409)

    @simulation_bp.route("/api/dispatches/<int:dispatch_id>/status", methods=["POST"])
    @admin_required
    @json_only
    def api_status(dispatch_id):
        """Move a demo officer's incident along: EN_ROUTE / ARRIVED / RESOLVED."""
        dispatch = db.session.get(Dispatch, dispatch_id)
        if dispatch is None:
            return jsonify({"ok": False, "reason": "not_found"}), 404
        if dispatch.accepted_by_id is None:
            return jsonify({"ok": False, "reason": "not_accepted",
                            "message": "Nobody has accepted this yet."}), 409

        _, error = _sim_officer_or_error(dispatch.accepted_by_id)
        if error:
            return jsonify(error[0]), error[1]

        payload = request.get_json(silent=True) or {}
        new_status = (payload.get("status") or "").upper()
        result = dispatch_engine.update_status(
            dispatch.id, dispatch.accepted_by_id, new_status,
        )
        return jsonify(result), (200 if result.get("ok") else 409)

    # -----------------------------------------------------------------------
    # DEMO VENUE
    # -----------------------------------------------------------------------
    @simulation_bp.route("/api/seed-venue", methods=["POST"])
    @admin_required
    @json_only
    def api_seed_venue():
        """
        Register a starter set of monitored sites around a centre point.

        Only runs when there is nothing seeded already, so pressing the
        button twice does not produce two overlapping venues. These are
        ordinary CameraLocation rows - the admin can rename them, move them
        or delete them from Admin > Locations like any other.
        """
        payload = request.get_json(silent=True) or {}
        latitude = payload.get("latitude", config.SIMULATION_DEFAULT_LAT)
        longitude = payload.get("longitude", config.SIMULATION_DEFAULT_LON)
        spread_m = float(payload.get("spread_m", 400))

        if not geo.is_valid_coordinate(latitude, longitude):
            return jsonify({"ok": False, "reason": "bad_coordinates",
                            "message": "Need a valid centre latitude/longitude."}), 400
        spread_m = max(50.0, min(5000.0, spread_m))

        already = CameraLocation.query.filter(
            CameraLocation.name.like(f"{config.SIMULATION_LOCATION_PREFIX}%")
        ).count()
        if already:
            return jsonify({
                "ok": False, "reason": "already_seeded",
                "message": (
                    f"{already} demo location(s) already registered. Purge "
                    "them first if you want a different centre."
                ),
            }), 409

        # Offsets in metres (east, north) laid out as a plausible venue:
        # a gate, two stands, a concourse, a platform and an exit.
        layout = [
            ("Main Gate",        0.0,  -1.0),
            ("North Concourse",  0.0,   1.0),
            ("East Stand",       1.0,   0.3),
            ("West Stand",      -1.0,   0.3),
            ("Platform 1",       0.7,  -0.8),
            ("Exit C",          -0.7,  -0.8),
        ]
        # Degrees per metre at this latitude, on the same sphere geo.py
        # measures with - a seeded venue should sit exactly where the
        # engine's own distance maths says it does. Longitude degrees shrink
        # with cos(latitude), which matters even at venue scale: ignoring it
        # would stretch the layout east-west by ~6% in Mumbai and much more
        # further from the equator.
        from math import cos, pi, radians
        metres_per_deg_lat = geo.EARTH_RADIUS_KM * 1000 * pi / 180
        lat0, lon0 = float(latitude), float(longitude)
        d_lat = 1.0 / metres_per_deg_lat
        d_lon = 1.0 / (metres_per_deg_lat * max(0.01, cos(radians(lat0))))

        created = []
        for name, east, north in layout:
            location = CameraLocation(
                name=f"{config.SIMULATION_LOCATION_PREFIX}{name}",
                description="Seeded by the 3D simulation. Safe to rename or delete.",
                latitude=lat0 + north * spread_m * d_lat,
                longitude=lon0 + east * spread_m * d_lon,
                is_active=True,
            )
            db.session.add(location)
            created.append(location)
        db.session.commit()

        logger.info("Simulation seeded %s demo locations at %.5f, %.5f.",
                    len(created), lat0, lon0)
        return jsonify({
            "ok": True,
            "created": [{"id": c.id, "name": c.name,
                         "latitude": c.latitude, "longitude": c.longitude}
                        for c in created],
        })

    # -----------------------------------------------------------------------
    # PURGE
    # -----------------------------------------------------------------------
    @simulation_bp.route("/api/purge", methods=["POST"])
    @admin_required
    @json_only
    def api_purge():
        """
        Remove everything the simulation created, and nothing else.

        Scoped by the two markers rather than by name-guessing or a time
        window: Dispatch.is_simulated for incidents, the username prefix for
        demo officers. Incidents go first - deleting an officer still
        attached to one would null that incident's accepted_by_id instead of
        removing the pair together.
        """
        sim_dispatches = Dispatch.query.filter_by(is_simulated=True).all()
        dispatch_count = len(sim_dispatches)
        offer_count = sum(len(d.offers) for d in sim_dispatches)
        for dispatch in sim_dispatches:
            db.session.delete(dispatch)        # offers cascade with it
        db.session.commit()

        officer_count, skipped = 0, []
        for officer in _sim_officers():
            if any(not d.is_simulated for d in officer.dispatches_accepted):
                skipped.append(officer.username)
                continue
            db.session.delete(officer)
            officer_count += 1
        db.session.commit()

        # Seeded locations last, and only if nothing real still points at
        # them. A genuine incident raised at a demo gate is still a genuine
        # incident, and deleting the site under it would strip the record of
        # where it happened.
        location_count, locations_skipped = 0, []
        seeded = CameraLocation.query.filter(
            CameraLocation.name.like(f"{config.SIMULATION_LOCATION_PREFIX}%")
        ).all()
        for location in seeded:
            still_used = Dispatch.query.filter_by(location_id=location.id).count()
            if still_used:
                locations_skipped.append(location.name)
                continue
            db.session.delete(location)
            location_count += 1
        db.session.commit()

        logger.warning(
            "Simulation purge: %s dispatches, %s offers, %s demo officers, "
            "%s demo locations removed.",
            dispatch_count, offer_count, officer_count, location_count,
        )
        return jsonify({
            "ok": True,
            "dispatches_removed": dispatch_count,
            "offers_removed": offer_count,
            "officers_removed": officer_count,
            "locations_removed": location_count,
            "officers_skipped": skipped,
            "locations_skipped": locations_skipped,
        })


_register_views()
