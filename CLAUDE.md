# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

Full-stack limo dispatch system for Tsatsral Limo LLC — a self-hosted alternative to MoovsApp. Handles customer bookings, operator dispatch, driver GPS tracking, and Twilio SMS driver notification.

## Commands

**Run locally:**
```bash
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

**Production (Render/Railway):**
```bash
.venv/bin/gunicorn -c gunicorn_conf.py app:app
```
Gunicorn binds to port `10000`, uses `uvicorn.workers.UvicornWorker`.

**Install dependencies:**
```bash
.venv/bin/pip install -r requirements.txt
```

**Expose locally over HTTPS (for mobile/Twilio testing):**
```bash
ngrok http 8000
# Get URL: curl -s http://localhost:4040/api/tunnels
```

There are no tests or linting configs in this repo.

## Architecture

### Single-file backend: `app.py`

FastAPI + SQLAlchemy (SQLite). Everything lives in one file — models, routes, auth, WebSocket, Twilio webhook.

**Database models** (SQLite via `dispatch.db`):
- `UserModel` — email, hashed_password (PBKDF2-SHA256), name, role (`admin`/`operator`)
- `DriverModel` — name, phone, vehicle, status (Online/Offline/Busy), lat/lon
- `ReservationModel` — full trip data, status, assigned_driver, coordinates, `created_by` (FK to users), `archived` flag
- `EventModel` — audit trail entries attached to each reservation

**Auth** — session-based, httpOnly cookie (`session_token`). Sessions stored in `_active_sessions` dict in memory (lost on restart). Password hashing uses `hashlib.pbkdf2_hmac` (Python stdlib, no bcrypt). On startup, seeds an admin user from `ADMIN_EMAIL` / `ADMIN_PASSWORD` env vars (falls back to `DISPATCH_PASSWORD`).

**IDOR protection**: `GET /api/reservations` filters by role — admin sees all rows; operator sees only trips where `assigned_driver == their name` OR `created_by == their user_id`. Never trust client-supplied user IDs.

**WebSocket** (`/ws/dispatch`) — broadcasts `UPDATE` and `DRIVER_LOCATION` events to all connected operator dashboards.

**Midnight archive loop** — asyncio background task auto-archives Completed/Cancelled trips daily.

**DB migrations** run at startup via raw `ALTER TABLE` statements — check `PRAGMA table_info` before adding columns.

### HTML pages: `index/`

Static files served by FastAPI (`FileResponse`). No build step — plain HTML + vanilla JS + CDN-loaded libraries.

| Page | Route | Auth |
|---|---|---|
| `booking.html` | `/` | Public |
| `login.html` | `/login` | Public |
| `dispatch.html` | `/dispatch` | Session required |
| `driver.html` | `/driver` | Session required |

**Key front-end libraries (all CDN):**
- Leaflet 1.9.4 — maps on dispatch and booking confirmation
- Flatpickr 4.6.13 — date/time pickers on dispatch
- Tailwind CSS (CDN) — login.html only
- OSRM public API — route polylines on booking confirmation
- Photon/Komoot — address autocomplete proxy via `/api/geocode`

### Environment variables

| Variable | Purpose |
|---|---|
| `DISPATCH_PASSWORD` | Legacy fallback; used as admin password seed if `ADMIN_PASSWORD` not set |
| `ADMIN_EMAIL` | Admin account email (default: `admin@tsatslimo.com`) |
| `ADMIN_PASSWORD` | Admin account password (overrides `DISPATCH_PASSWORD`) |
| `TWILIO_ACCOUNT_SID` | Twilio SMS |
| `TWILIO_AUTH_TOKEN` | Twilio webhook signature verification |
| `TWILIO_FROM_NUMBER` | Twilio outbound number |
| `PRODUCTION` | Set to `true` to enable `Secure` flag on session cookies |

### Key design decisions

- **In-memory sessions** — `_active_sessions` is a plain Python dict. Sessions are lost on server restart; users must re-login.
- **SQLite** — fine for single-server deploy; `check_same_thread=False` set for FastAPI's async context.
- **Twilio SMS flow** — dispatch assigns a driver → SMS sent → driver replies YES/NO → `/twilio/reply` webhook updates status and broadcasts via WebSocket.
- **Geocode cache** — 500-entry LRU in memory, 10-minute TTL, hard-bounded to California bbox.
- **Rate limiting** — implemented in-memory (5 bookings/hr/IP, 15 logins/15min/IP).
- **XSS protection** — all dynamic HTML uses `escapeHTML()` before `innerHTML` assignment (defined in dispatch.html).
