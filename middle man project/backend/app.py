import asyncio
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
from twilio.request_validator import RequestValidator
from twilio.rest import Client

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
    UniqueConstraint,
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


class CompanyModel(Base):
    __tablename__ = "companies"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    slug = Column(String, unique=True, index=True)
    phone = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Per-tenant Twilio credentials (empty = fall back to env for default company)
    twilio_account_sid = Column(String, nullable=True)
    twilio_auth_token = Column(String, nullable=True)
    twilio_from_number = Column(String, nullable=True)

    # Per-tenant Stripe credentials
    stripe_secret_key = Column(String, nullable=True)
    stripe_publishable_key = Column(String, nullable=True)
    stripe_webhook_secret = Column(String, nullable=True)
    stripe_payment_link = Column(String, nullable=True)


class UserModel(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    name = Column(String)
    role = Column(String, default="operator")  # "admin" or "operator"
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)


class DriverModel(Base):
    __tablename__ = "drivers"
    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_drivers_company_name"),
    )
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=False)
    email = Column(String, nullable=True)
    phone = Column(String)
    vehicle = Column(String)
    status = Column(String, default="Online")  # Online, Offline, Busy
    lat = Column(String, nullable=True)
    lon = Column(String, nullable=True)
    location_token = Column(String, nullable=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)


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
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)

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


_ALLOWED_VEHICLES = {"Black car", "Executive SUV", "Sprinter van"}
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
    # Which company this public booking belongs to (resolved server-side)
    company_slug: Optional[str] = None
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
    """Connections are partitioned by company_id so a dispatch board only ever
    receives events for its own tenant."""

    def __init__(self):
        self.active_connections: dict[int, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, company_id: int):
        await websocket.accept()
        self.active_connections.setdefault(company_id, []).append(websocket)

    def disconnect(self, websocket: WebSocket, company_id: int):
        conns = self.active_connections.get(company_id, [])
        if websocket in conns:
            conns.remove(websocket)
        if not conns:
            self.active_connections.pop(company_id, None)

    async def broadcast(self, company_id: int, message: dict):
        for connection in list(self.active_connections.get(company_id, [])):
            try:
                await connection.send_json(message)
            except Exception:
                self.disconnect(connection, company_id)

    async def broadcast_all(self, message: dict):
        for cid in list(self.active_connections):
            await self.broadcast(cid, message)


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


# --- TENANT HELPERS ---
def _slugify(value: str) -> str:
    """Turn a company name into a URL-safe slug (lowercase, hyphenated)."""
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug or "company"


def _unique_slug(db: Session, base: str) -> str:
    """Return a slug derived from base that is not yet taken."""
    base = _slugify(base)
    candidate = base
    n = 2
    while db.query(CompanyModel).filter(CompanyModel.slug == candidate).first():
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def get_company_by_slug(db: Session, slug: str) -> Optional["CompanyModel"]:
    if not slug:
        return None
    return db.query(CompanyModel).filter(CompanyModel.slug == slug).first()


def require_company_by_slug(db: Session, slug: str) -> "CompanyModel":
    company = get_company_by_slug(db, slug)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


def _allow_env_credential_fallback(company: Optional["CompanyModel"]) -> bool:
    if company is None:
        return True
    default_slug = os.getenv("DEFAULT_COMPANY_SLUG", "tsatsral")
    return company.slug == default_slug


def _company_twilio(company: Optional["CompanyModel"]) -> tuple:
    """(account_sid, auth_token, from_number) preferring per-company creds,
    falling back to env only for the legacy/default company."""
    allow_env = _allow_env_credential_fallback(company)
    sid = (company.twilio_account_sid if company else None) or (
        os.getenv("TWILIO_ACCOUNT_SID") if allow_env else None
    )
    token = (company.twilio_auth_token if company else None) or (
        os.getenv("TWILIO_AUTH_TOKEN") if allow_env else None
    )
    from_number = (company.twilio_from_number if company else None) or (
        os.getenv("TWILIO_FROM_NUMBER") if allow_env else None
    )
    return sid, token, from_number


def _company_stripe(company: Optional["CompanyModel"]) -> dict:
    """Resolve Stripe config preferring per-company creds.

    Env fallback is restricted to the legacy/default company so new tenants
    cannot accidentally charge through the platform owner's Stripe account.
    """
    allow_env = _allow_env_credential_fallback(company)
    return {
        "secret_key": (company.stripe_secret_key if company else None)
        or (_STRIPE_SECRET_KEY if allow_env else ""),
        "publishable_key": (company.stripe_publishable_key if company else None)
        or (_STRIPE_PUBLISHABLE_KEY if allow_env else ""),
        "webhook_secret": (company.stripe_webhook_secret if company else None)
        or (_STRIPE_WEBHOOK_SECRET if allow_env else ""),
        "payment_link": (company.stripe_payment_link if company else None)
        or (_STRIPE_PAYMENT_LINK if allow_env else ""),
    }


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
            "connect-src 'self' https://router.project-osrm.org https://*.basemaps.cartocdn.com https://nominatim.openstreetmap.org; "
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


_VEHICLE_RATES = {
    "Black car": (3.50, 65),  # ($/mile, minimum $)
    "Executive SUV": (4.50, 88),
    "Sprinter van": (6.00, 150),
}


def _calc_total_cents(vehicle: str, distance_miles: float = 0, hour: int = 12) -> int:
    rate, minimum = _VEHICLE_RATES.get(vehicle, (4.50, 88))
    base = max(rate * distance_miles, float(minimum))
    # Time-of-day multiplier
    if hour >= 22 or hour < 5:  # 10 pm – 5 am
        base *= 1.25
    elif (6 <= hour < 9) or (16 <= hour < 19):  # rush hours
        base *= 1.15
    base = round(base)
    gratuity = round(base * 0.18)
    return (base + gratuity + 22) * 100


# --- INITIAL DATA ---
def init_drivers(db: Session, company_id: int):
    if db.query(DriverModel).filter(DriverModel.company_id == company_id).first():
        return

    drivers_to_add = [
        {"name": "Jack Dorj", "phone": "4156994052", "vehicle": "Executive SUV"},
        {"name": "Bataa", "phone": "4155550101", "vehicle": "Black car"},
        {"name": "Boldoo", "phone": "4155550102", "vehicle": "Sprinter van"},
        {"name": "Hanaa", "phone": "4155550103", "vehicle": "Executive SUV"},
        {"name": "Irmoon", "phone": "4155550104", "vehicle": "Black car"},
    ]

    for d_data in drivers_to_add:
        db.add(
            DriverModel(
                company_id=company_id,
                location_token=secrets.token_urlsafe(24),
                **d_data,
            )
        )

    db.commit()


# --- WEBSOCKETS ---
@app.websocket("/ws/dispatch")
async def websocket_endpoint(websocket: WebSocket):
    token = websocket.cookies.get("session_token")
    if not _session_valid(token):
        await websocket.close(code=4001)
        return
    sess = _active_sessions[token]
    company_id = sess.get("company_id")
    await manager.connect(websocket, company_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, company_id)


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
                # Archives span every tenant in one UPDATE; nudge all boards.
                await manager.broadcast_all(
                    {
                        "type": "UPDATE",
                        "message": "Resolved trips archived at midnight",
                    }
                )
        finally:
            db.close()


def seed_admin_user(db: Session, company_id: int):
    import sqlalchemy.exc

    admin_email = os.getenv("ADMIN_EMAIL", "admin@gobilimo.live")
    admin_password = os.getenv("ADMIN_PASSWORD", os.getenv("DISPATCH_PASSWORD", ""))
    if not admin_password:
        return
    hashed = _hash_password(admin_password)
    # Scope the admin lookup to the default company so a tenant admin elsewhere
    # cannot be mistaken for (and overwritten as) this company's admin.
    existing = (
        db.query(UserModel)
        .filter(UserModel.role == "admin", UserModel.company_id == company_id)
        .first()
    )
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
                    company_id=company_id,
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
        if "location_token" not in existing_cols:
            conn.execute(text("ALTER TABLE drivers ADD COLUMN location_token VARCHAR"))
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

        # --- Multi-tenant migration (order matters) ---
        # 1. Ensure a default company row exists, capture its id.
        default_slug = os.getenv("DEFAULT_COMPANY_SLUG", "tsatsral")
        default_name = os.getenv("DEFAULT_COMPANY_NAME", "Tsatsral Limo")
        row = conn.execute(
            text("SELECT id FROM companies WHERE slug = :slug"),
            {"slug": default_slug},
        ).fetchone()
        if row:
            default_company_id = row[0]
        else:
            conn.execute(
                text(
                    "INSERT INTO companies (name, slug, created_at) "
                    "VALUES (:n, :s, :t)"
                ),
                {"n": default_name, "s": default_slug, "t": datetime.utcnow()},
            )
            conn.commit()
            default_company_id = conn.execute(
                text("SELECT id FROM companies WHERE slug = :slug"),
                {"slug": default_slug},
            ).fetchone()[0]

        # 2. Add company_id to legacy tables (guarded, nullable via ALTER).
        for table in ("users", "drivers", "reservations"):
            cols = [
                r[1]
                for r in conn.execute(
                    text(f"PRAGMA table_info({table})")
                ).fetchall()
            ]
            if "company_id" not in cols:
                conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN company_id INTEGER")
                )
                conn.commit()

        # 3. Backfill existing rows to the default company.
        for table in ("users", "drivers", "reservations"):
            conn.execute(
                text(
                    f"UPDATE {table} SET company_id = :cid "
                    "WHERE company_id IS NULL"
                ),
                {"cid": default_company_id},
            )
        conn.commit()

        # 3b. Backfill per-driver public location tokens.
        token_rows = conn.execute(
            text("SELECT id FROM drivers WHERE location_token IS NULL OR location_token = ''")
        ).fetchall()
        for row in token_rows:
            conn.execute(
                text("UPDATE drivers SET location_token = :token WHERE id = :id"),
                {"token": secrets.token_urlsafe(24), "id": row[0]},
            )
        if token_rows:
            conn.commit()

        # 4. Swap the globally-unique driver-name index for a composite one.
        #    (Must run after backfill so company_id is populated.)
        idx_names = [
            r[1]
            for r in conn.execute(text("PRAGMA index_list(drivers)")).fetchall()
        ]
        if "ix_drivers_name" in idx_names:
            conn.execute(text("DROP INDEX ix_drivers_name"))
            conn.commit()
        if "uq_drivers_company_name" not in idx_names:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_drivers_company_name "
                    "ON drivers (company_id, name)"
                )
            )
            conn.commit()

    db = SessionLocal()
    try:
        company = (
            db.query(CompanyModel)
            .filter(CompanyModel.slug == default_slug)
            .first()
        )
        if company:
            init_drivers(db, company.id)
            seed_admin_user(db, company.id)
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
        "company_id": user.company_id,
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
def get_me(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    company = (
        db.query(CompanyModel)
        .filter(CompanyModel.id == current_user["company_id"])
        .first()
    )
    return {
        "user_id": current_user["user_id"],
        "name": current_user["name"],
        "email": current_user["email"],
        "role": current_user["role"],
        "company_id": current_user["company_id"],
        "company_name": company.name if company else None,
        "company_slug": company.slug if company else None,
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
    user = UserModel(
        email=email,
        hashed_password=hashed,
        name=name,
        role=role,
        company_id=current_user["company_id"],
    )
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


@app.post("/api/signup")
async def signup(
    request: Request,
    payload: dict,
    _: None = Depends(check_login_rate),
    db: Session = Depends(get_db),
):
    company_name = (payload.get("company_name") or "").strip()
    name = (payload.get("name") or "").strip()
    email = (payload.get("email") or "").lower().strip()
    password = payload.get("password") or ""
    if not company_name or not name or not email or not password:
        raise HTTPException(
            status_code=422,
            detail="company_name, name, email, and password are required",
        )
    if len(password) < 8:
        raise HTTPException(
            status_code=422, detail="Password must be at least 8 characters"
        )
    if db.query(UserModel).filter(UserModel.email == email).first():
        raise HTTPException(status_code=409, detail="Email already registered")

    company = CompanyModel(
        name=company_name,
        slug=_unique_slug(db, company_name),
        phone=payload.get("phone") or None,
    )
    db.add(company)
    db.commit()
    db.refresh(company)

    user = UserModel(
        email=email,
        hashed_password=_hash_password(password),
        name=name,
        role="admin",
        company_id=company.id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # Log the new admin straight in (mirror the /login session/cookie logic).
    token = secrets.token_urlsafe(32)
    _active_sessions[token] = {
        "user_id": user.id,
        "role": user.role,
        "name": user.name,
        "email": user.email,
        "company_id": company.id,
        "expires": datetime.utcnow() + _SESSION_DURATION,
    }
    response = JSONResponse(
        {
            "status": "Success",
            "company": {"name": company.name, "slug": company.slug},
            "redirect": "/dispatch",
        }
    )
    response.set_cookie(
        "session_token",
        token,
        httponly=True,
        samesite="strict",
        max_age=int(_SESSION_DURATION.total_seconds()),
        secure=_IS_PRODUCTION,
    )
    return response


@app.get("/api/company/settings")
def get_company_settings(
    current_user: dict = Depends(require_admin), db: Session = Depends(get_db)
):
    company = (
        db.query(CompanyModel)
        .filter(CompanyModel.id == current_user["company_id"])
        .first()
    )
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    base = os.getenv("APP_BASE_URL", "").rstrip("/")
    return {
        "name": company.name,
        "slug": company.slug,
        "phone": company.phone,
        "twilio_account_sid": company.twilio_account_sid,
        "twilio_auth_token": company.twilio_auth_token,
        "twilio_from_number": company.twilio_from_number,
        "stripe_secret_key": company.stripe_secret_key,
        "stripe_publishable_key": company.stripe_publishable_key,
        "stripe_webhook_secret": company.stripe_webhook_secret,
        "stripe_payment_link": company.stripe_payment_link,
        "booking_url": f"{base}/book/{company.slug}" if base else f"/book/{company.slug}",
        "twilio_webhook_url": (
            f"{base}/twilio/reply/{company.slug}"
            if base
            else f"/twilio/reply/{company.slug}"
        ),
        "stripe_webhook_url": (
            f"{base}/stripe/webhook/{company.slug}"
            if base
            else f"/stripe/webhook/{company.slug}"
        ),
    }


@app.put("/api/company/settings")
async def update_company_settings(
    payload: dict,
    current_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    company = (
        db.query(CompanyModel)
        .filter(CompanyModel.id == current_user["company_id"])
        .first()
    )
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    if "name" in payload and (payload.get("name") or "").strip():
        company.name = payload["name"].strip()
    if "phone" in payload:
        company.phone = (payload.get("phone") or "").strip() or None
    for field in (
        "twilio_account_sid",
        "twilio_auth_token",
        "twilio_from_number",
        "stripe_secret_key",
        "stripe_publishable_key",
        "stripe_webhook_secret",
        "stripe_payment_link",
    ):
        if field in payload:
            value = payload.get(field)
            setattr(company, field, (value or "").strip() or None)
    db.commit()
    return {"status": "Success"}


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


@app.get("/book/{slug}")
def company_booking_page(slug: str, db: Session = Depends(get_db)):
    require_company_by_slug(db, slug)
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/signup")
def signup_page():
    return FileResponse(FRONTEND_DIR / "signup.html")


@app.get("/driver")
def driver_location_page():
    return FileResponse(FRONTEND_DIR / "driver.html")


@app.get("/driver/{slug}")
def company_driver_page(slug: str, db: Session = Depends(get_db)):
    require_company_by_slug(db, slug)
    return FileResponse(FRONTEND_DIR / "driver.html")


@app.get("/driver/{slug}/{token}")
def company_driver_token_page(
    slug: str, token: str, db: Session = Depends(get_db)
):
    company = require_company_by_slug(db, slug)
    driver = (
        db.query(DriverModel)
        .filter(
            DriverModel.company_id == company.id,
            DriverModel.location_token == token,
        )
        .first()
    )
    if not driver:
        raise HTTPException(status_code=404, detail="Driver link not found")
    return FileResponse(FRONTEND_DIR / "driver.html")


@app.get("/api/company/{slug}")
def get_company_public(slug: str, db: Session = Depends(get_db)):
    company = require_company_by_slug(db, slug)
    return {"name": company.name, "phone": company.phone, "slug": company.slug}


@app.get("/api/drivers/public")
def list_drivers_public(
    company: str = "", token: str = "", db: Session = Depends(get_db)
):
    # Driver GPS links are public, so the company slug is not enough authority.
    # Unknown/missing token → empty roster.
    comp = get_company_by_slug(db, company)
    if not comp or not token:
        return {"status": "Success", "drivers": []}
    drivers = (
        db.query(DriverModel)
        .filter(
            DriverModel.status != "Offline",
            DriverModel.company_id == comp.id,
            DriverModel.location_token == token,
        )
        .all()
    )
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


@app.get("/payment/{slug}/{res_id}")
def company_payment_page(slug: str, res_id: int, db: Session = Depends(get_db)):
    company = require_company_by_slug(db, slug)
    reservation = (
        db.query(ReservationModel)
        .filter(
            ReservationModel.id == res_id,
            ReservationModel.company_id == company.id,
        )
        .first()
    )
    if not reservation:
        raise HTTPException(status_code=404, detail="Reservation not found")
    return FileResponse(FRONTEND_DIR / "payment.html")


@app.get("/invoice/{res_id}")
def invoice_page(
    res_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    from fastapi.responses import HTMLResponse

    r = (
        db.query(ReservationModel)
        .filter(
            ReservationModel.id == res_id,
            ReservationModel.company_id == current_user["company_id"],
        )
        .first()
    )
    if not r:
        raise HTTPException(status_code=404, detail="Trip not found")
    company = (
        db.query(CompanyModel)
        .filter(CompanyModel.id == current_user["company_id"])
        .first()
    )
    company_name = company.name if company else "Limo"
    company_phone = (company.phone if company and company.phone else "") or ""

    try:
        hour = int((r.time or "12:00").split(":")[0])
        if r.custom_price is not None:
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
            total_cents = _calc_total_cents(r.vehicle, r.distance_miles or 0, hour)
            total = total_cents / 100
            rate, minimum = _VEHICLE_RATES.get(r.vehicle, (4.50, 88))
            base = round(max(rate * (r.distance_miles or 0), float(minimum)), 2)
            if hour >= 22 or hour < 5:
                base = round(base * 1.25, 2)
            elif (6 <= hour < 9) or (16 <= hour < 19):
                base = round(base * 1.15, 2)
            gratuity = round(base * 0.18, 2)
            service_fee = 22.00
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
<title>Invoice {order_no} — {html.escape(company_name)}</title>
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
      <div class="logo-name">{html.escape(company_name)}</div>
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
      <div class="cname">{html.escape(company_name)}</div>
      <div class="detail">{html.escape(company_phone)}</div>
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
      <tr><td>Gratuity (18%)</td><td>${gratuity:.2f}</td></tr>
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

  <div class="footer">{html.escape(company_name)} &nbsp;·&nbsp; {html.escape(r.date)} &nbsp;·&nbsp; {html.escape(company_phone)}</div>
</div>
</body>
</html>"""
    return HTMLResponse(content=html_content)


@app.post("/stripe/webhook")
async def stripe_webhook_default(request: Request, db: Session = Depends(get_db)):
    # Back-compat: env-credentialed webhook maps to the default company.
    default_slug = os.getenv("DEFAULT_COMPANY_SLUG", "tsatsral")
    company = get_company_by_slug(db, default_slug)
    return await _handle_stripe_webhook(request, db, company)


@app.post("/stripe/webhook/{slug}")
async def stripe_webhook_tenant(
    slug: str, request: Request, db: Session = Depends(get_db)
):
    company = require_company_by_slug(db, slug)
    return await _handle_stripe_webhook(request, db, company)


async def _handle_stripe_webhook(
    request: Request, db: Session, company: Optional["CompanyModel"]
):
    stripe_cfg = _company_stripe(company)
    if not _stripe or not stripe_cfg["secret_key"]:
        raise HTTPException(status_code=404)
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        if not stripe_cfg["webhook_secret"]:
            raise HTTPException(status_code=400, detail="Webhook secret not configured")
        event = _stripe.Webhook.construct_event(
            payload, sig, stripe_cfg["webhook_secret"]
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    if event["type"] == "checkout.session.completed":
        sess_obj = event["data"]["object"]
        res_id_str = (sess_obj.get("metadata") or {}).get(
            "reservation_id"
        ) or sess_obj.get("client_reference_id")
        if res_id_str:
            try:
                res_id = int(res_id_str)
                reservation = (
                    db.query(ReservationModel)
                    .filter(ReservationModel.id == res_id)
                    .first()
                )
                # Make sure the trip actually belongs to the webhook's company.
                if reservation and company and reservation.company_id != company.id:
                    reservation = None
                if reservation:
                    reservation.payment = "Paid"
                    db.commit()
                    add_db_event(
                        db,
                        reservation.id,
                        "Payment received",
                        "Stripe checkout completed.",
                    )
                    await manager.broadcast(
                        reservation.company_id,
                        {
                            "type": "UPDATE",
                            "message": f"Payment confirmed for trip #{res_id_str}",
                        },
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


@app.get("/api/reservations")
def list_reservations(
    db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)
):
    query = db.query(ReservationModel).filter(
        ReservationModel.archived == 0,
        ReservationModel.company_id == current_user["company_id"],
    )
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
        total_cents = _calc_total_cents(r.vehicle, r.distance_miles or 0, hour)
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
    query = db.query(ReservationModel).filter(
        ReservationModel.date == target_day,
        ReservationModel.company_id == current_user["company_id"],
    )
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
    query = db.query(ReservationModel).filter(
        ReservationModel.date == target_day,
        ReservationModel.company_id == current_user["company_id"],
    )
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
    company_slug = res_data.pop("company_slug", None)
    # Resolve the owning company: authenticated operators use their own session
    # company; public bookings must carry a valid company_slug.
    session_token = request.cookies.get("session_token")
    sess = _get_session(session_token)
    if sess:
        res_data["created_by"] = sess["user_id"]
        company = (
            db.query(CompanyModel)
            .filter(CompanyModel.id == sess["company_id"])
            .first()
        )
    else:
        res_data["created_by"] = None
        company = get_company_by_slug(db, company_slug)
    if not company:
        raise HTTPException(status_code=404, detail="Unknown company")
    res_data["company_id"] = company.id

    db_res = ReservationModel(**res_data)
    db.add(db_res)
    db.commit()
    db.refresh(db_res)

    stripe_cfg = _company_stripe(company)
    local_payment_url = f"/payment/{company.slug}/{db_res.id}"
    # Prefer static payment link (supports client_reference_id for tracking)
    if stripe_cfg["payment_link"]:
        sep = "&" if "?" in stripe_cfg["payment_link"] else "?"
        payment_url = f"{stripe_cfg['payment_link']}{sep}client_reference_id={db_res.id}"
    elif _stripe and stripe_cfg["secret_key"]:
        try:
            origin = os.getenv("APP_BASE_URL", "").rstrip("/") or str(
                request.base_url
            ).rstrip("/")
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
                            ),
                            "product_data": {
                                "name": f"{company.name} — {db_res.vehicle}",
                                "description": f"{db_res.pickup} → {db_res.dropoff} on {db_res.date}",
                            },
                        },
                        "quantity": 1,
                    }
                ],
                metadata={"reservation_id": str(db_res.id)},
                customer_email=db_res.email or None,
                success_url=f"{origin}/book/{company.slug}?paid=1&res={db_res.id}",
                cancel_url=f"{origin}/book/{company.slug}",
                api_key=stripe_cfg["secret_key"],
            )
            payment_url = stripe_sess.url
            db_res.stripe_session_id = stripe_sess.id
        except Exception as stripe_err:
            print(f"Stripe session error: {stripe_err}")
            payment_url = local_payment_url
    else:
        payment_url = local_payment_url
    db_res.payment_url = payment_url
    db.commit()

    add_db_event(
        db, db_res.id, "Reservation created", f"{db_res.vehicle} booked via dashboard."
    )

    await manager.broadcast(
        company.id,
        {"type": "UPDATE", "message": f"New reservation from {db_res.customer}"},
    )
    return {
        "status": "Success",
        "reservation": {"id": db_res.id, "payment_url": payment_url},
    }


@app.get("/api/reservations/public/{res_id}")
def get_public_reservation(
    res_id: int, company: str = "", db: Session = Depends(get_db)
):
    comp = get_company_by_slug(db, company)
    if not comp:
        raise HTTPException(status_code=404, detail="Reservation not found")
    query = db.query(ReservationModel).filter(
        ReservationModel.id == res_id,
        ReservationModel.company_id == comp.id,
    )
    r = query.first()
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
        db.query(ReservationModel)
        .filter(
            ReservationModel.id == reservation_id,
            ReservationModel.company_id == current_user["company_id"],
        )
        .first()
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
            current_user["company_id"],
            {"type": "UPDATE", "message": f"Trip #{reservation_id} updated"},
        )

    return {"status": "Success", "changed": changed}


@app.post("/api/reservations/{reservation_id}/archive")
async def archive_reservation(
    reservation_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    reservation = (
        db.query(ReservationModel)
        .filter(
            ReservationModel.id == reservation_id,
            ReservationModel.company_id == current_user["company_id"],
        )
        .first()
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
        current_user["company_id"],
        {"type": "UPDATE", "message": f"Trip #{reservation_id} closed"},
    )
    return {"status": "Success"}


@app.post("/api/reservations/archive-all")
async def archive_all_resolved(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    status = payload.get("status")
    if status not in ("Completed", "Cancelled"):
        raise HTTPException(
            status_code=422, detail="Can only archive Completed or Cancelled trips"
        )
    db.query(ReservationModel).filter(
        ReservationModel.status == status,
        ReservationModel.archived == 0,
        ReservationModel.company_id == current_user["company_id"],
    ).update({"archived": 1})
    db.commit()
    await manager.broadcast(
        current_user["company_id"],
        {"type": "UPDATE", "message": f"All {status} trips cleared"},
    )
    return {"status": "Success"}


@app.post("/api/reservations/{reservation_id}/notes")
async def add_note(
    reservation_id: int,
    note: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    reservation = (
        db.query(ReservationModel)
        .filter(
            ReservationModel.id == reservation_id,
            ReservationModel.company_id == current_user["company_id"],
        )
        .first()
    )
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")

    add_db_event(db, reservation_id, "Internal Note", note.get("text", ""))
    await manager.broadcast(
        current_user["company_id"],
        {"type": "UPDATE", "message": f"Note added to trip #{reservation_id}"},
    )
    return {"status": "Success"}


@app.post("/api/reservations/{reservation_id}/complete")
async def complete_reservation(
    reservation_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    company_id = current_user["company_id"]
    reservation = (
        db.query(ReservationModel)
        .filter(
            ReservationModel.id == reservation_id,
            ReservationModel.company_id == company_id,
        )
        .first()
    )
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")

    reservation.status = "Completed"

    driver = None
    if reservation.assigned_driver:
        driver = (
            db.query(DriverModel)
            .filter(
                DriverModel.name == reservation.assigned_driver,
                DriverModel.company_id == company_id,
            )
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
        company = (
            db.query(CompanyModel).filter(CompanyModel.id == company_id).first()
        )
        company_name = company.name if company else "Limo"
        try:
            _send_sms(
                company,
                driver.phone,
                build_completion_message(
                    reservation,
                    company_name,
                    company.slug if company else "",
                ),
            )
        except Exception:
            pass

    await manager.broadcast(
        company_id,
        {"type": "UPDATE", "message": f"Trip #{reservation_id} completed"},
    )
    return {"status": "Success"}


@app.post("/api/reservations/{reservation_id}/start")
async def start_trip(
    reservation_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    company_id = current_user["company_id"]
    reservation = (
        db.query(ReservationModel)
        .filter(
            ReservationModel.id == reservation_id,
            ReservationModel.company_id == company_id,
        )
        .first()
    )
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    if reservation.status != "Assigned":
        raise HTTPException(
            status_code=409, detail="Trip must be in Assigned state to start"
        )
    if not reservation.assigned_driver:
        raise HTTPException(status_code=409, detail="No driver assigned")

    company = db.query(CompanyModel).filter(CompanyModel.id == company_id).first()
    company_name = company.name if company else "Limo"
    driver = (
        db.query(DriverModel)
        .filter(
            DriverModel.name == reservation.assigned_driver,
            DriverModel.company_id == company_id,
        )
        .first()
    )
    reservation.status = "In Progress"
    add_db_event(
        db, reservation_id, "Trip started", "Driver dispatched to pickup location."
    )
    db.commit()

    if driver:
        try:
            _send_sms(company, driver.phone, build_passenger_info_message(reservation))
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
                company,
                reservation.phone,
                build_customer_en_route_message(reservation, driver, company_name),
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
        company_id,
        {"type": "UPDATE", "message": f"Trip #{reservation_id} is now In Progress"},
    )
    return {"status": "Success"}


@app.post("/api/reservations/{reservation_id}/arrive")
async def mark_driver_arrived(
    reservation_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    company_id = current_user["company_id"]
    reservation = (
        db.query(ReservationModel)
        .filter(
            ReservationModel.id == reservation_id,
            ReservationModel.company_id == company_id,
        )
        .first()
    )
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    if reservation.status not in ("In Progress", "Arrived"):
        raise HTTPException(
            status_code=409, detail="Trip must be in progress before arrival"
        )
    if not reservation.assigned_driver:
        raise HTTPException(status_code=409, detail="No driver assigned")

    company = db.query(CompanyModel).filter(CompanyModel.id == company_id).first()
    company_name = company.name if company else "Limo"
    driver = (
        db.query(DriverModel)
        .filter(
            DriverModel.name == reservation.assigned_driver,
            DriverModel.company_id == company_id,
        )
        .first()
    )
    reservation.status = "Arrived"
    add_db_event(db, reservation_id, "Driver arrived", "Driver reached pickup.")
    db.commit()

    if reservation.phone and driver:
        try:
            _send_sms(
                company,
                reservation.phone,
                build_customer_arrived_message(reservation, driver, company_name),
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
        company_id,
        {"type": "UPDATE", "message": f"Driver arrived for trip #{reservation_id}"},
    )
    return {"status": "Success"}


@app.post("/api/reservations/{reservation_id}/cancel")
async def cancel_reservation(
    reservation_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    company_id = current_user["company_id"]
    reservation = (
        db.query(ReservationModel)
        .filter(
            ReservationModel.id == reservation_id,
            ReservationModel.company_id == company_id,
        )
        .first()
    )
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")

    reservation.status = "Cancelled"

    if reservation.assigned_driver:
        driver = (
            db.query(DriverModel)
            .filter(
                DriverModel.name == reservation.assigned_driver,
                DriverModel.company_id == company_id,
            )
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
        company_id,
        {"type": "UPDATE", "message": f"Trip #{reservation_id} cancelled"},
    )
    return {"status": "Success"}


@app.post("/api/reservations/{reservation_id}/assign")
async def assign_driver(
    reservation_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    company_id = current_user["company_id"]
    reservation = (
        db.query(ReservationModel)
        .filter(
            ReservationModel.id == reservation_id,
            ReservationModel.company_id == company_id,
        )
        .first()
    )
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")

    driver_id = payload.get("driver_id")
    if not driver_id:
        # Fallback to first online driver in this company if ID not provided
        driver = (
            db.query(DriverModel)
            .filter(
                DriverModel.status == "Online",
                DriverModel.company_id == company_id,
            )
            .first()
        )
    else:
        driver = (
            db.query(DriverModel)
            .filter(
                DriverModel.id == driver_id,
                DriverModel.company_id == company_id,
            )
            .first()
        )

    if not driver:
        raise HTTPException(status_code=503, detail="Requested driver not available")

    company = db.query(CompanyModel).filter(CompanyModel.id == company_id).first()

    # Twilio logic
    try:
        sid = notify_driver_of_reservation(
            company,
            reservation,
            {
                "name": driver.name,
                "phone": driver.phone,
                "location_token": driver.location_token,
            },
        )
        if sid == "TWILIO_NOT_CONFIGURED":
            raise HTTPException(status_code=503, detail="Twilio not configured")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Twilio error: {e}")

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
        company_id,
        {
            "type": "UPDATE",
            "message": f"{driver.name} assigned to trip #{reservation_id}",
        },
    )

    return {
        "status": "Success",
        "reservation_id": reservation_id,
        "driver": {"name": driver.name, "vehicle": driver.vehicle},
        "message_sid": sid,
    }


@app.post("/twilio/reply")
async def twilio_reply_default(
    request: Request,
    From: str = Form(...),
    Body: str = Form(...),
    db: Session = Depends(get_db),
):
    # Back-compat: the original env-credentialed webhook maps to the default company.
    default_slug = os.getenv("DEFAULT_COMPANY_SLUG", "tsatsral")
    company = get_company_by_slug(db, default_slug)
    return await _handle_twilio_reply(request, From, Body, db, company)


@app.post("/twilio/reply/{slug}")
async def twilio_reply_tenant(
    slug: str,
    request: Request,
    From: str = Form(...),
    Body: str = Form(...),
    db: Session = Depends(get_db),
):
    company = require_company_by_slug(db, slug)
    return await _handle_twilio_reply(request, From, Body, db, company)


async def _handle_twilio_reply(
    request: Request,
    From: str,
    Body: str,
    db: Session,
    company: Optional["CompanyModel"],
):
    _, auth_token, _ = _company_twilio(company)
    if not auth_token:
        raise HTTPException(status_code=503, detail="Twilio not configured")
    validator = RequestValidator(auth_token)
    form_data = dict(await request.form())
    signature = request.headers.get("X-Twilio-Signature", "")

    # Behind Render's proxy, request.url often shows the internal http://host:PORT
    # URL, but Twilio signs the public https URL. Rebuild the URL Twilio actually
    # signed using X-Forwarded-Proto / X-Forwarded-Host (or APP_BASE_URL override).
    fwd_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    fwd_host = request.headers.get(
        "x-forwarded-host", request.headers.get("host", request.url.netloc)
    )
    path = request.url.path
    query = f"?{request.url.query}" if request.url.query else ""
    candidate_urls = [
        f"{fwd_proto}://{fwd_host}{path}{query}",
        str(request.url),
    ]
    app_base = os.getenv("APP_BASE_URL", "").rstrip("/")
    if app_base:
        candidate_urls.insert(0, f"{app_base}{path}{query}")

    if not any(validator.validate(u, form_data, signature) for u in candidate_urls):
        print(
            f"[twilio] signature mismatch. tried={candidate_urls} "
            f"sig={signature!r} headers={dict(request.headers)}"
        )
        raise HTTPException(status_code=403, detail="Invalid request signature")
    reply = Body.strip().upper()
    # Normalize to 10-digit and also try E.164 to match however the number is stored in DB
    digits = re.sub(r"\D", "", From)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    e164 = f"+1{digits}" if len(digits) == 10 else From

    driver_q = db.query(DriverModel).filter(
        (DriverModel.phone == digits) | (DriverModel.phone == e164)
    )
    if company:
        driver_q = driver_q.filter(DriverModel.company_id == company.id)
    driver = driver_q.first()
    if not driver:
        return PlainTextResponse(
            "<?xml version='1.0'?><Response></Response>", media_type="application/xml"
        )

    # Find the most recent trip pending this driver's confirmation (same company)
    reservation = (
        db.query(ReservationModel)
        .filter(
            ReservationModel.assigned_driver == driver.name,
            ReservationModel.status == "Pending confirmation",
            ReservationModel.company_id == driver.company_id,
        )
        .order_by(ReservationModel.id.desc())
        .first()
    )

    if not reservation:
        return PlainTextResponse(
            "<?xml version='1.0'?><Response></Response>", media_type="application/xml"
        )

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
    else:
        return PlainTextResponse(
            "<?xml version='1.0'?><Response><Message>Reply YES to accept or NO to decline.</Message></Response>",
            media_type="application/xml",
        )

    db.commit()
    await manager.broadcast(
        driver.company_id,
        {
            "type": "UPDATE",
            "message": f"{driver.name} {'accepted' if reply.startswith('YES') else 'declined'} trip #{reservation.id}",
        },
    )

    return PlainTextResponse(
        f"<?xml version='1.0'?><Response><Message>{sms_back}</Message></Response>",
        media_type="application/xml",
    )


@app.get("/api/drivers")
def list_drivers(
    db: Session = Depends(get_db), current_user: dict = Depends(require_admin)
):
    company = (
        db.query(CompanyModel)
        .filter(CompanyModel.id == current_user["company_id"])
        .first()
    )
    company_slug = company.slug if company else ""
    drivers = (
        db.query(DriverModel)
        .filter(DriverModel.company_id == current_user["company_id"])
        .all()
    )
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
                "location_token": d.location_token,
                "location_url": _driver_page_url(company_slug, d.location_token or ""),
            }
            for d in drivers
        ],
    }


@app.post("/api/drivers")
async def create_driver(
    data: DriverCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    company_id = current_user["company_id"]
    existing = (
        db.query(DriverModel)
        .filter(
            DriverModel.name == data.name,
            DriverModel.company_id == company_id,
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409, detail="A driver with that name already exists"
        )
    driver = DriverModel(
        company_id=company_id,
        location_token=secrets.token_urlsafe(24),
        **data.model_dump(),
    )
    db.add(driver)
    db.commit()
    db.refresh(driver)
    await manager.broadcast(
        company_id,
        {"type": "UPDATE", "message": f"{driver.name} added to the roster"},
    )
    return {"status": "Success", "driver": {"id": driver.id, "name": driver.name}}


@app.put("/api/drivers/{driver_id}")
async def update_driver(
    driver_id: int,
    data: DriverUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    company_id = current_user["company_id"]
    driver = (
        db.query(DriverModel)
        .filter(
            DriverModel.id == driver_id,
            DriverModel.company_id == company_id,
        )
        .first()
    )
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")
    if data.name is not None:
        conflict = (
            db.query(DriverModel)
            .filter(
                DriverModel.name == data.name,
                DriverModel.id != driver_id,
                DriverModel.company_id == company_id,
            )
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
        company_id,
        {"type": "UPDATE", "message": f"{driver.name} profile updated"},
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
    current_user: dict = Depends(require_admin),
):
    company_id = current_user["company_id"]
    driver = (
        db.query(DriverModel)
        .filter(
            DriverModel.id == driver_id,
            DriverModel.company_id == company_id,
        )
        .first()
    )
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
    await manager.broadcast(company_id, loc_event)
    await manager.broadcast(
        company_id,
        {"type": "UPDATE", "message": f"{driver.name} is now {new_status}"},
    )
    return {"status": "Success", "driver_status": new_status}


@app.delete("/api/drivers/{driver_id}")
async def delete_driver(
    driver_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    company_id = current_user["company_id"]
    driver = (
        db.query(DriverModel)
        .filter(
            DriverModel.id == driver_id,
            DriverModel.company_id == company_id,
        )
        .first()
    )
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")
    active_trips = (
        db.query(ReservationModel)
        .filter(
            ReservationModel.assigned_driver == driver.name,
            ReservationModel.status.in_(["Assigned", "Pending confirmation"]),
            ReservationModel.company_id == company_id,
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
        company_id,
        {"type": "UPDATE", "message": f"{name} removed from roster"},
    )
    return {"status": "Success"}


@app.put("/api/drivers/{driver_id}/location")
async def update_driver_location(
    driver_id: int,
    payload: dict,
    company: str = "",
    token: str = "",
    db: Session = Depends(get_db),
):
    comp = require_company_by_slug(db, company)
    driver = (
        db.query(DriverModel)
        .filter(
            DriverModel.id == driver_id,
            DriverModel.company_id == comp.id,
            DriverModel.location_token == token,
        )
        .first()
    )
    if not driver:
        raise HTTPException(status_code=403, detail="Invalid driver location link")

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
        driver.company_id,
        {
            "type": "DRIVER_LOCATION",
            "driver_id": driver_id,
            "name": driver.name,
            "lat": lat,
            "lon": lon,
            "status": driver.status,
        },
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


def _send_sms(
    company: Optional["CompanyModel"], phone: str, body: str
) -> Optional[str]:
    account_sid, auth_token, from_number = _company_twilio(company)
    if not account_sid or not auth_token or not from_number:
        return None
    client = Client(account_sid, auth_token)
    msg = client.messages.create(body=body, from_=from_number, to=_to_e164(phone))
    return msg.sid


def _driver_page_url(company_slug: str = "", token: str = "") -> str:
    if company_slug and token:
        path = f"/driver/{company_slug}/{token}"
    elif company_slug:
        path = f"/driver/{company_slug}"
    else:
        path = "/driver"
    base_url = os.getenv("APP_BASE_URL", "").rstrip("/")
    return f"{base_url}{path}" if base_url else path


def build_dispatch_alert(
    reservation: ReservationModel, company_slug: str = "", token: str = ""
) -> str:
    pickup_time = format_pickup_time(reservation.date, reservation.time)
    details_line = f"\nDetails: {_driver_page_url(company_slug, token)}"
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
    reservation: ReservationModel, driver: DriverModel, company_name: str = "Limo"
) -> str:
    driver_phone = f" ({driver.phone})" if driver.phone else ""
    return (
        f"{company_name}: Your driver {driver.name}{driver_phone} is on the way "
        f"to {reservation.pickup} for your trip to {reservation.dropoff}."
    )


def build_customer_arrived_message(
    reservation: ReservationModel, driver: DriverModel, company_name: str = "Limo"
) -> str:
    return (
        f"{company_name}: Your driver {driver.name} has arrived at "
        f"{reservation.pickup}. Please meet your driver when ready."
    )


def build_completion_message(
    reservation: ReservationModel,
    company_name: str = "Limo",
    company_slug: str = "",
) -> str:
    dashboard_line = f"\nView trip details: {_driver_page_url(company_slug)}"
    return (
        f"TRIP COMPLETE: Great job! Trip #{reservation.id} has been closed.\n"
        f"Thank you for driving with {company_name}.{dashboard_line}"
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
            .filter(
                DriverModel.name == reservation.assigned_driver,
                DriverModel.company_id == reservation.company_id,
            )
            .first()
        )
        if not driver:
            return
        company = (
            db.query(CompanyModel)
            .filter(CompanyModel.id == reservation.company_id)
            .first()
        )
        _send_sms(company, driver.phone, build_reminder_message(reservation))
        add_db_event(
            db, reservation_id, "Reminder sent", "60-min pre-trip SMS sent to driver."
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
    company: Optional["CompanyModel"],
    reservation: ReservationModel,
    driver: dict[str, str],
) -> str:
    account_sid, auth_token, from_number = _company_twilio(company)
    if not account_sid or not auth_token or not from_number:
        return "TWILIO_NOT_CONFIGURED"
    client = Client(account_sid, auth_token)
    to_number = _to_e164(driver.get("phone", ""))
    message = client.messages.create(
        body=build_dispatch_alert(
            reservation,
            company.slug if company else "",
            driver.get("location_token", ""),
        ),
        from_=from_number,
        to=to_number,
    )
    return message.sid
