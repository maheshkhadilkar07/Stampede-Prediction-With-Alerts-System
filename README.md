# AI-Based Real-Time Stampede Prediction and Alert System

Final year major project — Mode 1 (Upload Video) pipeline is implemented end
to end: **Upload → YOLO Person Detection → Crowd Density → Rule-Based Risk
Analyzer → Heatmap → Dashboard**, with authentication, SQLite persistence,
alert history, admin panel, and PDF report generation.

## 1. Setup

```bash
cd Stampede-Prediction-System
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

First run will auto-download the YOLO26 nano weights (`yolo26n.pt`) into
`trained_models/` the first time `PersonDetector` is instantiated — this
requires internet access on the machine actually running the app.
YOLO26 (Ultralytics, Jan 2026) is the current recommended model —
NMS-free end-to-end inference and faster CPU inference than YOLOv8/YOLO11,
which matters on a CPU-only dev machine.

## 2. Initialize the database

The database is created automatically on first run via `init_db()` in
`database/database.py`. No manual migration step is required for SQLite.

## 3. Run

```bash
python app.py
```

Visit `http://localhost:5000`, register an account, log in, and upload a
video from the Upload page. Processing runs in a background thread; the
dashboard polls the JSON status endpoint until the video finishes.

## 4. Live CCTV (Mode 2) — Fluvio streaming

The live-CCTV pipeline (`streaming/`) is: **Camera → Fluvio Producer →
Fluvio Topic → Fluvio Consumer → YOLO → Density → Risk → Alerts →
Dashboard**, exposed at `/live` with an MJPEG preview and live-polling
stat cards.

**`fluvio` (the Python client) is a compiled Rust extension and is kept
OUT of `requirements.txt` on purpose** — installing it on plain Windows
fails with `error: can't find Rust compiler` because there's no
prebuilt Windows wheel. Because of that:

- **On native Windows without WSL2**: just run
  `pip install -r requirements.txt` as normal — Fluvio is not attempted,
  so nothing fails. The app detects that the `fluvio` package isn't
  installed and **automatically falls back to "direct mode"**: camera
  frames go straight into the YOLO pipeline in-process, skipping the
  message broker. Live CCTV still works end-to-end for demos; you just
  won't be exercising the actual Fluvio hop until Fluvio is set up.
- **To use real Fluvio streaming**, run the project inside WSL2:
  1. Install WSL2 + Ubuntu (`wsl --install` in an admin PowerShell,
     then reboot).
  2. Inside the Ubuntu shell:
     ```bash
     curl -fsS https://raw.githubusercontent.com/fluvio-community/fluvio/master/install.sh | bash
     fluvio cluster start
     fluvio topic create stampede-live-frames
     ```
  3. Clone/copy the project into the WSL2 filesystem (or open it via
     `\\wsl$\Ubuntu\...` / the VS Code "Remote - WSL" extension), create
     a venv there, run:
     ```bash
     pip install -r requirements.txt
     pip install -r requirements-fluvio.txt
     ```
     (this time `fluvio` installs correctly since a Linux wheel exists),
     then run `python app.py` from inside WSL2.
  4. Visit `/live`, pick "Laptop Webcam" (or an RTSP URL), and start the
     session — the mode badge on the page will read **FLUVIO STREAMING**
     instead of **DIRECT MODE** once the cluster is actually being used.

Either way, `/live/start` accepts:
- **Laptop Webcam** — opens local device index `0`
- **RTSP CCTV Stream** / **IP Camera** — provide a stream URL, e.g.
  `rtsp://user:pass@192.168.1.10:554/stream1`

## 5. Field-officer dispatch (Uber-style assignment)

When a HIGH or CRITICAL alert fires at a camera with known coordinates, the
system pages the **nearest on-duty officer** on their phone. They get ~25
seconds to accept; if they don't, the incident moves to the next-nearest,
and so on. Accepting opens Google Maps navigation to the spot.

### 5.1 One-time setup

```bash
# 1. Add the new columns and tables to an existing database.
#    Backs up database/stampede.db first; safe to re-run.
python scripts/migrate_add_dispatch.py

# 2. (Optional) background push, so a phone rings while the app is closed.
pip install pywebpush
python scripts/generate_vapid_keys.py     # paste the output into .env
```

Skip step 2 and dispatch still works — the officer app polls for new
assignments every few seconds **while it is open on screen**. Push is what
lets a phone ring from a pocket.

### 5.2 Create an officer account

Register as normal and pick **Field Officer** as the role (badge number and
mobile number are optional but show up on the dispatch board). An existing
account can be switched to `officer` from **Admin → Dispatch Board → Field
Officers**. Officers land on `/officer` instead of the operator dashboard.

On that screen the officer taps **Go on duty**, which asks for location
permission and starts reporting their position every 20 seconds. Only
officers who are on duty, verified, and have reported a position in the last
5 minutes are eligible to be paged.

### 5.3 ⚠️ HTTPS is required on anything but localhost

Browsers only expose the Geolocation API and service workers in a **secure
context**. `http://localhost:5000` counts as secure, so the officer app works
fully on the machine running Flask. A phone pointed at
`http://192.168.1.x:5000` does **not** — it will silently refuse to share
location, and the officer can never go on duty.

For a phone demo, pick one:

**Option A — self-signed HTTPS (no extra tools).** Set `SSL_ADHOC=true` in
`.env`, set `HOST=0.0.0.0` so the phone can reach it, and run `python app.py`
as usual. Your browser will warn about the certificate; accept it. Needs the
`cryptography` package, which `pip install pywebpush` already pulls in — if
it's missing the app says so and falls back to plain HTTP rather than failing
to start.

> **Set `FLASK_DEBUG=false` whenever `HOST` is not `127.0.0.1`.** Flask's
> debugger puts an interactive Python console on error pages, so binding it
> to `0.0.0.0` hands remote code execution to anyone on the same Wi-Fi.
> Debug mode is fine on localhost; it is not fine on a network.

**Option B — a public HTTPS tunnel.** `ngrok http 5000`, then open the
`https://…` URL it prints on the phone. Slower, but no certificate warning
and no need to bind to `0.0.0.0`, which makes it the safer and better choice
for a live demo to an audience.

Chrome can also be told to trust one LAN origin, via
`chrome://flags/#unsafely-treat-insecure-origin-as-secure`. That is a
development workaround, not a deployment strategy.

### 5.4 What happens, step by step

1. Analysis raises a HIGH/CRITICAL alert, and the alert is **committed first** —
   an officer is never paged about an incident that failed to save.
2. A `Dispatch` is created with a snapshot of the location's coordinates.
   If an alert already raised a dispatch within 150 m in the last 10
   minutes, it folds into that one. One crowd is one incident, not one
   incident per alert.
3. The nearest eligible officer is offered the incident, and only that one.
   At most **one offer is ever pending** per incident, which is what makes a
   double-accept impossible rather than merely unlikely.
4. On accept → `ACCEPTED`, then the officer moves it through `EN_ROUTE` →
   `ARRIVED` → `RESOLVED`. Transitions are checked server-side against an
   allow-list, so an incident cannot be resolved without being accepted, nor
   walked backwards.
5. On decline or timeout → the next-nearest officer, up to 5 officers.
6. If nobody takes it → the incident is marked `UNASSIGNED` and admins get an
   email through the existing alert path. A later alert from the same crowd
   re-searches **that same record** (an officer may have come on duty since)
   without opening a duplicate or re-sending the email.
7. Resolving the incident also acknowledges the parent alert — an officer
   standing at the scene is the most reliable acknowledgement available.

Admins watch all of this live at **Admin → Dispatch Board**, which also
allows raising an incident by hand, cancelling one, and retrying an
unassigned one.

### 5.5 Configuration

Every knob has a working default, so the feature runs with nothing set. The
full list, with the reasoning behind each default, is documented in
`.env.example` under **FIELD-OFFICER DISPATCH** — the main ones being
`DISPATCH_ENABLED`, `DISPATCH_OFFER_TIMEOUT_SECONDS` (25),
`DISPATCH_MAX_OFFERS` (5) and `DISPATCH_SEARCH_RADIUS_KM` (15).

### 5.6 Known limits of this module

- **Single process only.** The state machine is serialised with an in-process
  lock, which is correct under the Flask dev server or one multi-threaded
  worker. Running multiple worker processes would need the transitions moved
  into real database transactions. This is a deliberate scope choice, not an
  oversight.
- **Straight-line distance.** "Nearest" is great-circle distance, not drive
  time — it ignores roads, rivers and one-way streets. The officer gets real
  turn-by-turn routing from Google Maps the moment they accept, and the ETA
  shown before that is labelled as an estimate.
- **No coordinates, no dispatch.** A video or live session that was never
  tagged with a registered location (Admin → Locations) cannot be dispatched
  to, because there is no destination to send anyone. This is logged loudly
  with the fix spelled out, since it is the most likely reason dispatch
  appears to do nothing.

## 6. 3D dispatch simulation

**Admin &rsaquo; 3D Simulation** renders the dispatch system as a map you can
fly around: registered camera sites, officers where they last reported in,
and live incidents with the offer currently sitting on somebody's phone.

It is wired to the real engine. Raising an incident here calls the same
`create_manual_dispatch()` the dispatch board calls, which runs the same
nearest-first ranking and sends the same Web Push notification. If you have a
real officer on duty with a phone, that phone rings. There is no parallel
"simulation mode" inside the engine, because a simulation that exercises
different code from production tells you nothing about production.

### 6.1 Getting something on screen

On a fresh database there are no camera locations, so the map opens empty.
Either register real sites under **Admin &rsaquo; Locations**, or press
**Seed venue** to register six in a ring around a centre point &mdash; those
are ordinary location rows you can rename, move or delete afterwards. The
centre box starts at a default and there's a **Use my location** button next
to it.

Then press **Add demo officer** a few times. Demo officers exist because the
honest behaviour of an empty roster takes over otherwise: every incident goes
straight to `UNASSIGNED`, which is correct but demonstrates nothing. They are
real officer accounts (the engine has to rank them as it ranks anyone) created
with a random unusable password and an address at the reserved `.invalid`
domain, so they can never be signed into and can never receive mail.

Click a camera site to raise an incident there. Watch the dashed line snap to
the nearest officer, the countdown run, and the line turn solid when somebody
accepts. **Accept** and **Decline** appear only for demo officers &mdash; the
simulation will not answer on a real officer's behalf, because that would
falsify a real person's response record.

Drag an officer by double-clicking and moving the mouse; releasing writes the
new position, which is what makes them dispatchable from there.

### 6.2 What it leaves behind, and how to clean up

Driving the real engine means leaving real rows behind. Two things contain
that, and **Purge simulated data** reverses both:

- Incidents raised here are flagged `Dispatch.is_simulated`, and the dispatch
  board excludes them from its average response time. That figure is the one
  someone would quote as evidence the system works, and a demo officer
  accepting two seconds after an incident is raised would make nonsense of it.
- Demo officers and seeded locations are found again by their name prefixes.

The purge is scoped by those markers rather than by a time window, and it
refuses to delete a demo officer still attached to a genuine incident, or a
seeded site a genuine incident happened at &mdash; removing either would strip
a real record of who attended or where.

### 6.3 Offline for a presentation

The scene needs Three.js. It loads `static/js/vendor/three.min.js` first and
only falls back to the CDN if that file is missing, so vendoring it means the
demo does not depend on the venue's Wi-Fi:

```bash
curl -o static/js/vendor/three.min.js https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js
```

If neither is reachable the page says so and the lists still work.

### 6.4 What the picture does and does not claim

The scene is a flat map plane with markers, not a modelled venue. That is
deliberate: what dispatch reasons about is coordinates, distances and who is
standing where, and a plan view with true metric spacing shows those honestly.
A stadium mesh would imply the system knows about walls, stairs and doorways,
which it does not.

Distances are straight-line, as the engine's own ranking is. The projection
uses the same Earth model as `dispatch/geo.py` (a sphere of radius 6371.0088
km), so the scale printed on the grid and the distances the engine reports
cannot disagree. Officer and camera markers are drawn at a readable size, not
to scale.

`SIMULATION_ENABLED=false` in `.env` refuses incident-raising while leaving
the page viewable.

## 7. Known limitation (next module)

OpenCV's `VideoWriter` with the `mp4v` fourcc produces an MP4 container that
Chrome/Firefox often refuse to play back directly, because the codec stream
inside isn't H.264. This is **intentionally left for the FFmpeg module**,
which will re-encode `videos/processed/*.mp4` into a browser-safe H.264 file
with `+faststart`. Until then, download the processed file and play it in
VLC, or view it via the heatmap/analytics charts on the dashboard.

## 8. Project status

**Completed**
- Folder structure, Flask app factory, routing
- User auth (register/login/logout) via Flask-Login
- SQLite persistence (Users, Videos, Alerts) via SQLAlchemy
- Video upload with extension/size validation
- YOLO26 person detection (`detector/person_detector.py`)
- Person counter + crowd density estimator (grid-based)
- Rule-based risk analyzer (density + growth-rate scoring)
- Heatmap prototype (decaying accumulation buffer + JET colormap overlay)
- Background (threaded) video processing pipeline
- Dashboard, alerts, video analytics, admin panel templates (Bootstrap 5)
- PDF report generation (ReportLab)
- **Live CCTV / RTSP / webcam streaming (Mode 2)**: `streaming/camera_reader.py`,
  `streaming/producer.py`, `streaming/consumer.py`, `streaming/stream_manager.py`
- **Fluvio producer/consumer integration**, with automatic fallback to
  direct in-process streaming when no Fluvio cluster is reachable
- Live MJPEG preview (`/live/feed`) + live-polling stat cards + live alerts
- **Field-officer dispatch (section 5)**: officer PWA, sequential
  nearest-first assignment, Google Maps navigation, Web Push, admin dispatch
  board
- **3D dispatch simulation (section 6)**: live WebGL view of sites, officers
  and incidents, driving the real engine

**Pending (next modules)**
- FFmpeg re-encode for browser-compatible playback of Mode 1 recordings
- CNN-based risk prediction (to cross-check the rule-based analyzer)
- Chart.js analytics wiring for the live dashboard (currently recorded-video only)
- Dark mode toggle
- Final responsive UI pass

## 9. Folder structure

```
Stampede-Prediction-System/
├── app.py                  # Flask app, routes, auth wiring
├── config.py                # Central settings (paths, thresholds, DB URI)
├── requirements.txt
├── database/
│   ├── database.py          # SQLAlchemy init
│   ├── models.py             # User, Video, Alert, Dispatch, DispatchOffer, …
│   └── history.py            # Alert/video history queries
├── detector/
│   ├── person_detector.py    # YOLO26 wrapper
│   ├── crowd_counter.py
│   ├── density_estimator.py
│   ├── risk_analyzer.py      # Rule-based scoring
│   ├── heatmap.py
│   └── video_processor.py    # Orchestrates the full per-frame pipeline
├── cnn/                      # (pending) train.py / predict.py / dataset_loader.py
├── streaming/
│   ├── camera_reader.py       # Threaded webcam/RTSP/IP camera reader
│   ├── producer.py            # Fluvio frame producer (+ FluvioUnavailableError)
│   ├── consumer.py            # Fluvio frame consumer
│   └── stream_manager.py      # Orchestrates Mode 2 end-to-end, incl. fallback
├── dispatch/                  # Field-officer dispatch (section 5)
│   ├── geo.py                 # Haversine ranking + Maps links (stdlib only)
│   ├── engine.py              # Sequential nearest-first state machine
│   ├── push.py                # Web Push via VAPID (degrades to polling)
│   ├── routes.py              # Officer PWA, officer JSON API, admin board
│   └── simulation.py          # 3D simulation page + JSON API (section 6)
├── scripts/
│   ├── migrate_add_dispatch.py  # Idempotent SQLite migration (backs up first)
│   ├── generate_vapid_keys.py   # One-time VAPID key pair for Web Push
│   ├── generate_officer_icons.py
│   ├── verify_dispatch.py       # Dispatch checks (no deps needed)
│   └── verify_simulation.py     # Simulation + purge-scoping checks
├── utils/
│   ├── logger.py
│   ├── helpers.py
│   ├── forms.py               # WTForms (login/register/upload)
│   └── report_generator.py    # PDF report builder
├── templates/                 # Bootstrap 5 UI
├── static/
├── videos/{uploads,processed}
├── trained_models/            # YOLO weights land here
└── reports/                   # Generated PDF reports land here
```
