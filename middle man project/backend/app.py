import asyncio
import base64
import hashlib
import html
import hmac
import json
import os
import re
import secrets
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import httpx
from dotenv import load_dotenv
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.base import BaseHTTPMiddleware

try:
    import stripe as _stripe
except ImportError:
    _stripe = None

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    or_,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session, relationship, sessionmaker

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
FRONTEND_DIR = PROJECT_DIR / "frontend"
IMAGES_DIR = PROJECT_DIR / "images"
SITE_DIR = PROJECT_DIR.parent / "tsatsral-limo-llc" / "tsatsral-limo-site"

# Fixed point-to-point rate tables (SF city / SFO / OAK origins). Loaded by
# absolute path so it resolves no matter how the app is launched (app:app,
# backend.app:app, or the importlib shim in the repo-root app.py).
import importlib.util as _ilu

_rt_spec = _ilu.spec_from_file_location("rate_tables", str(BASE_DIR / "rate_tables.py"))
rate_tables = _ilu.module_from_spec(_rt_spec)
_rt_spec.loader.exec_module(rate_tables)
fixed_quote = rate_tables.fixed_quote

load_dotenv(PROJECT_DIR / ".env", override=True)
load_dotenv(BASE_DIR / ".env", override=True)


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return f"pbkdf2:sha256:260000${salt}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        _, rest = stored.split("$", 1)
        salt, dk_hex = rest.split("$", 1)
        parts = stored.split(":")
        iterations = int(parts[2].split("$")[0])
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


# --- AUTH STATE ---
# {token: {"user_id": int, "role": str, "name": str, "email": str, "expires": datetime}}
_active_sessions: dict[str, dict] = {}
_scheduled_reminders: dict[int, asyncio.Task] = (
    {}
)  # reservation_id → pre-trip reminder task
_SESSION_DURATION = timedelta(hours=8)
_REMEMBER_ME_DURATION = timedelta(days=30)
_booking_rate: dict[str, list[datetime]] = {}
_BOOKING_RATE_LIMIT = 5
_BOOKING_RATE_WINDOW = timedelta(hours=1)
_login_rate: dict[str, list[datetime]] = {}
_LOGIN_RATE_LIMIT = 15
_LOGIN_RATE_WINDOW = timedelta(minutes=15)
_IS_PRODUCTION = os.getenv("PRODUCTION", "").lower() in ("1", "true", "yes")
_STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
_STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
_STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
_STRIPE_PAYMENT_LINK = os.getenv("STRIPE_PAYMENT_LINK", "")
_GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
if _stripe and _STRIPE_SECRET_KEY:
    _stripe.api_key = _STRIPE_SECRET_KEY

# --- DATABASE SETUP ---
# On Render, set DATABASE_URL to a path on the persistent disk, e.g.
# sqlite:////var/data/dispatch.db (note the four slashes for absolute paths).
# Without a persistent disk, every redeploy wipes the SQLite file.
#
# Locally, keep the default DB path anchored to the app folder so starting
# uvicorn from different directories does not create separate dispatch.db files.
DEFAULT_DB_PATH = Path("/tmp/dispatch.db")
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DEFAULT_DB_PATH}")


def _ensure_sqlite_path_writable(url: str) -> str:
    """For sqlite URLs, make sure the parent directory exists and is writable.
    Falls back to /tmp/dispatch.db if not (e.g. Render disk not attached yet)."""
    if not url.startswith("sqlite"):
        return url
    # sqlite:///relative or sqlite:////absolute
    prefix, _, path_part = url.partition("sqlite:///")
    if not path_part:
        return url
    db_path = Path("/" + path_part if url.startswith("sqlite:////") else path_part)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # Touch a probe file to confirm writability
        probe = db_path.parent / ".write_probe"
        probe.touch()
        probe.unlink(missing_ok=True)
        return url
    except (OSError, PermissionError) as exc:
        print(
            f"[db] WARNING: cannot use {db_path} ({exc}); "
            f"falling back to {DEFAULT_DB_PATH}"
        )
        DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{DEFAULT_DB_PATH}"


DATABASE_URL = _ensure_sqlite_path_writable(DATABASE_URL)
_sqlite_kwargs = (
    {"connect_args": {"check_same_thread": False}}
    if DATABASE_URL.startswith("sqlite")
    else {}
)
engine = create_engine(DATABASE_URL, **_sqlite_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class UserModel(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    name = Column(String)
    role = Column(String, default="operator")  # "admin" or "operator"


class DriverModel(Base):
    __tablename__ = "drivers"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    email = Column(String, nullable=True)
    phone = Column(String)
    vehicle = Column(String)
    status = Column(String, default="Online")  # Online, Offline, Busy
    lat = Column(String, nullable=True)
    lon = Column(String, nullable=True)


class ReservationModel(Base):
    __tablename__ = "reservations"
    id = Column(Integer, primary_key=True, index=True)
    customer = Column(String)
    phone = Column(String, nullable=True)
    email = Column(String, nullable=True)
    pickup = Column(String)
    dropoff = Column(String)
    date = Column(String)
    time = Column(String)
    passengers = Column(String)
    vehicle = Column(String)
    payment = Column(String)
    trip_type = Column(String, default="One Way")
    notes = Column(Text, nullable=True)
    status = Column(String, default="Needs driver")
    assigned_driver = Column(String, nullable=True)
    payment_url = Column(String, nullable=True)

    # Coordinates for mapping
    pickup_lat = Column(String, nullable=True)
    pickup_lon = Column(String, nullable=True)
    dropoff_lat = Column(String, nullable=True)
    dropoff_lon = Column(String, nullable=True)

    distance_miles = Column(Float, nullable=True)
    archived = Column(Integer, default=0)  # 0 = visible, 1 = archived
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    stripe_session_id = Column(String, nullable=True)

    # Trip-type-specific fields
    airline = Column(String, nullable=True)  # for Airport Arrival/Departure
    flight_number = Column(String, nullable=True)  # for Airport Arrival/Departure
    hours_count = Column(Integer, nullable=True)  # for Hourly (1–12)
    custom_price = Column(Float, nullable=True)
    custom_base_rate = Column(Float, nullable=True)
    custom_gratuity = Column(Float, nullable=True)
    custom_service_fee = Column(Float, nullable=True)

    events = relationship(
        "EventModel", back_populates="reservation", cascade="all, delete-orphan"
    )


class EventModel(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, index=True)
    reservation_id = Column(Integer, ForeignKey("reservations.id"))
    time = Column(String)
    title = Column(String)
    body = Column(Text)

    reservation = relationship("ReservationModel", back_populates="events")


Base.metadata.create_all(bind=engine)


# --- DEPENDENCY ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


_ALLOWED_VEHICLES = {"Sedan", "SUV"}
_ALLOWED_TRIP_TYPES = {
    "Airport Arrival",
    "Airport Departure",
    "Point-to-Point",
    "Hourly",
    "One Way",
}
_ALLOWED_STATUSES = {"Online", "Offline", "Busy"}


# --- SCHEMAS ---
class DriverCreate(BaseModel):
    name: str = Field(..., min_length=1)
    email: Optional[str] = None
    phone: str = Field(..., min_length=1)
    vehicle: str
    status: str = "Online"

    @field_validator("vehicle")
    @classmethod
    def validate_vehicle(cls, v: str) -> str:
        if v not in _ALLOWED_VEHICLES:
            raise ValueError(
                f"Invalid vehicle. Allowed: {', '.join(_ALLOWED_VEHICLES)}"
            )
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in _ALLOWED_STATUSES:
            raise ValueError(f"Invalid status. Allowed: {', '.join(_ALLOWED_STATUSES)}")
        return v


class DriverUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1)
    email: Optional[str] = None
    phone: Optional[str] = Field(None, min_length=1)
    vehicle: Optional[str] = None

    @field_validator("vehicle")
    @classmethod
    def validate_vehicle(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in _ALLOWED_VEHICLES:
            raise ValueError(
                f"Invalid vehicle. Allowed: {', '.join(_ALLOWED_VEHICLES)}"
            )
        return v


class ReservationCreate(BaseModel):
    customer: str = Field(..., min_length=1)
    phone: Optional[str] = None
    email: Optional[str] = None
    pickup: str = Field(..., min_length=1)
    dropoff: str = Field(..., min_length=1)
    date: str
    time: str
    passengers: str
    vehicle: str
    payment: str
    trip_type: str = "One Way"
    notes: Optional[str] = None
    # Optional coordinates
    pickup_lat: Optional[str] = None
    pickup_lon: Optional[str] = None
    dropoff_lat: Optional[str] = None
    dropoff_lon: Optional[str] = None
    distance_miles: Optional[float] = None
    created_by: Optional[int] = None
    # Trip-type-specific
    airline: Optional[str] = None
    flight_number: Optional[str] = None
    hours_count: Optional[int] = Field(default=None, ge=1, le=12)
    custom_price: Optional[float] = None
    custom_base_rate: Optional[float] = None
    custom_gratuity: Optional[float] = None
    custom_service_fee: Optional[float] = None

    @field_validator("vehicle")
    @classmethod
    def validate_vehicle(cls, v: str) -> str:
        if v not in _ALLOWED_VEHICLES:
            raise ValueError(
                f"Invalid vehicle. Allowed: {', '.join(_ALLOWED_VEHICLES)}"
            )
        return v

    @field_validator("trip_type")
    @classmethod
    def validate_trip_type(cls, v: str) -> str:
        if v not in _ALLOWED_TRIP_TYPES:
            raise ValueError(
                f"Invalid trip type. Allowed: {', '.join(_ALLOWED_TRIP_TYPES)}"
            )
        return v


class ReservationUpdate(BaseModel):
    customer: Optional[str] = Field(None, min_length=1)
    phone: Optional[str] = None
    email: Optional[str] = None
    pickup: Optional[str] = Field(None, min_length=1)
    dropoff: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    passengers: Optional[str] = None
    vehicle: Optional[str] = None
    payment: Optional[str] = None
    trip_type: Optional[str] = None
    notes: Optional[str] = None
    airline: Optional[str] = None
    flight_number: Optional[str] = None
    hours_count: Optional[int] = Field(default=None, ge=1, le=12)
    custom_price: Optional[float] = None
    custom_base_rate: Optional[float] = None
    custom_gratuity: Optional[float] = None
    custom_service_fee: Optional[float] = None

    @field_validator("vehicle")
    @classmethod
    def validate_vehicle(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in _ALLOWED_VEHICLES:
            raise ValueError(
                f"Invalid vehicle. Allowed: {', '.join(_ALLOWED_VEHICLES)}"
            )
        return v

    @field_validator("trip_type")
    @classmethod
    def validate_trip_type(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in _ALLOWED_TRIP_TYPES:
            raise ValueError(
                f"Invalid trip type. Allowed: {', '.join(_ALLOWED_TRIP_TYPES)}"
            )
        return v


# --- WEBSOCKET MANAGER ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            await connection.send_json(message)


manager = ConnectionManager()


# --- AUTH HELPERS ---
def _session_valid(token: Optional[str]) -> bool:
    if not token or token not in _active_sessions:
        return False
    if datetime.utcnow() > _active_sessions[token]["expires"]:
        _active_sessions.pop(token, None)
        return False
    return True


def _get_session(token: Optional[str]) -> Optional[dict]:
    if not _session_valid(token):
        return None
    return _active_sessions[token]


def get_current_user(session_token: Optional[str] = Cookie(default=None)) -> dict:
    sess = _get_session(session_token)
    if not sess:
        raise HTTPException(status_code=401, detail="Authentication required")
    return sess


def require_admin(session_token: Optional[str] = Cookie(default=None)) -> dict:
    sess = get_current_user(session_token)
    if sess["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return sess


def _real_ip(request: Request) -> str:
    # request.client.host is the real IP when uvicorn is configured with
    # forwarded_allow_ips (set in gunicorn_conf.py). Never trust the
    # X-Forwarded-For header directly — it is attacker-controlled.
    return request.client.host if request.client else "unknown"


def check_booking_rate(request: Request) -> None:
    ip = _real_ip(request)
    now = datetime.utcnow()
    window_start = now - _BOOKING_RATE_WINDOW
    recent = [t for t in _booking_rate.get(ip, []) if t > window_start]
    if len(recent) >= _BOOKING_RATE_LIMIT:
        raise HTTPException(
            status_code=429, detail="Too many booking requests. Try again later."
        )
    recent.append(now)
    _booking_rate[ip] = recent


def check_login_rate(request: Request) -> None:
    ip = _real_ip(request)
    now = datetime.utcnow()
    window_start = now - _LOGIN_RATE_WINDOW
    recent = [t for t in _login_rate.get(ip, []) if t > window_start]
    if len(recent) >= _LOGIN_RATE_LIMIT:
        raise HTTPException(
            status_code=429, detail="Too many login attempts. Try again in 15 minutes."
        )
    recent.append(now)
    _login_rate[ip] = recent


# --- APP SETUP ---
app = FastAPI(title="Tsatsral Limo Dispatch API", docs_url=None, redoc_url=None)

_MAX_BODY_BYTES = 1_000_000  # 1 MB cap on request bodies


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(self), microphone=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net https://cdn.tailwindcss.com; "
            "style-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: https://*.tile.openstreetmap.org https://*.basemaps.cartocdn.com; "
            "connect-src 'self' https://router.project-osrm.org https://*.basemaps.cartocdn.com; "
            "frame-ancestors 'none';"
        )
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        cl = request.headers.get("content-length")
        if cl:
            try:
                if int(cl) > _MAX_BODY_BYTES:
                    return JSONResponse(
                        status_code=413, content={"detail": "Request body too large"}
                    )
            except ValueError:
                return JSONResponse(
                    status_code=400, content={"detail": "Invalid content-length"}
                )
        return await call_next(request)


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(BodySizeLimitMiddleware)
app.mount("/images", StaticFiles(directory=IMAGES_DIR), name="images")


def add_db_event(db: Session, reservation_id: int, title: str, body: str):
    now = datetime.now().strftime("%I:%M %p")
    db_event = EventModel(
        reservation_id=reservation_id, time=now, title=title, body=body
    )
    db.add(db_event)
    db.commit()


# Per-mile formula rates — used only as a FALLBACK for trips that are not on the
# fixed SF/SFO/OAK rate tables (see rate_tables.py).
_VEHICLE_RATES = {
    "Sedan": (3.50, 65),  # ($/mile, minimum $)
    "SUV": (4.50, 88),
}


def _fare_breakdown(
    vehicle: str,
    distance_miles: float = 0,
    hour: int = 12,
    pickup: str = None,
    dropoff: str = None,
    pickup_lat: float = None,
    pickup_lon: float = None,
    dropoff_lat: float = None,
    dropoff_lon: float = None,
) -> dict:
    """Single source of truth for trip price.

    Returns a breakdown dict with a ``mode``:
      * ``"fixed"``   — a known SF/SFO/OAK ↔ destination route. The price is the
        all-inclusive sheet total (vehicle base + 20% gratuity, no extra fees).
      * ``"formula"`` — anything else: per-mile rate + time-of-day surcharge +
        18% gratuity + $22 service fee (the original pricing model).

    Every other pricing path (the Stripe charge, the live quote endpoint, the
    invoice, and the dispatch list) derives from this function so the displayed
    price always equals the charged price.
    """
    fixed = fixed_quote(
        pickup,
        dropoff,
        vehicle,
        pickup_lat,
        pickup_lon,
        dropoff_lat,
        dropoff_lon,
    ) if (pickup and dropoff) else None
    if fixed is not None:
        total = round(float(fixed), 2)
        base = round(total / 1.20, 2)  # sheet total = base + 20% gratuity
        gratuity = round(total - base, 2)
        return {
            "mode": "fixed",
            "base": base,
            "surcharge": 0.0,
            "surcharge_pct": 0,
            "gratuity": gratuity,
            "gratuity_pct": 20,
            "fees": 0.0,
            "total": total,
            "minimum_fare": False,
        }

    rate, minimum = _VEHICLE_RATES.get(vehicle, (4.50, 88))
    miles = distance_miles or 0
    pre = max(rate * miles, float(minimum))
    if hour >= 22 or hour < 5:  # 10 pm – 5 am
        mult, spct = 1.25, 25
    elif (6 <= hour < 9) or (16 <= hour < 19):  # rush hours
        mult, spct = 1.15, 15
    else:
        mult, spct = 1.0, 0
    base_incl = round(pre * mult)  # base including surcharge (matches legacy math)
    gratuity = round(base_incl * 0.18)
    fees = 22
    base_display = round(pre)
    return {
        "mode": "formula",
        "base": float(base_display),
        "surcharge": float(base_incl - base_display),
        "surcharge_pct": spct,
        "gratuity": float(gratuity),
        "gratuity_pct": 18,
        "fees": float(fees),
        "total": float(base_incl + gratuity + fees),
        "minimum_fare": (rate * miles) <= float(minimum),
        "rate": rate,
        "minimum": float(minimum),
        "miles": miles,
    }


def _calc_total_cents(
    vehicle: str,
    distance_miles: float = 0,
    hour: int = 12,
    pickup: str = None,
    dropoff: str = None,
    pickup_lat: float = None,
    pickup_lon: float = None,
    dropoff_lat: float = None,
    dropoff_lon: float = None,
) -> int:
    total = _fare_breakdown(
        vehicle,
        distance_miles,
        hour,
        pickup,
        dropoff,
        pickup_lat,
        pickup_lon,
        dropoff_lat,
        dropoff_lon,
    )["total"]
    return round(total * 100)



# --- INITIAL DATA ---
def init_drivers(db: Session):
    if db.query(DriverModel).first():
        return

    drivers_to_add = [
        {"name": "Jack Dorj", "phone": "4156994052", "vehicle": "SUV"},
        {"name": "Bataa", "phone": "4155550101", "vehicle": "Sedan"},
        {"name": "Boldoo", "phone": "4155550102", "vehicle": "SUV"},
        {"name": "Hanaa", "phone": "4155550103", "vehicle": "SUV"},
        {"name": "Irmoon", "phone": "4155550104", "vehicle": "Sedan"},
    ]

    for d_data in drivers_to_add:
        db.add(DriverModel(**d_data))

    db.commit()


# --- WEBSOCKETS ---
@app.websocket("/ws/dispatch")
async def websocket_endpoint(websocket: WebSocket):
    token = websocket.cookies.get("session_token")
    if not _session_valid(token):
        await websocket.close(code=4001)
        return
    sess = _active_sessions[token]
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


async def _midnight_archive_loop():
    while True:
        now = datetime.utcnow()
        # seconds until next midnight UTC
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        await asyncio.sleep((tomorrow - now).total_seconds())
        db = SessionLocal()
        try:
            count = (
                db.query(ReservationModel)
                .filter(
                    ReservationModel.status.in_(["Completed", "Cancelled"]),
                    ReservationModel.archived == 0,
                )
                .update({"archived": 1})
            )
            db.commit()
            if count:
                await manager.broadcast(
                    {
                        "type": "UPDATE",
                        "message": f"{count} resolved trips archived at midnight",
                    }
                )
        finally:
            db.close()


def seed_admin_user(db: Session):
    import sqlalchemy.exc

    admin_email = os.getenv("ADMIN_EMAIL", "admin@gobilimo.live")
    admin_password = os.getenv("ADMIN_PASSWORD", os.getenv("DISPATCH_PASSWORD", ""))
    if not admin_password:
        return
    hashed = _hash_password(admin_password)
    existing = db.query(UserModel).filter(UserModel.role == "admin").first()
    if existing:
        existing.email = admin_email
        existing.hashed_password = hashed
        try:
            db.commit()
        except sqlalchemy.exc.IntegrityError:
            db.rollback()
    else:
        try:
            db.add(
                UserModel(
                    email=admin_email,
                    hashed_password=hashed,
                    name="Admin",
                    role="admin",
                )
            )
            db.commit()
        except sqlalchemy.exc.IntegrityError:
            db.rollback()


@app.on_event("startup")
async def startup_event():
    from sqlalchemy import text

    with engine.connect() as conn:
        # drivers migration
        result = conn.execute(text("PRAGMA table_info(drivers)"))
        existing_cols = [row[1] for row in result.fetchall()]
        if "email" not in existing_cols:
            conn.execute(text("ALTER TABLE drivers ADD COLUMN email VARCHAR"))
            conn.commit()
        # reservations migration
        result = conn.execute(text("PRAGMA table_info(reservations)"))
        existing_cols = [row[1] for row in result.fetchall()]
        if "archived" not in existing_cols:
            conn.execute(
                text("ALTER TABLE reservations ADD COLUMN archived INTEGER DEFAULT 0")
            )
            conn.commit()
        if "created_by" not in existing_cols:
            conn.execute(text("ALTER TABLE reservations ADD COLUMN created_by INTEGER"))
            conn.commit()
        if "stripe_session_id" not in existing_cols:
            conn.execute(
                text("ALTER TABLE reservations ADD COLUMN stripe_session_id VARCHAR")
            )
            conn.commit()
        if "distance_miles" not in existing_cols:
            conn.execute(
                text("ALTER TABLE reservations ADD COLUMN distance_miles FLOAT")
            )
            conn.commit()
        if "airline" not in existing_cols:
            conn.execute(text("ALTER TABLE reservations ADD COLUMN airline VARCHAR"))
            conn.commit()
        if "flight_number" not in existing_cols:
            conn.execute(
                text("ALTER TABLE reservations ADD COLUMN flight_number VARCHAR")
            )
            conn.commit()
        if "hours_count" not in existing_cols:
            conn.execute(
                text("ALTER TABLE reservations ADD COLUMN hours_count INTEGER")
            )
            conn.commit()
        if "custom_price" not in existing_cols:
            conn.execute(
                text("ALTER TABLE reservations ADD COLUMN custom_price FLOAT")
            )
            conn.commit()
        if "custom_base_rate" not in existing_cols:
            conn.execute(
                text("ALTER TABLE reservations ADD COLUMN custom_base_rate FLOAT")
            )
            conn.commit()
        if "custom_gratuity" not in existing_cols:
            conn.execute(
                text("ALTER TABLE reservations ADD COLUMN custom_gratuity FLOAT")
            )
            conn.commit()
        if "custom_service_fee" not in existing_cols:
            conn.execute(
                text("ALTER TABLE reservations ADD COLUMN custom_service_fee FLOAT")
            )
            conn.commit()
    db = SessionLocal()
    try:
        init_drivers(db)
        seed_admin_user(db)
    finally:
        db.close()
    asyncio.create_task(_midnight_archive_loop())


# --- ROUTES ---
@app.get("/login")
def login_page():
    return FileResponse(FRONTEND_DIR / "login.html")


@app.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    remember: str = Form(default=""),
    _: None = Depends(check_login_rate),
    db: Session = Depends(get_db),
):
    user = db.query(UserModel).filter(UserModel.email == email.lower().strip()).first()
    if not user or not _verify_password(password, user.hashed_password):
        return RedirectResponse(url="/login?error=1", status_code=303)
    duration = _REMEMBER_ME_DURATION if remember == "on" else _SESSION_DURATION
    token = secrets.token_urlsafe(32)
    _active_sessions[token] = {
        "user_id": user.id,
        "role": user.role,
        "name": user.name,
        "email": user.email,
        "expires": datetime.utcnow() + duration,
    }
    response = RedirectResponse(url="/dispatch", status_code=303)
    response.set_cookie(
        "session_token",
        token,
        httponly=True,
        samesite="strict",
        max_age=int(duration.total_seconds()),
        secure=_IS_PRODUCTION,
    )
    return response


@app.get("/api/me")
def get_me(current_user: dict = Depends(get_current_user)):
    return {
        "user_id": current_user["user_id"],
        "name": current_user["name"],
        "email": current_user["email"],
        "role": current_user["role"],
    }


@app.post("/api/auth/forgot-password")
async def forgot_password(payload: dict):
    # Placeholder — no email service wired yet. Returns success so the UI can show a message.
    return {
        "status": "ok",
        "message": "If that email exists, instructions will be sent.",
    }


@app.post("/api/users")
async def create_user(
    payload: dict,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    email = payload.get("email", "").lower().strip()
    password = payload.get("password", "")
    name = payload.get("name", "")
    role = payload.get("role", "operator")
    if not email or not password or not name:
        raise HTTPException(
            status_code=422, detail="email, password, and name are required"
        )
    if role not in ("admin", "operator"):
        raise HTTPException(status_code=422, detail="role must be admin or operator")
    if db.query(UserModel).filter(UserModel.email == email).first():
        raise HTTPException(status_code=409, detail="Email already registered")
    hashed = _hash_password(password)
    user = UserModel(email=email, hashed_password=hashed, name=name, role=role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return {
        "status": "Success",
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
        },
    }


@app.get("/logout")
def logout(session_token: Optional[str] = Cookie(default=None)):
    if session_token:
        _active_sessions.pop(session_token, None)
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("session_token")
    return response


@app.get("/")
def home():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/driver")
def driver_location_page():
    return FileResponse(FRONTEND_DIR / "driver.html")


@app.get("/api/drivers/public")
def list_drivers_public(db: Session = Depends(get_db)):
    drivers = db.query(DriverModel).filter(DriverModel.status != "Offline").all()
    return {
        "status": "Success",
        "drivers": [
            {"id": d.id, "name": d.name, "vehicle": d.vehicle, "status": d.status}
            for d in drivers
        ],
    }


@app.get("/dispatch")
@app.get("/dispatch.html")
def dispatch_page(session_token: Optional[str] = Cookie(default=None)):
    if not _session_valid(session_token):
        return RedirectResponse(url="/login", status_code=302)
    return FileResponse(FRONTEND_DIR / "dispatch.html")


@app.get("/booking")
@app.get("/booking.html")
def booking_page():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/about")
@app.get("/about.html")
def about_page():
    return FileResponse(SITE_DIR / "about.html")


@app.get("/blog")
@app.get("/blog.html")
def blog_page():
    return FileResponse(SITE_DIR / "blog.html")


@app.get("/contact")
@app.get("/contact.html")
def contact_page():
    return FileResponse(SITE_DIR / "contact.html")


@app.get("/services")
@app.get("/services.html")
def services_page():
    return FileResponse(SITE_DIR / "services.html")


@app.get("/index")
@app.get("/index.html")
@app.get("/demo")
@app.get("/demo.html")
def landing_page():
    return FileResponse(SITE_DIR / "demo.html")


@app.get("/payment/{res_id}")
def payment_page(res_id: int):
    return FileResponse(FRONTEND_DIR / "payment.html")


@app.get("/invoice/{res_id}")
def invoice_page(
    res_id: int, db: Session = Depends(get_db), _: dict = Depends(require_admin)
):
    from fastapi.responses import HTMLResponse

    r = db.query(ReservationModel).filter(ReservationModel.id == res_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Trip not found")

    try:
        hour = int((r.time or "12:00").split(":")[0])
    except Exception:
        hour = 12
    gratuity_pct = 18
    price_mode = "formula"
    try:
        if r.custom_price is not None:
            price_mode = "custom"
            total = float(r.custom_price)
            if r.custom_service_fee is not None:
                service_fee = float(r.custom_service_fee)
            else:
                service_fee = 22.00 if total >= 25.00 else 0.0

            if r.custom_base_rate is not None:
                base = float(r.custom_base_rate)
            else:
                remaining = total - service_fee
                base = round(remaining / 1.18, 2) if total >= 25.00 else total

            if r.custom_gratuity is not None:
                gratuity = float(r.custom_gratuity)
            else:
                gratuity = round(total - base - service_fee, 2) if total >= 25.00 else 0.0
        else:
            bd = _fare_breakdown(
                r.vehicle,
                r.distance_miles or 0,
                hour,
                r.pickup,
                r.dropoff,
                r.pickup_lat,
                r.pickup_lon,
                r.dropoff_lat,
                r.dropoff_lon,
            )
            total = bd["total"]
            base = round(bd["base"] + bd["surcharge"], 2)
            gratuity = bd["gratuity"]
            service_fee = bd["fees"]
            gratuity_pct = bd["gratuity_pct"]
            price_mode = bd["mode"]
        payout = round(total * 0.70, 2)
        commission = round(total * 0.30, 2)
    except Exception:
        base = gratuity = service_fee = payout = commission = 0.0
        total = 0.0

    try:
        dt = datetime.fromisoformat(f"{r.date}T{r.time}")
        trip_date = dt.strftime("%B %-d, %Y · %-I:%M %p")
    except Exception:
        trip_date = f"{html.escape(r.date)} {html.escape(r.time)}"

    phone_fmt = html.escape(r.phone or "")
    dist_str = f"{r.distance_miles:.2f} miles" if r.distance_miles else "N/A"
    order_no = f"TSL-{r.id:03d}"

    surcharge_rows = ""
    if price_mode == "formula":
        if hour >= 22 or hour < 5:
            surcharge_rows = "<tr><td>Late night surcharge (25%)</td><td>included</td></tr>"
        elif (6 <= hour < 9) or (16 <= hour < 19):
            surcharge_rows = "<tr><td>Rush hour surcharge (15%)</td><td>included</td></tr>"

    notes_row = (
        f"<div class='itin-row'><span class='itin-label'>Notes:</span> {html.escape(r.notes)}</div>"
        if r.notes
        else ""
    )

    html_content = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Invoice {order_no} — Tsatsral Limo LLC</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Helvetica Neue',Arial,sans-serif;font-size:13px;color:#222;background:#fff;padding:48px}}
  .page{{max-width:760px;margin:0 auto}}
  .header{{display:flex;justify-content:space-between;align-items:flex-start;padding-bottom:28px;border-bottom:2px solid #111}}
  .logo-name{{font-size:20px;font-weight:800;letter-spacing:-.5px;text-transform:uppercase}}
  .logo-sub{{font-size:11px;color:#666;letter-spacing:.5px;margin-top:4px}}
  .header-meta{{text-align:right;font-size:12px;color:#444;line-height:1.9}}
  .header-meta strong{{color:#111}}
  h1{{font-size:32px;font-weight:800;letter-spacing:-1px;margin:28px 0 24px}}
  .contacts{{display:grid;grid-template-columns:1fr 1fr;gap:24px;padding:20px 0;border-top:1px solid #e5e5e5;border-bottom:1px solid #e5e5e5;margin-bottom:32px}}
  .label{{font-size:10px;text-transform:uppercase;letter-spacing:.8px;color:#999;margin-bottom:6px}}
  .cname{{font-weight:700;font-size:14px;margin-bottom:3px}}
  .detail{{color:#555;font-size:12px;line-height:1.7}}
  h2{{font-size:16px;font-weight:700;margin-bottom:16px}}
  .trip-card{{border:1px solid #e5e5e5;border-radius:8px;overflow:hidden;margin-bottom:24px}}
  .trip-meta{{display:grid;grid-template-columns:repeat(5,1fr);border-bottom:1px solid #e5e5e5}}
  .trip-meta-item{{padding:14px 16px;border-right:1px solid #e5e5e5}}
  .trip-meta-item:last-child{{border-right:none}}
  .mlabel{{font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:#999;margin-bottom:5px}}
  .mval{{font-weight:600;font-size:12px;color:#111}}
  .itinerary{{padding:14px 16px;border-bottom:1px solid #e5e5e5;background:#fafafa}}
  .itin-row{{display:flex;gap:12px;font-size:12px;color:#333;margin-bottom:4px}}
  .itin-row:last-child{{margin-bottom:0}}
  .itin-label{{color:#999;width:64px;flex-shrink:0}}
  .line-items{{width:100%;border-collapse:collapse}}
  .line-items td{{padding:11px 16px;font-size:12px}}
  .line-items td:last-child{{text-align:right;font-weight:600}}
  .line-items .hr td{{font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:#999;padding-bottom:8px;border-bottom:1px solid #e5e5e5}}
  .line-items .sub td{{border-top:1px solid #e5e5e5;font-weight:700;background:#f7f7f7}}
  .totals-block{{display:flex;justify-content:flex-end;margin-bottom:32px}}
  .totals-table{{width:280px;border:1px solid #e5e5e5;border-radius:8px;overflow:hidden;border-collapse:collapse}}
  .totals-table td{{padding:11px 16px;font-size:13px}}
  .totals-table td:last-child{{text-align:right;font-weight:600}}
  .grand td{{background:#111;color:#fff!important;font-weight:800!important;font-size:14px}}
  .pay-table{{width:100%;border:1px solid #e5e5e5;border-radius:8px;overflow:hidden;border-collapse:collapse}}
  .pay-table th{{padding:10px 16px;font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:#999;text-align:left;border-bottom:1px solid #e5e5e5;background:#fafafa}}
  .pay-table td{{padding:12px 16px;font-size:12px;color:#333}}
  .due-row td{{font-weight:700;font-size:13px;color:#111;border-top:1px solid #e5e5e5;background:#fff9f0}}
  .footer{{text-align:center;font-size:11px;color:#bbb;padding-top:24px;border-top:1px solid #eee;margin-top:32px}}
  .print-btn{{display:block;margin:0 auto 32px;padding:10px 28px;background:#111;color:#fff;border:none;border-radius:6px;font-size:13px;font-weight:700;cursor:pointer;letter-spacing:.3px}}
  .print-btn:hover{{background:#333}}
  @media print{{.print-btn{{display:none}}body{{padding:0}}.page{{max-width:100%}}}}
</style>
</head>
<body>
<div class="page">
  <button class="print-btn" onclick="window.print()">Print / Save as PDF</button>

  <div class="header">
    <div>
      <div class="logo-name">Tsatsral Limo LLC</div>
      <div class="logo-sub">Professional Chauffeur Service</div>
    </div>
    <div class="header-meta">
      <div><strong>ORDER NO</strong> &nbsp;{order_no}</div>
      <div><strong>DATE</strong> &nbsp;{html.escape(r.date)}</div>
      <div><strong>STATUS</strong> &nbsp;{html.escape(r.status)}</div>
    </div>
  </div>

  <h1>Invoice</h1>

  <div class="contacts">
    <div>
      <div class="label">Billed To</div>
      <div class="cname">{html.escape(r.customer)}</div>
      <div class="detail">{phone_fmt}<br>{html.escape(r.email or '')}</div>
    </div>
    <div>
      <div class="label">Contact Us</div>
      <div class="cname">Tsatsral Limo LLC</div>
      <div class="detail">contact@gobilimo.com<br>(415) 699-4052</div>
    </div>
  </div>

  <h2>Pricing</h2>
  <div class="trip-card">
    <div class="trip-meta">
      <div class="trip-meta-item"><div class="mlabel">Trip #</div><div class="mval">{order_no}</div></div>
      <div class="trip-meta-item"><div class="mlabel">Type</div><div class="mval">{html.escape(r.trip_type)}</div></div>
      <div class="trip-meta-item"><div class="mlabel">Driver</div><div class="mval">{html.escape(r.assigned_driver or '—')}</div></div>
      <div class="trip-meta-item"><div class="mlabel">Date &amp; Time</div><div class="mval">{trip_date}</div></div>
      <div class="trip-meta-item"><div class="mlabel">Vehicle</div><div class="mval">{html.escape(r.vehicle)}</div></div>
    </div>
    <div class="itinerary">
      <div class="mlabel" style="margin-bottom:8px">Itinerary</div>
      <div class="itin-row"><span class="itin-label">Pick-up:</span>{html.escape(r.pickup)}</div>
      <div class="itin-row"><span class="itin-label">Drop-off:</span>{html.escape(r.dropoff)}</div>
      <div class="itin-row"><span class="itin-label">Distance:</span>{dist_str}</div>
      {notes_row}
    </div>
    <table class="line-items">
      <tr class="hr"><td>Item</td><td>Amount</td></tr>
      <tr><td>Base Rate ({html.escape(r.vehicle)})</td><td>${base:.2f}</td></tr>
      {surcharge_rows}
      <tr><td>Gratuity ({gratuity_pct}%)</td><td>${gratuity:.2f}</td></tr>
      <tr><td>Service Fee</td><td>${service_fee:.2f}</td></tr>
      <tr class="sub"><td>Subtotal</td><td>${total:.2f}</td></tr>
    </table>
  </div>

  <div class="totals-block">
    <table class="totals-table">
      <tr><td>Subtotal</td><td>${total:.2f}</td></tr>
      <tr class="grand"><td>Total Amount</td><td>${total:.2f}</td></tr>
    </table>
  </div>

  <h2 style="margin-bottom:16px">Payments</h2>
  <table class="pay-table">
    <thead><tr><th>Type</th><th>Method</th><th>Note</th><th>Date</th><th style="text-align:right">Amount</th></tr></thead>
    <tbody>
      <tr><td colspan="4" style="color:#bbb">No payments recorded</td><td style="text-align:right;font-weight:600">$0.00</td></tr>
      <tr class="due-row"><td colspan="4">Amount Due</td><td style="text-align:right">${total:.2f}</td></tr>
    </tbody>
  </table>

  <div class="footer">Tsatsral Limo LLC &nbsp;·&nbsp; {html.escape(r.date)} &nbsp;·&nbsp; contact@gobilimo.com</div>
</div>
</body>
</html>"""
    return HTMLResponse(content=html_content)


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    if not _stripe or not _STRIPE_SECRET_KEY:
        raise HTTPException(status_code=404)
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        if not _STRIPE_WEBHOOK_SECRET:
            raise HTTPException(status_code=400, detail="Webhook secret not configured")
        event = _stripe.Webhook.construct_event(payload, sig, _STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    if event["type"] == "checkout.session.completed":
        sess_obj = event["data"]["object"]
        res_id_str = (sess_obj.get("metadata") or {}).get("reservation_id")
        if res_id_str:
            try:
                reservation = (
                    db.query(ReservationModel)
                    .filter(ReservationModel.id == int(res_id_str))
                    .first()
                )
                if reservation:
                    reservation.payment = "Paid"
                    db.commit()
                    add_db_event(
                        db,
                        reservation.id,
                        "Payment received",
                        "Stripe checkout completed.",
                    )
                    _notify_customer(
                        reservation, build_customer_receipt_message(reservation)
                    )
                    await manager.broadcast(
                        {
                            "type": "UPDATE",
                            "message": f"Payment confirmed for trip #{res_id_str}",
                        }
                    )
            except Exception as e:
                print(f"Webhook processing error: {e}")
    return JSONResponse({"status": "ok"})


_GMAPS_BASE = "https://places.googleapis.com/v1"

_geocode_cache: dict[str, tuple] = {}  # input (lower) → (results, ts)
_place_cache: dict[str, tuple] = {}  # place_id → (detail, ts)
_GEOCODE_TTL = 600  # seconds
_PLACE_TTL = 3600  # seconds

# Bias autocomplete toward California / the Bay Area without hard-restricting,
# so statewide airports (LAX, SAN, …) still rank when typed.
_CA_BIAS = {
    "rectangle": {
        "low": {"latitude": 32.5, "longitude": -124.5},
        "high": {"latitude": 42.0, "longitude": -114.0},
    }
}


@app.get("/api/geocode")
async def geocode_proxy(q: str, token: str = ""):
    """Google Places Autocomplete (New). Returns predictions only (no
    coordinates) — resolve a chosen prediction via /api/place."""
    if not q or len(q) < 3 or len(q) > 200:
        return {"results": []}
    if not _GOOGLE_MAPS_API_KEY:
        return {"results": []}
    key = q.lower().strip()
    cached = _geocode_cache.get(key)
    if cached:
        results, ts = cached
        if (datetime.utcnow().timestamp() - ts) < _GEOCODE_TTL:
            return {"results": results}

    body = {
        "input": q,
        "includedRegionCodes": ["us"],
        "locationBias": _CA_BIAS,
    }
    if token:
        body["sessionToken"] = token
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.post(
                f"{_GMAPS_BASE}/places:autocomplete",
                json=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": _GOOGLE_MAPS_API_KEY,
                },
            )
            data = r.json() if r.status_code == 200 else {}
    except Exception:
        data = {}

    suggestions = data.get("suggestions", []) if isinstance(data, dict) else []
    safe = []
    for s in suggestions:
        p = s.get("placePrediction")
        if not p:
            continue
        pid = p.get("placeId")
        if not pid:
            continue
        fmt = p.get("structuredFormat") or {}
        main = (fmt.get("mainText") or {}).get("text", "")
        secondary = (fmt.get("secondaryText") or {}).get("text", "")
        full = (p.get("text") or {}).get("text", "") or main
        if not main:
            main = full
        safe.append(
            {
                "place_id": pid,
                "display_name": full[:300],
                "line1": main[:150],
                "line2": secondary[:120],
            }
        )

    if len(_geocode_cache) > 500:
        oldest = min(_geocode_cache, key=lambda k: _geocode_cache[k][1])
        _geocode_cache.pop(oldest, None)
    _geocode_cache[key] = (safe, datetime.utcnow().timestamp())
    return {"results": safe}


@app.get("/api/place")
async def place_details(place_id: str, token: str = ""):
    """Resolve a Google place_id to coordinates + a formatted address."""
    if not place_id or len(place_id) > 300:
        return {"ok": False}
    if not _GOOGLE_MAPS_API_KEY:
        return {"ok": False}
    cached = _place_cache.get(place_id)
    if cached:
        detail, ts = cached
        if (datetime.utcnow().timestamp() - ts) < _PLACE_TTL:
            return detail

    params = {"sessionToken": token} if token else {}
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(
                f"{_GMAPS_BASE}/places/{place_id}",
                params=params,
                headers={
                    "X-Goog-Api-Key": _GOOGLE_MAPS_API_KEY,
                    "X-Goog-FieldMask": "location,formattedAddress,displayName",
                },
            )
            data = r.json() if r.status_code == 200 else {}
    except Exception:
        data = {}

    loc = data.get("location") if isinstance(data, dict) else None
    if not loc or loc.get("latitude") is None or loc.get("longitude") is None:
        return {"ok": False}
    name = (data.get("displayName") or {}).get("text", "")
    addr = data.get("formattedAddress", "")
    if name and name not in addr:
        display = ", ".join(filter(None, [name, addr]))
    else:
        display = addr or name
    detail = {
        "ok": True,
        "lat": str(loc["latitude"]),
        "lon": str(loc["longitude"]),
        "display_name": display[:300],
    }
    if len(_place_cache) > 1000:
        oldest = min(_place_cache, key=lambda k: _place_cache[k][1])
        _place_cache.pop(oldest, None)
    _place_cache[place_id] = (detail, datetime.utcnow().timestamp())
    return detail


class QuoteRequest(BaseModel):
    vehicle: str
    pickup: Optional[str] = None
    dropoff: Optional[str] = None
    distance_miles: Optional[float] = None
    time: Optional[str] = None
    pickup_lat: Optional[float] = None
    pickup_lon: Optional[float] = None
    dropoff_lat: Optional[float] = None
    dropoff_lon: Optional[float] = None


@app.post("/api/quote")
def quote(data: QuoteRequest):
    """Live price quote for the public booking form.

    Uses the same ``_fare_breakdown`` as the Stripe charge, so the price shown to
    the customer is exactly the price they will be charged.
    """
    try:
        hour = int((data.time or "12:00").split(":")[0])
    except Exception:
        hour = 12
    vehicle = data.vehicle if data.vehicle in _ALLOWED_VEHICLES else "SUV"
    return _fare_breakdown(
        vehicle,
        data.distance_miles or 0,
        hour,
        data.pickup,
        data.dropoff,
        data.pickup_lat,
        data.pickup_lon,
        data.dropoff_lat,
        data.dropoff_lon,
    )


@app.get("/api/reservations")
def list_reservations(
    db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)
):
    query = db.query(ReservationModel).filter(ReservationModel.archived == 0)
    if current_user["role"] != "admin":
        query = query.filter(
            or_(
                ReservationModel.assigned_driver == current_user["name"],
                ReservationModel.created_by == current_user["user_id"],
            )
        )
    res_list = query.all()
    # Manual serialization because of relationship
    output = []
    for r in res_list:
        events = [{"time": e.time, "title": e.title, "body": e.body} for e in r.events]
        # Sort events by ID descending for timeline (newest first)
        events.reverse()
        output.append(
            {
                "id": r.id,
                "customer": r.customer,
                "phone": r.phone,
                "email": r.email,
                "pickup": r.pickup,
                "dropoff": r.dropoff,
                "date": r.date,
                "time": r.time,
                "passengers": r.passengers,
                "vehicle": r.vehicle,
                "payment": r.payment,
                "trip_type": r.trip_type,
                "notes": r.notes,
                "status": r.status,
                "assigned_driver": r.assigned_driver,
                "payment_url": r.payment_url,
                "pickup_lat": r.pickup_lat,
                "pickup_lon": r.pickup_lon,
                "dropoff_lat": r.dropoff_lat,
                "dropoff_lon": r.dropoff_lon,
                "airline": r.airline,
                "flight_number": r.flight_number,
                "hours_count": r.hours_count,
                "custom_price": r.custom_price,
                "custom_base_rate": r.custom_base_rate,
                "custom_gratuity": r.custom_gratuity,
                "custom_service_fee": r.custom_service_fee,
                "price": _get_reservation_price(r),
                "events": events,
            }
        )
    return {"status": "Success", "reservations": output}


def _get_reservation_price(r: ReservationModel) -> float:
    if r.custom_price is not None:
        return float(r.custom_price)
    try:
        hour = int((r.time or "12:00").split(":")[0])
        total_cents = _calc_total_cents(
            r.vehicle,
            r.distance_miles or 0,
            hour,
            r.pickup,
            r.dropoff,
            r.pickup_lat,
            r.pickup_lon,
            r.dropoff_lat,
            r.dropoff_lon,
        )
        return total_cents / 100
    except Exception:
        return 0.0


def _serialize_reservation(r: ReservationModel) -> dict:
    events = [{"time": e.time, "title": e.title, "body": e.body} for e in r.events]
    events.reverse()
    return {
        "id": r.id,
        "customer": r.customer,
        "phone": r.phone,
        "email": r.email,
        "pickup": r.pickup,
        "dropoff": r.dropoff,
        "date": r.date,
        "time": r.time,
        "passengers": r.passengers,
        "vehicle": r.vehicle,
        "payment": r.payment,
        "trip_type": r.trip_type,
        "notes": r.notes,
        "status": r.status,
        "assigned_driver": r.assigned_driver,
        "payment_url": r.payment_url,
        "pickup_lat": r.pickup_lat,
        "pickup_lon": r.pickup_lon,
        "dropoff_lat": r.dropoff_lat,
        "dropoff_lon": r.dropoff_lon,
        "airline": r.airline,
        "flight_number": r.flight_number,
        "hours_count": r.hours_count,
        "custom_price": r.custom_price,
        "custom_base_rate": r.custom_base_rate,
        "custom_gratuity": r.custom_gratuity,
        "custom_service_fee": r.custom_service_fee,
        "price": _get_reservation_price(r),
        "archived": r.archived,
        "events": events,
    }


def _serialize_history_reservation(r: ReservationModel) -> dict:
    return {
        "id": r.id,
        "customer": r.customer,
        "phone": r.phone,
        "email": r.email,
        "pickup": r.pickup,
        "dropoff": r.dropoff,
        "date": r.date,
        "time": r.time,
        "passengers": r.passengers,
        "vehicle": r.vehicle,
        "payment": r.payment,
        "trip_type": r.trip_type,
        "notes": r.notes,
        "status": r.status,
        "assigned_driver": r.assigned_driver,
        "archived": r.archived,
        "custom_price": r.custom_price,
        "custom_base_rate": r.custom_base_rate,
        "custom_gratuity": r.custom_gratuity,
        "custom_service_fee": r.custom_service_fee,
        "price": _get_reservation_price(r),
    }


@app.get("/api/reservations/metrics")
def reservation_metrics(
    day: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    target_day = day or date.today().isoformat()
    query = db.query(ReservationModel).filter(ReservationModel.date == target_day)
    if current_user["role"] != "admin":
        query = query.filter(
            or_(
                ReservationModel.assigned_driver == current_user["name"],
                ReservationModel.created_by == current_user["user_id"],
            )
        )
    return {
        "status": "Success",
        "date": target_day,
        "trips_today": query.count(),
    }


@app.get("/api/reservations/history")
def reservation_history(
    day: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    target_day = day or date.today().isoformat()
    query = db.query(ReservationModel).filter(ReservationModel.date == target_day)
    if current_user["role"] != "admin":
        query = query.filter(
            or_(
                ReservationModel.assigned_driver == current_user["name"],
                ReservationModel.created_by == current_user["user_id"],
            )
        )
    trips = query.order_by(ReservationModel.time.asc(), ReservationModel.id.asc()).all()
    return {
        "status": "Success",
        "date": target_day,
        "reservations": [_serialize_history_reservation(r) for r in trips],
    }


@app.post("/api/reservations")
async def create_reservation(
    request: Request,
    data: ReservationCreate,
    db: Session = Depends(get_db),
    _: None = Depends(check_booking_rate),
):
    res_data = data.model_dump()
    # If an operator creates a reservation via API (authenticated), stamp created_by
    session_token = request.cookies.get("session_token")
    sess = _get_session(session_token)
    if sess:
        res_data["created_by"] = sess["user_id"]
    else:
        res_data["created_by"] = None
    db_res = ReservationModel(**res_data)
    db.add(db_res)
    db.commit()
    db.refresh(db_res)

    # Prefer static payment link (supports client_reference_id for tracking)
    if _STRIPE_PAYMENT_LINK:
        payment_url = f"{_STRIPE_PAYMENT_LINK}?client_reference_id={db_res.id}"
    elif _stripe and _STRIPE_SECRET_KEY:
        try:
            origin = str(request.base_url).rstrip("/")
            stripe_sess = _stripe.checkout.Session.create(
                mode="payment",
                line_items=[
                    {
                        "price_data": {
                            "currency": "usd",
                            "unit_amount": _calc_total_cents(
                                db_res.vehicle,
                                db_res.distance_miles or 0,
                                int((db_res.time or "12:00").split(":")[0]),
                                db_res.pickup,
                                db_res.dropoff,
                                db_res.pickup_lat,
                                db_res.pickup_lon,
                                db_res.dropoff_lat,
                                db_res.dropoff_lon,
                            ),
                            "product_data": {
                                "name": f"Tsatsral Limo — {db_res.vehicle}",
                                "description": f"{db_res.pickup} → {db_res.dropoff} on {db_res.date}",
                            },
                        },
                        "quantity": 1,
                    }
                ],
                metadata={"reservation_id": str(db_res.id)},
                customer_email=db_res.email or None,
                success_url=f"{origin}/?paid=1&res={db_res.id}",
                cancel_url=f"{origin}/",
            )
            payment_url = stripe_sess.url
            db_res.stripe_session_id = stripe_sess.id
        except Exception as stripe_err:
            print(f"Stripe session error: {stripe_err}")
            payment_url = f"/payment/{db_res.id}"
    else:
        payment_url = f"/payment/{db_res.id}"
    db_res.payment_url = payment_url
    db.commit()

    add_db_event(
        db, db_res.id, "Reservation created", f"{db_res.vehicle} booked via dashboard."
    )

    _notify_customer(db_res, build_customer_booking_message(db_res))

    await manager.broadcast(
        {"type": "UPDATE", "message": f"New reservation from {db_res.customer}"}
    )
    return {
        "status": "Success",
        "reservation": {"id": db_res.id, "payment_url": payment_url},
    }


@app.get("/api/reservations/public/{res_id}")
def get_public_reservation(res_id: int, db: Session = Depends(get_db)):
    r = db.query(ReservationModel).filter(ReservationModel.id == res_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Reservation not found")
    return {
        "id": r.id,
        "customer": r.customer,
        "price": _get_reservation_price(r),
        "custom_price": r.custom_price,
        "custom_base_rate": r.custom_base_rate,
        "custom_gratuity": r.custom_gratuity,
        "custom_service_fee": r.custom_service_fee,
        "status": r.status,
        "pickup": r.pickup,
        "dropoff": r.dropoff,
        "date": r.date,
        "time": r.time
    }


@app.put("/api/reservations/{reservation_id}")
async def update_reservation(
    reservation_id: int,
    data: ReservationUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    reservation = (
        db.query(ReservationModel).filter(ReservationModel.id == reservation_id).first()
    )
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    # IDOR: operators can only edit trips they created or were assigned to
    if current_user["role"] != "admin":
        if (
            reservation.created_by != current_user["user_id"]
            and reservation.assigned_driver != current_user["name"]
        ):
            raise HTTPException(status_code=403, detail="Not allowed")

    updates = data.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    changed = []
    for key, value in updates.items():
        old = getattr(reservation, key, None)
        if old != value:
            setattr(reservation, key, value)
            changed.append(key)

    if changed:
        db.commit()
        add_db_event(
            db,
            reservation.id,
            "Reservation edited",
            f"{current_user['name']} updated: {', '.join(changed)}.",
        )
        await manager.broadcast(
            {"type": "UPDATE", "message": f"Trip #{reservation_id} updated"}
        )

    return {"status": "Success", "changed": changed}


@app.post("/api/reservations/{reservation_id}/archive")
async def archive_reservation(
    reservation_id: int, db: Session = Depends(get_db), _: None = Depends(require_admin)
):
    reservation = (
        db.query(ReservationModel).filter(ReservationModel.id == reservation_id).first()
    )
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    if reservation.status not in ("Completed", "Cancelled"):
        raise HTTPException(
            status_code=409, detail="Only completed or cancelled trips can be archived"
        )
    reservation.archived = 1
    db.commit()
    await manager.broadcast(
        {"type": "UPDATE", "message": f"Trip #{reservation_id} closed"}
    )
    return {"status": "Success"}


@app.post("/api/reservations/archive-all")
async def archive_all_resolved(
    payload: dict, db: Session = Depends(get_db), _: None = Depends(require_admin)
):
    status = payload.get("status")
    if status not in ("Completed", "Cancelled"):
        raise HTTPException(
            status_code=422, detail="Can only archive Completed or Cancelled trips"
        )
    db.query(ReservationModel).filter(
        ReservationModel.status == status,
        ReservationModel.archived == 0,
    ).update({"archived": 1})
    db.commit()
    await manager.broadcast(
        {"type": "UPDATE", "message": f"All {status} trips cleared"}
    )
    return {"status": "Success"}


@app.post("/api/reservations/{reservation_id}/notes")
async def add_note(
    reservation_id: int,
    note: dict,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    reservation = (
        db.query(ReservationModel).filter(ReservationModel.id == reservation_id).first()
    )
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")

    add_db_event(db, reservation_id, "Internal Note", note.get("text", ""))
    await manager.broadcast(
        {"type": "UPDATE", "message": f"Note added to trip #{reservation_id}"}
    )
    return {"status": "Success"}


@app.post("/api/reservations/{reservation_id}/complete")
async def complete_reservation(
    reservation_id: int, db: Session = Depends(get_db), _: None = Depends(require_admin)
):
    reservation = (
        db.query(ReservationModel).filter(ReservationModel.id == reservation_id).first()
    )
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")

    reservation.status = "Completed"

    driver = None
    if reservation.assigned_driver:
        driver = (
            db.query(DriverModel)
            .filter(DriverModel.name == reservation.assigned_driver)
            .first()
        )
        if driver:
            driver.status = "Online"

    add_db_event(db, reservation_id, "Trip completed", "Reservation moved to history.")
    db.commit()

    # Cancel pre-trip reminder if pending
    reminder_task = _scheduled_reminders.pop(reservation_id, None)
    if reminder_task:
        reminder_task.cancel()

    # Send completion SMS to driver
    if driver:
        try:
            _send_sms(driver.phone, build_completion_message(reservation))
        except Exception:
            pass

    await manager.broadcast(
        {"type": "UPDATE", "message": f"Trip #{reservation_id} completed"}
    )
    return {"status": "Success"}


@app.post("/api/reservations/{reservation_id}/start")
async def start_trip(
    reservation_id: int, db: Session = Depends(get_db), _: None = Depends(require_admin)
):
    reservation = (
        db.query(ReservationModel).filter(ReservationModel.id == reservation_id).first()
    )
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    if reservation.status != "Assigned":
        raise HTTPException(
            status_code=409, detail="Trip must be in Assigned state to start"
        )
    if not reservation.assigned_driver:
        raise HTTPException(status_code=409, detail="No driver assigned")

    driver = (
        db.query(DriverModel)
        .filter(DriverModel.name == reservation.assigned_driver)
        .first()
    )
    reservation.status = "In Progress"
    add_db_event(
        db, reservation_id, "Trip started", "Driver dispatched to pickup location."
    )
    db.commit()

    if driver:
        try:
            _send_sms(driver.phone, build_passenger_info_message(reservation))
            add_db_event(
                db,
                reservation_id,
                "Passenger info sent",
                f"Contact details SMS sent to {driver.name}.",
            )
            db.commit()
        except Exception:
            pass

    if reservation.phone and driver:
        try:
            _send_sms(
                reservation.phone,
                build_customer_en_route_message(reservation, driver),
            )
            add_db_event(
                db,
                reservation_id,
                "Customer notified",
                f"En-route SMS sent to {reservation.customer}.",
            )
            db.commit()
        except Exception:
            pass

    await manager.broadcast(
        {"type": "UPDATE", "message": f"Trip #{reservation_id} is now In Progress"}
    )
    return {"status": "Success"}


@app.post("/api/reservations/{reservation_id}/arrive")
async def mark_driver_arrived(
    reservation_id: int, db: Session = Depends(get_db), _: None = Depends(require_admin)
):
    reservation = (
        db.query(ReservationModel).filter(ReservationModel.id == reservation_id).first()
    )
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    if reservation.status not in ("In Progress", "Arrived"):
        raise HTTPException(
            status_code=409, detail="Trip must be in progress before arrival"
        )
    if not reservation.assigned_driver:
        raise HTTPException(status_code=409, detail="No driver assigned")

    driver = (
        db.query(DriverModel)
        .filter(DriverModel.name == reservation.assigned_driver)
        .first()
    )
    reservation.status = "Arrived"
    add_db_event(db, reservation_id, "Driver arrived", "Driver reached pickup.")
    db.commit()

    if reservation.phone and driver:
        try:
            _send_sms(
                reservation.phone,
                build_customer_arrived_message(reservation, driver),
            )
            add_db_event(
                db,
                reservation_id,
                "Customer notified",
                f"Arrival SMS sent to {reservation.customer}.",
            )
            db.commit()
        except Exception:
            pass

    await manager.broadcast(
        {"type": "UPDATE", "message": f"Driver arrived for trip #{reservation_id}"}
    )
    return {"status": "Success"}


@app.post("/api/reservations/{reservation_id}/cancel")
async def cancel_reservation(
    reservation_id: int, db: Session = Depends(get_db), _: None = Depends(require_admin)
):
    reservation = (
        db.query(ReservationModel).filter(ReservationModel.id == reservation_id).first()
    )
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")

    reservation.status = "Cancelled"

    if reservation.assigned_driver:
        driver = (
            db.query(DriverModel)
            .filter(DriverModel.name == reservation.assigned_driver)
            .first()
        )
        if driver:
            driver.status = "Online"

    add_db_event(
        db, reservation_id, "Trip cancelled", "Reservation was cancelled by operator."
    )
    db.commit()

    reminder_task = _scheduled_reminders.pop(reservation_id, None)
    if reminder_task:
        reminder_task.cancel()

    await manager.broadcast(
        {"type": "UPDATE", "message": f"Trip #{reservation_id} cancelled"}
    )
    return {"status": "Success"}


@app.post("/api/reservations/{reservation_id}/assign")
async def assign_driver(
    reservation_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    reservation = (
        db.query(ReservationModel).filter(ReservationModel.id == reservation_id).first()
    )
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")

    driver_id = payload.get("driver_id")
    if not driver_id:
        # Fallback to first online driver if ID not provided
        driver = db.query(DriverModel).filter(DriverModel.status == "Online").first()
    else:
        driver = db.query(DriverModel).filter(DriverModel.id == driver_id).first()

    if not driver:
        raise HTTPException(status_code=503, detail="Requested driver not available")

    # Linq dispatch
    try:
        sid = notify_driver_of_reservation(
            reservation, {"name": driver.name, "phone": driver.phone}
        )
        if sid == "LINQ_NOT_CONFIGURED":
            raise HTTPException(status_code=503, detail="Linq not configured")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Linq error: {e}")

    reservation.status = "Pending confirmation"
    reservation.assigned_driver = driver.name

    # Update driver status to Busy so they aren't recommended for other trips
    driver.status = "Busy"

    add_db_event(
        db,
        reservation_id,
        "Driver notified",
        f"SMS sent to {driver.name}. Awaiting reply.",
    )
    db.commit()

    await manager.broadcast(
        {
            "type": "UPDATE",
            "message": f"{driver.name} assigned to trip #{reservation_id}",
        }
    )

    return {
        "status": "Success",
        "reservation_id": reservation_id,
        "driver": {"name": driver.name, "vehicle": driver.vehicle},
        "message_id": sid,
    }


def _verify_linq_signature(
    webhook_id: str, timestamp: str, body: bytes, signature_header: str, secret: str
) -> bool:
    """Verify a Standard Webhooks (Linq) signature.

    HMAC-SHA256 over "{webhook-id}.{webhook-timestamp}.{raw-body}". The signing
    secret is `whsec_` + base64 key. The webhook-signature header carries one or
    more space-separated "v1,<base64sig>" tokens.
    """
    if not secret or not signature_header:
        return False
    try:
        key_part = secret.split("_", 1)[1] if secret.startswith("whsec_") else secret
        key = base64.b64decode(key_part)
    except Exception:
        return False
    signed = webhook_id.encode() + b"." + timestamp.encode() + b"." + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for token in signature_header.split():
        _, _, sig = token.partition(",")
        if sig and hmac.compare_digest(sig, expected):
            return True
    return False


@app.post("/linq/webhook")
async def linq_webhook(request: Request, db: Session = Depends(get_db)):
    raw = await request.body()

    # Verify the Standard Webhooks signature when a secret is configured. This
    # endpoint mutates trip state, so signing is strongly recommended; if no
    # secret is set we process anyway (and warn) to match Linq's example setup.
    secret = os.getenv("LINQ_WEBHOOK_SECRET", "")
    if secret:
        webhook_id = request.headers.get("webhook-id", "")
        timestamp = request.headers.get("webhook-timestamp", "")
        signature = request.headers.get("webhook-signature", "")
        try:
            if abs(datetime.now().timestamp() - int(timestamp)) > 300:
                raise HTTPException(status_code=403, detail="Stale webhook timestamp")
        except (ValueError, TypeError):
            raise HTTPException(status_code=403, detail="Invalid webhook timestamp")
        if not _verify_linq_signature(webhook_id, timestamp, raw, signature, secret):
            print(f"[linq] signature mismatch id={webhook_id!r}")
            raise HTTPException(status_code=403, detail="Invalid webhook signature")
    else:
        print("[linq] WARNING: LINQ_WEBHOOK_SECRET unset — signature NOT verified")

    event = json.loads(raw)
    # Only driver replies matter; ack everything else.
    if event.get("event_type") != "message.received":
        return {"status": "ignored"}

    data = event.get("data", {})
    if data.get("is_from_me"):
        return {"status": "ignored"}

    from_handle = data.get("from", "")
    chat_id = data.get("chat_id", "")
    parts = (data.get("message") or {}).get("parts", [])
    text = " ".join(p.get("value", "") for p in parts if p.get("type") == "text")
    reply = text.strip().upper()

    # Normalize to 10-digit and also try E.164 to match however the number is stored in DB
    digits = re.sub(r"\D", "", from_handle)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    e164 = f"+1{digits}" if len(digits) == 10 else from_handle

    driver = (
        db.query(DriverModel)
        .filter((DriverModel.phone == digits) | (DriverModel.phone == e164))
        .first()
    )
    if not driver:
        return {"status": "ignored"}

    # Find the most recent trip pending this driver's confirmation
    reservation = (
        db.query(ReservationModel)
        .filter(
            ReservationModel.assigned_driver == driver.name,
            ReservationModel.status == "Pending confirmation",
        )
        .order_by(ReservationModel.id.desc())
        .first()
    )
    if not reservation:
        return {"status": "ignored"}

    if reply.startswith("YES"):
        reservation.status = "Assigned"
        add_db_event(
            db, reservation.id, "Driver confirmed", f"{driver.name} accepted the trip."
        )
        sms_back = f"Got it! You're confirmed for the trip from {reservation.pickup} to {reservation.dropoff}. See you there."
        db.commit()
        _schedule_trip_reminder(reservation)
    elif reply.startswith("NO"):
        reservation.status = "Needs driver"
        reservation.assigned_driver = None

        # Set driver back to Online
        driver.status = "Online"

        add_db_event(
            db,
            reservation.id,
            "Driver declined",
            f"{driver.name} declined. Trip returned to queue.",
        )
        sms_back = "No problem. The trip has been returned to the dispatch queue."
        db.commit()
    else:
        # Unrecognized reply — reply in the same chat thread via the API.
        _linq_reply(chat_id, "Reply YES to accept or NO to decline.")
        return {"status": "ok"}

    await manager.broadcast(
        {
            "type": "UPDATE",
            "message": f"{driver.name} {'accepted' if reply.startswith('YES') else 'declined'} trip #{reservation.id}",
        }
    )

    # Replies are out-of-band on Linq: send the confirmation into the same chat.
    _linq_reply(chat_id, sms_back)
    return {"status": "ok"}


@app.get("/api/integrations/status")
def integrations_status():
    """Booleans only — reports which integrations are configured. No secrets."""
    return {
        "linq_outbound": bool(
            os.getenv("LINQ_API_TOKEN") and os.getenv("LINQ_FROM_NUMBER")
        ),
        "linq_webhook_signed": bool(os.getenv("LINQ_WEBHOOK_SECRET")),
        "stripe": bool(
            os.getenv("STRIPE_SECRET_KEY") or os.getenv("STRIPE_PAYMENT_LINK")
        ),
        "stripe_webhook": bool(os.getenv("STRIPE_WEBHOOK_SECRET")),
        "google_maps": bool(os.getenv("GOOGLE_MAPS_API_KEY")),
        "app_base_url": bool(os.getenv("APP_BASE_URL")),
    }


@app.get("/api/drivers")
def list_drivers(db: Session = Depends(get_db), _: None = Depends(require_admin)):
    drivers = db.query(DriverModel).all()
    return {
        "status": "Success",
        "drivers": [
            {
                "id": d.id,
                "name": d.name,
                "email": d.email,
                "phone": d.phone,
                "vehicle": d.vehicle,
                "status": d.status,
                "lat": d.lat,
                "lon": d.lon,
            }
            for d in drivers
        ],
    }


@app.post("/api/drivers")
async def create_driver(
    data: DriverCreate, db: Session = Depends(get_db), _: None = Depends(require_admin)
):
    existing = db.query(DriverModel).filter(DriverModel.name == data.name).first()
    if existing:
        raise HTTPException(
            status_code=409, detail="A driver with that name already exists"
        )
    driver = DriverModel(**data.model_dump())
    db.add(driver)
    db.commit()
    db.refresh(driver)
    await manager.broadcast(
        {"type": "UPDATE", "message": f"{driver.name} added to the roster"}
    )
    return {"status": "Success", "driver": {"id": driver.id, "name": driver.name}}


@app.put("/api/drivers/{driver_id}")
async def update_driver(
    driver_id: int,
    data: DriverUpdate,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    driver = db.query(DriverModel).filter(DriverModel.id == driver_id).first()
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")
    if data.name is not None:
        conflict = (
            db.query(DriverModel)
            .filter(DriverModel.name == data.name, DriverModel.id != driver_id)
            .first()
        )
        if conflict:
            raise HTTPException(
                status_code=409, detail="A driver with that name already exists"
            )
        driver.name = data.name
    if data.email is not None:
        driver.email = data.email
    if data.phone is not None:
        driver.phone = data.phone
    if data.vehicle is not None:
        driver.vehicle = data.vehicle
    db.commit()
    db.refresh(driver)
    await manager.broadcast(
        {"type": "UPDATE", "message": f"{driver.name} profile updated"}
    )
    return {
        "status": "Success",
        "driver": {
            "id": driver.id,
            "name": driver.name,
            "email": driver.email,
            "phone": driver.phone,
            "vehicle": driver.vehicle,
            "status": driver.status,
        },
    }


@app.patch("/api/drivers/{driver_id}/status")
async def update_driver_status(
    driver_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    driver = db.query(DriverModel).filter(DriverModel.id == driver_id).first()
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")
    new_status = payload.get("status")
    if new_status not in _ALLOWED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid status. Allowed: {', '.join(_ALLOWED_STATUSES)}",
        )
    if driver.status == "Busy" and new_status == "Offline":
        raise HTTPException(
            status_code=409, detail="Cannot go offline while on an active trip"
        )
    driver.status = new_status
    db.commit()
    loc_event = {
        "type": "DRIVER_LOCATION",
        "driver_id": driver_id,
        "name": driver.name,
        "status": new_status,
        "lat": float(driver.lat) if driver.lat and new_status != "Offline" else None,
        "lon": float(driver.lon) if driver.lon and new_status != "Offline" else None,
    }
    await manager.broadcast(loc_event)
    await manager.broadcast(
        {"type": "UPDATE", "message": f"{driver.name} is now {new_status}"}
    )
    return {"status": "Success", "driver_status": new_status}


@app.delete("/api/drivers/{driver_id}")
async def delete_driver(
    driver_id: int, db: Session = Depends(get_db), _: None = Depends(require_admin)
):
    driver = db.query(DriverModel).filter(DriverModel.id == driver_id).first()
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")
    active_trips = (
        db.query(ReservationModel)
        .filter(
            ReservationModel.assigned_driver == driver.name,
            ReservationModel.status.in_(["Assigned", "Pending confirmation"]),
        )
        .all()
    )
    for trip in active_trips:
        trip.assigned_driver = None
        trip.status = "Needs driver"
        add_db_event(
            db,
            trip.id,
            "Driver removed",
            f"{driver.name} was removed from the roster. Trip returned to queue.",
        )
    name = driver.name
    db.delete(driver)
    db.commit()
    await manager.broadcast(
        {"type": "UPDATE", "message": f"{name} removed from roster"}
    )
    return {"status": "Success"}


@app.put("/api/drivers/{driver_id}/location")
async def update_driver_location(
    driver_id: int, payload: dict, db: Session = Depends(get_db)
):
    driver = db.query(DriverModel).filter(DriverModel.id == driver_id).first()
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")

    lat = payload.get("lat")
    lon = payload.get("lon")
    if lat is None or lon is None:
        raise HTTPException(status_code=422, detail="lat and lon required")

    try:
        lat = float(lat)
        lon = float(lon)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="lat and lon must be numbers")

    driver.lat = str(lat)
    driver.lon = str(lon)
    db.commit()

    await manager.broadcast(
        {
            "type": "DRIVER_LOCATION",
            "driver_id": driver_id,
            "name": driver.name,
            "lat": lat,
            "lon": lon,
            "status": driver.status,
        }
    )
    return {"status": "Success"}


def _to_e164(phone: str) -> str:
    digits = re.sub(r"\D", "", (phone or ""))
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if phone and phone.startswith("+"):
        return phone
    return f"+{digits}" if digits else phone


def format_pickup_time(date_value: str, time_value: str) -> str:
    try:
        parsed = datetime.fromisoformat(f"{date_value}T{time_value}")
        return f"{parsed.strftime('%a %b')} {parsed.day} at {parsed.strftime('%I:%M %p').lstrip('0')}"
    except ValueError:
        return f"{date_value} {time_value}"


def _maps_link(
    address: str, lat: Optional[str] = None, lon: Optional[str] = None
) -> str:
    if lat and lon:
        return f"https://maps.google.com/?q={lat},{lon}"
    return f"https://maps.google.com/?q={urllib.parse.quote(address)}"


LINQ_BASE_URL = os.getenv(
    "LINQ_API_BASE_URL", "https://api.linqapp.com/api/partner/v3"
)


def _linq_post(path: str, payload: dict) -> dict:
    """POST to the Linq Partner API (Blue v3). On error, surface Linq's response body."""
    resp = httpx.post(
        f"{LINQ_BASE_URL}{path}",
        json=payload,
        headers={
            "Authorization": f"Bearer {os.getenv('LINQ_API_TOKEN', '')}",
            "Content-Type": "application/json",
        },
        timeout=15.0,
    )
    if resp.status_code >= 400:
        body = resp.text[:600]
        print(f"[linq] {resp.status_code} POST {path} payload={json.dumps(payload)} -> {body}")
        raise RuntimeError(f"Linq {resp.status_code}: {body}")
    return resp.json()


def _linq_msg_id(data: dict) -> Optional[str]:
    # POST /chats           -> {"id": <chat_id>, "last_message": {"id": ...}}
    # POST /chats/{id}/messages -> {"chat_id": ..., "message": {"id": ...}}
    inner = data.get("message") or data.get("last_message") or {}
    return inner.get("id") or data.get("id")


def _send_sms(phone: str, body: str) -> Optional[str]:
    """Start (or reuse) a chat to a phone number and send a text via Linq.

    Returns the Linq message id, or None if Linq isn't configured.
    """
    if not os.getenv("LINQ_API_TOKEN") or not os.getenv("LINQ_FROM_NUMBER"):
        return None
    data = _linq_post(
        "/chats",
        {
            "from": os.getenv("LINQ_FROM_NUMBER"),
            "to": [_to_e164(phone)],
            "message": {"parts": [{"type": "text", "value": body}]},
        },
    )
    return _linq_msg_id(data)


def _linq_reply(chat_id: str, body: str) -> Optional[str]:
    """Reply within an existing Linq chat (used from the inbound webhook)."""
    if not os.getenv("LINQ_API_TOKEN") or not chat_id:
        return None
    data = _linq_post(
        f"/chats/{chat_id}/messages",
        {"message": {"parts": [{"type": "text", "value": body}]}},
    )
    return _linq_msg_id(data)


def _notify_customer(reservation: "ReservationModel", body: str) -> None:
    """Best-effort customer SMS. Never raises into the request/webhook flow."""
    if not getattr(reservation, "phone", None):
        return
    try:
        _send_sms(reservation.phone, body)
    except Exception as e:
        print(f"[linq] customer notify failed for trip #{reservation.id}: {e}")


def build_dispatch_alert(reservation: ReservationModel) -> str:
    pickup_time = format_pickup_time(reservation.date, reservation.time)
    base_url = os.getenv("APP_BASE_URL", "")
    details_line = f"\nDetails: {base_url}/driver" if base_url else ""
    return (
        f"NEW TRIP ASSIGNED\n"
        f"Date: {pickup_time}\n"
        f"Type: {reservation.trip_type}\n"
        f"From: {reservation.pickup}\n"
        f"To: {reservation.dropoff}\n"
        f"Vehicle: {reservation.vehicle}{details_line}\n"
        f"Reply YES to accept or NO to decline."
    )


def build_reminder_message(reservation: ReservationModel) -> str:
    maps_url = _maps_link(
        reservation.pickup, reservation.pickup_lat, reservation.pickup_lon
    )
    return (
        f"REMINDER: Your trip for {reservation.customer} starts in 60 mins.\n"
        f"Pickup: {reservation.pickup}\n"
        f"Map: {maps_url}"
    )


def build_passenger_info_message(reservation: ReservationModel) -> str:
    dest_url = _maps_link(
        reservation.dropoff, reservation.dropoff_lat, reservation.dropoff_lon
    )
    phone_line = f"\nPhone: {reservation.phone}" if reservation.phone else ""
    notes_line = f"\nNotes: {reservation.notes}" if reservation.notes else ""
    return (
        f"TRIP ACTIVE: {reservation.customer}{phone_line}{notes_line}\n"
        f"Track Route: {dest_url}"
    )


def build_customer_en_route_message(
    reservation: ReservationModel, driver: DriverModel
) -> str:
    driver_phone = f" ({driver.phone})" if driver.phone else ""
    return (
        f"Tsatsral Limo: Your driver {driver.name}{driver_phone} is on the way "
        f"to {reservation.pickup} for your trip to {reservation.dropoff}."
    )


def build_customer_arrived_message(
    reservation: ReservationModel, driver: DriverModel
) -> str:
    return (
        f"Tsatsral Limo: Your driver {driver.name} has arrived at "
        f"{reservation.pickup}. Please meet your driver when ready."
    )


def build_completion_message(reservation: ReservationModel) -> str:
    base_url = os.getenv("APP_BASE_URL", "")
    dashboard_line = f"\nView trip details: {base_url}/driver" if base_url else ""
    return (
        f"TRIP COMPLETE: Great job! Trip #{reservation.id} has been closed.\n"
        f"Thank you for driving with Tsatsral Limo.{dashboard_line}"
    )


def build_customer_booking_message(reservation: ReservationModel) -> str:
    pickup_time = format_pickup_time(reservation.date, reservation.time)
    return (
        f"Tsatsral Limo: Booking confirmed! {reservation.vehicle} on {pickup_time}.\n"
        f"From: {reservation.pickup}\n"
        f"To: {reservation.dropoff}\n"
        f"We'll text you when your driver is on the way."
    )


def build_customer_reminder_message(reservation: ReservationModel) -> str:
    pickup_time = format_pickup_time(reservation.date, reservation.time)
    return (
        f"Tsatsral Limo reminder: your trip is in about 1 hour ({pickup_time}).\n"
        f"Pickup: {reservation.pickup}"
    )


def build_customer_receipt_message(reservation: ReservationModel) -> str:
    amount = f"${_get_reservation_price(reservation):,.2f}"
    return (
        f"Tsatsral Limo: Payment received — thank you! "
        f"Receipt for trip #{reservation.id}: {amount}.\n"
        f"{reservation.pickup} → {reservation.dropoff}"
    )


async def _send_reminder_at_time(reservation_id: int, remind_at: datetime):
    delay = (remind_at - datetime.now()).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)
    db = SessionLocal()
    try:
        reservation = (
            db.query(ReservationModel)
            .filter(ReservationModel.id == reservation_id)
            .first()
        )
        if not reservation or reservation.status not in (
            "Assigned",
            "In Progress",
            "Arrived",
        ):
            return
        driver = (
            db.query(DriverModel)
            .filter(DriverModel.name == reservation.assigned_driver)
            .first()
        )
        if not driver:
            return
        _send_sms(driver.phone, build_reminder_message(reservation))
        _notify_customer(reservation, build_customer_reminder_message(reservation))
        add_db_event(
            db,
            reservation_id,
            "Reminder sent",
            "60-min pre-trip SMS sent to driver and customer.",
        )
        db.commit()
    finally:
        db.close()
    _scheduled_reminders.pop(reservation_id, None)


def _schedule_trip_reminder(reservation: ReservationModel):
    if reservation.id in _scheduled_reminders:
        return
    try:
        pickup_dt = datetime.fromisoformat(f"{reservation.date}T{reservation.time}")
        remind_at = pickup_dt - timedelta(hours=1)
        if remind_at <= datetime.now():
            return
        task = asyncio.create_task(_send_reminder_at_time(reservation.id, remind_at))
        _scheduled_reminders[reservation.id] = task
    except Exception:
        pass


def notify_driver_of_reservation(
    reservation: ReservationModel, driver: dict[str, str]
) -> str:
    if not os.getenv("LINQ_API_TOKEN") or not os.getenv("LINQ_FROM_NUMBER"):
        return "LINQ_NOT_CONFIGURED"
    data = _linq_post(
        "/chats",
        {
            "from": os.getenv("LINQ_FROM_NUMBER"),
            "to": [_to_e164(driver.get("phone", ""))],
            "message": {
                "parts": [
                    {"type": "text", "value": build_dispatch_alert(reservation)}
                ]
            },
        },
    )
    return _linq_msg_id(data) or ""
