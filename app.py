import os
import json
import asyncio
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List

import httpx
from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect, Request, Form, Cookie
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from twilio.request_validator import RequestValidator
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator
from twilio.rest import Client
import hashlib
import hmac

try:
    import stripe as _stripe
except ImportError:
    _stripe = None

from sqlalchemy import create_engine, Column, Integer, String, Text, Float, ForeignKey, DateTime, or_
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship


BASE_DIR = Path(__file__).resolve().parent
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
_SESSION_DURATION = timedelta(hours=8)
_REMEMBER_ME_DURATION = timedelta(days=30)
_booking_rate: dict[str, list[datetime]] = {}
_BOOKING_RATE_LIMIT = 5
_BOOKING_RATE_WINDOW = timedelta(hours=1)
_login_rate: dict[str, list[datetime]] = {}
_LOGIN_RATE_LIMIT = 15
_LOGIN_RATE_WINDOW = timedelta(minutes=15)
_IS_PRODUCTION = os.getenv("PRODUCTION", "").lower() in ("1", "true", "yes")
_STRIPE_SECRET_KEY      = os.getenv("STRIPE_SECRET_KEY", "")
_STRIPE_WEBHOOK_SECRET  = os.getenv("STRIPE_WEBHOOK_SECRET", "")
_STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
_STRIPE_PAYMENT_LINK    = os.getenv("STRIPE_PAYMENT_LINK", "")
if _stripe and _STRIPE_SECRET_KEY:
    _stripe.api_key = _STRIPE_SECRET_KEY

# --- DATABASE SETUP ---
DATABASE_URL = "sqlite:///./dispatch.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
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

    events = relationship("EventModel", back_populates="reservation", cascade="all, delete-orphan")


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
_ALLOWED_TRIP_TYPES = {"Airport Arrival", "Airport Departure", "Point-to-Point", "Hourly", "One Way"}
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
            raise ValueError(f"Invalid vehicle. Allowed: {', '.join(_ALLOWED_VEHICLES)}")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in _ALLOWED_STATUSES:
            raise ValueError(f"Invalid status. Allowed: {', '.join(_ALLOWED_STATUSES)}")
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

    @field_validator("vehicle")
    @classmethod
    def validate_vehicle(cls, v: str) -> str:
        if v not in _ALLOWED_VEHICLES:
            raise ValueError(f"Invalid vehicle. Allowed: {', '.join(_ALLOWED_VEHICLES)}")
        return v

    @field_validator("trip_type")
    @classmethod
    def validate_trip_type(cls, v: str) -> str:
        if v not in _ALLOWED_TRIP_TYPES:
            raise ValueError(f"Invalid trip type. Allowed: {', '.join(_ALLOWED_TRIP_TYPES)}")
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
        raise HTTPException(status_code=429, detail="Too many booking requests. Try again later.")
    recent.append(now)
    _booking_rate[ip] = recent

def check_login_rate(request: Request) -> None:
    ip = _real_ip(request)
    now = datetime.utcnow()
    window_start = now - _LOGIN_RATE_WINDOW
    recent = [t for t in _login_rate.get(ip, []) if t > window_start]
    if len(recent) >= _LOGIN_RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again in 15 minutes.")
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
            "img-src 'self' data: https://*.tile.openstreetmap.org; "
            "connect-src 'self' https://photon.komoot.io https://router.project-osrm.org; "
            "frame-ancestors 'none';"
        )
        return response

class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        cl = request.headers.get("content-length")
        if cl:
            try:
                if int(cl) > _MAX_BODY_BYTES:
                    return JSONResponse(status_code=413, content={"detail": "Request body too large"})
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Invalid content-length"})
        return await call_next(request)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(BodySizeLimitMiddleware)
app.mount("/images", StaticFiles(directory=BASE_DIR / "images"), name="images")


def add_db_event(db: Session, reservation_id: int, title: str, body: str):
    now = datetime.now().strftime("%I:%M %p")
    db_event = EventModel(reservation_id=reservation_id, time=now, title=title, body=body)
    db.add(db_event)
    db.commit()


_VEHICLE_RATES = {
    "Black car":    (3.50, 65),   # ($/mile, minimum $)
    "Executive SUV": (4.50, 88),
    "Sprinter van": (6.00, 150),
}

def _calc_total_cents(vehicle: str, distance_miles: float = 0, hour: int = 12) -> int:
    rate, minimum = _VEHICLE_RATES.get(vehicle, (4.50, 88))
    base = max(rate * distance_miles, float(minimum))
    # Time-of-day multiplier
    if hour >= 22 or hour < 5:          # 10 pm – 5 am
        base *= 1.25
    elif (6 <= hour < 9) or (16 <= hour < 19):  # rush hours
        base *= 1.15
    base = round(base)
    gratuity = round(base * 0.18)
    return (base + gratuity + 22) * 100


# --- INITIAL DATA ---
def init_drivers(db: Session):
    existing_drivers = {d.name for d in db.query(DriverModel).all()}
    
    drivers_to_add = [
        {"name": "Jack Dorj", "phone": "4156994052", "vehicle": "Executive SUV"},
        {"name": "Bataa", "phone": "4155550101", "vehicle": "Black car"},
        {"name": "Boldoo", "phone": "4155550102", "vehicle": "Sprinter van"},
        {"name": "Hanaa", "phone": "4155550103", "vehicle": "Executive SUV"},
        {"name": "Irmoon", "phone": "4155550104", "vehicle": "Black car"},
    ]

    for d_data in drivers_to_add:
        if d_data["name"] not in existing_drivers:
            db.add(DriverModel(**d_data))
        elif d_data["name"] == "Jack Dorj":
            # Update Jack's phone if he already exists
            jack = db.query(DriverModel).filter(DriverModel.name == "Jack Dorj").first()
            if jack:
                jack.phone = d_data["phone"]
    
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
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        await asyncio.sleep((tomorrow - now).total_seconds())
        db = SessionLocal()
        try:
            count = db.query(ReservationModel).filter(
                ReservationModel.status.in_(["Completed", "Cancelled"]),
                ReservationModel.archived == 0,
            ).update({"archived": 1})
            db.commit()
            if count:
                await manager.broadcast({"type": "UPDATE", "message": f"{count} resolved trips archived at midnight"})
        finally:
            db.close()


def seed_admin_user(db: Session):
    admin_email = os.getenv("ADMIN_EMAIL", "admin@tsatslimo.com")
    admin_password = os.getenv("ADMIN_PASSWORD", os.getenv("DISPATCH_PASSWORD", ""))
    if not admin_password:
        return
    existing = db.query(UserModel).filter(UserModel.email == admin_email).first()
    if not existing:
        hashed = _hash_password(admin_password)
        db.add(UserModel(email=admin_email, hashed_password=hashed, name="Admin", role="admin"))
        db.commit()


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
            conn.execute(text("ALTER TABLE reservations ADD COLUMN archived INTEGER DEFAULT 0"))
            conn.commit()
        if "created_by" not in existing_cols:
            conn.execute(text("ALTER TABLE reservations ADD COLUMN created_by INTEGER"))
            conn.commit()
        if "stripe_session_id" not in existing_cols:
            conn.execute(text("ALTER TABLE reservations ADD COLUMN stripe_session_id VARCHAR"))
            conn.commit()
        if "distance_miles" not in existing_cols:
            conn.execute(text("ALTER TABLE reservations ADD COLUMN distance_miles FLOAT"))
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
    return FileResponse(BASE_DIR / "login.html")


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
        "session_token", token,
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
    return {"status": "ok", "message": "If that email exists, instructions will be sent."}


@app.post("/api/users")
async def create_user(payload: dict, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    email = payload.get("email", "").lower().strip()
    password = payload.get("password", "")
    name = payload.get("name", "")
    role = payload.get("role", "operator")
    if not email or not password or not name:
        raise HTTPException(status_code=422, detail="email, password, and name are required")
    if role not in ("admin", "operator"):
        raise HTTPException(status_code=422, detail="role must be admin or operator")
    if db.query(UserModel).filter(UserModel.email == email).first():
        raise HTTPException(status_code=409, detail="Email already registered")
    hashed = _hash_password(password)
    user = UserModel(email=email, hashed_password=hashed, name=name, role=role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"status": "Success", "user": {"id": user.id, "email": user.email, "name": user.name, "role": user.role}}


@app.get("/logout")
def logout(session_token: Optional[str] = Cookie(default=None)):
    if session_token:
        _active_sessions.pop(session_token, None)
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("session_token")
    return response


@app.get("/")
def home():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/driver")
def driver_location_page(session_token: Optional[str] = Cookie(default=None)):
    if not _session_valid(session_token):
        return RedirectResponse(url="/login", status_code=302)
    return FileResponse(BASE_DIR / "driver.html")


@app.get("/dispatch")
@app.get("/dispatch.html")
def dispatch_page(session_token: Optional[str] = Cookie(default=None)):
    if not _session_valid(session_token):
        return RedirectResponse(url="/login", status_code=302)
    return FileResponse(BASE_DIR / "dispatch.html")


@app.get("/booking")
@app.get("/booking.html")
def booking_page():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/about")
@app.get("/about.html")
def about_page():
    return FileResponse(BASE_DIR / "about.html")


@app.get("/blog")
@app.get("/blog.html")
def blog_page():
    return FileResponse(BASE_DIR / "blog.html")


@app.get("/contact")
@app.get("/contact.html")
def contact_page():
    return FileResponse(BASE_DIR / "contact.html")


@app.get("/services")
@app.get("/services.html")
def services_page():
    return FileResponse(BASE_DIR / "services.html")


@app.get("/index")
@app.get("/index.html")
@app.get("/demo")
@app.get("/demo.html")
def landing_page():
    return FileResponse(BASE_DIR / "demo.html")


@app.get("/payment/{res_id}")
def payment_page(res_id: int):
    return FileResponse(BASE_DIR / "payment.html")


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
                reservation = db.query(ReservationModel).filter(
                    ReservationModel.id == int(res_id_str)
                ).first()
                if reservation:
                    reservation.payment = "Paid"
                    db.commit()
                    add_db_event(db, reservation.id, "Payment received", "Stripe checkout completed.")
                    await manager.broadcast({"type": "UPDATE", "message": f"Payment confirmed for trip #{res_id_str}"})
            except Exception as e:
                print(f"Webhook processing error: {e}")
    return JSONResponse({"status": "ok"})


_geocode_cache: dict[str, tuple] = {}  # q → (results, timestamp)
_GEOCODE_TTL = 600  # seconds

@app.get("/api/geocode")
async def geocode_proxy(q: str):
    if not q or len(q) < 3 or len(q) > 200:
        return {"results": []}
    key = q.lower().strip()
    cached = _geocode_cache.get(key)
    if cached:
        results, ts = cached
        if (datetime.utcnow().timestamp() - ts) < _GEOCODE_TTL:
            return {"results": results}
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(
                "https://photon.komoot.io/api/",
                params={
                    "q": q, "limit": 6, "lang": "en",
                    "lat": 37.7749, "lon": -122.4194,
                    "bbox": "-124.5,32.5,-114.0,42.0",  # California hard boundary
                },
                headers={"User-Agent": "TsatsralLimoLLC/1.0"},
            )
            data = r.json() if r.status_code == 200 else {}
    except Exception:
        data = {}

    features = data.get("features", []) if isinstance(data, dict) else []
    safe = []
    for f in features:
        props = f.get("properties", {})
        coords = f.get("geometry", {}).get("coordinates", [None, None])
        if coords[0] is None or coords[1] is None:
            continue
        # Skip results outside the US
        if props.get("countrycode", "US") != "US":
            continue
        # Build a clean display name
        name = props.get("name", "")
        street = props.get("street", "")
        housenumber = props.get("housenumber", "")
        city = props.get("city") or props.get("county", "")
        state = props.get("state", "")
        if housenumber and street:
            line1 = f"{housenumber} {street}"
        elif street:
            line1 = street
        elif name:
            line1 = name
        else:
            continue
        # Append name as prefix if it's a landmark (not just a street)
        if name and name not in line1:
            line1 = f"{name}, {line1}"
        line2 = ", ".join(filter(None, [city, state]))
        display = f"{line1}, {line2}" if line2 else line1
        safe.append({
            "display_name": display[:300],
            "line1": line1[:150],
            "line2": line2[:100],
            "lat": str(coords[1]),
            "lon": str(coords[0]),
        })
    if len(_geocode_cache) > 500:
        oldest = min(_geocode_cache, key=lambda k: _geocode_cache[k][1])
        _geocode_cache.pop(oldest, None)
    _geocode_cache[key] = (safe, datetime.utcnow().timestamp())
    return {"results": safe}


@app.get("/api/reservations")
def list_reservations(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
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
        output.append({
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
            "events": events
        })
    return {"status": "Success", "reservations": output}


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
                line_items=[{
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": _calc_total_cents(
                            db_res.vehicle,
                            db_res.distance_miles or 0,
                            int((db_res.time or "12:00").split(":")[0]),
                        ),
                        "product_data": {
                            "name": f"Tsatsral Limo — {db_res.vehicle}",
                            "description": f"{db_res.pickup} → {db_res.dropoff} on {db_res.date}",
                        },
                    },
                    "quantity": 1,
                }],
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

    add_db_event(db, db_res.id, "Reservation created", f"{db_res.vehicle} booked via dashboard.")

    await manager.broadcast({"type": "UPDATE", "message": f"New reservation from {db_res.customer}"})
    return {"status": "Success", "reservation": {"id": db_res.id, "payment_url": payment_url}}


@app.post("/api/reservations/{reservation_id}/archive")
async def archive_reservation(reservation_id: int, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    reservation = db.query(ReservationModel).filter(ReservationModel.id == reservation_id).first()
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    if reservation.status not in ("Completed", "Cancelled"):
        raise HTTPException(status_code=409, detail="Only completed or cancelled trips can be archived")
    reservation.archived = 1
    db.commit()
    await manager.broadcast({"type": "UPDATE", "message": f"Trip #{reservation_id} closed"})
    return {"status": "Success"}


@app.post("/api/reservations/archive-all")
async def archive_all_resolved(payload: dict, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    status = payload.get("status")
    if status not in ("Completed", "Cancelled"):
        raise HTTPException(status_code=422, detail="Can only archive Completed or Cancelled trips")
    db.query(ReservationModel).filter(
        ReservationModel.status == status,
        ReservationModel.archived == 0,
    ).update({"archived": 1})
    db.commit()
    await manager.broadcast({"type": "UPDATE", "message": f"All {status} trips cleared"})
    return {"status": "Success"}


@app.post("/api/reservations/{reservation_id}/notes")
async def add_note(reservation_id: int, note: dict, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    reservation = db.query(ReservationModel).filter(ReservationModel.id == reservation_id).first()
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")

    add_db_event(db, reservation_id, "Internal Note", note.get("text", ""))
    await manager.broadcast({"type": "UPDATE", "message": f"Note added to trip #{reservation_id}"})
    return {"status": "Success"}


@app.post("/api/reservations/{reservation_id}/complete")
async def complete_reservation(reservation_id: int, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    reservation = db.query(ReservationModel).filter(ReservationModel.id == reservation_id).first()
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")

    reservation.status = "Completed"
    
    # Release the driver if one was assigned
    if reservation.assigned_driver:
        driver = db.query(DriverModel).filter(DriverModel.name == reservation.assigned_driver).first()
        if driver:
            driver.status = "Online"
            
    add_db_event(db, reservation_id, "Trip completed", "Reservation moved to history.")
    db.commit()

    await manager.broadcast({"type": "UPDATE", "message": f"Trip #{reservation_id} completed"})
    return {"status": "Success"}


@app.post("/api/reservations/{reservation_id}/cancel")
async def cancel_reservation(reservation_id: int, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    reservation = db.query(ReservationModel).filter(ReservationModel.id == reservation_id).first()
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")

    reservation.status = "Cancelled"
    
    # Release the driver if one was assigned
    if reservation.assigned_driver:
        driver = db.query(DriverModel).filter(DriverModel.name == reservation.assigned_driver).first()
        if driver:
            driver.status = "Online"

    add_db_event(db, reservation_id, "Trip cancelled", "Reservation was cancelled by operator.")
    db.commit()

    await manager.broadcast({"type": "UPDATE", "message": f"Trip #{reservation_id} cancelled"})
    return {"status": "Success"}


@app.post("/api/reservations/{reservation_id}/assign")
async def assign_driver(reservation_id: int, payload: dict, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    reservation = db.query(ReservationModel).filter(ReservationModel.id == reservation_id).first()
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

    # Twilio logic
    sid = "MOCKED_SID"
    try:
        sid = notify_driver_of_reservation(reservation, {"name": driver.name, "phone": driver.phone})
    except Exception as e:
        print(f"Twilio failed: {e}")

    reservation.status = "Pending confirmation"
    reservation.assigned_driver = driver.name
    
    # Update driver status to Busy so they aren't recommended for other trips
    driver.status = "Busy"
    
    add_db_event(db, reservation_id, "Driver notified", f"SMS sent to {driver.name}. Awaiting reply.")
    db.commit()

    await manager.broadcast({"type": "UPDATE", "message": f"{driver.name} assigned to trip #{reservation_id}"})

    return {
        "status": "Success",
        "reservation_id": reservation_id,
        "driver": {"name": driver.name, "vehicle": driver.vehicle},
        "message_sid": sid,
    }


@app.post("/twilio/reply")
async def twilio_reply(
    request: Request,
    From: str = Form(...),
    Body: str = Form(...),
    db: Session = Depends(get_db),
):
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    if not auth_token:
        raise HTTPException(status_code=503, detail="Twilio not configured")
    validator = RequestValidator(auth_token)
    form_data = dict(await request.form())
    signature = request.headers.get("X-Twilio-Signature", "")
    url = str(request.url)
    if not validator.validate(url, form_data, signature):
        raise HTTPException(status_code=403, detail="Invalid request signature")
    reply = Body.strip().upper()
    # Normalize phone: Twilio sends +1XXXXXXXXXX, DB stores 10-digit
    phone_raw = From.replace("+1", "").replace("+", "").replace("-", "").replace(" ", "").replace("(", "").replace(")", "")

    driver = db.query(DriverModel).filter(DriverModel.phone == phone_raw).first()
    if not driver:
        return PlainTextResponse("<?xml version='1.0'?><Response></Response>", media_type="application/xml")

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
        return PlainTextResponse("<?xml version='1.0'?><Response></Response>", media_type="application/xml")

    if reply.startswith("YES"):
        reservation.status = "Assigned"
        add_db_event(db, reservation.id, "Driver confirmed", f"{driver.name} accepted the trip.")
        sms_back = f"Got it! You're confirmed for the trip from {reservation.pickup} to {reservation.dropoff}. See you there."
    elif reply.startswith("NO"):
        reservation.status = "Needs driver"
        reservation.assigned_driver = None
        
        # Set driver back to Online
        driver.status = "Online"
        
        add_db_event(db, reservation.id, "Driver declined", f"{driver.name} declined. Trip returned to queue.")
        sms_back = "No problem. The trip has been returned to the dispatch queue."
    else:
        return PlainTextResponse(
            "<?xml version='1.0'?><Response><Message>Reply YES to accept or NO to decline.</Message></Response>",
            media_type="application/xml",
        )

    db.commit()
    await manager.broadcast({"type": "UPDATE", "message": f"{driver.name} {'accepted' if reply.startswith('YES') else 'declined'} trip #{reservation.id}"})

    return PlainTextResponse(
        f"<?xml version='1.0'?><Response><Message>{sms_back}</Message></Response>",
        media_type="application/xml",
    )


@app.get("/api/drivers")
def list_drivers(db: Session = Depends(get_db), _: None = Depends(require_admin)):
    drivers = db.query(DriverModel).all()
    return {"status": "Success", "drivers": [
        {"id": d.id, "name": d.name, "email": d.email, "phone": d.phone,
         "vehicle": d.vehicle, "status": d.status, "lat": d.lat, "lon": d.lon}
        for d in drivers
    ]}


@app.post("/api/drivers")
async def create_driver(data: DriverCreate, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    existing = db.query(DriverModel).filter(DriverModel.name == data.name).first()
    if existing:
        raise HTTPException(status_code=409, detail="A driver with that name already exists")
    driver = DriverModel(**data.model_dump())
    db.add(driver)
    db.commit()
    db.refresh(driver)
    await manager.broadcast({"type": "UPDATE", "message": f"{driver.name} added to the roster"})
    return {"status": "Success", "driver": {"id": driver.id, "name": driver.name}}


@app.patch("/api/drivers/{driver_id}/status")
async def update_driver_status(driver_id: int, payload: dict, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    driver = db.query(DriverModel).filter(DriverModel.id == driver_id).first()
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")
    new_status = payload.get("status")
    if new_status not in _ALLOWED_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid status. Allowed: {', '.join(_ALLOWED_STATUSES)}")
    if driver.status == "Busy" and new_status == "Offline":
        raise HTTPException(status_code=409, detail="Cannot go offline while on an active trip")
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
    await manager.broadcast({"type": "UPDATE", "message": f"{driver.name} is now {new_status}"})
    return {"status": "Success", "driver_status": new_status}


@app.delete("/api/drivers/{driver_id}")
async def delete_driver(driver_id: int, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    driver = db.query(DriverModel).filter(DriverModel.id == driver_id).first()
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")
    active_trips = db.query(ReservationModel).filter(
        ReservationModel.assigned_driver == driver.name,
        ReservationModel.status.in_(["Assigned", "Pending confirmation"]),
    ).all()
    for trip in active_trips:
        trip.assigned_driver = None
        trip.status = "Needs driver"
        add_db_event(db, trip.id, "Driver removed", f"{driver.name} was removed from the roster. Trip returned to queue.")
    name = driver.name
    db.delete(driver)
    db.commit()
    await manager.broadcast({"type": "UPDATE", "message": f"{name} removed from roster"})
    return {"status": "Success"}


@app.put("/api/drivers/{driver_id}/location")
async def update_driver_location(driver_id: int, payload: dict, db: Session = Depends(get_db), _: None = Depends(get_current_user)):
    driver = db.query(DriverModel).filter(DriverModel.id == driver_id).first()
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")

    lat = payload.get("lat")
    lon = payload.get("lon")
    if lat is None or lon is None:
        raise HTTPException(status_code=422, detail="lat and lon required")

    driver.lat = str(lat)
    driver.lon = str(lon)
    db.commit()

    await manager.broadcast({
        "type": "DRIVER_LOCATION",
        "driver_id": driver_id,
        "name": driver.name,
        "lat": float(lat),
        "lon": float(lon),
        "status": driver.status,
    })
    return {"status": "Success"}


def notify_driver_of_reservation(reservation: ReservationModel, driver: dict[str, str]) -> str:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_FROM_NUMBER")

    if not account_sid or not auth_token or not from_number:
        return "TWILIO_NOT_CONFIGURED"

    client = Client(account_sid, auth_token)
    message = client.messages.create(
        body=build_driver_message(reservation),
        from_=from_number,
        to=driver["phone"],
    )
    return message.sid


def build_driver_message(reservation: ReservationModel) -> str:
    pickup_time = format_pickup_time(reservation.date, reservation.time)
    notes = f"\nSpecial requests: {reservation.notes}" if reservation.notes else ""
    return (
        f"Hi, I'm assigning you a trip from {reservation.pickup} to {reservation.dropoff}.\n"
        f"Passenger: {reservation.customer}\n"
        f"Pickup time: {pickup_time}\n"
        f"Vehicle: {reservation.vehicle}{notes}\n"
        "Will you accept? Reply YES to confirm or NO to decline."
    )


def format_pickup_time(date_value: str, time_value: str) -> str:
    try:
        parsed = datetime.fromisoformat(f"{date_value}T{time_value}")
        return f"{parsed.strftime('%a %b')} {parsed.day} at {parsed.strftime('%I:%M %p').lstrip('0')}"
    except ValueError:
        return f"{date_value} {time_value}"
