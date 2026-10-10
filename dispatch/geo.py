"""
dispatch/geo.py
=================
Distance maths and navigation links for officer dispatch.

This module imports nothing but the standard library. That is a deliberate
constraint, not an accident: ranking officers by distance is the one piece
of dispatch logic that decides who gets woken up at 2am, so it must be
testable on its own without a database, an app context or a running Flask.

An honest note on what "distance" means here: every number produced by this
module is a great-circle (straight-line) distance. It ignores roads, rivers,
one-way streets and traffic. An officer 800 m away across a railway line may
be a 10-minute drive while one 2 km away down a main road is 4 minutes. Road
distances would need a routing service (and an API key and a billing
account); the straight-line figure is a good enough proxy for "who is
closest" at city scale, and the officer gets real turn-by-turn navigation
from Google Maps the moment they accept. The ETA shown is labelled as an
estimate for the same reason.

Save this file at: Stampede-Prediction-System/dispatch/geo.py
"""

from math import asin, cos, radians, sin, sqrt

# Mean Earth radius (WGS-84). Using the mean radius keeps the error of the
# spherical approximation around 0.3%, which at a 15 km dispatch radius is
# a few tens of metres - far below GPS noise on a phone.
EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1, lon1, lat2, lon2):
    """
    Great-circle distance between two WGS-84 points, in kilometres.

    The haversine form is used rather than the simpler spherical law of
    cosines because it stays numerically stable for the small distances
    that matter here - cos-based formulas lose precision badly once two
    points are within a few hundred metres of each other, which is exactly
    the case when an officer is already near the incident.
    """
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = radians(lon2 - lon1)

    h = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    # min(1.0, ...) guards against a floating-point value a hair above 1
    # for antipodal points, which would make asin() raise.
    return 2 * EARTH_RADIUS_KM * asin(min(1.0, sqrt(h)))


def is_valid_coordinate(lat, lon):
    """True if both values are present, numeric and inside Earth's range."""
    if lat is None or lon is None:
        return False
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return -90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0


def location_age_seconds(last_location_at, now=None):
    """
    Seconds since a position was reported, or None if never reported.
    Timestamps are naive UTC throughout this project (datetime.utcnow).
    """
    if last_location_at is None:
        return None
    from datetime import datetime
    reference = now or datetime.utcnow()
    return (reference - last_location_at).total_seconds()


def rank_by_distance(incident_lat, incident_lon, candidates, max_radius_km=None):
    """
    Order candidates by how close they are to the incident.

    `candidates` is any iterable of objects exposing .last_latitude,
    .last_longitude and .id - in practice User rows, but the duck-typed
    signature is what lets this be tested with plain stubs.

    Returns a list of (candidate, distance_km) tuples, nearest first.
    Candidates with unusable coordinates are dropped, and anything beyond
    max_radius_km is excluded when that limit is given.

    Ties break on .id so the ordering is deterministic. Two officers at the
    same distance (the same control room, say) must not be offered an
    incident in a different order on every run - that turns a reproducible
    dispatch decision into a coin toss, and makes bug reports untestable.
    """
    ranked = []
    for candidate in candidates:
        lat = getattr(candidate, "last_latitude", None)
        lon = getattr(candidate, "last_longitude", None)
        if not is_valid_coordinate(lat, lon):
            continue
        distance = haversine_km(incident_lat, incident_lon, float(lat), float(lon))
        if max_radius_km is not None and distance > max_radius_km:
            continue
        ranked.append((candidate, distance))

    ranked.sort(key=lambda pair: (pair[1], getattr(pair[0], "id", 0)))
    return ranked


def google_maps_directions_url(dest_lat, dest_lon):
    """
    A Google Maps universal deep link for turn-by-turn navigation.

    No origin is supplied on purpose: omitting it makes Maps route from the
    device's live position, which is both more accurate than the last
    position we recorded and still correct if the officer has moved since
    accepting. On Android and iOS this opens the Maps app; on desktop it
    opens the website. Chosen over the Directions API because it needs no
    API key and no billing account, and over geo: URIs because those have
    no cross-platform behaviour worth relying on.
    """
    return (
        "https://www.google.com/maps/dir/?api=1"
        f"&destination={float(dest_lat):.6f},{float(dest_lon):.6f}"
        "&travelmode=driving"
    )


def google_maps_place_url(lat, lon):
    """A plain map pin, for admin screens where nobody is navigating."""
    return f"https://www.google.com/maps/search/?api=1&query={float(lat):.6f},{float(lon):.6f}"


def format_distance(distance_km):
    """
    Human-sized distance string.

    Under a kilometre it reads in metres rounded to 10 m, because "0.4 km"
    is harder to act on than "420 m" when you are deciding whether to run
    or drive. Above that, one decimal place - an officer does not need
    centimetre precision on a 3 km drive.
    """
    if distance_km is None:
        return "unknown distance"
    if distance_km < 0.995:
        # Rounded to the nearest 10 m: false precision ("427 m") implies an
        # accuracy that straight-line distance does not have.
        return f"{int(round(distance_km * 1000 / 10.0)) * 10} m"
    return f"{distance_km:.1f} km"


def estimate_eta_minutes(distance_km, average_speed_kmh=25.0):
    """
    Rough minutes to arrive, from straight-line distance.

    This is an estimate and is labelled as one everywhere it is shown. The
    default speed is deliberately low for a city so the figure errs
    pessimistic: an officer who arrives sooner than promised is a non-event,
    one who arrives later than a confident-looking ETA is a broken promise.
    Always at least 1, since "0 min away" reads as a bug.
    """
    if distance_km is None or average_speed_kmh <= 0:
        return None
    return max(1, int(round((distance_km / average_speed_kmh) * 60)))
