# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

Full-stack limo dispatch system for Tsatsral Limo LLC — a self-hosted alternative to MoovsApp. Handles customer bookings, operator dispatch, driver GPS tracking, Twilio SMS driver notification, Stripe payments, and Google Maps address autocomplete.

---

## Repository layout

```
/                                        ← repo root
├── app.py                               ← thin loader; dynamically imports the real backend
├── gunicorn_conf.py                     ← root-level Gunicorn config (for Procfile / Railway)
├── requirements.txt                     ← mirrors middle man project/backend/requirements.txt
├── Procfile                             ← web: gunicorn -c gunicorn_conf.py app:app
├── render.yaml                          ← Render deploy (rootDir = "middle man project")
├── .env.example                         ← template for all env vars
│
├── middle man project/                  ← THE ACTUAL APPLICATION
│   ├── backend/
│   │   ├── app.py                       ← ★ entire backend (~2360 lines)
│   │   ├── gunicorn_conf.py             ← binds $PORT (default 10000), 1 worker
│   │   └── requirements.txt            ← pinned deps
│   ├── frontend/                        ← static HTML served by FastAPI
│   │   ├── index.html                   ← public booking form (dark luxury theme)
│   │   ├── login.html                   ← operator login (Tailwind CSS)
│   │   ├── dispatch.html                ← ★ operator dashboard (DispatchLink)
│   │   ├── driver.html                  ← driver GPS location share
│   │   └── payment.html                 ← Stripe payment redirect page
│   └── images/                          ← hero/vehicle images served at /images/*
│
└── tsatsral-limo-llc/tsatsral-limo-site/ ← public marketing website
    ├── about.html / blog.html / contact.html / services.html
    ├── demo.html                         ← served at /demo and /index
    └── merged.html                       ← single-file export for reference
```

**Important:** `app.py` at the root is only a shim that `importlib`-loads
`middle man project/backend/app.py`. All real code lives in the latter.

---

## Commands

**Run locally (from repo root):**
```bash
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

**Run directly from the backend directory:**
```bash
cd "middle man project"
../.venv/bin/uvicorn backend.app:app --host 0.0.0.0 --port 8000 --reload
```

**Production (Render — uses render.yaml, rootDir = "middle man project"):**
```bash
gunicorn -c backend/gunicorn_conf.py backend.app:app
```

**Install dependencies:**
```bash
.venv/bin/pip install -r requirements.txt
# or
.venv/bin/pip install -r "middle man project/backend/requirements.txt"
```

**Expose locally over HTTPS (for Twilio webhook testing):**
```bash
ngrok http 8000
# Then set APP_BASE_URL=https://<ngrok-url> in your .env
```

There are no automated tests or linting configs in this repo.

---

## Architecture

### Backend: `middle man project/backend/app.py`

FastAPI + SQLAlchemy (SQLite). A single ~2360-line file — models, routes, auth, WebSocket, Twilio webhook, Stripe webhook, invoice renderer.

#### Database models (SQLite via `dispatch.db`)

| Model | Key columns |
|---|---|
| `UserModel` | `id`, `email` (unique), `hashed_password` (PBKDF2-SHA256), `name`, `role` (`admin`/`operator`) |
| `DriverModel` | `id`, `name` (unique), `email`, `phone`, `vehicle`, `status` (Online/Offline/Busy), `lat`, `lon` |
| `ReservationModel` | Full trip data — see table below |
| `EventModel` | `reservation_id` (FK), `time`, `title`, `body` — audit trail |

**ReservationModel fields (all nullable unless noted):**
`customer`*, `phone`, `email`, `pickup`*, `dropoff`*, `date`*, `time`*, `passengers`*, `vehicle`*, `payment`*, `trip_type`* (default One Way), `notes`, `status`* (default "Needs driver"), `assigned_driver`, `payment_url`, `pickup_lat/lon`, `dropoff_lat/lon`, `distance_miles`, `archived` (0/1), `created_by` (FK→users), `stripe_session_id`, `airline`, `flight_number`, `hours_count` (1–12 for Hourly), `custom_price`, `custom_base_rate`, `custom_gratuity`, `custom_service_fee`

#### Allowed enum values

```python
_ALLOWED_VEHICLES  = {"Black car", "Executive SUV", "Sprinter van"}
_ALLOWED_TRIP_TYPES = {"Airport Arrival", "Airport Departure",
                       "Point-to-Point", "Hourly", "One Way"}
_ALLOWED_STATUSES  = {"Online", "Offline", "Busy"}
```

#### Trip lifecycle (reservation statuses)
```
Needs driver → Pending confirmation → Assigned → In Progress → Arrived → Completed
                                                                        ↘ Cancelled
```

#### Auth

- Session-based, httpOnly cookie `session_token`.
- Sessions stored in `_active_sessions` dict (in-memory; lost on server restart).
- Default session: 8 hours. "Remember me": 30 days.
- Password hashing: `hashlib.pbkdf2_hmac` (PBKDF2-SHA256, 260,000 iterations).
- Startup seeds an admin from `ADMIN_EMAIL` / `ADMIN_PASSWORD` env vars (default email: `admin@gobilimo.live`).
- `get_current_user` dependency raises 401. `require_admin` raises 403 for non-admins.

#### IDOR protection

`GET /api/reservations` and `PUT /api/reservations/{id}` — admins see/edit all rows; operators are filtered to trips where `assigned_driver == their name` OR `created_by == their user_id`. Never trust client-supplied user IDs.

#### Pricing

```python
_VEHICLE_RATES = {
    "Black car":    (3.50, $65 min),
    "Executive SUV":(4.50, $88 min),
    "Sprinter van": (6.00, $150 min),
}
# Time-of-day multipliers: 10pm–5am +25%, rush hours (6–9am, 4–7pm) +15%
# Final: base + 18% gratuity + $22 service fee
# custom_price overrides all of the above
```

Invoice page (`GET /invoice/{res_id}`, admin only) generates a print-ready HTML invoice.

#### WebSocket (`/ws/dispatch`)

Authenticated via `session_token` cookie at connection time. Broadcasts two event types to all connected dashboards:
- `{"type": "UPDATE", "message": "..."}` — trip/driver state change
- `{"type": "DRIVER_LOCATION", "driver_id": ..., "lat": ..., "lon": ..., "status": "..."}` — GPS ping

#### Twilio SMS flow

1. Admin assigns a driver → `notify_driver_of_reservation()` sends dispatch alert SMS
2. Driver replies YES/NO → `POST /twilio/reply` validates Twilio signature, updates status, broadcasts via WebSocket
3. On YES: schedules a 60-minute pre-trip reminder (`_schedule_trip_reminder`)
4. On "Start trip": sends passenger info (destination map link) to driver + en-route SMS to customer
5. On "Arrived": sends arrival SMS to customer
6. On "Complete": sends completion SMS to driver

**Twilio webhook URL reconstruction**: Behind Render's proxy, the app rebuilds the URL Twilio signed from `x-forwarded-proto`/`x-forwarded-host` headers (or `APP_BASE_URL` env override). Set `APP_BASE_URL` to your public HTTPS domain.

#### Stripe integration

- Prefers `STRIPE_PAYMENT_LINK` (static link with `?client_reference_id={res_id}`) over dynamic Checkout Sessions.
- Falls back to creating a `checkout.Session` dynamically if `STRIPE_SECRET_KEY` is set.
- `POST /stripe/webhook` — verifies signature, marks reservation `payment = "Paid"` on `checkout.session.completed`.

#### Address autocomplete / geocoding

- `GET /api/geocode?q=...` — proxies Google Places Autocomplete (New API). Returns place predictions only (no coordinates). Cache: 500 entries, 10-min TTL, biased to California.
- `GET /api/place?place_id=...` — resolves a `place_id` to `lat/lon` + formatted address. Cache: 1000 entries, 1-hour TTL.
- Set `GOOGLE_MAPS_API_KEY` for both. Falls back to empty results if key absent.

#### Background tasks

- **Midnight archive loop** — asyncio task auto-archives Completed/Cancelled trips at midnight UTC.
- **Pre-trip reminder** — asyncio task per trip; sends driver SMS 60 minutes before pickup. Cancelled on complete/cancel.

#### DB migrations

Run at startup via raw `ALTER TABLE` / `PRAGMA table_info` checks. When adding a new column, follow the pattern in `startup_event()` — check `existing_cols` before executing the `ALTER TABLE`.

#### Security middleware

- `SecurityHeadersMiddleware` — sets `X-Content-Type-Options`, `X-Frame-Options`, CSP, `Referrer-Policy`, `Permissions-Policy` on all responses.
- `BodySizeLimitMiddleware` — rejects bodies > 1 MB (413).
- Rate limiting (in-memory): 5 bookings/hr/IP, 15 logins/15min/IP. Uses `request.client.host` (set correctly by Gunicorn's `forwarded_allow_ips = "*"`).

---

### Frontend pages: `middle man project/frontend/`

Static files served by FastAPI via `FileResponse`. No build step — plain HTML + vanilla JS + CDN libraries.

| File | Route(s) | Auth | Description |
|---|---|---|---|
| `index.html` | `/`, `/booking`, `/booking.html` | Public | Dark luxury booking form |
| `login.html` | `/login` | Public | Operator login (Tailwind CSS) |
| `dispatch.html` | `/dispatch`, `/dispatch.html` | Session required | DispatchLink operator dashboard |
| `driver.html` | `/driver` | Session required | Driver GPS location share page |
| `payment.html` | `/payment/{res_id}` | Public | Stripe payment redirect page |

**Marketing site** (`tsatsral-limo-llc/tsatsral-limo-site/`):

| Route | File |
|---|---|
| `/about`, `/about.html` | `about.html` |
| `/blog`, `/blog.html` | `blog.html` |
| `/contact`, `/contact.html` | `contact.html` |
| `/services`, `/services.html` | `services.html` |
| `/demo`, `/demo.html`, `/index`, `/index.html` | `demo.html` |

**CDN libraries:**
- Leaflet 1.9.4 — maps (dispatch + booking confirmation)
- Flatpickr 4.6.13 — date/time pickers (dispatch)
- Tailwind CSS (CDN) — login.html only
- OSRM public API — route polylines on booking confirmation map
- Google Fonts (Cormorant Garamond + DM Sans) — booking page
- Google Maps Places API (New) — address autocomplete, proxied via backend

**XSS protection**: all dynamic HTML in dispatch.html uses `escapeHTML()` before any `innerHTML` assignment.

---

### Environment variables

| Variable | Purpose | Required |
|---|---|---|
| `ADMIN_EMAIL` | Admin account email (default: `admin@gobilimo.live`) | Recommended |
| `ADMIN_PASSWORD` | Admin account password | Yes |
| `DISPATCH_PASSWORD` | Legacy fallback for `ADMIN_PASSWORD` | No |
| `PRODUCTION` | Set to `true` to enable `Secure` flag on cookies | Production |
| `DATABASE_URL` | SQLite path, e.g. `sqlite:////var/data/dispatch.db` | Recommended |
| `APP_BASE_URL` | Public HTTPS URL for Twilio signature verification | Yes (Twilio) |
| `TWILIO_ACCOUNT_SID` | Twilio SMS | For SMS |
| `TWILIO_AUTH_TOKEN` | Twilio webhook signature verification | For SMS |
| `TWILIO_FROM_NUMBER` | Twilio outbound number | For SMS |
| `STRIPE_SECRET_KEY` | Stripe API secret | For payments |
| `STRIPE_PUBLISHABLE_KEY` | Stripe front-end key | For payments |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signature | For payments |
| `STRIPE_PAYMENT_LINK` | Static payment link (preferred over dynamic sessions) | For payments |
| `GOOGLE_MAPS_API_KEY` | Google Places API (New) key | For autocomplete |

Copy `.env.example` → `.env` to get started.

---

## Deployment

### Render (canonical)

`render.yaml` defines the service:
- `rootDir: "middle man project"`
- Build: `pip install -r backend/requirements.txt`
- Start: `gunicorn -c backend/gunicorn_conf.py backend.app:app`
- 1 GB persistent disk mounted at `/var/data`
- Set `DATABASE_URL=sqlite:////var/data/dispatch.db` to survive redeploys

### Railway / Heroku (Procfile)

Uses the root-level `Procfile`: `gunicorn -c gunicorn_conf.py app:app`, which loads the shim `app.py` that dynamically imports the real backend. The root `gunicorn_conf.py` binds to `$PORT` (default 10000).

### SQLite persistence note

Without a persistent disk (e.g. Render free tier), `dispatch.db` is wiped on every deploy. The app auto-falls back to `/tmp/dispatch.db` if `DATABASE_URL` points to a non-writable path.

---

## Key design decisions & gotchas

- **In-memory sessions** — `_active_sessions` is a plain dict. All sessions are lost on restart; users re-login. In-memory rate-limit buckets are also reset on restart.
- **SQLite + single worker** — `check_same_thread=False` is set. Gunicorn is configured with 1 worker (`WEB_CONCURRENCY=1`) to avoid SQLite write contention.
- **`app.py` shim** — the root `app.py` uses `importlib` to load the backend from a path with spaces (`middle man project/`). Don't rename the directory without updating the shim.
- **Driver name as FK** — `assigned_driver` stores the driver's *name* (string), not their ID. Renaming a driver does not cascade to existing reservations.
- **Twilio signature validation** — the webhook tries multiple candidate URLs (forwarded headers + `APP_BASE_URL`). If Twilio replies fail with 403, check that `APP_BASE_URL` matches the exact URL Twilio is configured to POST to.
- **No forgot-password email** — `POST /api/auth/forgot-password` is a placeholder that always returns success. No email service is wired.
- **Stripe static link vs. dynamic session** — `STRIPE_PAYMENT_LINK` is preferred; it appends `?client_reference_id={res_id}` but the backend currently only processes `checkout.session.completed` (which requires dynamic sessions). Static links do not fire `checkout.session.completed` in the same way — the webhook handler uses `metadata.reservation_id`, which only exists on dynamic sessions.
- **CSP allows `unsafe-inline`** — required by the vanilla JS approach (no build tool). CDN sources are whitelisted individually.
