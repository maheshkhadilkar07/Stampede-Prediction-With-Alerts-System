"""
dispatch/routes.py
====================
Everything an officer or an admin touches: the officer PWA, the JSON API
behind it, Web Push registration, and the admin dispatch board.

Organised as blueprints rather than more routes in app.py. app.py is already
1,400 lines with every route in one module; adding twenty more to it would
make the dispatch feature impossible to read as a unit. The blueprints are
registered by register_dispatch_blueprints(app) at the end of app.py.

CSRF
----
app.py installs a global CSRFProtect, which guards every POST in this file
too. The browser-side code therefore sends the token in an X-CSRFToken
header, which Flask-WTF checks exactly as it checks a hidden form field.
These endpoints are deliberately NOT exempted: a JSON API that mutates
dispatch state is precisely the kind of thing CSRF protection exists for,
and `json_only` below is a second line of defence rather than a substitute.

Save this file at: Stampede-Prediction-System/dispatch/routes.py
"""

from datetime import datetime
from functools import wraps

from flask import (
    Blueprint, Response, abort, flash, jsonify, redirect,
    render_template, request, url_for,
)
from flask_login import current_user, login_required

import config
from database.database import db
from database.models import (
    CameraLocation, Dispatch, DispatchOffer, PushSubscription, User,
)
from utils.logger import get_logger

from dispatch import geo, push
from dispatch.engine import dispatch_engine

logger = get_logger(__name__)

officer_bp = Blueprint("officer", __name__, url_prefix="/officer")
officer_api_bp = Blueprint("officer_api", __name__, url_prefix="/api/officer")
push_api_bp = Blueprint("push_api", __name__, url_prefix="/api/push")
dispatch_admin_bp = Blueprint("dispatch_admin", __name__, url_prefix="/admin")


# ---------------------------------------------------------------------------
# ACCESS CONTROL
# ---------------------------------------------------------------------------
def officer_required(view):
    """
    Officers and admins both. Admins are allowed in so an administrator can
    see exactly what their officers see while testing, without having to
    create a second account and juggle logins.
    """
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not (current_user.is_officer or current_user.is_admin):
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def json_only(view):
    """
    Refuse anything that is not application/json.

    Defence in depth behind CSRFProtect: a cross-site form POST cannot set
    this content type without triggering a CORS preflight the browser will
    block, so even a hypothetical CSRF bypass still cannot drive these
    endpoints from another origin.
    """
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not request.is_json:
            return jsonify({
                "ok": False,
                "reason": "expected_json",
                "message": "This endpoint expects a JSON request body.",
            }), 415
        return view(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# SERIALISERS
# ---------------------------------------------------------------------------
def _place_of(dispatch):
    """
    The destination described three ways, because the officer reads them at
    different moments: the name to recognise it, the description for context,
    and raw coordinates as the fallback that is always true even when nobody
    bothered to name the camera.
    """
    description = None
    if dispatch.location_id:
        location = db.session.get(CameraLocation, dispatch.location_id)
        if location is not None:
            description = location.description

    return {
        "name": dispatch.location_name or "Unnamed camera location",
        "venue": description or "",
        "address": f"{dispatch.latitude:.5f}, {dispatch.longitude:.5f}",
    }


def _serialise_offer(offer):
    dispatch = offer.dispatch
    return {
        "offer_id": offer.id,
        "dispatch_id": dispatch.id,
        "sequence": offer.sequence,
        "risk_level": dispatch.risk_level,
        "message": dispatch.message,
        "distance_text": geo.format_distance(offer.distance_km),
        "eta_minutes": geo.estimate_eta_minutes(
            offer.distance_km, config.DISPATCH_AVERAGE_SPEED_KMH,
        ),
        "seconds_remaining": offer.seconds_remaining,
        "timeout_seconds": config.DISPATCH_OFFER_TIMEOUT_SECONDS,
        "location": _place_of(dispatch),
    }


def _serialise_active(dispatch):
    return {
        "dispatch_id": dispatch.id,
        "status": dispatch.status,
        "risk_level": dispatch.risk_level,
        "location": _place_of(dispatch),
        "detail_url": url_for("officer.dispatch_detail", dispatch_id=dispatch.id),
        "navigate_url": geo.google_maps_directions_url(dispatch.latitude, dispatch.longitude),
    }


# ===========================================================================
# OFFICER PWA (HTML)
# ===========================================================================
@officer_bp.route("/")
@officer_required
def home():
    offer = dispatch_engine.get_live_offer_for_officer(current_user.id)
    active = dispatch_engine.get_active_dispatch_for_officer(current_user.id)

    # An officer who already has an incident belongs on the incident screen,
    # not on a dashboard showing them a duty switch they should not touch.
    if active is not None and offer is None:
        return redirect(url_for("officer.dispatch_detail", dispatch_id=active.id))

    recent = (
        Dispatch.query
        .filter_by(accepted_by_id=current_user.id)
        .order_by(Dispatch.created_at.desc())
        .limit(5).all()
    )

    return render_template(
        "officer/home.html",
        offer=_serialise_offer(offer) if offer else None,
        recent=recent,
        push_configured=push.push_is_configured(),
        push_reason=push.push_unavailable_reason(),
        ping_seconds=config.OFFICER_LOCATION_PING_SECONDS,
        location_max_age=config.OFFICER_LOCATION_MAX_AGE_SECONDS,
        offer_timeout=config.DISPATCH_OFFER_TIMEOUT_SECONDS,
    )


@officer_bp.route("/dispatch/<int:dispatch_id>")
@officer_required
def dispatch_detail(dispatch_id):
    dispatch = db.session.get(Dispatch, dispatch_id)
    if dispatch is None:
        abort(404)

    # An officer may only open an incident they were actually involved in -
    # either they hold it, or they were offered it at some point.
    was_offered = any(o.officer_id == current_user.id for o in dispatch.offers)
    if dispatch.accepted_by_id != current_user.id and not was_offered:
        if not current_user.is_admin:
            abort(403)

    steps = []
    for status in Dispatch.PROGRESS_SEQUENCE:
        index = Dispatch.PROGRESS_SEQUENCE.index(status)
        current_index = (
            Dispatch.PROGRESS_SEQUENCE.index(dispatch.status)
            if dispatch.status in Dispatch.PROGRESS_SEQUENCE else -1
        )
        steps.append({
            "status": status,
            "label": {
                "ACCEPTED": "Assignment accepted",
                "EN_ROUTE": "On the way",
                "ARRIVED": "Arrived on scene",
                "RESOLVED": "Incident resolved",
            }[status],
            "done": index <= current_index,
            "current": index == current_index,
            "at": dispatch.timestamp_for(status),
        })

    return render_template(
        "officer/dispatch_detail.html",
        dispatch=dispatch,
        place=_place_of(dispatch),
        steps=steps,
        navigate_url=geo.google_maps_directions_url(dispatch.latitude, dispatch.longitude),
        is_mine=dispatch.accepted_by_id == current_user.id,
        ping_seconds=config.OFFICER_LOCATION_PING_SECONDS,
    )


@officer_bp.route("/history")
@officer_required
def history():
    dispatches = (
        Dispatch.query
        .filter_by(accepted_by_id=current_user.id)
        .order_by(Dispatch.created_at.desc())
        .limit(50).all()
    )
    # Incidents this officer was asked about but did not take. Shown because
    # an officer should be able to see their own response record, not just
    # their successes.
    missed = (
        DispatchOffer.query
        .filter_by(officer_id=current_user.id)
        .filter(DispatchOffer.status.in_(["DECLINED", "TIMEOUT"]))
        .order_by(DispatchOffer.offered_at.desc())
        .limit(20).all()
    )
    return render_template(
        "officer/history.html", dispatches=dispatches, missed=missed,
        ping_seconds=config.OFFICER_LOCATION_PING_SECONDS,
    )


@officer_bp.route("/sw.js")
def service_worker():
    """
    Served from /officer/ rather than /static/ for one unavoidable reason: a
    service worker's default scope is the directory it is served from, so a
    worker at /static/js/sw.js could only ever control /static/js/. Serving
    it here scopes it to /officer/, which is what it needs to control.

    No login required: the browser fetches this without session cookies in
    some update scenarios, and the file contains no private data.
    """
    body = render_template("officer/sw.js")
    response = Response(body, mimetype="application/javascript")
    response.headers["Service-Worker-Allowed"] = "/officer/"
    # The worker must not be cached stale, or a bug fix in it can persist on
    # an officer's phone indefinitely.
    response.headers["Cache-Control"] = "no-cache"
    return response


@officer_bp.route("/manifest.json")
def manifest():
    body = render_template("officer/manifest.json")
    return Response(body, mimetype="application/manifest+json")


# ===========================================================================
# OFFICER JSON API
# ===========================================================================
@officer_api_bp.route("/state")
@officer_required
def api_state():
    """
    One call that answers everything the phone needs to render itself.

    Deliberately a single endpoint rather than three: the officer's view has
    to be internally consistent (you cannot be shown an offer and an active
    incident from two different instants), and one round trip is also kinder
    to a phone on mobile data.
    """
    offer = dispatch_engine.get_live_offer_for_officer(current_user.id)
    active = dispatch_engine.get_active_dispatch_for_officer(current_user.id)

    return jsonify({
        "ok": True,
        "on_duty": bool(current_user.on_duty),
        "location_fresh": current_user.has_fresh_location(
            config.OFFICER_LOCATION_MAX_AGE_SECONDS
        ),
        "offer": _serialise_offer(offer) if offer else None,
        "active": _serialise_active(active) if active else None,
        "server_time": datetime.utcnow().isoformat() + "Z",
    })


@officer_api_bp.route("/duty", methods=["POST"])
@officer_required
@json_only
def api_duty():
    """
    Go on or off duty.

    Going ON duty requires coordinates in the same request. That is not
    bureaucracy: an officer marked available with no position cannot be
    ranked, so they would sit in the roster looking available while never
    being offered anything - the worst possible failure, because it is
    invisible to everyone involved.
    """
    payload = request.get_json(silent=True) or {}
    going_on = bool(payload.get("on_duty"))

    if going_on:
        latitude = payload.get("latitude")
        longitude = payload.get("longitude")
        if not geo.is_valid_coordinate(latitude, longitude):
            return jsonify({
                "ok": False,
                "reason": "location_required",
                "message": "Your location is needed to go on duty, so dispatch "
                           "can work out who is nearest.",
            }), 400
        current_user.last_latitude = float(latitude)
        current_user.last_longitude = float(longitude)
        current_user.last_location_at = datetime.utcnow()

    current_user.on_duty = going_on
    db.session.commit()

    logger.info(
        "Officer %s went %s duty.", current_user.username, "on" if going_on else "off",
    )
    return jsonify({"ok": True, "on_duty": going_on})


@officer_api_bp.route("/location", methods=["POST"])
@officer_required
@json_only
def api_location():
    """
    A position ping from an on-duty officer's phone.

    Off-duty pings are accepted and discarded rather than rejected: the phone
    may have a ping in flight when the officer toggles off, and an error
    response there would surface a scary message for a harmless race. We just
    decline to store it, which is the privacy-correct behaviour anyway.
    """
    payload = request.get_json(silent=True) or {}
    latitude = payload.get("latitude")
    longitude = payload.get("longitude")

    if not geo.is_valid_coordinate(latitude, longitude):
        return jsonify({"ok": False, "reason": "bad_coordinates"}), 400

    if not current_user.on_duty:
        return jsonify({"ok": True, "stored": False})

    current_user.last_latitude = float(latitude)
    current_user.last_longitude = float(longitude)
    current_user.last_location_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"ok": True, "stored": True})


@officer_api_bp.route("/offer/<int:offer_id>/accept", methods=["POST"])
@officer_required
@json_only
def api_accept(offer_id):
    result = dispatch_engine.accept_offer(offer_id, current_user.id)
    if result.get("ok"):
        result["redirect"] = url_for(
            "officer.dispatch_detail", dispatch_id=result["dispatch_id"],
        )
    return jsonify(result)


@officer_api_bp.route("/offer/<int:offer_id>/decline", methods=["POST"])
@officer_required
@json_only
def api_decline(offer_id):
    return jsonify(dispatch_engine.decline_offer(offer_id, current_user.id))


@officer_api_bp.route("/dispatch/<int:dispatch_id>/status", methods=["POST"])
@officer_required
@json_only
def api_update_status(dispatch_id):
    payload = request.get_json(silent=True) or {}
    new_status = (payload.get("status") or "").upper()
    return jsonify(dispatch_engine.update_status(dispatch_id, current_user.id, new_status))


# ===========================================================================
# WEB PUSH REGISTRATION
# ===========================================================================
@push_api_bp.route("/vapid-key")
@officer_required
def api_vapid_key():
    """
    The public half of the VAPID pair. Safe to hand out - that is its whole
    purpose; the browser needs it to encrypt a subscription to us.
    """
    return jsonify({
        "configured": push.push_is_configured(),
        "public_key": config.VAPID_PUBLIC_KEY,
        "reason": push.push_unavailable_reason(),
    })


@push_api_bp.route("/subscribe", methods=["POST"])
@officer_required
@json_only
def api_subscribe():
    """
    Register this device for background alerts.

    The endpoint URL is the device's identity, so re-subscribing the same
    browser is an update rather than a duplicate - otherwise every page load
    would add another row and the officer would get N copies of each alert.
    """
    payload = request.get_json(silent=True) or {}
    endpoint = payload.get("endpoint")
    keys = payload.get("keys") or {}
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")

    if not (endpoint and p256dh and auth):
        return jsonify({"ok": False, "reason": "incomplete_subscription"}), 400

    existing = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if existing is not None:
        existing.user_id = current_user.id
        existing.p256dh = p256dh
        existing.auth = auth
        existing.user_agent = (request.headers.get("User-Agent") or "")[:255]
    else:
        db.session.add(PushSubscription(
            user_id=current_user.id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            user_agent=(request.headers.get("User-Agent") or "")[:255],
        ))

    db.session.commit()
    logger.info("Push device registered for officer %s.", current_user.username)
    return jsonify({"ok": True})


@push_api_bp.route("/unsubscribe", methods=["POST"])
@officer_required
@json_only
def api_unsubscribe():
    payload = request.get_json(silent=True) or {}
    endpoint = payload.get("endpoint")
    if endpoint:
        PushSubscription.query.filter_by(
            endpoint=endpoint, user_id=current_user.id,
        ).delete()
        db.session.commit()
    return jsonify({"ok": True})


# ===========================================================================
# ADMIN: DISPATCH BOARD
# ===========================================================================
@dispatch_admin_bp.route("/dispatches")
@admin_required
def admin_dispatches():
    open_dispatches = (
        Dispatch.query
        .filter(Dispatch.status.in_(["PENDING", "OFFERED", "ACCEPTED", "EN_ROUTE", "ARRIVED"]))
        .order_by(Dispatch.created_at.desc()).all()
    )
    closed_dispatches = (
        Dispatch.query
        .filter(Dispatch.status.in_(["RESOLVED", "CANCELLED", "UNASSIGNED"]))
        .order_by(Dispatch.created_at.desc()).limit(40).all()
    )

    officers = User.query.filter_by(role="officer").order_by(User.username).all()
    on_duty = [
        o for o in officers
        if o.on_duty and o.has_fresh_location(config.OFFICER_LOCATION_MAX_AGE_SECONDS)
    ]

    # Response time is the number that tells an administrator whether this
    # feature is working at all, so it is computed over resolved incidents
    # rather than left for someone to work out from timestamps.
    #
    # Simulated incidents are excluded. A demo officer tapping Accept two
    # seconds after an incident is raised would drag this average down to
    # something no real roster could achieve, and this is precisely the
    # figure someone would quote as evidence the system works.
    answered = [
        d for d in closed_dispatches
        if d.response_seconds is not None and not d.is_simulated
    ]
    average_response = (
        round(sum(d.response_seconds for d in answered) / len(answered))
        if answered else None
    )

    return render_template(
        "admin_dispatches.html",
        open_dispatches=open_dispatches,
        closed_dispatches=closed_dispatches,
        officers=officers,
        on_duty_count=len(on_duty),
        average_response=average_response,
        locations=CameraLocation.query.filter_by(is_active=True).order_by(CameraLocation.name).all(),
        push_configured=push.push_is_configured(),
        push_reason=push.push_unavailable_reason(),
        dispatch_enabled=config.DISPATCH_ENABLED,
        offer_timeout=config.DISPATCH_OFFER_TIMEOUT_SECONDS,
        search_radius=config.DISPATCH_SEARCH_RADIUS_KM,
        location_max_age=config.OFFICER_LOCATION_MAX_AGE_SECONDS,
    )


@dispatch_admin_bp.route("/api/dispatches")
@admin_required
def api_admin_dispatches():
    """Feeds the live-refreshing table on the dispatch board."""
    rows = (
        Dispatch.query
        .order_by(Dispatch.created_at.desc())
        .limit(60).all()
    )
    return jsonify({
        "ok": True,
        "dispatches": [d.to_dict() for d in rows],
        "on_duty": [
            {
                "id": o.id,
                "username": o.username,
                "badge_number": o.badge_number,
                "latitude": o.last_latitude,
                "longitude": o.last_longitude,
                "seconds_since_fix": int(
                    geo.location_age_seconds(o.last_location_at) or 0
                ),
            }
            for o in User.query.filter_by(role="officer", on_duty=True).all()
            if o.has_fresh_location(config.OFFICER_LOCATION_MAX_AGE_SECONDS)
        ],
    })


@dispatch_admin_bp.route("/dispatches/<int:dispatch_id>/cancel", methods=["POST"])
@admin_required
def admin_cancel_dispatch(dispatch_id):
    reason = (request.form.get("reason") or "").strip() or "Closed by an administrator."
    result = dispatch_engine.cancel_dispatch(dispatch_id, reason)
    if result.get("ok"):
        flash(f"Dispatch #{dispatch_id} cancelled.", "success")
    else:
        flash(f"Could not cancel dispatch #{dispatch_id}: {result.get('reason')}.", "warning")
    return redirect(url_for("dispatch_admin.admin_dispatches"))


@dispatch_admin_bp.route("/dispatches/<int:dispatch_id>/retry", methods=["POST"])
@admin_required
def admin_retry_dispatch(dispatch_id):
    result = dispatch_engine.retry_dispatch(dispatch_id)
    if result.get("ok") and result.get("offered"):
        flash(f"Dispatch #{dispatch_id} re-offered to the nearest officer.", "success")
    elif result.get("ok"):
        flash(
            f"Dispatch #{dispatch_id} was retried but there is still no "
            "on-duty officer in range.", "warning",
        )
    else:
        flash(f"Could not retry dispatch #{dispatch_id}: {result.get('reason')}.", "warning")
    return redirect(url_for("dispatch_admin.admin_dispatches"))


@dispatch_admin_bp.route("/dispatches/manual", methods=["POST"])
@admin_required
def admin_manual_dispatch():
    """Raise an incident by hand - a radio report, a phone call, a drill."""
    location_id = request.form.get("location_id", type=int)
    risk_level = (request.form.get("risk_level") or "HIGH").upper()
    message = (request.form.get("message") or "").strip()

    if not location_id:
        flash("Choose a registered location to dispatch an officer to.", "warning")
        return redirect(url_for("dispatch_admin.admin_dispatches"))

    dispatch_id = dispatch_engine.create_manual_dispatch(location_id, risk_level, message)
    if dispatch_id is None:
        flash(
            "Could not raise that dispatch. The location needs valid "
            "coordinates before an officer can be routed to it.", "danger",
        )
    else:
        flash(f"Dispatch #{dispatch_id} raised and offered to the nearest officer.", "success")
    return redirect(url_for("dispatch_admin.admin_dispatches"))


@dispatch_admin_bp.route("/users/<int:user_id>/role", methods=["POST"])
@admin_required
def admin_set_role(user_id):
    """
    Promote an account to field officer (or back).

    Going off the officer role also clears duty status and the stored
    position. Keeping a last-known location for someone who is no longer an
    officer would be retaining location data with no purpose, which is
    exactly the kind of thing that should expire by itself.
    """
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)

    new_role = (request.form.get("role") or "").strip()
    if new_role not in ("user", "officer", "admin"):
        flash("Unknown role.", "warning")
        return redirect(url_for("dispatch_admin.admin_dispatches"))

    if user.id == current_user.id and new_role != "admin":
        flash("You cannot remove your own admin role.", "warning")
        return redirect(url_for("dispatch_admin.admin_dispatches"))

    badge = (request.form.get("badge_number") or "").strip()
    user.role = new_role
    if new_role == "officer":
        if badge:
            user.badge_number = badge
    else:
        user.on_duty = False
        user.last_latitude = None
        user.last_longitude = None
        user.last_location_at = None

    db.session.commit()
    flash(f"{user.username} is now a {new_role}.", "success")
    return redirect(url_for("dispatch_admin.admin_dispatches"))


# ---------------------------------------------------------------------------
# REGISTRATION
# ---------------------------------------------------------------------------
def register_dispatch_blueprints(app) -> None:
    """Called once from app.py after the app and database are configured."""
    app.register_blueprint(officer_bp)
    app.register_blueprint(officer_api_bp)
    app.register_blueprint(push_api_bp)
    app.register_blueprint(dispatch_admin_bp)

    # Imported here, not at module scope: dispatch.simulation imports the
    # admin_required and json_only decorators from THIS module, so a
    # top-level import either way round would be circular. By the time this
    # function runs, this module is fully imported and that import resolves.
    from dispatch.simulation import simulation_bp
    app.register_blueprint(simulation_bp)

    logger.info(
        "Dispatch blueprints registered "
        "(/officer, /api/officer, /api/push, /admin, /admin/simulation)."
    )
