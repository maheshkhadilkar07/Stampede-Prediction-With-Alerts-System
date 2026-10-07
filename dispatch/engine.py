"""
dispatch/engine.py
====================
The sequential nearest-first dispatch state machine.

The model is Uber's, applied to crowd incidents: when a HIGH/CRITICAL alert
fires at a camera with known coordinates, the incident is offered to the
single NEAREST on-duty officer. They get DISPATCH_OFFER_TIMEOUT_SECONDS to
accept. If they decline or go quiet, the offer moves to the next-nearest,
and so on up to DISPATCH_MAX_OFFERS officers, after which admins are told
nobody took it.

Why one-at-a-time rather than broadcasting to everyone:

  - Broadcasting creates a race. Five officers tap "Accept" within the same
    second and four of them get an error after having already started
    walking. That is a worse experience than not being asked.
  - Broadcasting also pulls officers off whatever else they were doing, five
    at a time, for an incident that needs one person.
  - Sequential offers produce an audit trail that answers the question an
    enquiry actually asks: who was asked, in what order, and how long did
    each take to respond. DispatchOffer.sequence is that record.

The cost is latency - a declined offer adds up to the timeout before the
next officer hears anything - which is why the timeout is 25 seconds and not
two minutes.

CONCURRENCY
-----------
Every state transition runs inside one process-wide RLock, and re-reads the
row from the database INSIDE the critical section before deciding anything.
Two officers accepting simultaneously is additionally impossible by
construction, because at most one offer is ever PENDING for a dispatch: the
second officer has no live offer to accept in the first place. This belt-and
-braces approach is deliberate - SQLite gives no usable row-level locking,
so correctness cannot be delegated to the database here.

The lock is process-wide, which means this is correct under Flask's threaded
dev server and under a single multi-threaded waitress/gunicorn worker. It
would NOT be correct across multiple worker processes; that deployment would
need the state machine moved into the database with a real transaction, and
is noted in the README as a known limit rather than pretended away.

Save this file at: Stampede-Prediction-System/dispatch/engine.py
"""

import os
import threading
import time
from datetime import datetime, timedelta

import config
from utils.logger import get_logger

from dispatch import geo, push

logger = get_logger(__name__)


class DispatchEngine:
    """
    Owns the lifecycle of every dispatch.

    All public methods except the monitor loop assume they are called with a
    Flask application context already pushed. That holds for request handlers
    and for the two background alert paths (_live_alert_callback and
    _process_video_job), both of which already wrap their work in
    `with app.app_context()`.
    """

    def __init__(self):
        self._app = None
        self._lock = threading.RLock()
        self._monitor = None
        self._stop = threading.Event()

    # -------------------------------------------------------------------
    # LIFECYCLE
    # -------------------------------------------------------------------
    def init_app(self, app) -> None:
        self._app = app
        if config.DISPATCH_ENABLED:
            self._start_monitor()
            logger.info(
                "Dispatch engine ready (offer timeout %ss, max %s officers, radius %skm).",
                config.DISPATCH_OFFER_TIMEOUT_SECONDS,
                config.DISPATCH_MAX_OFFERS,
                config.DISPATCH_SEARCH_RADIUS_KM,
            )
        else:
            logger.warning("Dispatch engine is disabled (DISPATCH_ENABLED=false).")

    def _start_monitor(self) -> None:
        if self._monitor is not None and self._monitor.is_alive():
            return
        # With Flask's reloader on, the module is imported twice - once in the
        # supervisor process that just watches files, and once in the worker
        # that actually serves. Only the worker should own the expiry monitor,
        # or every offer gets expired twice and admins get paged twice.
        if os.environ.get("WERKZEUG_RUN_MAIN") != "true" and config.DEBUG:
            logger.debug("Reloader supervisor process: not starting the dispatch monitor here.")
            return
        self._stop.clear()
        self._monitor = threading.Thread(
            target=self._monitor_loop, name="dispatch-monitor", daemon=True,
        )
        self._monitor.start()

    def shutdown(self) -> None:
        self._stop.set()

    # -------------------------------------------------------------------
    # CREATION
    # -------------------------------------------------------------------
    def create_dispatch_for_alert(self, alert_id: int):
        """
        Raise a dispatch for an alert and immediately offer it to the nearest
        officer. Returns the new dispatch id, or None if no officer should be
        paged for this alert.

        Returning None is a normal outcome, not a failure: most alerts should
        not page anyone.
        """
        if not config.DISPATCH_ENABLED or self._app is None:
            return None

        from database.database import db
        from database.models import Alert, Dispatch

        alert = db.session.get(Alert, alert_id)
        if alert is None:
            logger.error("create_dispatch_for_alert: alert %s not found.", alert_id)
            return None

        if alert.risk_level not in config.DISPATCH_TRIGGER_LEVELS:
            return None

        video = alert.video
        if video is None:
            return None

        # No coordinates means no destination, and an officer cannot be sent
        # to a place we cannot name. This is the single most likely reason
        # dispatch appears to "do nothing", so it is logged loudly with the
        # fix spelled out rather than silently skipped.
        if not geo.is_valid_coordinate(video.latitude, video.longitude):
            logger.warning(
                "Alert %s (%s) raised no dispatch: video %s has no coordinates. "
                "Tag the upload or live session with a registered location "
                "(Admin > Locations) so officers can be routed to it.",
                alert_id, alert.risk_level, video.id,
            )
            return None

        with self._lock:
            existing = self._find_reusable_dispatch_near(video.latitude, video.longitude)
            if existing is not None:
                # One crowd, one incident. Link this alert's dispatch-worthiness
                # to the one already in flight instead of paging a second
                # officer to the same place.
                if existing.status == "UNASSIGNED":
                    # Nobody took it last time. A fresh alert is the cue to
                    # look again - an officer may have come on duty in the
                    # meantime - but on the existing record, so the venue
                    # keeps one incident with one audit trail.
                    self._reoffer_unassigned(existing)
                logger.info(
                    "Alert %s folded into dispatch %s at %s (deduped, now %s).",
                    alert_id, existing.id,
                    existing.location_name or "unnamed location", existing.status,
                )
                return existing.id

            dispatch = Dispatch(
                alert_id=alert.id,
                video_id=video.id,
                location_id=self._match_location_id(video),
                location_name=video.location_name,
                latitude=float(video.latitude),
                longitude=float(video.longitude),
                risk_level=alert.risk_level,
                message=alert.message,
                status="PENDING",
            )
            db.session.add(dispatch)
            db.session.commit()

            logger.warning(
                "DISPATCH %s raised: %s at %s (%.5f, %.5f)",
                dispatch.id, dispatch.risk_level,
                dispatch.location_name or "unnamed location",
                dispatch.latitude, dispatch.longitude,
            )

            self._offer_to_next_officer(dispatch)
            return dispatch.id

    def create_manual_dispatch(self, location_id: int, risk_level: str, message: str,
                               is_simulated: bool = False):
        """
        An admin raising an incident by hand, for something the cameras never
        saw (a phone call, a radio report). Same machinery from here on.

        is_simulated=True is used by the 3D simulation. It changes NOTHING
        about how the incident is handled - the same ranking, the same push
        notifications, the same officer flow - it only marks the row so the
        dispatch board can keep it out of its response-time averages and
        purge it later.
        """
        if self._app is None:
            return None

        from database.database import db
        from database.models import CameraLocation, Dispatch

        location = db.session.get(CameraLocation, int(location_id))
        if location is None or not geo.is_valid_coordinate(location.latitude, location.longitude):
            return None

        with self._lock:
            dispatch = Dispatch(
                location_id=location.id,
                location_name=location.name,
                latitude=float(location.latitude),
                longitude=float(location.longitude),
                risk_level=risk_level if risk_level in config.DISPATCH_TRIGGER_LEVELS else "HIGH",
                message=message or f"Manual dispatch to {location.name}.",
                status="PENDING",
                is_simulated=bool(is_simulated),
            )
            db.session.add(dispatch)
            db.session.commit()
            logger.warning(
                "DISPATCH %s raised %s at %s.", dispatch.id,
                "by SIMULATION" if is_simulated else "manually", location.name,
            )
            self._offer_to_next_officer(dispatch)
            return dispatch.id

    def _match_location_id(self, video):
        """
        Best-effort link back to the registered CameraLocation a video was
        tagged with. Only used for reporting; the dispatch already carries
        its own coordinate snapshot, so a miss here costs nothing.
        """
        from database.models import CameraLocation
        if not video.location_name:
            return None
        match = CameraLocation.query.filter_by(name=video.location_name).first()
        return match.id if match else None

    def _find_reusable_dispatch_near(self, latitude, longitude):
        """
        A dispatch at effectively the same spot, raised recently, that this
        alert should join instead of duplicating.

        Proximity rather than camera identity is the right key: two cameras
        covering opposite ends of one concourse are one incident to the
        officer who has to walk there. 150 m is roughly "the same place" at
        venue scale while still separating genuinely distinct sites.

        UNASSIGNED is deliberately treated as reusable even though it is a
        terminal status elsewhere. It means "we asked and nobody came", which
        is a property of the roster at that moment, not a statement that the
        crowd problem is over - so the next alert from the same place is the
        same incident, and raising a second record for it would give an
        empty-roster venue one dispatch row and one escalation email per
        alert for as long as the surge lasted. RESOLVED and CANCELLED are
        the genuinely finished states: a human has said so, and a new alert
        after that really is new.
        """
        from database.models import Dispatch

        window_start = datetime.utcnow() - timedelta(
            seconds=config.DISPATCH_DEDUPE_WINDOW_SECONDS
        )
        recent = (
            Dispatch.query
            .filter(Dispatch.created_at >= window_start)
            .filter(~Dispatch.status.in_(["RESOLVED", "CANCELLED"]))
            .all()
        )
        for dispatch in recent:
            separation = geo.haversine_km(
                latitude, longitude, dispatch.latitude, dispatch.longitude,
            )
            if separation <= 0.15:
                return dispatch
        return None

    def _reoffer_unassigned(self, dispatch) -> None:
        """
        Look for an officer again for an incident nobody took, without
        raising a duplicate and without re-emailing admins.

        Must be called with the lock held.

        Deliberately different from retry_dispatch(): the previous offers are
        KEPT, so officers who already declined or timed out on this incident
        stay excluded. That is what makes this safe to call on every repeat
        alert - once the nearby roster is exhausted the search returns nobody,
        the dispatch settles back to UNASSIGNED, and nothing further happens.
        An admin pressing Retry is a different intent (ask everyone again from
        scratch), so that path still clears them.

        Escalation email is suppressed because admins were already told when
        this dispatch first went UNASSIGNED. Telling them again every time the
        crowd flickers back to HIGH would train them to ignore the one alert
        that matters.
        """
        from database.database import db

        if dispatch.offers_made >= config.DISPATCH_MAX_OFFERS:
            return

        dispatch.status = "PENDING"
        dispatch.close_reason = None
        db.session.commit()

        if self._offer_to_next_officer(dispatch, escalate_if_unassigned=False):
            logger.warning(
                "Dispatch %s re-offered: an officer became available since it went unassigned.",
                dispatch.id,
            )

    # -------------------------------------------------------------------
    # OFFERING
    # -------------------------------------------------------------------
    def _offer_to_next_officer(self, dispatch, escalate_if_unassigned: bool = True) -> bool:
        """
        Offer the incident to the nearest officer who has not been asked yet.

        Must be called with the lock held. Returns False when there is nobody
        left to ask, having marked the dispatch UNASSIGNED.

        escalate_if_unassigned=False is for repeat attempts on an incident
        admins have already been emailed about - see _reoffer_unassigned.
        """
        from database.database import db
        from database.models import DispatchOffer

        if dispatch.is_closed or dispatch.is_claimed:
            return False

        if dispatch.offers_made >= config.DISPATCH_MAX_OFFERS:
            self._mark_unassigned(
                dispatch,
                f"No response after asking {dispatch.offers_made} officers.",
                escalate=escalate_if_unassigned,
            )
            return False

        already_asked = [offer.officer_id for offer in dispatch.offers]
        ranked = self._rank_available_officers(dispatch, exclude_ids=already_asked)

        if not ranked:
            reason = (
                "No on-duty officer with a recent location is within "
                f"{config.DISPATCH_SEARCH_RADIUS_KM:g} km."
                if not already_asked else
                "Every nearby officer has already been asked."
            )
            self._mark_unassigned(dispatch, reason, escalate=escalate_if_unassigned)
            return False

        officer, distance_km = ranked[0]
        sequence = dispatch.offers_made + 1
        now = datetime.utcnow()

        offer = DispatchOffer(
            dispatch_id=dispatch.id,
            officer_id=officer.id,
            status="PENDING",
            sequence=sequence,
            distance_km=round(distance_km, 3),
            offered_at=now,
            expires_at=now + timedelta(seconds=config.DISPATCH_OFFER_TIMEOUT_SECONDS),
        )
        db.session.add(offer)

        dispatch.status = "OFFERED"
        dispatch.offers_made = sequence
        db.session.commit()

        distance_text = geo.format_distance(distance_km)
        eta = geo.estimate_eta_minutes(distance_km, config.DISPATCH_AVERAGE_SPEED_KMH)
        push.notify_officer_async(
            officer.id, push.build_offer_payload(dispatch, offer, distance_text, eta),
        )

        logger.info(
            "Dispatch %s offered to %s (#%s, %s away), expires in %ss.",
            dispatch.id, officer.username, sequence, distance_text,
            config.DISPATCH_OFFER_TIMEOUT_SECONDS,
        )
        return True

    def _rank_available_officers(self, dispatch, exclude_ids):
        """
        Everyone who could take this incident right now, nearest first.

        An officer qualifies only if they are on duty, verified, have
        reported a position recently, have not already been asked about this
        incident, and are not already committed to another one. That last
        filter is what stops the nearest officer being handed every incident
        in the city while they are still driving to the first.
        """
        from database.models import Dispatch, User

        candidates = (
            User.query
            .filter(User.role == "officer")
            .filter(User.on_duty.is_(True))
            .filter(User.is_verified.is_(True))
            .all()
        )

        busy_ids = {
            row.accepted_by_id for row in
            Dispatch.query
            .filter(Dispatch.status.in_(["ACCEPTED", "EN_ROUTE", "ARRIVED"]))
            .filter(Dispatch.accepted_by_id.isnot(None))
            .all()
        }

        excluded = set(exclude_ids or []) | busy_ids
        eligible = [
            officer for officer in candidates
            if officer.id not in excluded
            and officer.has_fresh_location(config.OFFICER_LOCATION_MAX_AGE_SECONDS)
        ]

        return geo.rank_by_distance(
            dispatch.latitude, dispatch.longitude, eligible,
            max_radius_km=config.DISPATCH_SEARCH_RADIUS_KM,
        )

    def _mark_unassigned(self, dispatch, reason: str, escalate: bool = True) -> None:
        """
        Nobody is coming. This must be noisy: a silent failure here means a
        stampede with no responder and no one aware of it.

        escalate=False suppresses only the email, never the status change or
        the log line - a repeat attempt on an incident admins already know
        about should still be visible in the log and on the dispatch board.
        """
        from database.database import db

        dispatch.status = "UNASSIGNED"
        dispatch.close_reason = reason
        db.session.commit()

        logger.error("DISPATCH %s UNASSIGNED: %s", dispatch.id, reason)
        if escalate:
            self._escalate_to_admins(dispatch, reason)

    def _escalate_to_admins(self, dispatch, reason: str) -> None:
        """
        Fall back to the existing email alert path so an unassigned incident
        reaches a human through a channel that does not depend on anybody
        being on duty.
        """
        dispatch_id = dispatch.id
        level = dispatch.risk_level
        where = dispatch.location_name or f"{dispatch.latitude:.5f}, {dispatch.longitude:.5f}"
        message = (
            f"NO OFFICER ASSIGNED to {level} crowd risk at {where}. {reason} "
            f"Incident: {dispatch.message}"
        )

        def _send():
            from app import app
            with app.app_context():
                try:
                    from utils.email_alert import send_alert_to_recipients
                    send_alert_to_recipients(
                        level, message,
                        source_label=f"Unassigned dispatch #{dispatch_id}",
                        location_name=where,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("Could not email unassigned-dispatch escalation: %s", exc)

        threading.Thread(target=_send, daemon=True).start()

    # -------------------------------------------------------------------
    # OFFICER RESPONSES
    # -------------------------------------------------------------------
    def accept_offer(self, offer_id: int, officer_id: int) -> dict:
        """
        An officer takes the incident.

        Every rejection path returns a distinct reason so the phone can say
        something true ("another officer took this") instead of a generic
        failure, which is what makes a lost race feel like normal operation
        rather than a bug.
        """
        from database.database import db
        from database.models import DispatchOffer

        with self._lock:
            offer = db.session.get(DispatchOffer, offer_id)
            if offer is None:
                return {"ok": False, "reason": "not_found",
                        "message": "That assignment no longer exists."}
            if offer.officer_id != officer_id:
                return {"ok": False, "reason": "not_your_offer",
                        "message": "That assignment was not offered to you."}

            dispatch = offer.dispatch
            if dispatch.is_claimed or dispatch.is_closed:
                return {"ok": False, "reason": "already_taken",
                        "message": "Another officer already took this incident."}
            if offer.status != "PENDING":
                return {"ok": False, "reason": "closed",
                        "message": "That assignment has already moved on."}
            if offer.seconds_remaining <= 0:
                # The monitor will escalate it on its next tick; don't do it
                # here, so expiry has exactly one owner.
                return {"ok": False, "reason": "expired",
                        "message": "Time ran out and the assignment moved to another officer."}

            now = datetime.utcnow()
            offer.status = "ACCEPTED"
            offer.responded_at = now
            dispatch.status = "ACCEPTED"
            dispatch.accepted_by_id = officer_id
            dispatch.accepted_at = now
            db.session.commit()

            logger.warning(
                "Dispatch %s ACCEPTED by officer %s in %ss.",
                dispatch.id, officer_id, dispatch.response_seconds,
            )
            return {
                "ok": True,
                "dispatch_id": dispatch.id,
                "navigate_url": geo.google_maps_directions_url(
                    dispatch.latitude, dispatch.longitude,
                ),
            }

    def decline_offer(self, offer_id: int, officer_id: int) -> dict:
        """
        An officer passes. Escalate immediately rather than waiting out the
        timeout - they have told us the truth, so there is no reason to make
        the incident sit idle for another 20 seconds.
        """
        from database.database import db
        from database.models import DispatchOffer

        with self._lock:
            offer = db.session.get(DispatchOffer, offer_id)
            if offer is None or offer.officer_id != officer_id:
                return {"ok": False, "reason": "not_found",
                        "message": "That assignment no longer exists."}
            if offer.status != "PENDING":
                return {"ok": False, "reason": "closed",
                        "message": "That assignment has already moved on."}

            offer.status = "DECLINED"
            offer.responded_at = datetime.utcnow()
            db.session.commit()

            logger.info("Dispatch %s declined by officer %s.", offer.dispatch_id, officer_id)
            self._offer_to_next_officer(offer.dispatch)
            return {"ok": True}

    def update_status(self, dispatch_id: int, officer_id: int, new_status: str) -> dict:
        """
        Move an accepted incident along: EN_ROUTE, ARRIVED, RESOLVED.

        Transitions are checked against an allow-list rather than just
        accepting whatever the client sends, so a replayed or tampered
        request cannot walk an incident backwards or mark something resolved
        that was never accepted.
        """
        from database.database import db
        from database.models import Dispatch

        allowed = {
            "EN_ROUTE": {"ACCEPTED"},
            "ARRIVED": {"ACCEPTED", "EN_ROUTE"},
            "RESOLVED": {"ACCEPTED", "EN_ROUTE", "ARRIVED"},
        }
        if new_status not in allowed:
            return {"ok": False, "reason": "unknown_status"}

        with self._lock:
            dispatch = db.session.get(Dispatch, dispatch_id)
            if dispatch is None:
                return {"ok": False, "reason": "not_found"}
            if dispatch.accepted_by_id != officer_id:
                return {"ok": False, "reason": "not_yours"}
            if dispatch.status not in allowed[new_status]:
                return {"ok": False, "reason": "bad_transition",
                        "message": f"Cannot go from {dispatch.status} to {new_status}."}

            now = datetime.utcnow()
            dispatch.status = new_status
            if new_status == "EN_ROUTE":
                dispatch.en_route_at = now
            elif new_status == "ARRIVED":
                dispatch.arrived_at = now
            elif new_status == "RESOLVED":
                dispatch.resolved_at = now
                # An officer standing at the scene saying it is handled is
                # the most reliable acknowledgement this system can get, so
                # it closes the parent alert too.
                if dispatch.alert is not None:
                    dispatch.alert.acknowledged = True

            db.session.commit()
            logger.info("Dispatch %s -> %s by officer %s.", dispatch_id, new_status, officer_id)
            return {"ok": True, "status": new_status}

    # -------------------------------------------------------------------
    # ADMIN ACTIONS
    # -------------------------------------------------------------------
    def cancel_dispatch(self, dispatch_id: int, reason: str = "Closed by an administrator.") -> dict:
        from database.database import db
        from database.models import Dispatch

        with self._lock:
            dispatch = db.session.get(Dispatch, dispatch_id)
            if dispatch is None:
                return {"ok": False, "reason": "not_found"}
            if dispatch.is_closed:
                return {"ok": False, "reason": "already_closed"}

            for offer in dispatch.offers:
                if offer.status == "PENDING":
                    offer.status = "SUPERSEDED"
                    offer.responded_at = datetime.utcnow()
                    push.notify_officer_async(
                        offer.officer_id, push.build_cancel_payload(dispatch.id, reason),
                    )

            dispatch.status = "CANCELLED"
            dispatch.close_reason = reason
            db.session.commit()
            logger.info("Dispatch %s cancelled: %s", dispatch_id, reason)
            return {"ok": True}

    def retry_dispatch(self, dispatch_id: int) -> dict:
        """
        Re-run the search for an incident nobody took. Useful when officers
        have since come on duty - the alternative is an admin raising a
        duplicate incident by hand and losing the original's audit trail.
        """
        from database.database import db
        from database.models import Dispatch

        with self._lock:
            dispatch = db.session.get(Dispatch, dispatch_id)
            if dispatch is None:
                return {"ok": False, "reason": "not_found"}
            if dispatch.status != "UNASSIGNED":
                return {"ok": False, "reason": "not_unassigned"}

            dispatch.status = "PENDING"
            dispatch.offers_made = 0
            dispatch.close_reason = None
            # Clear the previous round so the same officers can be asked
            # again; the old offers are what made them ineligible.
            for offer in list(dispatch.offers):
                db.session.delete(offer)
            db.session.commit()

            offered = self._offer_to_next_officer(dispatch)
            return {"ok": True, "offered": offered}

    # -------------------------------------------------------------------
    # MONITOR
    # -------------------------------------------------------------------
    def _monitor_loop(self) -> None:
        """
        Background sweep that turns lapsed offers into escalations.

        Expiry has to be driven by something other than the officer's phone:
        an officer whose battery died must not stall an incident forever. The
        whole body is wrapped so that one bad tick logs and continues instead
        of killing the thread and silently disabling escalation for the rest
        of the process's life.
        """
        while not self._stop.is_set():
            time.sleep(config.DISPATCH_MONITOR_TICK_SECONDS)
            if self._stop.is_set():
                break
            try:
                with self._app.app_context():
                    self._expire_and_escalate()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Dispatch monitor tick failed: %s", exc)

    def _expire_and_escalate(self) -> None:
        from database.database import db
        from database.models import DispatchOffer

        with self._lock:
            now = datetime.utcnow()
            lapsed = (
                DispatchOffer.query
                .filter(DispatchOffer.status == "PENDING")
                .filter(DispatchOffer.expires_at <= now)
                .all()
            )
            if not lapsed:
                return

            for offer in lapsed:
                offer.status = "TIMEOUT"
                offer.responded_at = now
                logger.info(
                    "Offer %s (dispatch %s, officer %s) timed out.",
                    offer.id, offer.dispatch_id, offer.officer_id,
                )
            db.session.commit()

            for offer in lapsed:
                dispatch = offer.dispatch
                if dispatch is not None and dispatch.is_open:
                    self._offer_to_next_officer(dispatch)

    # -------------------------------------------------------------------
    # READ HELPERS (used by the officer API)
    # -------------------------------------------------------------------
    def get_live_offer_for_officer(self, officer_id: int):
        from database.models import DispatchOffer

        offer = (
            DispatchOffer.query
            .filter_by(officer_id=officer_id, status="PENDING")
            .order_by(DispatchOffer.offered_at.desc())
            .first()
        )
        if offer is None or offer.seconds_remaining <= 0:
            return None
        if offer.dispatch is None or not offer.dispatch.is_open:
            return None
        return offer

    def get_active_dispatch_for_officer(self, officer_id: int):
        from database.models import Dispatch

        return (
            Dispatch.query
            .filter_by(accepted_by_id=officer_id)
            .filter(Dispatch.status.in_(["ACCEPTED", "EN_ROUTE", "ARRIVED"]))
            .order_by(Dispatch.accepted_at.desc())
            .first()
        )


# A single process-wide engine, mirroring how app.py already keeps one
# module-level LiveStreamManager. The lock inside it is only meaningful if
# there is exactly one instance.
dispatch_engine = DispatchEngine()
