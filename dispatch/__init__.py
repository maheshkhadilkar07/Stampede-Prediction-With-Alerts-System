"""
dispatch/
===========
Officer dispatch: deciding WHO goes to an incident, asking them, and
getting them there.

Nothing is imported here on purpose. dispatch.engine imports dispatch.geo
and dispatch.push, and dispatch.routes imports the engine, so eager
imports in this file would create a cycle the moment app.py touched any
of them.

Modules:
    geo.py    - distance maths and map links (pure stdlib, no Flask)
    push.py   - Web Push delivery to an officer's devices
    engine.py - the sequential nearest-first state machine
    routes.py - the officer PWA, its JSON API, and the admin screens
"""
