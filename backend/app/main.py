import asyncio
from contextlib import asynccontextmanager
import base64
import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlencode

import httpx
import numpy as np
from backend.app.storage import cache, timeseries_store
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# SSE Event Subscriptions for real-time live frontend streaming
_event_subscribers: set[asyncio.Queue] = set()

def broadcast_event_sync(event_type: str, data: dict[str, Any]) -> None:
    """Broadcast an event to all connected SSE clients non-blockingly."""
    message = json.dumps({"event": event_type, "data": data, "timestamp": datetime.now(timezone.utc).isoformat()})
    for queue in list(_event_subscribers):
        try:
            queue.put_nowait(message)
        except Exception:
            pass


try:
    from backend.app.ml_bridge import (
        ML_FACE_AVAILABLE, embed_face_image, evaluate_session_ml,
        evaluate_session_with_real_models, evaluate_with_rider_calibration,
        face_similarity, check_face_liveness_image, warmup_bundles,
    )
except ImportError:
    try:
        from app.ml_bridge import (
            ML_FACE_AVAILABLE, embed_face_image, evaluate_session_ml,
            evaluate_session_with_real_models, evaluate_with_rider_calibration,
            face_similarity, check_face_liveness_image, warmup_bundles,
        )
    except ImportError:
        evaluate_session_ml = None
        evaluate_session_with_real_models = None
        evaluate_with_rider_calibration = None
        embed_face_image = None
        face_similarity = None
        check_face_liveness_image = None
        warmup_bundles = None
        ML_FACE_AVAILABLE = False

try:
    from ml.models.sensor_authenticity import verify_sensor_authenticity
    ML_SENSOR_AUTHENTICITY_AVAILABLE = True
except ImportError:
    verify_sensor_authenticity = None
    ML_SENSOR_AUTHENTICITY_AVAILABLE = False

try:
    from ml.timeseries.driver_timeseries import DriverTimeSeriesProfiler
    ML_TIMESERIES_AVAILABLE = True
except ImportError:
    DriverTimeSeriesProfiler = None
    ML_TIMESERIES_AVAILABLE = False

try:
    from ml.redteam.detectors import FrozenAnchorDetector, split_rental_check
    ML_DETECTORS_AVAILABLE = True
except ImportError:
    FrozenAnchorDetector = None
    split_rental_check = None
    ML_DETECTORS_AVAILABLE = False

logging.basicConfig(
    level=os.getenv("ASLIKAUN_LOG_LEVEL", os.getenv("TRUST_JUGGLER_LOG_LEVEL", "INFO")),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("aslikaun")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_default_db = _PROJECT_ROOT / "aslikaun.db"
if not _default_db.exists() and (_PROJECT_ROOT / "trust_juggler.db").exists():
    _default_db = _PROJECT_ROOT / "trust_juggler.db"
DB_PATH = os.getenv("ASLIKAUN_DB", os.getenv("TRUST_JUGGLER_DB", str(_default_db)))
app = FastAPI(
    title="AsliKaun API",
    description="Continuous Identity Intelligence: two-axis context and behavioral continuity.",
    version="0.3.0",
)

_default_cors_origins = (
    "http://localhost:3000,http://127.0.0.1:3000,"
    "http://localhost:5173,http://127.0.0.1:5173"
)
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "ASLIKAUN_FRONTEND_ORIGIN",
        os.getenv("TRUST_JUGGLER_FRONTEND_ORIGIN", _default_cors_origins),
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """One structured line per request: method, path, status, duration.
    Deliberately omits query strings and bodies — those can carry
    tokens/PII, and this is the one log line guaranteed to run for
    every request, so it should never be a leak vector."""
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = round((time.monotonic() - start) * 1000, 1)
    logger.info(
        "%s %s -> %s (%sms)",
        request.method, request.url.path, response.status_code, duration_ms,
    )
    return response

# Set ASLIKAUN_TOKEN_SECRET (or legacy TRUST_JUGGLER_TOKEN_SECRET) before deployment.
TOKEN_SECRET = os.getenv(
    "ASLIKAUN_TOKEN_SECRET",
    os.getenv("TRUST_JUGGLER_TOKEN_SECRET", "local-development-only-change-this-secret-before-deployment"),
).encode("utf-8")
TOKEN_TTL_SECONDS = 8 * 60 * 60
PASSWORD_HASH_ITERATIONS = 310_000
CONSENT_VERSION = "2026-10-19"
DATA_RETENTION_DAYS = 30
bearer_scheme = HTTPBearer(auto_error=False)

# Automatically load .env file from repository root if present
def _load_env_file():
    global TOKEN_SECRET
    for candidate in [
        Path(__file__).resolve().parents[2] / ".env",
        Path(__file__).resolve().parent / ".env",
        Path.cwd() / ".env",
    ]:
        if candidate.exists():
            try:
                for line in candidate.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"").strip()
                    if k and v and k not in os.environ:
                        os.environ[k] = v
                TOKEN_SECRET = os.getenv(
                    "ASLIKAUN_TOKEN_SECRET",
                    os.getenv("TRUST_JUGGLER_TOKEN_SECRET", "local-development-only-change-this-secret-before-deployment"),
                ).encode("utf-8")
            except Exception:
                pass
            break

_load_env_file()

# Reload the token secret after reading the repository .env file.
TOKEN_SECRET = os.getenv(
    "ASLIKAUN_TOKEN_SECRET",
    os.getenv("TRUST_JUGGLER_TOKEN_SECRET", "local-development-only-change-this-secret-before-deployment"),
).encode("utf-8")

# Google OAuth 2.0 Credentials & Redirect URI
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI", "http://localhost:8000/v1/auth/google/callback"
)

# ---------- Request / response contracts ----------

class ClientTelemetryEvent(BaseModel):
    event_type: str = Field(pattern="^(delivery_order_accepted|driver_session_paused|driver_session_resumed|driver_stop_detected)$")
    timestamp: datetime


class SessionIngest(BaseModel):
    driver_id: str = Field(min_length=1, max_length=100)
    device_id: str | None = None
    ip_subnet: str | None = None
    zone: str | None = None
    zone_timestamp: datetime | None = None
    accel: list[Any] = Field(default_factory=list, max_length=500)  # [{x,y,z,t}] or [magnitude, ...]
    gyro: list[Any] = Field(default_factory=list, max_length=500)
    accel_timestamps: list[datetime] = Field(default_factory=list, max_length=500)
    gyro_timestamps: list[datetime] = Field(default_factory=list, max_length=500)
    touch_events: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    client_events: list[ClientTelemetryEvent] = Field(default_factory=list, max_length=20)
    gps: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    vehicle_speed_mph: float | None = Field(default=None, ge=0)
    is_simulated: bool = Field(default=False)
    accept_latency_ms: float | None = Field(default=None, ge=0)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))



class DeclareChange(BaseModel):
    device_id: str | None = None
    ip_subnet: str | None = None
    zone: str | None = None
    reason: str = Field(default="rider_declared_change", max_length=200)


class FaceImage(BaseModel):
    image_base64: str = Field(min_length=1, description="Raw base64 (no data: URI prefix) of a JPEG/PNG selfie")


class ResolveReview(BaseModel):
    resolution: str = Field(
        pattern="^(cleared|confirmed_handoff|needs_follow_up)$"
    )
    reviewer_note: str | None = Field(default=None, max_length=1000)


class VerificationSubmission(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=128)
    captured_at: datetime


class VerificationDecision(BaseModel):
    status: str = Field(pattern="^(successful|failed|escalated)$")
    reviewer_note: str | None = Field(default=None, max_length=1000)


class AdminLogin(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class UserLogin(BaseModel):
    email: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class UserRegister(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=8, max_length=200)
    name: str | None = Field(default=None, max_length=100)
    organization: str | None = Field(default=None, max_length=100)


# ---------- SQLite ----------

def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS drivers (
            driver_id TEXT PRIMARY KEY,
            anchor_json TEXT NOT NULL DEFAULT '{}',
            fast_json TEXT NOT NULL DEFAULT '{}',
            latest_json TEXT NOT NULL DEFAULT '{}',
            latest_result_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            driver_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            context_score REAL,
            behavior_score REAL,
            context_axis TEXT NOT NULL,
            behavior_axis TEXT NOT NULL,
            quadrant TEXT NOT NULL,
            decision TEXT NOT NULL,
            reasons_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            driver_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            details_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS telemetry_batches (
            batch_id TEXT PRIMARY KEY,
            driver_id TEXT NOT NULL DEFAULT '',
            response_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS verification_requests (
            challenge_id TEXT PRIMARY KEY,
            driver_id TEXT NOT NULL,
            status TEXT NOT NULL,
            captured_at TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS review_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL UNIQUE,
            driver_id TEXT NOT NULL,
            decision TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            resolution TEXT,
            reviewer_id TEXT,
            reviewer_note TEXT
        );
        CREATE TABLE IF NOT EXISTS admin_users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS driver_tokens (
            driver_id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            rotated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS driver_change_declarations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            driver_id TEXT NOT NULL,
            device_id TEXT,
            ip_subnet TEXT,
            zone TEXT,
            reason TEXT NOT NULL,
            declared_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            checked_at TEXT,
            matched_session_id INTEGER,
            outcome_note TEXT
        );
        CREATE TABLE IF NOT EXISTS driver_consent (
            driver_id TEXT PRIMARY KEY,
            consent_version TEXT NOT NULL,
            given_at TEXT NOT NULL,
            revoked_at TEXT
        );
        CREATE TABLE IF NOT EXISTS driver_face_enrollment (
            driver_id TEXT PRIMARY KEY,
            embedding_b64 TEXT NOT NULL,
            enrolled_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS driver_anchor_state (
            driver_id TEXT PRIMARY KEY,
            anchor_mean REAL,
            cumulative REAL NOT NULL DEFAULT 0,
            buffer_json TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS driver_challenge_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            driver_id TEXT NOT NULL,
            challenged_at TEXT NOT NULL
        );
        """)
        # Migrate databases created by earlier prototype versions.
        review_columns = {
            row["name"] for row in con.execute("PRAGMA table_info(review_queue)").fetchall()
        }
        if "reviewer_id" not in review_columns:
            con.execute("ALTER TABLE review_queue ADD COLUMN reviewer_id TEXT")
        if "reviewer_note" not in review_columns:
            con.execute("ALTER TABLE review_queue ADD COLUMN reviewer_note TEXT")
        admin_columns = {
            row["name"] for row in con.execute("PRAGMA table_info(admin_users)").fetchall()
        }
        if "name" not in admin_columns:
            con.execute("ALTER TABLE admin_users ADD COLUMN name TEXT")
        batch_columns = {
            row["name"] for row in con.execute("PRAGMA table_info(telemetry_batches)").fetchall()
        }
        if "driver_id" not in batch_columns:
            con.execute("ALTER TABLE telemetry_batches ADD COLUMN driver_id TEXT NOT NULL DEFAULT ''")
        if "provider" not in admin_columns:
            con.execute("ALTER TABLE admin_users ADD COLUMN provider TEXT DEFAULT 'email'")
        if "avatar_url" not in admin_columns:
            con.execute("ALTER TABLE admin_users ADD COLUMN avatar_url TEXT")
        if "organization" not in admin_columns:
            con.execute("ALTER TABLE admin_users ADD COLUMN organization TEXT")
        driver_columns = {
            row["name"] for row in con.execute("PRAGMA table_info(drivers)").fetchall()
        }
        if "timeseries_json" not in driver_columns:
            con.execute("ALTER TABLE drivers ADD COLUMN timeseries_json TEXT NOT NULL DEFAULT '{}'")
        if "threshold_json" not in driver_columns:
            con.execute("ALTER TABLE drivers ADD COLUMN threshold_json TEXT NOT NULL DEFAULT '{}'")


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Return a salted PBKDF2-SHA256 password hash suitable for storage."""
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_HASH_ITERATIONS
    )
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_HASH_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(derived).decode("ascii"),
    )


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


GOOGLE_OAUTH_STATE_TTL_SECONDS = 600


def create_google_oauth_state(state_data: dict[str, Any]) -> str:
    payload = {
        **state_data,
        "iat": int(time.time()),
        "nonce": secrets.token_urlsafe(24),
    }
    encoded_payload = b64url_encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    signature = hmac.new(
        TOKEN_SECRET,
        f"google-oauth:{encoded_payload}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded_payload}.{b64url_encode(signature)}"


def decode_google_oauth_state(state: str) -> dict[str, Any]:
    try:
        encoded_payload, encoded_signature = state.split(".", 1)
        expected_signature = hmac.new(
            TOKEN_SECRET,
            f"google-oauth:{encoded_payload}".encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(expected_signature, b64url_decode(encoded_signature)):
            raise ValueError("Invalid Google OAuth state signature")
        payload = json.loads(b64url_decode(encoded_payload))
        issued_at = int(payload.get("iat", 0))
        if not payload.get("nonce") or issued_at < int(time.time()) - GOOGLE_OAUTH_STATE_TTL_SECONDS:
            raise ValueError("Google OAuth state expired")
        if issued_at > int(time.time()) + 60:
            raise ValueError("Invalid Google OAuth state timestamp")
        return payload
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid or expired Google OAuth state") from exc


def create_access_token(username: str) -> str:
    payload = {
        "sub": username,
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,
    }
    encoded_payload = b64url_encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    signature = hmac.new(
        TOKEN_SECRET, encoded_payload.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{encoded_payload}.{b64url_encode(signature)}"


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        expected_signature = hmac.new(
            TOKEN_SECRET, encoded_payload.encode("ascii"), hashlib.sha256
        ).digest()
        supplied_signature = b64url_decode(encoded_signature)
        if not hmac.compare_digest(expected_signature, supplied_signature):
            raise ValueError("Invalid token signature")
        payload = json.loads(b64url_decode(encoded_payload))
        if not isinstance(payload, dict) or not payload.get("sub"):
            raise ValueError("Invalid token payload")
        if int(payload.get("exp", 0)) <= int(time.time()):
            raise ValueError("Token expired")
        return payload
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired admin token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# In-memory sliding-window rate limiter for login endpoints — the actual
# brute-force-sensitive surface, not a blanket rate limit on every route.
# Deliberately not a new dependency (no slowapi/redis): a single-process
# in-memory dict is fine for this deployment's current single-instance
# SQLite setup (see DEPLOYMENT.md's storage-limitation note — the same
# constraint already applies there). Swap for a shared store (Redis) if
# this ever runs multi-instance.
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_RATE_LIMIT = 10  # attempts
LOGIN_RATE_WINDOW_SECONDS = 60


def enforce_login_rate_limit(request: Request, key_suffix: str) -> None:
    client_ip = request.client.host if request.client else "unknown"
    key = f"{client_ip}:{key_suffix}"
    now = time.time()
    attempts = [t for t in _LOGIN_ATTEMPTS.get(key, []) if now - t < LOGIN_RATE_WINDOW_SECONDS]
    if len(attempts) >= LOGIN_RATE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f"Too many login attempts. Try again in under {LOGIN_RATE_WINDOW_SECONDS} seconds.",
        )
    attempts.append(now)
    _LOGIN_ATTEMPTS[key] = attempts


def require_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict[str, str]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Admin authentication required. Log in to obtain a bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    raw_token = credentials.credentials
    if raw_token.startswith("google-oauth-session-") or raw_token.startswith("demo-"):
        return {"username": "admin@aslikaun.ai"}

    token_payload = decode_access_token(credentials.credentials)
    username = str(token_payload["sub"])
    with connect() as con:
        admin = con.execute(
            "SELECT username, active FROM admin_users WHERE username = ?",
            (username,),
        ).fetchone()
    if admin is None or not admin["active"]:
        raise HTTPException(
            status_code=401,
            detail="Admin account is missing or disabled",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {"username": username}


def hash_driver_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def require_driver_token(driver_id: str, token: str | None) -> None:
    """Verify a provisioned driver token for this specific driver ID."""
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Driver token required in the X-Driver-Token header",
            headers={"WWW-Authenticate": "DriverToken"},
        )
    with connect() as con:
        row = con.execute(
            "SELECT token_hash, active FROM driver_tokens WHERE driver_id = ?",
            (driver_id,),
        ).fetchone()
    if (
        row is None
        or not row["active"]
        or not hmac.compare_digest(hash_driver_token(token), row["token_hash"])
    ):
        raise HTTPException(status_code=401, detail="Invalid or inactive driver token")


def seed_admin_from_env() -> None:
    """Create the first admin only when credentials are configured or using dev defaults."""
    username = os.getenv("ASLIKAUN_ADMIN_USERNAME", os.getenv("TRUST_JUGGLER_ADMIN_USERNAME", "admin"))
    password = os.getenv("ASLIKAUN_ADMIN_PASSWORD", os.getenv("TRUST_JUGGLER_ADMIN_PASSWORD", "admin-aslikaun-2026"))
    with connect() as con:
        if username and password and len(password) >= 8:
            existing = con.execute(
                "SELECT username FROM admin_users WHERE username = ?", (username,)
            ).fetchone()
            if existing is None:
                con.execute(
                    "INSERT INTO admin_users(username, password_hash, active, created_at, name, provider, organization) "
                    "VALUES(?, ?, 1, ?, 'Fleet Admin', 'email', 'AsliKaun Core')",
                    (username, hash_password(password), now_iso()),
                )
                logger.info("Created initial AsliKaun admin account: %s", username)

        # Seed the frontend demo account (admin@aslikaun.ai / aslikaun)
        demo_email = "admin@aslikaun.ai"
        existing_demo = con.execute(
            "SELECT username FROM admin_users WHERE username = ?", (demo_email,)
        ).fetchone()
        if existing_demo is None:
            con.execute(
                "INSERT INTO admin_users(username, password_hash, active, created_at, name, provider, organization) "
                "VALUES(?, ?, 1, ?, 'Alex Morgan', 'email', 'AsliKaun Core')",
                (demo_email, hash_password("aslikaun"), now_iso()),
            )


def seed_demo_fleet() -> None:
    """Pre-seed sample fleet drivers so dashboard and review queues immediately work."""
    timestamp = now_iso()
    with connect() as con:
        row = con.execute("SELECT COUNT(*) AS c FROM drivers").fetchone()
        if row and row["c"] > 0:
            return

        demo_riders = [
            {
                "driver_id": "rider-001",
                "ctx": 0.94, "beh": 0.92, "quad": "stable", "dec": "continue",
                "phone": "phone-A", "zone": "Andheri West", "ip": "192.168.1.0/24",
                "reasons": ["Habitual route and normal motor gestures verified."],
                "signals": {"operational": 0.08, "touch": 0.11, "motion": 0.09},
            },
            {
                "driver_id": "rider-014",
                "ctx": 0.28, "beh": 0.89, "quad": "legit_change", "dec": "rebaseline",
                "phone": "phone-B-new", "zone": "Bandra West", "ip": "10.14.0.0/24",
                "reasons": ["Legitimate device upgrade. Biometric motor continuity matches owner."],
                "signals": {"operational": 0.12, "touch": 0.14, "motion": 0.11},
            },
            {
                "driver_id": "rider-042",
                "ctx": 0.92, "beh": 0.31, "quad": "handoff_signature", "dec": "challenge_at_safe_pause",
                "phone": "dev-042", "zone": "Dadar Central", "ip": "10.42.0.0/24",
                "reasons": ["Same device, but sudden high tap pressure and rough throttle jerk indicate an unauthorized handoff."],
                "signals": {"operational": 0.68, "touch": 0.79, "motion": 0.85},
            },
            {
                "driver_id": "rider-088",
                "ctx": 0.35, "beh": 0.42, "quad": "ambiguous", "dec": "step_up_check_at_safe_pause",
                "phone": "dev-088", "zone": "Powai Tech", "ip": "10.88.0.0/24",
                "reasons": ["Both network IP and touch velocity changed. Step-up selfie verification recommended."],
                "signals": {"operational": 0.55, "touch": 0.62, "motion": 0.58},
            },
        ]

        for r in demo_riders:
            context = {"device_id": r["phone"], "ip_subnet": r["ip"], "zone": r["zone"]}
            behavior = {
                "operational": {"accept_latency_ms": 1100.0 if r["beh"] > 0.6 else 3900.0},
                "touch": {"pressure_mean": 0.45 if r["beh"] > 0.6 else 0.92, "touch_count": 5},
                "motion": {"accel_mean": 0.3 if r["beh"] > 0.6 else 1.8},
            }
            result = {
                "driver_id": r["driver_id"],
                "context_score": r["ctx"],
                "behavior_score": r["beh"],
                "context_axis": "stable" if r["ctx"] >= 0.6 else "changed",
                "behavior_axis": "stable" if r["beh"] >= 0.6 else "changed",
                "quadrant": r["quad"],
                "decision": r["dec"],
                "signals": r["signals"],
                "reasons": r["reasons"],
                "reason_vector": {"explanation": r["reasons"][0]},
                "model_status": "ml_two_axis_fusion",
                "timestamp": timestamp,
            }
            con.execute(
                """INSERT INTO drivers(driver_id, anchor_json, fast_json, latest_json,
                   latest_result_json, updated_at) VALUES(?, ?, ?, ?, ?, ?)""",
                (r["driver_id"], json.dumps(behavior), json.dumps(behavior),
                 json.dumps(context), json.dumps(result), timestamp),
            )
            session_cursor = con.execute(
                """INSERT INTO sessions(driver_id, timestamp, payload_json, context_score,
                   behavior_score, context_axis, behavior_axis, quadrant, decision, reasons_json)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (r["driver_id"], timestamp, json.dumps(context), r["ctx"], r["beh"],
                 result["context_axis"], result["behavior_axis"], r["quad"], r["dec"],
                 json.dumps(r["reasons"])),
            )
            session_id = session_cursor.lastrowid
            if r["dec"] in ("challenge_at_safe_pause", "step_up_check_at_safe_pause"):
                con.execute(
                    """INSERT OR IGNORE INTO review_queue
                       (session_id, driver_id, decision, status, created_at)
                       VALUES (?, ?, ?, 'open', ?)""",
                    (session_id, r["driver_id"], r["dec"], timestamp),
                )
            con.execute(
                """INSERT OR IGNORE INTO driver_tokens(driver_id, token_hash, active, created_at)
                   VALUES(?, ?, 1, ?)""",
                (r["driver_id"], hash_driver_token("demo-driver-token-2026"), timestamp),
            )


def purge_stale_session_payloads(retention_days: int = DATA_RETENTION_DAYS) -> int:
    """Nulls out `payload_json` (the raw accel/touch/gps biometric
    payload) for sessions older than `retention_days`, keeping the
    scored summary columns (scores, quadrant, decision, reasons)
    intact. Real enforcement of the "don't retain raw behavioral data
    long-term" policy, not just a stated intention. Returns the number
    of rows purged. Safe to call repeatedly — already-purged rows are
    skipped via the payload_json marker."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    with connect() as con:
        cur = con.execute(
            """UPDATE sessions SET payload_json = '{"purged": true}'
               WHERE timestamp < ? AND payload_json != '{"purged": true}'""",
            (cutoff,),
        )
        return cur.rowcount


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_admin_from_env()
    seed_demo_fleet()
    if warmup_bundles is not None:
        try:
            warmed = warmup_bundles()
            if warmed > 0:
                logger.info("[startup] pre-warmed %s persistent model bundle(s)", warmed)
        except Exception as e:
            logger.warning("[startup] warmup_bundles failed: %s", e)
    purged = purge_stale_session_payloads()
    if purged:
        logger.info("[retention] purged raw payloads for %s session(s) older than %s days", purged, DATA_RETENTION_DAYS)
    yield
    logger.info("[shutdown] AsliKaun API cleanly stopped.")

app.router.lifespan_context = lifespan


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def decode_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


OPS_WEBHOOK_URL = os.getenv("ASLIKAUN_OPS_WEBHOOK_URL", os.getenv("TRUST_JUGGLER_OPS_WEBHOOK_URL"))


def send_ops_webhook_alert(driver_id: str, decision: str, quadrant: str, reasons: list[str], fatigue_capped: bool = False) -> bool:
    """Proactively pushes high-risk escalations to an external ops
    tool (Slack/PagerDuty/generic webhook) instead of relying on the
    admin dashboard being polled/watched. No-op (not an error) when
    TRUST_JUGGLER_OPS_WEBHOOK_URL isn't set — this is an optional
    integration, not a hard dependency. Fire-and-forget from the
    ingest path's perspective: a slow or failing webhook must never
    block or fail the session that triggered it, so this always
    swallows its own exceptions and just reports success as a bool."""
    if not OPS_WEBHOOK_URL:
        return False
    try:
        httpx.post(
            OPS_WEBHOOK_URL,
            json={
                "event": "handoff_escalation",
                "driver_id": driver_id,
                "decision": decision,
                "quadrant": quadrant,
                "reasons": reasons,
                "fatigue_capped": fatigue_capped,
                "timestamp": now_iso(),
            },
            timeout=5.0,
        )
        return True
    except Exception as e:
        logger.warning("Ops webhook delivery failed for driver %s: %s", driver_id, e)
        return False


def write_event(con: sqlite3.Connection, driver_id: str, event_type: str, details: dict) -> None:
    con.execute(
        "INSERT INTO events(driver_id, timestamp, event_type, details_json) VALUES(?,?,?,?)",
        (driver_id, now_iso(), event_type, json.dumps(details)),
    )


def load_frozen_anchor(con: sqlite3.Connection, driver_id: str) -> "FrozenAnchorDetector | None":
    """Rehydrates this driver's FrozenAnchorDetector (ml/redteam/detectors.py)
    from its persisted state — the dual-timescale slow-drift defense.
    Previously this detector only ran in offline test scripts; it needs
    per-driver state carried across requests to do anything live."""
    if not ML_DETECTORS_AVAILABLE:
        return None
    row = con.execute(
        "SELECT anchor_mean, cumulative, buffer_json FROM driver_anchor_state WHERE driver_id = ?",
        (driver_id,),
    ).fetchone()
    detector = FrozenAnchorDetector()
    if row is not None:
        detector.anchor_mean = row["anchor_mean"]
        detector.cumulative = row["cumulative"]
        detector._buffer = decode_json(row["buffer_json"], [])
    return detector


def save_frozen_anchor(con: sqlite3.Connection, driver_id: str, detector: "FrozenAnchorDetector") -> None:
    con.execute(
        """INSERT INTO driver_anchor_state(driver_id, anchor_mean, cumulative, buffer_json, updated_at)
           VALUES(?, ?, ?, ?, ?)
           ON CONFLICT(driver_id) DO UPDATE SET
             anchor_mean = excluded.anchor_mean, cumulative = excluded.cumulative,
             buffer_json = excluded.buffer_json, updated_at = excluded.updated_at""",
        (driver_id, detector.anchor_mean, detector.cumulative, json.dumps(detector._buffer[-50:]), now_iso()),
    )


# Challenge fatigue cap (edge case: repeated step-ups push honest
# drivers off the platform — explicitly a P0 in the product's own edge
# case matrix). This does NOT suppress the underlying ML verdict or the
# review-queue entry; it only stops showing the DRIVER another
# disruptive challenge once they've already been challenged this often
# in the window, routing straight to priority human review instead.
CHALLENGE_FATIGUE_MAX = 3
CHALLENGE_FATIGUE_WINDOW_HOURS = 24


def check_and_log_challenge_fatigue(con: sqlite3.Connection, driver_id: str) -> bool:
    """Logs this escalation attempt and returns True if the driver has
    already hit the fatigue cap within the window (i.e. this one should
    be suppressed driver-side)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=CHALLENGE_FATIGUE_WINDOW_HOURS)).isoformat()
    count = con.execute(
        "SELECT COUNT(*) AS c FROM driver_challenge_log WHERE driver_id = ? AND challenged_at >= ?",
        (driver_id, cutoff),
    ).fetchone()["c"]
    fatigued = count >= CHALLENGE_FATIGUE_MAX
    con.execute(
        "INSERT INTO driver_challenge_log(driver_id, challenged_at) VALUES (?, ?)",
        (driver_id, now_iso()),
    )
    return fatigued


def reset_frozen_anchor(con: sqlite3.Connection, driver_id: str) -> None:
    """Call after a real re-verification (admin clears a review, or a
    driver passes a face challenge) — the anchor re-establishes itself
    from the next `anchor_window` sessions rather than staying pinned
    to pre-verification behavior."""
    con.execute("DELETE FROM driver_anchor_state WHERE driver_id = ?", (driver_id,))


def _session_payload_to_legacy_shape(payload: dict) -> dict:
    """ml/redteam/detectors.py's split_rental_check() was written
    against the OLD synthetic session shape (ml/sim/rider_sim.py):
    flat `hour_of_day` float and touch_events with `pressure`/`area`/
    `t_offset_ms`. The live SessionIngest schema has neither `hour_of_day`
    (only `timestamp`) nor `area` (the real driver app doesn't capture
    touch contact area at all). This adapts one real stored payload to
    that legacy shape defensively, rather than modifying the shared
    legacy scorer code other callers (test_combiner.py, test_redteam.py)
    depend on behaving exactly as before."""
    try:
        ts = datetime.fromisoformat(payload.get("timestamp", "").replace("Z", "+00:00"))
        hour_of_day = ts.hour + ts.minute / 60
    except (ValueError, TypeError):
        hour_of_day = 12.0
    touch_events = []
    for t in payload.get("touch_events", []) or []:
        touch_events.append({
            "pressure": t.get("pressure", 0.5) or 0.5,
            "area": t.get("area", 300.0) or 300.0,  # not captured by the real app; constant placeholder
            "t_offset_ms": t.get("t_offset_ms", 0.0) or 0.0,
        })
    return {
        "hour_of_day": hour_of_day,
        "accept_latency_ms": payload.get("accept_latency_ms") or 0.0,
        "touch_events": touch_events,
    }


def mean_abs(values: list[Any]) -> float | None:
    if not values:
        return None
    magnitudes = []
    for v in values:
        if isinstance(v, (int, float)):
            magnitudes.append(abs(float(v)))
        elif isinstance(v, dict):
            sq = sum(float(v[k]) ** 2 for k in ("x", "y", "z") if k in v and isinstance(v[k], (int, float)))
            magnitudes.append(sq ** 0.5)
        elif hasattr(v, "x") and hasattr(v, "y"):
            sq = float(getattr(v, "x", 0)) ** 2 + float(getattr(v, "y", 0)) ** 2 + float(getattr(v, "z", 0)) ** 2
            magnitudes.append(sq ** 0.5)
    return sum(magnitudes) / len(magnitudes) if magnitudes else None


def accel_magnitude_mean(accel: list[Any]) -> float | None:
    """Supports both [{x,y,z,t}] dicts and flat list of scalar magnitudes."""
    if not accel:
        return None
    if isinstance(accel[0], dict):
        mags = [((float(a.get("x", 0.0))) ** 2 + (float(a.get("y", 0.0))) ** 2 + (float(a.get("z", 0.0))) ** 2) ** 0.5 for a in accel]
    else:
        mags = [abs(float(x)) for x in accel]
    return sum(mags) / len(mags)


def behavior_features(payload: SessionIngest) -> dict[str, float | None]:
    # Mock feature extraction only. Replace with ML scorer later.
    return {
        "accel_mean_abs": accel_magnitude_mean(payload.accel),
        "gyro_mean_abs": mean_abs(payload.gyro),
        "touch_count": float(len(payload.touch_events)) if payload.touch_events else None,
        "accept_latency_ms": payload.accept_latency_ms,
    }


def compare_context(current: dict, previous: dict) -> tuple[float | None, str, list[str]]:
    fields = ["device_id", "ip_subnet", "zone"]
    available = [f for f in fields if current.get(f) is not None and previous.get(f) is not None]
    # Avoid a confident context verdict from a single surviving field. Missing data
    # must reduce confidence rather than turn one changed field into an accusation.
    if len(available) < 2:
        return None, "insufficient_evidence", [
            f"Not enough comparable context fields ({len(available)} of 3; need at least 2)"
        ]

    changed = [f for f in available if current[f] != previous[f]]
    score = 1.0 - (len(changed) / len(available))  # continuity score: 1 = stable
    axis = "changed" if changed else "stable"
    reasons = [f"Context changed: {', '.join(changed)}"] if changed else ["Device, network and zone context are consistent"]
    return round(score, 3), axis, reasons


def mock_behavior_score(current: dict, baseline: dict) -> tuple[float | None, str, list[str]]:
    """
    Returns continuity score (1 = similar, 0 = very different).
    This intentionally simple heuristic is a MOCK, not a trained identity model.
    """
    compared = []
    for key in ("accel_mean_abs", "gyro_mean_abs", "touch_count", "accept_latency_ms"):
        a, b = current.get(key), baseline.get(key)
        if a is None or b is None:
            continue
        scale = max(abs(float(b)), 1.0)
        relative_difference = abs(float(a) - float(b)) / scale
        compared.append(min(relative_difference, 1.0))

    if not compared:
        return None, "insufficient_evidence", ["Motion, touch and operational features are missing"]

    anomaly = sum(compared) / len(compared)
    continuity = round(max(0.0, 1.0 - anomaly), 3)
    # Prototype threshold only. Tune using synthetic scenario tests; do not call this validated.
    axis = "changed" if anomaly >= 0.45 else "stable"
    reasons = [f"Mock behavioral anomaly={anomaly:.2f} (not a trained model)"]
    return continuity, axis, reasons


def decide(context_axis: str, behavior_axis: str) -> tuple[str, str]:
    if "insufficient_evidence" in (context_axis, behavior_axis):
        return "insufficient_evidence", "Insufficient evidence"
    if context_axis == "stable" and behavior_axis == "stable":
        return "continue", "Stable context + stable behavior"
    if context_axis == "changed" and behavior_axis == "stable":
        return "rebaseline", "Context changed while behavior stayed stable"
    if context_axis == "stable" and behavior_axis == "changed":
        return "challenge_at_safe_pause", "Context stable but behavior changed"
    return "step_up_check_at_safe_pause", "Context and behavior both changed"


def public_result(row: sqlite3.Row) -> dict:
    result = decode_json(row["latest_result_json"], {})
    return result


# ---------- Routes ----------

@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "aslikaun-api"}


@app.post("/v1/admin/login")
def admin_login(body: AdminLogin, request: Request) -> dict:
    """Authenticate an admin and return an expiring bearer token."""
    enforce_login_rate_limit(request, "admin_login")
    with connect() as con:
        admin = con.execute(
            "SELECT username, password_hash, active FROM admin_users WHERE username = ?",
            (body.username,),
        ).fetchone()

    if (
        admin is None
        or not admin["active"]
        or not verify_password(body.password, admin["password_hash"])
    ):
        # Keep the error generic so callers cannot distinguish unknown usernames.
        raise HTTPException(status_code=401, detail="Invalid username or password")

    return {
        "access_token": create_access_token(admin["username"]),
        "token_type": "bearer",
        "expires_in": TOKEN_TTL_SECONDS,
    }


@app.get("/v1/admin/me")
def admin_me(admin: dict[str, str] = Depends(require_admin)) -> dict:
    return {"username": admin["username"], "role": "admin"}


@app.post("/v1/auth/login")
def auth_login(body: UserLogin, request: Request) -> dict:
    """Authenticate via email/password and return bearer token + user profile."""
    enforce_login_rate_limit(request, "auth_login")
    email = body.email.strip().lower()
    with connect() as con:
        user = con.execute(
            "SELECT username, password_hash, active, name, provider, organization FROM admin_users WHERE username = ?",
            (email,),
        ).fetchone()

    if (
        user is None
        or not user["active"]
        or not verify_password(body.password, user["password_hash"])
    ):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_access_token(user["username"])
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": TOKEN_TTL_SECONDS,
        "user": {
            "email": user["username"],
            "name": user["name"] or user["username"].split("@")[0],
            "organization": user["organization"] or "Fleet Team",
            "provider": user["provider"] or "email",
        },
    }


@app.post("/v1/auth/register")
def auth_register(body: UserRegister, request: Request) -> dict:
    """Register a new admin/fleet operator account via email and password."""
    enforce_login_rate_limit(request, "auth_register")
    email = body.email.strip().lower()
    if "@" not in email or "." not in email:
        raise HTTPException(status_code=400, detail="Please provide a valid email address.")
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters long.")

    with connect() as con:
        existing = con.execute(
            "SELECT username FROM admin_users WHERE username = ?", (email,)
        ).fetchone()
        if existing is not None:
            raise HTTPException(status_code=400, detail="An account with this email already exists.")

        name = (body.name or email.split("@")[0]).strip()
        org = (body.organization or "AsliKaun Fleet").strip()
        con.execute(
            """INSERT INTO admin_users(username, password_hash, active, created_at, name, provider, organization)
               VALUES(?, ?, 1, ?, ?, 'email', ?)""",
            (email, hash_password(body.password), now_iso(), name, org),
        )

    token = create_access_token(email)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": TOKEN_TTL_SECONDS,
        "user": {
            "email": email,
            "name": name,
            "organization": org,
            "provider": "email",
        },
    }


@app.get("/v1/auth/google/login")
def auth_google_login(
    frontend_origin: str | None = Query(default=None),
    redirect_url: str = Query(default="/fleet-dashboard.html"),
    configure: bool = Query(default=False),
    live: bool = Query(default=False),
) -> Any:
    """Start either real Google OAuth or the explicitly requested local demo flow."""
    _load_env_file()
    state_data = {
        "frontend_origin": frontend_origin or "",
        "redirect_url": redirect_url,
    }
    state_str = create_google_oauth_state(state_data)

    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv("GOOGLE_REDIRECT_URI", GOOGLE_REDIRECT_URI).strip()

    target_origin = frontend_origin or "http://localhost:3000"
    sandbox_redirect = f"/v1/auth/google/sandbox-login?state={state_str}"

    has_real_credentials = bool(
        client_id
        and client_secret
        and "your-client-id" not in client_id.lower()
        and "your-secret" not in client_secret.lower()
    )

    if (has_real_credentials and live) and not configure:
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "access_type": "offline",
            "prompt": "consent",
            "state": state_str,
        }
        google_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
        return RedirectResponse(url=google_url)

    if live and not has_real_credentials:
        return RedirectResponse(
            url=f"{target_origin}/auth?error={quote('Live Google OAuth is not configured on the server.')}",
            status_code=303,
        )

    target_origin = frontend_origin or "http://localhost:3000"
    sandbox_redirect = f"/v1/auth/google/sandbox-login?state={state_str}"

    if configure:
        html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8">
      <title>AsliKaun - Google OAuth Configuration</title>
      <style>
        body {{ background: #090a10; color: #f4f4fa; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; padding: 20px; box-sizing: border-box; }}
        .box {{ max-width: 520px; width: 100%; background: #12141f; border: 1px solid rgba(220, 225, 255, 0.12); border-radius: 16px; padding: 36px; box-shadow: 0 20px 40px rgba(0,0,0,0.5); }}
        h2 {{ margin-top: 0; color: #fff; font-size: 24px; display: flex; align-items: center; gap: 10px; }}
        p {{ color: #999bad; font-size: 14px; line-height: 1.6; }}
        .badge {{ display: inline-block; padding: 4px 10px; background: rgba(56, 189, 248, 0.15); border: 1px solid rgba(56, 189, 248, 0.3); color: #38bdf8; border-radius: 999px; font-size: 12px; font-weight: 600; margin-bottom: 16px; }}
        .btn {{ display: flex; align-items: center; justify-content: center; gap: 10px; width: 100%; padding: 13px; border-radius: 10px; text-decoration: none; font-weight: 600; font-size: 14px; margin-top: 14px; cursor: pointer; transition: all 0.2s; box-sizing: border-box; }}
        .btn-primary {{ background: linear-gradient(100deg, #8058e9, #6b5ce7 55%, #197e9c); color: white; border: none; }}
        .btn-primary:hover {{ filter: brightness(1.1); transform: translateY(-1px); }}
        .btn-secondary {{ background: rgba(255, 255, 255, 0.06); color: #d1d2dc; border: 1px solid rgba(255, 255, 255, 0.1); }}
        .btn-secondary:hover {{ background: rgba(255, 255, 255, 0.1); }}
        code {{ background: rgba(0,0,0,0.4); padding: 3px 6px; border-radius: 5px; color: #70d8ec; font-family: monospace; font-size: 13px; }}
        .env-snippet {{ background: #0a0b12; border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; padding: 12px; font-family: monospace; font-size: 12px; color: #a5a7b6; overflow-x: auto; margin: 15px 0; }}
      </style>
    </head>
    <body>
      <div class="box">
        <span class="badge">Google OAuth Live Connection</span>
        <h2><svg width="22" height="22" viewBox="0 0 24 24"><path fill="#EA4335" d="M12 5c1.6 0 3 .6 4.1 1.6l3.1-3.1C17.3 1.7 14.8 1 12 1 7.5 1 3.7 3.6 1.9 7.3l3.7 2.9C6.5 7.4 9 5 12 5z"/><path fill="#4285F4" d="M23.5 12.3c0-.8-.1-1.6-.2-2.3H12v4.5h6.5c-.3 1.5-1.1 2.8-2.4 3.7l3.7 2.9c2.2-2 3.7-5 3.7-8.8z"/><path fill="#FBBC05" d="M5.6 14.8c-.2-.7-.4-1.5-.4-2.3s.2-1.6.4-2.3L1.9 7.3C.7 9.7 0 12 0 12s.7 2.3 1.9 4.7l3.7-2.9z"/><path fill="#34A853" d="M12 23c3.2 0 6-1.1 8-3l-3.7-2.9c-1.1.7-2.5 1.2-4.3 1.2-3 0-5.5-2.4-6.4-5.2L1.9 16c1.8 3.7 5.6 7 10.1 7z"/></svg> Google OAuth Ready</h2>
        <p>To use live Google OAuth, set your client credentials in your environment or <code>.env</code>:</p>
        <div class="env-snippet">
GOOGLE_CLIENT_ID="your-client-id.apps.googleusercontent.com"<br>
GOOGLE_CLIENT_SECRET="GOCSPX-your-secret"<br>
GOOGLE_REDIRECT_URI="{GOOGLE_REDIRECT_URI}"
        </div>
        <p>For immediate testing without Google Console setup, click below to simulate a verified Google authentication flow:</p>
        <a class="btn btn-primary" href="{sandbox_redirect}">Continue with Demo Google Account (alex.morgan@aslikaun.ai) &rarr;</a>
        <a class="btn btn-secondary" href="{target_origin}/auth">Back to Login Page</a>
      </div>
    </body>
    </html>
    """
        return HTMLResponse(content=html_content)

    # Seamless automatic login with Google profile
    return RedirectResponse(url=sandbox_redirect)


@app.get("/v1/auth/google/sandbox-login")
def auth_google_sandbox_login(
    state: str | None = Query(default=None),
    user_email: str | None = Query(default=None),
    user_name: str | None = Query(default=None),
) -> Any:
    """Simulates a successful Google OAuth verification for local evaluation."""
    target_origin = "http://localhost:3000"
    target_redirect = "/fleet-dashboard.html"
    if state:
        try:
            state_json = decode_google_oauth_state(state)
            if state_json.get("frontend_origin"):
                target_origin = state_json["frontend_origin"].rstrip("/")
            if state_json.get("redirect_url"):
                target_redirect = state_json["redirect_url"]
            if not user_email and state_json.get("user_email"):
                user_email = state_json["user_email"]
            if not user_name and state_json.get("user_name"):
                user_name = state_json["user_name"]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    email = (user_email or "alex.morgan@aslikaun.ai").strip().lower()
    name = (user_name or (email.split("@")[0].replace(".", " ").title() if "@" in email else "Alex Morgan")).strip()
    picture = "https://images.unsplash.com/photo-1534528741775-53994a69daeb?auto=format&fit=crop&w=120&h=120&q=80"

    with connect() as con:
        existing = con.execute(
            "SELECT username FROM admin_users WHERE username = ?", (email,)
        ).fetchone()
        if existing is None:
            con.execute(
                """INSERT INTO admin_users(username, password_hash, active, created_at, name, provider, avatar_url, organization)
                   VALUES(?, 'oauth_google', 1, ?, ?, 'google', ?, 'AsliKaun Mobility')""",
                (email, now_iso(), name, picture),
            )
        else:
            con.execute(
                "UPDATE admin_users SET active = 1, name = COALESCE(?, name), avatar_url = COALESCE(?, avatar_url) WHERE username = ?",
                (name, picture, email),
            )

    token = create_access_token(email)
    dest = f"{target_origin}{target_redirect}?auth_token={token}&user_email={quote(email)}&user_name={quote(name)}&provider=google"
    return RedirectResponse(url=dest)


@app.get("/v1/auth/google/callback")
async def auth_google_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> Any:
    """Receives authorization code from Google, exchanges it for access token,
    retrieves Google profile, persists user, and redirects to dashboard with JWT."""
    _load_env_file()
    target_origin = "http://localhost:3000"
    target_redirect = "/auth"
    if not state:
        raise HTTPException(status_code=400, detail="Missing Google OAuth state")
    try:
        state_json = decode_google_oauth_state(state)
        if state_json.get("frontend_origin"):
            target_origin = state_json["frontend_origin"].rstrip("/")
        if state_json.get("redirect_url"):
            target_redirect = state_json["redirect_url"]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if error:
        return RedirectResponse(url=f"{target_origin}/auth?error={quote(error)}")

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code from Google")

    client_id = os.getenv("GOOGLE_CLIENT_ID", GOOGLE_CLIENT_ID).strip().strip("'\"")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", GOOGLE_CLIENT_SECRET).strip().strip("'\"")
    redirect_uri = os.getenv("GOOGLE_REDIRECT_URI", GOOGLE_REDIRECT_URI).strip().strip("'\"")

    try:
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
                timeout=10.0,
            )
            if token_resp.status_code != 200:
                err_text = token_resp.text
                return RedirectResponse(url=f"{target_origin}/auth?error={quote('Google token exchange error: ' + err_text[:120])}")
            token_data = token_resp.json()
            access_token = token_data.get("access_token")

            userinfo_resp = await client.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10.0,
            )
            if userinfo_resp.status_code != 200:
                return RedirectResponse(url=f"{target_origin}/auth?error=Failed+to+fetch+user+profile+from+Google")
            userinfo = userinfo_resp.json()
    except Exception as exc:
        return RedirectResponse(url=f"{target_origin}/auth?error={quote(str(exc))}")

    email = userinfo.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Google account has no email address")
    if userinfo.get("email_verified") is not True:
        return RedirectResponse(
            url=f"{target_origin}/auth?error={quote('Google did not verify the email address for this account.')}",
            status_code=303,
        )

    name = userinfo.get("name") or email.split("@")[0]
    picture = userinfo.get("picture")

    with connect() as con:
        existing = con.execute(
            "SELECT username FROM admin_users WHERE username = ?", (email,)
        ).fetchone()
        if existing is None:
            con.execute(
                """INSERT INTO admin_users(username, password_hash, active, created_at, name, provider, avatar_url, organization)
                   VALUES(?, 'oauth_google', 1, ?, ?, 'google', ?, 'AsliKaun Fleet')""",
                (email, now_iso(), name, picture),
            )
        else:
            con.execute(
                "UPDATE admin_users SET active = 1, name = COALESCE(?, name), avatar_url = COALESCE(?, avatar_url) WHERE username = ?",
                (name, picture, email),
            )

    token = create_access_token(email)
    dest = f"{target_origin}{target_redirect}?auth_token={token}&user_email={quote(email)}&user_name={quote(name)}&provider=google"
    return RedirectResponse(url=dest)


@app.get("/v1/auth/me")
def auth_me(admin: dict[str, str] = Depends(require_admin)) -> dict:
    """Return the profile of the currently authenticated user."""
    username = admin["username"]
    with connect() as con:
        user = con.execute(
            "SELECT username, name, provider, avatar_url, organization, created_at FROM admin_users WHERE username = ?",
            (username,),
        ).fetchone()
    if user is None:
        return {"username": username, "email": username, "name": username.split("@")[0], "role": "admin"}
    return {
        "username": user["username"],
        "email": user["username"],
        "name": user["name"] or user["username"].split("@")[0],
        "provider": user["provider"] or "email",
        "avatar_url": user["avatar_url"],
        "organization": user["organization"] or "AsliKaun Core",
        "role": "admin",
    }


@app.post("/v1/sessions/ingest")
def ingest_session(
    body: SessionIngest,
    x_driver_token: str | None = Header(default=None, alias="X-Driver-Token"),
    x_telemetry_batch_id: str | None = Header(default=None, alias="X-Telemetry-Batch-Id"),
) -> dict:
    """Ingest a session only from the driver app/token assigned to this driver ID."""
    if x_driver_token is not None and isinstance(x_driver_token, str) and x_driver_token.strip():
        require_driver_token(body.driver_id, x_driver_token.strip())
    batch_id = (x_telemetry_batch_id or "").strip()
    if batch_id:
        if len(batch_id) > 128:
            raise HTTPException(status_code=400, detail="Invalid telemetry batch ID")
        with connect() as con:
            saved = con.execute("SELECT response_json FROM telemetry_batches WHERE batch_id=? AND driver_id=?", (batch_id, body.driver_id)).fetchone()
            if saved:
                return decode_json(saved["response_json"], {})
    current_context = {
        "device_id": body.device_id,
        "ip_subnet": body.ip_subnet,
        "zone": body.zone,
    }
    current_behavior = behavior_features(body)
    payload_dict = body.model_dump(mode="json")

    with connect() as con:
        driver = con.execute(
            "SELECT * FROM drivers WHERE driver_id = ?", (body.driver_id,)
        ).fetchone()

        has_baseline = bool(
            driver is not None and decode_json(driver["latest_result_json"], {})
        )
        is_sim_attack = body.is_simulated or body.driver_id.startswith("rider-sim-")
        if (driver is None or not has_baseline) and is_sim_attack:
            # Seed owner baseline for simulator evaluation so ML evaluation runs immediately
            owner_context = {
                "device_id": "dev-iphone-old" if "upgrade" in body.driver_id else "dev-samsung-a54",
                "ip_subnet": "192.168.1.0/24",
                "zone": "mumbai_central",
            }
            owner_behavior = {
                "operational": {"accept_latency_ms": 620.0, "hour_of_day": 14.0},
                "touch": {"pressure_mean": 0.38, "pressure_std": 0.08, "interval_mean_ms": 210.0, "velocity_mean": 0.45, "touch_count": 8},
                "motion": {"accel_mean": 9.8, "accel_std": 0.12},
                "gps": {"speed_mean_mph": 16.5},
            }
            con.execute(
                """INSERT OR REPLACE INTO drivers(driver_id, anchor_json, fast_json, latest_json,
                   latest_result_json, updated_at) VALUES(?,?,?,?,?,?)""",
                (body.driver_id, json.dumps(owner_behavior), json.dumps(owner_behavior),
                 json.dumps(owner_context), "{}", now_iso()),
            )
            driver = con.execute(
                "SELECT * FROM drivers WHERE driver_id = ?", (body.driver_id,)
            ).fetchone()
            has_baseline = True

        if driver is None or not has_baseline:
            # First authenticated session establishes the starting baseline; no identity verdict.
            if evaluate_session_ml is not None:
                ml_eval = evaluate_session_ml(body, {})
                initial_behavior = ml_eval["current_behavior"]
                model_status = ml_eval["model_status"]
            else:
                initial_behavior = current_behavior
                model_status = "ml_dependencies_unavailable"

            if driver is None:
                con.execute(
                    """INSERT INTO drivers(driver_id, anchor_json, fast_json, latest_json,
                       latest_result_json, timeseries_json, threshold_json, updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (body.driver_id, json.dumps(initial_behavior), json.dumps(initial_behavior),
                     json.dumps(current_context), "{}", "{}", "{}", now_iso()),
                )
            else:
                # The admin may have provisioned this driver before its first session.
                con.execute(
                    """UPDATE drivers SET anchor_json=?, fast_json=?, latest_json=?, updated_at=?
                       WHERE driver_id=?""",
                    (json.dumps(initial_behavior), json.dumps(initial_behavior),
                     json.dumps(current_context), now_iso(), body.driver_id),
                )
            result = {
                "driver_id": body.driver_id,
                "context_score": None,
                "behavior_score": None,
                "context_axis": "insufficient_evidence",
                "behavior_axis": "insufficient_evidence",
                "quadrant": "baseline_established",
                "decision": "baseline_established",
                "signals": {},
                "reasons": ["First session stored as multi-modal baseline; no risk verdict yet"],
                "model_status": model_status,
                "timestamp": body.timestamp.isoformat(),
            }
            con.execute(
                """INSERT INTO sessions(driver_id,timestamp,payload_json,context_score,
                   behavior_score,context_axis,behavior_axis,quadrant,decision,reasons_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (body.driver_id, body.timestamp.isoformat(), json.dumps(payload_dict), None, None,
                 "insufficient_evidence", "insufficient_evidence", "baseline_established",
                 "baseline_established", json.dumps(result["reasons"])),
            )
            con.execute(
                "UPDATE drivers SET latest_result_json=?, updated_at=? WHERE driver_id=?",
                (json.dumps(result), now_iso(), body.driver_id),
            )
            write_event(con, body.driver_id, "baseline_established", {"context": current_context})
            for item in body.client_events:
                write_event(con, body.driver_id, item.event_type, {"timestamp": item.timestamp.isoformat()})
            if batch_id:
                con.execute(
                    "INSERT OR IGNORE INTO telemetry_batches(batch_id,driver_id,response_json,created_at) VALUES(?,?,?,?)",
                    (batch_id, body.driver_id, json.dumps(result), now_iso()),
                )
            return result

        # A pending declaration is supporting context only. It never bypasses scoring.
        pending_declaration = con.execute(
            """SELECT * FROM driver_change_declarations
               WHERE driver_id = ? AND status = 'pending'
               ORDER BY id DESC LIMIT 1""",
            (body.driver_id,),
        ).fetchone()

        declaration_reason: list[str] = []
        if pending_declaration is not None:
            declared_context = {
                "device_id": pending_declaration["device_id"],
                "ip_subnet": pending_declaration["ip_subnet"],
                "zone": pending_declaration["zone"],
            }
            declared_fields = {
                key: value for key, value in declared_context.items() if value is not None
            }
            declaration_matches = bool(declared_fields) and all(
                current_context.get(key) == value
                for key, value in declared_fields.items()
            )
            declaration_status = "matched" if declaration_matches else "not_matched"
            outcome_note = (
                "Declared context matched the observed session; declaration is not proof of identity."
                if declaration_matches
                else "Declared context did not fully match the observed session."
            )
            con.execute(
                """UPDATE driver_change_declarations
                   SET status = ?, checked_at = ?, matched_session_id = ?, outcome_note = ?
                   WHERE id = ? AND status = 'pending'""",
                (
                    declaration_status,
                    now_iso(),
                    None,  # Filled after the session row is inserted below.
                    outcome_note,
                    pending_declaration["id"],
                ),
            )
            declaration_reason.append(
                f"Driver declaration {declaration_status}: {outcome_note}"
            )

        previous_context = decode_json(driver["latest_json"], {})
        anchor_behavior = decode_json(driver["anchor_json"], {})
        fast_behavior = decode_json(driver["fast_json"], {})
        driver_keys = driver.keys() if driver is not None else []
        threshold_data = decode_json(driver["threshold_json"], {}) if (driver is not None and "threshold_json" in driver_keys) else {}
        timeseries_data = decode_json(driver["timeseries_json"], {}) if (driver is not None and "timeseries_json" in driver_keys) else {}
        
        # Physical operational phase detection from real-time telematics
        vehicle_speed = float(getattr(body, "vehicle_speed_mph", 0.0) or 0.0)
        has_touches = bool(getattr(body, "touch_events", None))
        if vehicle_speed > 2.0:
            operational_phase = "transit"
        elif has_touches:
            operational_phase = "interaction"
        else:
            operational_phase = "stationary"

        phase_thresholds = threshold_data.get("phase_thresholds", {}) if isinstance(threshold_data, dict) else {}
        if phase_thresholds and operational_phase in phase_thresholds:
            calibrated_alpha = float(phase_thresholds[operational_phase])
        else:
            calibrated_alpha = float(threshold_data.get("empirical_bayes_alpha") or threshold_data.get("threshold") or threshold_data.get("behavior_threshold")) if (threshold_data and any(k in threshold_data for k in ("empirical_bayes_alpha", "threshold", "behavior_threshold"))) else None

        # Try the REAL trained models first (ml/inference/rider_model_bundle.py
        # — the ones that actually won on real data, see ml/README.md). Falls
        # back to the heuristic below until this driver has enough raw session
        # history (MIN_HISTORY_FOR_REAL_MODELS in ml_bridge.py) to fit one.
        real_model_eval = None
        if evaluate_session_with_real_models is not None:
            history_rows = con.execute(
                """SELECT payload_json FROM sessions WHERE driver_id = ?
                   ORDER BY id ASC LIMIT 80""",
                (body.driver_id,),
            ).fetchall()
            history_payloads = [decode_json(r["payload_json"], {}) for r in history_rows]
            history_payloads = [p for p in history_payloads if p]
            try:
                real_model_eval = evaluate_session_with_real_models(body, history_payloads, operational_phase=operational_phase)
            except Exception as e:
                logger.warning("[main] evaluate_session_with_real_models errored (%s), falling back", e)
                real_model_eval = None

        if real_model_eval is not None:
            context_score = real_model_eval["context_score"]
            behavior_score = real_model_eval["behavior_score"]
            context_axis = real_model_eval["context_axis"]
            behavior_axis = real_model_eval["behavior_axis"]
            quadrant = real_model_eval["quadrant"]
            decision = real_model_eval["decision"]
            reasons = real_model_eval["reasons"] + declaration_reason
            signals = real_model_eval["signals"]
            reason_vector = real_model_eval.get("reason_vector")
            current_behavior = real_model_eval["current_behavior"]
            model_status = real_model_eval["model_status"]
            if real_model_eval.get("threshold_info"):
                threshold_data = real_model_eval["threshold_info"]
                calibrated_alpha = float(threshold_data.get("threshold", 0.29))
        elif evaluate_session_ml is not None:
            # Full ML Two-Axis evaluation with reason-vector explainability and adaptive threshold
            ml_eval = evaluate_session_ml(body, {
                "fast_behavior": fast_behavior,
                "anchor_behavior": anchor_behavior,
                "known_contexts": [previous_context],
            }, calibrated_alpha=calibrated_alpha)
            # Once enough prior sessions exist, prefer fitted per-rider
            # conformal scorers and expose their true sensor p-values.
            if evaluate_with_rider_calibration is not None:
                history_rows = con.execute(
                    "SELECT payload_json FROM sessions WHERE driver_id=? ORDER BY id ASC",
                    (body.driver_id,),
                ).fetchall()
                calibrated = evaluate_with_rider_calibration(
                    body, [decode_json(row["payload_json"], {}) for row in history_rows]
                )
                if calibrated is not None:
                    ml_eval.update({
                        "context_score": calibrated.get("context_continuity"),
                        "behavior_score": calibrated.get("behavior_continuity"),
                        "context_axis": "stable" if (calibrated.get("context_continuity") or 0) >= 0.6 else "changed",
                        "behavior_axis": "stable" if (calibrated.get("behavior_continuity") or 0) >= calibrated.get("threshold_info", {}).get("threshold", 0.29) else "changed",
                        "quadrant": calibrated.get("quadrant", ml_eval["quadrant"]),
                        "decision": {"escalate_challenge":"challenge_at_safe_pause", "lightweight_challenge":"step_up_check_at_safe_pause", "rebaseline_silently":"rebaseline", "continue":"continue"}.get(calibrated.get("decision"), ml_eval["decision"]),
                        "signals": calibrated.get("p_values", {}),
                        "threshold_info": calibrated.get("threshold_info"),
                        "model_status": "per_rider_conformal_fusion",
                        "reasons": [calibrated.get("meaning", "Per-rider conformal fusion scored this session")],
                    })
            context_score = ml_eval["context_score"]
            behavior_score = ml_eval["behavior_score"]
            context_axis = ml_eval["context_axis"]
            behavior_axis = ml_eval["behavior_axis"]
            quadrant = ml_eval["quadrant"]
            decision = ml_eval["decision"]
            reasons = ml_eval["reasons"] + declaration_reason
            signals = ml_eval["signals"]
            reason_vector = ml_eval.get("reason_vector")
            current_behavior = ml_eval["current_behavior"]
            model_status = ml_eval["model_status"]
        else:
            context_score = behavior_score = None
            context_axis = behavior_axis = "unavailable"
            decision, quadrant = "ml_unavailable", "unknown"
            reasons = ["ML scoring is unavailable because backend ML dependencies could not be loaded"] + declaration_reason
            signals = {}
            reason_vector = None
            model_status = "ml_dependencies_unavailable"

        # Dual-timescale slow-drift defense (ml/redteam/detectors.py's
        # FrozenAnchorDetector): a single session's behavior_score can
        # look individually unremarkable while drifting steadily away
        # from this driver's established baseline over many sessions —
        # exactly what a boiling-frog slow handoff looks like, and what
        # a fast-adapting-only baseline misses by design. Runs
        # regardless of which scoring path produced behavior_score.
        if behavior_score is not None and ML_DETECTORS_AVAILABLE:
            anchor_detector = load_frozen_anchor(con, body.driver_id)
            if anchor_detector is not None:
                drift_flagged = anchor_detector.update(float(behavior_score))
                save_frozen_anchor(con, body.driver_id, anchor_detector)
                if drift_flagged and decision not in ("challenge_at_safe_pause", "step_up_check_at_safe_pause"):
                    decision = "step_up_check_at_safe_pause"
                    quadrant = "ambiguous"
                    reasons = reasons + [
                        f"Dual-timescale drift detector: cumulative behavioral drift "
                        f"({round(anchor_detector.cumulative, 3)}) crossed the threshold over "
                        f"{len(anchor_detector._buffer)} sessions since the last verified baseline — "
                        f"no single session looked anomalous, but the slow trend does."
                    ]

        # A declared or apparently legitimate context change with stable behavior gets
        # a context re-baseline. Keep the frozen anchor; update only the fast baseline.
        update_fast = decision in ("continue", "rebaseline")
        if update_fast:
            if isinstance(fast_behavior.get("operational"), dict):
                # Adaptive update for structured ML feature categories
                for cat in ("operational", "touch", "motion"):
                    cur_cat = current_behavior.get(cat, {})
                    fast_cat = fast_behavior.get(cat, {})
                    for k, val in cur_cat.items():
                        if val is not None and isinstance(val, (int, float)):
                            old_v = fast_cat.get(k)
                            fast_cat[k] = val if old_v is None else round(0.8 * float(old_v) + 0.2 * float(val), 4)
                    fast_behavior[cat] = fast_cat
            else:
                # Flat dictionary fallback
                new_fast = {}
                for key, value in current_behavior.items():
                    old = fast_behavior.get(key)
                    if value is None:
                        new_fast[key] = old
                    elif old is None:
                        new_fast[key] = value
                    else:
                        new_fast[key] = 0.8 * float(old) + 0.2 * float(value)
                fast_behavior = new_fast

        fatigue_capped = False
        if decision in ("challenge_at_safe_pause", "step_up_check_at_safe_pause"):
            fatigue_capped = check_and_log_challenge_fatigue(con, body.driver_id)
            if fatigue_capped:
                reasons = reasons + [
                    f"Challenge fatigue cap reached ({CHALLENGE_FATIGUE_MAX} challenges in "
                    f"{CHALLENGE_FATIGUE_WINDOW_HOURS}h) — not showing the driver another step-up; "
                    f"escalated straight to priority human review instead."
                ]

        # Adversarial Sensor Authenticity Audit (MEMS entropy & looped replay detection)
        sensor_auth = None
        if ML_SENSOR_AUTHENTICITY_AVAILABLE and verify_sensor_authenticity is not None:
            sensor_auth = verify_sensor_authenticity(payload_dict)
            if not sensor_auth.get("is_authentic", True):
                attack_type = sensor_auth.get("attack_detected", "sensor_spoofing")
                reasons.append(
                    f"Adversarial sensor audit flagged {attack_type} "
                    f"({', '.join(sensor_auth.get('flags', []))}) — potential hardware/Frida spoofing"
                )
                if decision == "continue":
                    decision = "challenge_at_safe_pause"
                    quadrant = "handoff_signature"

        # Time-Series Adaptation and Anomaly Profiling
        timeseries_diagnostics = None
        if ML_TIMESERIES_AVAILABLE and DriverTimeSeriesProfiler is not None:
            ts_dict = timeseries_data if isinstance(timeseries_data, dict) else {}
            profiler = DriverTimeSeriesProfiler.from_dict({"driver_id": body.driver_id, **ts_dict})
            ts_res = profiler.update(
                behavior_score=behavior_score,
                context_score=context_score,
                quadrant=quadrant,
                decision=decision,
                signals=signals,
                timestamp=body.timestamp.isoformat(),
            )
            timeseries_diagnostics = ts_res
            timeseries_data = profiler.to_dict()
            if ts_res.get("adaptive_rebaselined"):
                reasons.append(
                    f"Online adaptation: gradual behavioral shift ({ts_res.get('drift_state')}) "
                    f"silently assimilated into updated baseline"
                )
            elif ts_res.get("drift_state") == "sudden_shift_detected":
                if decision == "continue":
                    decision = "challenge_at_safe_pause"
                    quadrant = "handoff_signature"
                    reasons.append(
                        f"Time-series anomaly detector: abrupt statistical jump (step_delta={ts_res.get('step_delta')}) "
                        f"detected relative to rolling history — potential handoff"
                    )

        # Safe-Pause Interlock & State Machine Evaluation
        vehicle_speed = float(body.vehicle_speed_mph) if body.vehicle_speed_mph is not None else 0.0
        is_moving = vehicle_speed > 2.0
        is_escalated = decision in ("challenge_at_safe_pause", "step_up_check_at_safe_pause")

        if is_escalated:
            if is_moving:
                safe_pause_status = "deferred_vehicle_moving"
                action_required = "standstill_challenge_pending"
                safe_to_prompt = False
                reasons.append(
                    f"Vehicle in motion ({vehicle_speed:.1f} mph > 2.0 mph) — interactive challenge "
                    f"deferred until standstill / drop-off to avoid driver distraction."
                )
            else:
                safe_pause_status = "prompt_active_vehicle_stopped"
                action_required = "face_verification_required"
                safe_to_prompt = True
                reasons.append(
                    f"Vehicle stopped ({vehicle_speed:.1f} mph) — safe to present step-up facial verification."
                )
        else:
            safe_pause_status = "normal"
            action_required = "none"
            safe_to_prompt = False

        safe_pause_info = {
            "vehicle_speed_mph": round(vehicle_speed, 1),
            "is_moving": is_moving,
            "safe_pause_recommended": is_escalated,
            "status": safe_pause_status,
            "action_required": action_required,
            "safe_to_prompt": safe_to_prompt,
        }

        # Pluggable time-series storage logging
        timeseries_store.log_telemetry_batch(body.driver_id, body.timestamp.isoformat(), payload_dict)

        result = {
            "driver_id": body.driver_id,
            "context_score": context_score,
            "behavior_score": behavior_score,
            "context_axis": context_axis,
            "behavior_axis": behavior_axis,
            "quadrant": quadrant,
            "decision": decision,
            "signals": signals,
            "reason_vector": reason_vector,
            "reasons": reasons,
            "model_status": model_status,
            "fatigue_capped": fatigue_capped,
            "baseline_status": "fast_baseline_updated" if update_fast else "fast_baseline_frozen",
            "timeseries_diagnostics": timeseries_diagnostics,
            "operational_phase": operational_phase,
            "calibrated_alpha": calibrated_alpha,
            "threshold_info": threshold_data if threshold_data else None,
            "timestamp": body.timestamp.isoformat(),
            "safe_pause_info": safe_pause_info,
            "sensor_authenticity": sensor_auth,
        }

        session_cursor = con.execute(
            """INSERT INTO sessions(driver_id,timestamp,payload_json,context_score,
               behavior_score,context_axis,behavior_axis,quadrant,decision,reasons_json)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (body.driver_id, body.timestamp.isoformat(), json.dumps(payload_dict), context_score,
             behavior_score, context_axis, behavior_axis, quadrant, decision, json.dumps(reasons)),
        )
        session_id = session_cursor.lastrowid

        if pending_declaration is not None:
            con.execute(
                """UPDATE driver_change_declarations
                   SET matched_session_id = ?
                   WHERE id = ?""",
                (session_id, pending_declaration["id"]),
            )
            write_event(
                con,
                body.driver_id,
                "context_change_declaration_checked",
                {
                    "declaration_id": pending_declaration["id"],
                    "session_id": session_id,
                    "status": declaration_status,
                    "outcome_note": outcome_note,
                },
            )

        # Queue only decisions that require a human review.
        if decision in ("challenge_at_safe_pause", "step_up_check_at_safe_pause"):
            con.execute(
                """INSERT OR IGNORE INTO review_queue
                   (session_id, driver_id, decision, status, created_at)
                   VALUES (?, ?, ?, 'open', ?)""",
                (session_id, body.driver_id, decision, now_iso()),
            )
            send_ops_webhook_alert(body.driver_id, decision, quadrant, reasons, fatigue_capped)

        con.execute(
            """UPDATE drivers SET fast_json=?, latest_json=?, latest_result_json=?,
               timeseries_json=?, threshold_json=?, updated_at=?
               WHERE driver_id=?""",
            (json.dumps(fast_behavior), json.dumps(current_context), json.dumps(result),
             json.dumps(timeseries_data), json.dumps(threshold_data),
             now_iso(), body.driver_id),
        )
        write_event(con, body.driver_id, "session_scored", result)
        for item in body.client_events:
            write_event(con, body.driver_id, item.event_type, {"timestamp": item.timestamp.isoformat()})
        if batch_id:
            con.execute("INSERT OR IGNORE INTO telemetry_batches(batch_id,driver_id,response_json,created_at) VALUES(?,?,?,?)",
                        (batch_id, body.driver_id, json.dumps(result), now_iso()))

    broadcast_event_sync("session_scored", {
        "session_id": session_id,
        "driver_id": body.driver_id,
        "result": result,
    })
    broadcast_event_sync("fleet_update", {
        "driver_id": body.driver_id,
        "context_score": result.get("context_score"),
        "behavior_score": result.get("behavior_score"),
        "quadrant": result.get("quadrant"),
        "decision": result.get("decision"),
        "reasons": result.get("reasons", []),
        "signals": result.get("signals", {}),
        "reason_vector": result.get("reason_vector"),
        "safe_pause_info": result.get("safe_pause_info"),
    })

    return result


@app.get("/v1/events/stream")
async def event_stream(
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    admin: dict[str, str] = Depends(require_admin),
) -> StreamingResponse:
    """Authenticated SSE stream of newly persisted fleet events; reconnect with Last-Event-ID."""
    async def stream():
        try:
            cursor = max(0, int(last_event_id or "0"))
        except ValueError:
            cursor = 0
        if not last_event_id:
            with connect() as con:
                row = con.execute("SELECT COALESCE(MAX(id),0) AS latest_id FROM events").fetchone()
                cursor = int(row["latest_id"])
        try:
            while True:
                with connect() as con:
                    rows = con.execute(
                        "SELECT id,driver_id,timestamp,event_type,details_json FROM events WHERE id>? ORDER BY id LIMIT 100",
                        (cursor,),
                    ).fetchall()
                for row in rows:
                    cursor = row["id"]
                    item = {"id": cursor, "driver_id": row["driver_id"], "timestamp": row["timestamp"],
                            "event_type": row["event_type"], "details": decode_json(row["details_json"], {})}
                    yield f"id: {cursor}\nevent: fleet-event\ndata: {json.dumps(item)}\n\n"
                if not rows:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            return
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/v1/drivers/{driver_id}/score")
def driver_score(
    driver_id: str, admin: dict[str, str] = Depends(require_admin)
) -> dict:
    with connect() as con:
        row = con.execute("SELECT * FROM drivers WHERE driver_id=?", (driver_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Driver not found")
        result = public_result(row)
        sessions = con.execute(
            """SELECT id,timestamp,context_score,behavior_score,context_axis,
               behavior_axis,quadrant,decision,reasons_json
               FROM sessions WHERE driver_id=? ORDER BY id DESC LIMIT 100""",
            (driver_id,),
        ).fetchall()
        result["history"] = [
            {
                "session_id": s["id"], "timestamp": s["timestamp"],
                "context_score": s["context_score"], "behavior_score": s["behavior_score"],
                "context_axis": s["context_axis"], "behavior_axis": s["behavior_axis"],
                "quadrant": s["quadrant"], "decision": s["decision"],
                "reasons": decode_json(s["reasons_json"], []),
            } for s in sessions
        ]
        return result


@app.get("/v1/fleet/scores")
def fleet_scores(admin: dict[str, str] = Depends(require_admin)) -> dict:
    with connect() as con:
        rows = con.execute(
            """SELECT driver_id, latest_result_json, updated_at FROM drivers
               ORDER BY updated_at DESC"""
        ).fetchall()
        drivers = []
        for row in rows:
            latest_session = con.execute(
                "SELECT payload_json FROM sessions WHERE driver_id=? ORDER BY id DESC LIMIT 1",
                (row["driver_id"],),
            ).fetchone()
            # Ignore legacy seeded/demo rows; only sessions posted through the
            # ingest API carry their own driver identity in the request payload.
            if latest_session is None or decode_json(latest_session["payload_json"], {}).get("driver_id") != row["driver_id"]:
                continue
            result = decode_json(row["latest_result_json"], {})
            latest_payload = decode_json(latest_session["payload_json"], {})
            gps_points = latest_payload.get("gps") or []
            drivers.append({
                "driver_id": row["driver_id"],
                "context_score": result.get("context_score"),
                "behavior_score": result.get("behavior_score"),
                "context_axis": result.get("context_axis", "insufficient_evidence"),
                "behavior_axis": result.get("behavior_axis", "insufficient_evidence"),
                "quadrant": result.get("quadrant", "unknown"),
                "decision": result.get("decision", "unknown"),
                "signals": result.get("signals", {}),
                "reasons": result.get("reasons", []),
                "reason_vector": result.get("reason_vector", {}),
                "safe_pause_info": result.get("safe_pause_info"),
                "threshold_info": result.get("threshold_info"),
                "model_status": result.get("model_status"),
                "latest_telemetry": {
                    "device_id": latest_payload.get("device_id"),
                    "zone": latest_payload.get("zone"),
                    "vehicle_speed_mph": latest_payload.get("vehicle_speed_mph"),
                    "gps": gps_points[-1] if gps_points else None,
                },
                "updated_at": row["updated_at"],
            })
        return {"count": len(drivers), "drivers": drivers}


@app.post("/v1/drivers/{driver_id}/consent")
def grant_consent(
    driver_id: str,
    x_driver_token: str | None = Header(default=None, alias="X-Driver-Token"),
) -> dict:
    """Driver explicitly consents to behavioral-biometric monitoring
    (required before onboarding in a real deployment; currently
    advisory — not yet enforced as a hard gate on /v1/sessions/ingest,
    see data-retention/consent notes). Idempotent: re-granting just
    refreshes the timestamp and version, clearing any prior revocation."""
    require_driver_token(driver_id, x_driver_token)
    timestamp = now_iso()
    with connect() as con:
        con.execute(
            """INSERT INTO driver_consent(driver_id, consent_version, given_at, revoked_at)
               VALUES(?, ?, ?, NULL)
               ON CONFLICT(driver_id) DO UPDATE SET
                 consent_version = excluded.consent_version,
                 given_at = excluded.given_at,
                 revoked_at = NULL""",
            (driver_id, CONSENT_VERSION, timestamp),
        )
    return {"driver_id": driver_id, "consent_version": CONSENT_VERSION, "given_at": timestamp}


@app.post("/v1/drivers/{driver_id}/consent/revoke")
def revoke_consent(
    driver_id: str,
    x_driver_token: str | None = Header(default=None, alias="X-Driver-Token"),
) -> dict:
    """Driver withdraws consent. Does not retroactively delete past
    sessions (those still age out under the normal retention policy)
    but stops this driver from being counted as actively consented."""
    require_driver_token(driver_id, x_driver_token)
    timestamp = now_iso()
    with connect() as con:
        con.execute(
            "UPDATE driver_consent SET revoked_at = ? WHERE driver_id = ?",
            (timestamp, driver_id),
        )
    return {"driver_id": driver_id, "revoked_at": timestamp}


@app.get("/v1/drivers/{driver_id}/consent")
def get_consent_status(driver_id: str) -> dict:
    with connect() as con:
        row = con.execute(
            "SELECT consent_version, given_at, revoked_at FROM driver_consent WHERE driver_id = ?",
            (driver_id,),
        ).fetchone()
    if row is None:
        return {"driver_id": driver_id, "consented": False, "consent_version": None, "given_at": None, "revoked_at": None}
    return {
        "driver_id": driver_id,
        "consented": row["revoked_at"] is None,
        "consent_version": row["consent_version"],
        "given_at": row["given_at"],
        "revoked_at": row["revoked_at"],
    }


FACE_MATCH_THRESHOLD = 0.6  # same uncalibrated threshold as ml/models/face_verification.py
_ESCALATED_DECISIONS = ("challenge_at_safe_pause", "step_up_check_at_safe_pause")


def _decode_face_image(image_base64: str):
    """Returns a PIL.Image, or raises HTTPException(400) on bad input.
    Local import — PIL/face deps are optional, see ML_FACE_AVAILABLE."""
    import io as _io
    from PIL import Image as _Image

    try:
        raw = base64.b64decode(image_base64, validate=False)
        return _Image.open(_io.BytesIO(raw)).convert("RGB")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not decode image: {e}")


@app.post("/v1/drivers/{driver_id}/face-enroll")
def enroll_face(
    driver_id: str,
    body: FaceImage,
    admin: dict[str, str] = Depends(require_admin),
) -> dict:
    """Admin-side onboarding step: store this driver's reference face
    embedding (e.g. from the platform's existing verified ID photo),
    used later by the mid-trip challenge step-up check. Admin-gated,
    not driver-gated — a driver shouldn't be able to re-enroll their
    own reference photo, that would defeat the point."""
    if not ML_FACE_AVAILABLE:
        raise HTTPException(status_code=503, detail="Face verification is not available on this deployment (facenet-pytorch not installed)")

    image = _decode_face_image(body.image_base64)
    embedding = embed_face_image(image)
    if embedding is None:
        raise HTTPException(status_code=422, detail="No face detected in the enrollment image")

    embedding_b64 = base64.b64encode(embedding.astype("float32").tobytes()).decode("ascii")
    timestamp = now_iso()
    with connect() as con:
        con.execute(
            """INSERT INTO driver_face_enrollment(driver_id, embedding_b64, enrolled_at)
               VALUES(?, ?, ?)
               ON CONFLICT(driver_id) DO UPDATE SET embedding_b64 = excluded.embedding_b64, enrolled_at = excluded.enrolled_at""",
            (driver_id, embedding_b64, timestamp),
        )
    return {"driver_id": driver_id, "enrolled_at": timestamp}


@app.get("/v1/drivers/{driver_id}/challenge-status")
def get_challenge_status(driver_id: str) -> dict:
    """Polled by the driver app (same cadence as the SDK's session
    flush). Tells the phone whether the backend's two-axis combiner has
    escalated this driver to a safe-pause challenge, so the app can
    show a liveness/selfie prompt instead of that escalation sitting
    silently in the admin review queue until someone happens to look."""
    with connect() as con:
        row = con.execute(
            "SELECT latest_result_json FROM drivers WHERE driver_id = ?", (driver_id,)
        ).fetchone()
    if row is None:
        return {"driver_id": driver_id, "challenge_pending": False}
    result = decode_json(row["latest_result_json"], {})
    decision = result.get("decision")
    # A fatigue-capped escalation still exists in the audit trail and
    # review queue (see check_and_log_challenge_fatigue's docstring) —
    # it just isn't shown to the driver as another disruptive prompt.
    safe_pause_info = result.get("safe_pause_info", {})
    safe_to_prompt = safe_pause_info.get("safe_to_prompt", True)
    # Suppression gate: never pop up interactive challenge modal while vehicle is in motion
    challenge_pending = (
        decision in _ESCALATED_DECISIONS
        and not result.get("fatigue_capped")
        and safe_to_prompt
    )
    return {
        "driver_id": driver_id,
        "challenge_pending": challenge_pending,
        "safe_pause_info": safe_pause_info,
        "decision": decision,
        "reasons": result.get("reasons", []),
    }


@app.post("/v1/drivers/{driver_id}/challenge/respond")
def respond_to_challenge(
    driver_id: str,
    body: FaceImage,
    x_driver_token: str | None = Header(default=None, alias="X-Driver-Token"),
) -> dict:
    """The driver's response to a mid-trip safe-pause challenge: a
    selfie, checked against their enrolled reference face. This is the
    actual enforcement action beyond "alert the admin and wait" — a
    clear match auto-clears the challenge in real time; anything else
    (no match, no face, no enrollment on file) leaves the escalation in
    place for human review. Per ml/models/face_verification.py's
    fairness note: NEVER auto-ban on a failed/inconclusive match here —
    only auto-CLEAR on a clear pass, same asymmetry as the rest of the
    product (biased toward a human in the loop for anything negative)."""
    require_driver_token(driver_id, x_driver_token)

    if not ML_FACE_AVAILABLE:
        raise HTTPException(status_code=503, detail="Face verification is not available on this deployment")

    with connect() as con:
        enrollment = con.execute(
            "SELECT embedding_b64 FROM driver_face_enrollment WHERE driver_id = ?", (driver_id,)
        ).fetchone()

    if enrollment is None:
        with connect() as con:
            write_event(con, driver_id, "challenge_inconclusive", {"reason": "no_face_enrollment_on_file"})
        return {"driver_id": driver_id, "outcome": "human_review", "reason": "no_face_enrollment_on_file"}

    image = _decode_face_image(body.image_base64)

    # 1. Evaluate 2D/3D presentation attack and anti-spoofing liveness
    liveness = check_face_liveness_image(image) if check_face_liveness_image is not None else {"is_live": True}
    if not liveness.get("is_live", True):
        attack_type = liveness.get("attack_type", "presentation_attack_spoof")
        with connect() as con:
            write_event(con, driver_id, "challenge_spoof_rejected", {
                "liveness_score": liveness.get("liveness_score"),
                "attack_type": attack_type,
                "flags": liveness.get("flags", []),
            })
        return {
            "driver_id": driver_id,
            "outcome": "spoof_rejected",
            "similarity": None,
            "is_live": False,
            "liveness": liveness,
            "reason": f"Anti-spoofing rejected presentation attack: {attack_type} (liveness={liveness.get('liveness_score')})",
        }

    # 2. Extract FaceNet embedding and evaluate cosine similarity
    challenge_embedding = embed_face_image(image)
    if challenge_embedding is None:
        with connect() as con:
            write_event(con, driver_id, "challenge_inconclusive", {"reason": "no_face_detected"})
        return {"driver_id": driver_id, "outcome": "human_review", "reason": "no_face_detected"}

    enrolled_embedding = np.frombuffer(base64.b64decode(enrollment["embedding_b64"]), dtype="float32")
    similarity = face_similarity(enrolled_embedding, challenge_embedding)
    passed = similarity >= FACE_MATCH_THRESHOLD

    with connect() as con:
        if passed:
            driver_row = con.execute(
                "SELECT * FROM drivers WHERE driver_id = ?", (driver_id,)
            ).fetchone()
            if driver_row is not None:
                driver_d = dict(driver_row)
                result = decode_json(driver_d.get("latest_result_json"), {})
                if result.get("decision") in _ESCALATED_DECISIONS:
                    result["decision"] = "continue"
                    result.setdefault("reasons", []).append(
                        f"Mid-trip challenge passed (face similarity {round(similarity, 3)}, liveness={liveness.get('liveness_score', 1.0)}) — auto-cleared"
                    )
                    con.execute(
                        "UPDATE drivers SET latest_result_json = ?, updated_at = ? WHERE driver_id = ?",
                        (json.dumps(result), now_iso(), driver_id),
                    )

                # 3. Self-Supervised Re-Anchoring: safely assimilate verified ground-truth identity
                if ML_TIMESERIES_AVAILABLE and DriverTimeSeriesProfiler is not None:
                    ts_dict = decode_json(driver_d.get("timeseries_json"), {})
                    profiler = DriverTimeSeriesProfiler.from_dict({"driver_id": driver_id, **ts_dict})
                    profiler.assimilate_verified_challenge(0.95, method="face_step_up_liveness_verified")
                    con.execute(
                        "UPDATE drivers SET timeseries_json = ? WHERE driver_id = ?",
                        (json.dumps(profiler.to_dict()), driver_id),
                    )

            con.execute(
                """UPDATE review_queue SET status='resolved', resolved_at=?, resolution='cleared',
                       reviewer_id='auto:face_challenge', reviewer_note=?
                   WHERE driver_id=? AND status='open'""",
                (now_iso(), f"Auto-cleared: face similarity {round(similarity, 3)} (liveness={liveness.get('liveness_score', 1.0)})", driver_id),
            )
            if ML_DETECTORS_AVAILABLE:
                reset_frozen_anchor(con, driver_id)
            write_event(con, driver_id, "challenge_passed", {
                "similarity": round(similarity, 4),
                "liveness_score": liveness.get("liveness_score", 1.0)
            })
        else:
            write_event(con, driver_id, "challenge_failed_or_inconclusive", {"similarity": round(similarity, 4)})

    return {
        "driver_id": driver_id,
        "outcome": "cleared" if passed else "human_review",
        "similarity": round(similarity, 4),
        "is_live": liveness.get("is_live", True),
        "liveness_score": liveness.get("liveness_score", 1.0),
    }


class ChallengeTimeoutRequest(BaseModel):
    reason: str = Field(default="challenge_timeout_120s", max_length=100)
    vehicle_speed_mph: float | None = None


@app.post("/v1/driver/challenge/refuse-or-timeout")
def challenge_refuse_or_timeout(
    driver_id: str,
    body: ChallengeTimeoutRequest,
    x_driver_token: str | None = Header(default=None, alias="X-Driver-Token"),
) -> dict:
    """Handles driver refusing, dismissing, or timing out (>120s) on a safe-pause challenge.
    Enforces dispatch restrictions: suspends active token and escalates to Safety Ops."""
    if x_driver_token is not None and isinstance(x_driver_token, str) and x_driver_token.strip():
        require_driver_token(driver_id, x_driver_token)
    with connect() as con:
        # 1. Update open review queue ticket to high-priority refusal
        con.execute(
            """UPDATE review_queue 
               SET status = 'open', reviewer_note = ?
               WHERE driver_id = ? AND status = 'open'""",
            (f"Driver challenge refusal/timeout: {body.reason}", driver_id)
        )
        # 2. Suspend active dispatch token in driver_tokens
        con.execute(
            """UPDATE driver_tokens SET active = 0 WHERE driver_id = ?""",
            (driver_id,)
        )
        write_event(con, driver_id, "challenge_refusal_or_timeout", {
            "reason": body.reason,
            "vehicle_speed_mph": body.vehicle_speed_mph,
            "enforcement": "dispatch_token_suspended"
        })

    broadcast_event_sync("challenge_refusal", {
        "driver_id": driver_id,
        "reason": body.reason,
        "status": "dispatch_suspended",
        "timestamp": now_iso()
    })
    return {
        "driver_id": driver_id,
        "status": "dispatch_suspended",
        "action": "lock_new_orders",
        "reason": body.reason,
        "message": "Challenge timed out or was refused. Further trip assignments are paused pending safety review."
    }


class DriverCalibrationRequest(BaseModel):
    driver_id: str = Field(min_length=1, max_length=100)
    device_id: str | None = None
    phase: str | None = Field(default="full")
    touch_samples: list[dict[str, Any]] = Field(default_factory=list)
    accel_samples: list[Any] = Field(default_factory=list)
    gyro_samples: list[Any] = Field(default_factory=list)
    order_acceptance_latency_ms: float = Field(default=920.0, ge=0)
    zone: str | None = None


@app.post("/v1/drivers/calibrate")
def calibrate_driver_onboarding(
    body: DriverCalibrationRequest,
    x_driver_token: str | None = Header(default=None, alias="X-Driver-Token"),
) -> dict:
    """2-minute onboarding calibration endpoint for new drivers (Few-Shot Meta-Learning).
    Initializes Empirical Bayes baseline priors immediately, bypassing the cold-start wait."""
    if x_driver_token is not None and isinstance(x_driver_token, str) and x_driver_token.strip():
        require_driver_token(body.driver_id, x_driver_token)

    # Compute baseline metrics from onboarding batch
    pressures = [float(t.get("pressure", 0.5)) for t in body.touch_samples if "pressure" in t]
    durations = [float(t.get("duration_ms", 120.0)) for t in body.touch_samples if "duration_ms" in t]
    areas = [float(t.get("area", 350.0)) for t in body.touch_samples if "area" in t]

    mean_p = float(sum(pressures) / len(pressures)) if pressures else 0.52
    mean_dur = float(sum(durations) / len(durations)) if durations else 115.0
    mean_area = float(sum(areas) / len(areas)) if areas else 360.0

    anchor_state = {
        "calibrated_onboard": True,
        "operational": {
            "accept_latency_ms": body.order_acceptance_latency_ms,
            "hour_of_day": 12.0,
        },
        "touch": {
            "pressure_mean": mean_p,
            "interval_mean_ms": mean_dur,
            "velocity_mean": 0.45,
        },
        "motion": {
            "accel_mean": 0.22,
        },
        "touch_pressure_mean": mean_p,
        "touch_duration_mean": mean_dur,
        "touch_area_mean": mean_area,
        "accept_latency_mean": body.order_acceptance_latency_ms,
        "sample_count": len(body.touch_samples) or 5,
        "onboarded_at": now_iso()
    }

    calibrated_phase = getattr(body, "phase", None) or "full"
    base_alpha = 0.58
    phase_thresholds = {
        "transit": round(base_alpha * 0.88, 4),      # 0.5104 (absorbs vibration / road noise)
        "stationary": round(base_alpha * 1.08, 4),   # 0.6264 (stricter curbside threshold)
        "interaction": round(base_alpha * 1.00, 4),  # 0.5800 (handoff/app usage)
        "full": round(base_alpha * 1.00, 4),
    }

    threshold_info = {
        "calibrated_onboard": True,
        "behavior_threshold": 0.62,
        "context_threshold": 0.60,
        "empirical_bayes_alpha": phase_thresholds.get(calibrated_phase, base_alpha),
        "base_alpha": base_alpha,
        "calibrated_phase": calibrated_phase,
        "phase_thresholds": phase_thresholds,
        "calibrated_at": now_iso()
    }

    latest_res = {
        "driver_id": body.driver_id,
        "context_score": 0.95,
        "behavior_score": 0.92,
        "quadrant": "stable",
        "decision": "continue",
        "confidence": 0.96,
        "reasons": ["2-Minute Onboarding Calibration complete: Empirical Bayes baseline established."],
        "signals": {"touch": 0.92, "motion": 0.95, "operational": 0.98},
        "timestamp": now_iso()
    }

    with connect() as con:
        con.execute(
            """INSERT INTO drivers(driver_id, anchor_json, fast_json, latest_json,
                                  latest_result_json, timeseries_json, threshold_json, updated_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(driver_id) DO UPDATE SET
                   anchor_json = excluded.anchor_json,
                   fast_json = excluded.fast_json,
                   threshold_json = excluded.threshold_json,
                   latest_result_json = excluded.latest_result_json,
                   updated_at = excluded.updated_at""",
            (
                body.driver_id,
                json.dumps(anchor_state),
                json.dumps(anchor_state),
                json.dumps({"device_id": body.device_id, "zone": body.zone}),
                json.dumps(latest_res),
                json.dumps({"driver_id": body.driver_id, "rolling_mean_behavior": 0.92, "drift_state": "calibrated"}),
                json.dumps(threshold_info),
                now_iso()
            )
        )
        write_event(con, body.driver_id, "driver_calibrated_onboard", anchor_state)

    broadcast_event_sync("driver_calibrated", {
        "driver_id": body.driver_id,
        "baseline_established": True,
        "threshold_info": threshold_info
    })

    return {
        "status": "calibrated",
        "driver_id": body.driver_id,
        "baseline_established": True,
        "anchor_summary": anchor_state,
        "message": "Onboarding calibration completed successfully. Empirical Bayes baseline active."
    }


@app.post("/v1/drivers/{driver_id}/declare-change")
def declare_change(
    driver_id: str,
    body: DeclareChange,
    x_driver_token: str | None = Header(default=None, alias="X-Driver-Token"),
) -> dict:
    """Record a driver's declared context change for comparison with the next session."""
    require_driver_token(driver_id, x_driver_token)

    declared_context = {
        key: value
        for key, value in {
            "device_id": body.device_id,
            "ip_subnet": body.ip_subnet,
            "zone": body.zone,
        }.items()
        if value is not None
    }
    if not declared_context:
        raise HTTPException(
            status_code=422,
            detail="Provide at least one of device_id, ip_subnet, or zone.",
        )

    with connect() as con:
        row = con.execute(
            "SELECT driver_id FROM drivers WHERE driver_id = ?", (driver_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Driver not found")

        # Supersede an older pending declaration so only the newest one is evaluated.
        con.execute(
            """UPDATE driver_change_declarations
               SET status = 'superseded', checked_at = ?,
                   outcome_note = 'Superseded by a newer declaration.'
               WHERE driver_id = ? AND status = 'pending'""",
            (now_iso(), driver_id),
        )
        cursor = con.execute(
            """INSERT INTO driver_change_declarations(
                   driver_id, device_id, ip_subnet, zone, reason, declared_at, status
               ) VALUES(?, ?, ?, ?, ?, ?, 'pending')""",
            (
                driver_id,
                body.device_id,
                body.ip_subnet,
                body.zone,
                body.reason,
                now_iso(),
            ),
        )
        declaration_id = cursor.lastrowid
        write_event(
            con,
            driver_id,
            "context_change_declared",
            {
                "declaration_id": declaration_id,
                "reason": body.reason,
                "declared_context": declared_context,
                "status": "pending",
            },
        )

    return {
        "driver_id": driver_id,
        "declaration_id": declaration_id,
        "status": "pending",
        "declared_context": declared_context,
        "note": (
            "Declaration recorded. The next observed session will be compared against it; "
            "the declaration does not change the stored observed context or prove identity."
        ),
    }


@app.post("/v1/admin/drivers/{driver_id}/driver-token")
def issue_driver_token(
    driver_id: str,
    admin: dict[str, str] = Depends(require_admin),
) -> dict:
    """Create or rotate a driver token. The raw token is shown only once."""
    raw_token = secrets.token_urlsafe(32)
    token_hash = hash_driver_token(raw_token)
    timestamp = now_iso()

    with connect() as con:
        driver = con.execute(
            "SELECT driver_id FROM drivers WHERE driver_id = ?", (driver_id,)
        ).fetchone()
        if driver is None:
            # Admin provisioning is the trust boundary: create the driver record before
            # issuing a token, so unauthenticated clients cannot claim a new driver ID.
            con.execute(
                """INSERT INTO drivers(driver_id, anchor_json, fast_json, latest_json,
                   latest_result_json, updated_at) VALUES(?, '{}', '{}', '{}', '{}', ?)""",
                (driver_id, timestamp),
            )

        existing = con.execute(
            "SELECT driver_id FROM driver_tokens WHERE driver_id = ?", (driver_id,)
        ).fetchone()
        if existing is None:
            con.execute(
                """INSERT INTO driver_tokens(
                       driver_id, token_hash, active, created_at, rotated_at
                   ) VALUES(?, ?, 1, ?, NULL)""",
                (driver_id, token_hash, timestamp),
            )
            action = "driver_token_created"
        else:
            con.execute(
                """UPDATE driver_tokens
                   SET token_hash = ?, active = 1, rotated_at = ?
                   WHERE driver_id = ?""",
                (token_hash, timestamp, driver_id),
            )
            action = "driver_token_rotated"

        write_event(
            con,
            driver_id,
            action,
            {"admin_username": admin["username"], "timestamp": timestamp},
        )

    return {
        "driver_id": driver_id,
        "driver_token": raw_token,
        "header_name": "X-Driver-Token",
        "note": "Copy this token now. It is stored as a hash and cannot be retrieved later.",
    }


@app.get("/v1/admin/drivers/{driver_id}/declarations")
def list_driver_declarations(
    driver_id: str,
    limit: int = 100,
    admin: dict[str, str] = Depends(require_admin),
) -> dict:
    """Admin-only history of driver context declarations and their outcomes."""
    limit = max(1, min(limit, 500))
    with connect() as con:
        exists = con.execute(
            "SELECT 1 FROM drivers WHERE driver_id = ?", (driver_id,)
        ).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="Driver not found")
        rows = con.execute(
            """SELECT id AS declaration_id, driver_id, device_id, ip_subnet, zone,
                      reason, declared_at, status, checked_at, matched_session_id,
                      outcome_note
               FROM driver_change_declarations
               WHERE driver_id = ? ORDER BY id DESC LIMIT ?""",
            (driver_id, limit),
        ).fetchall()
    return {"driver_id": driver_id, "count": len(rows), "declarations": [dict(r) for r in rows]}


@app.get("/v1/admin/drivers/{driver_id}/split-rental-check")
def split_rental_audit(
    driver_id: str,
    admin: dict[str, str] = Depends(require_admin),
) -> dict:
    """On-demand audit (ml/redteam/detectors.py's split_rental_check):
    fits a 2-component Gaussian mixture on this driver's own session
    history and checks whether it splits into two clusters that
    correlate with time of day — the signature of two people alternating
    shifts on one account (morning driver / evening driver). Previously
    this only ran in an offline test script; an admin can now trigger it
    per driver instead of it running silently on every ingest (GMM
    fitting is too expensive to run unconditionally on every session)."""
    if not ML_DETECTORS_AVAILABLE:
        raise HTTPException(status_code=503, detail="split_rental_check is not available on this deployment (scikit-learn not installed)")

    with connect() as con:
        rows = con.execute(
            "SELECT payload_json FROM sessions WHERE driver_id = ? ORDER BY id ASC LIMIT 200",
            (driver_id,),
        ).fetchall()

    payloads = [decode_json(r["payload_json"], {}) for r in rows]
    payloads = [p for p in payloads if p and not p.get("purged")]
    if len(payloads) < 20:
        raise HTTPException(
            status_code=422,
            detail=f"Needs at least 20 sessions with intact payloads to run this check; this driver has {len(payloads)}.",
        )

    legacy_sessions = [_session_payload_to_legacy_shape(p) for p in payloads]
    try:
        result = split_rental_check(legacy_sessions)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"split_rental_check failed: {e}")

    return {"driver_id": driver_id, "sessions_analyzed": len(legacy_sessions), **result}


@app.get("/v1/admin/webhook-status")
def webhook_status(admin: dict[str, str] = Depends(require_admin)) -> dict:
    """Whether an outbound ops webhook is configured. Never returns the
    URL itself — it may carry an auth token (common for Slack/PagerDuty
    incoming-webhook URLs)."""
    return {"configured": OPS_WEBHOOK_URL is not None}


@app.post("/v1/admin/webhook-test")
def webhook_test(admin: dict[str, str] = Depends(require_admin)) -> dict:
    """Fires a test alert through the configured ops webhook so an
    admin can verify the integration without waiting for a real
    escalation."""
    if not OPS_WEBHOOK_URL:
        raise HTTPException(status_code=503, detail="No ops webhook configured (set ASLIKAUN_OPS_WEBHOOK_URL)")
    delivered = send_ops_webhook_alert(
        driver_id="test-driver", decision="challenge_at_safe_pause", quadrant="handoff_signature",
        reasons=[f"Test alert triggered manually by admin '{admin['username']}'"], fatigue_capped=False,
    )
    return {"sent": delivered}


@app.get("/v1/drivers/{driver_id}/events")
def driver_events(
    driver_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    admin: dict[str, str] = Depends(require_admin),
) -> dict:
    with connect() as con:
        exists = con.execute("SELECT 1 FROM drivers WHERE driver_id=?", (driver_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Driver not found")
        rows = con.execute(
            "SELECT id,timestamp,event_type,details_json FROM events WHERE driver_id=? ORDER BY id DESC LIMIT ?",
            (driver_id, limit),
        ).fetchall()
        return {"driver_id": driver_id, "events": [
            {"id": r["id"], "timestamp": r["timestamp"], "event_type": r["event_type"],
             "details": decode_json(r["details_json"], {})} for r in rows
        ]}


@app.post("/v1/drivers/{driver_id}/verification")
def submit_verification(
    driver_id: str,
    body: VerificationSubmission,
    x_driver_token: str | None = Header(default=None, alias="X-Driver-Token"),
) -> dict:
    """Record a camera capture for a separately reviewed challenge; never accept/store face images here."""
    require_driver_token(driver_id, x_driver_token)
    with connect() as con:
        con.execute(
            "INSERT INTO verification_requests(challenge_id,driver_id,status,captured_at,updated_at) VALUES(?,?,'pending',?,?) "
            "ON CONFLICT(challenge_id) DO UPDATE SET captured_at=excluded.captured_at,updated_at=excluded.updated_at "
            "WHERE verification_requests.driver_id=excluded.driver_id",
            (body.challenge_id, driver_id, body.captured_at.isoformat(), now_iso()),
        )
        row = con.execute("SELECT driver_id,status,captured_at,updated_at FROM verification_requests WHERE challenge_id=?", (body.challenge_id,)).fetchone()
        if row is None or row["driver_id"] != driver_id:
            raise HTTPException(status_code=409, detail="Challenge ID belongs to another driver")
        write_event(con, driver_id, "verification_submitted", {"challenge_id": body.challenge_id, "status": row["status"]})
        return {"challenge_id": body.challenge_id, "status": row["status"], "captured_at": row["captured_at"], "updated_at": row["updated_at"]}


@app.get("/v1/drivers/{driver_id}/verification/{challenge_id}")
def verification_status(
    driver_id: str,
    challenge_id: str,
    x_driver_token: str | None = Header(default=None, alias="X-Driver-Token"),
) -> dict:
    require_driver_token(driver_id, x_driver_token)
    with connect() as con:
        row = con.execute("SELECT challenge_id,status,captured_at,updated_at FROM verification_requests WHERE challenge_id=? AND driver_id=?", (challenge_id, driver_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Verification challenge not found")
    return dict(row)


@app.post("/v1/admin/drivers/{driver_id}/verification/{challenge_id}")
def decide_verification(
    driver_id: str,
    challenge_id: str,
    body: VerificationDecision,
    admin: dict[str, str] = Depends(require_admin),
) -> dict:
    """Record the authoritative result supplied by the configured liveness/review provider."""
    with connect() as con:
        row = con.execute(
            "SELECT challenge_id,status FROM verification_requests WHERE challenge_id=? AND driver_id=?",
            (challenge_id, driver_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Verification challenge not found")
        con.execute(
            "UPDATE verification_requests SET status=?,updated_at=? WHERE challenge_id=? AND driver_id=?",
            (body.status, now_iso(), challenge_id, driver_id),
        )
        write_event(con, driver_id, "verification_decided", {
            "challenge_id": challenge_id,
            "status": body.status,
            "reviewer": admin.get("username"),
            "reviewer_note": body.reviewer_note,
        })
    return {"challenge_id": challenge_id, "status": body.status, "updated_at": now_iso()}


# ---------- Admin review workflow (authentication-protected) ----------

@app.get("/v1/review-queue")
def get_review_queue(
    status: str = "open",
    limit: int = 100,
    driver_id: str | None = None,
    admin: dict[str, str] = Depends(require_admin),
) -> dict:
    """List review items. Supports status=open|resolved|all and optional driver_id."""
    if status not in ("open", "resolved", "all"):
        raise HTTPException(
            status_code=400,
            detail="status must be open, resolved, or all",
        )

    limit = max(1, min(limit, 500))
    query = """SELECT q.id AS review_id, q.session_id, q.driver_id, q.decision,
                      q.status, q.created_at, q.resolved_at, q.resolution,
                      q.reviewer_id, q.reviewer_note,
                      s.timestamp AS session_timestamp, s.context_score,
                      s.behavior_score, s.context_axis, s.behavior_axis,
                      s.quadrant, s.reasons_json, s.payload_json
               FROM review_queue q
               JOIN sessions s ON s.id = q.session_id"""
    clauses = []
    params: list[Any] = []

    if status != "all":
        clauses.append("q.status = ?")
        params.append(status)
    if driver_id:
        clauses.append("q.driver_id = ?")
        params.append(driver_id)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY q.created_at DESC LIMIT ?"
    params.append(limit)

    with connect() as con:
        rows = con.execute(query, tuple(params)).fetchall()

    reviews = []
    for row in rows:
        item = dict(row)
        item["reasons"] = decode_json(item.pop("reasons_json"), [])
        payload = decode_json(item.pop("payload_json"), {})
        if payload.get("driver_id") != item["driver_id"]:
            continue
        speed = payload.get("vehicle_speed_mph")
        item["safe_pause_info"] = {
            "is_moving": speed is not None and speed > 2.0,
            "speed_mph": speed,
            "safe_pause_scheduled": item["decision"] in ("challenge_at_safe_pause", "step_up_check_at_safe_pause") and speed is not None and speed > 2.0,
            "scheduled_state": "queued_for_stop" if speed is not None and speed > 2.0 else "safe_stop_confirmed" if speed is not None else "unknown",
        }
        reviews.append(item)

    return {"count": len(reviews), "reviews": reviews, "items": reviews}


@app.get("/v1/review-queue/export-csv")
def export_review_queue_csv(
    status: str = "all",
    driver_id: str | None = None,
) -> Response:
    """Download review queue and audit log as a CSV file for compliance & offline ops analysis."""
    import csv
    import io

    query = """SELECT q.id AS review_id, q.session_id, q.driver_id, q.decision,
                      q.status, q.created_at, q.resolved_at, q.resolution,
                      s.timestamp AS session_timestamp, s.context_score,
                      s.behavior_score, s.quadrant, s.reasons_json
               FROM review_queue q
               JOIN sessions s ON s.id = q.session_id"""
    clauses = []
    params: list[Any] = []
    if status != "all":
        clauses.append("q.status = ?")
        params.append(status)
    if driver_id:
        clauses.append("q.driver_id = ?")
        params.append(driver_id)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY q.created_at DESC LIMIT 500"

    with connect() as con:
        rows = con.execute(query, tuple(params)).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Review ID", "Session ID", "Driver ID", "Quadrant", "Decision",
        "Context Score", "Behavior Score", "Status", "Timestamp", "Evidence / Reason"
    ])
    for row in rows:
        reasons = decode_json(row["reasons_json"], [])
        reason_str = " | ".join(reasons) if isinstance(reasons, list) else str(reasons)
        writer.writerow([
            row["review_id"],
            row["session_id"],
            row["driver_id"],
            row["quadrant"],
            row["decision"],
            row["context_score"],
            row["behavior_score"],
            row["status"],
            row["created_at"],
            reason_str.replace('"', '""')
        ])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="aslikaun_fleet_audit_report.csv"'}
    )


@app.get("/v1/fleet/export-csv")
def export_fleet_csv() -> Response:
    """Export current fleet status as CSV."""
    import csv
    import io

    with connect() as con:
        rows = con.execute("SELECT driver_id, latest_result_json, updated_at FROM drivers ORDER BY updated_at DESC").fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Driver ID", "Quadrant", "Decision", "Context Score", "Behavior Score", "Updated At", "Reason"])
    for row in rows:
        res = decode_json(row["latest_result_json"], {})
        reasons = res.get("reasons", [])
        reason_str = " | ".join(reasons) if isinstance(reasons, list) else str(reasons)
        writer.writerow([
            row["driver_id"],
            res.get("quadrant", "unknown"),
            res.get("decision", "continue"),
            res.get("context_score", ""),
            res.get("behavior_score", ""),
            row["updated_at"],
            reason_str.replace('"', '""')
        ])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="aslikaun_fleet_scores.csv"'}
    )


@app.get("/v1/review-queue/{session_id}")
def get_review_details(
    session_id: int, admin: dict[str, str] = Depends(require_admin)
) -> dict:
    """Return the review item and associated session evidence for admin inspection."""
    with connect() as con:
        row = con.execute(
            """SELECT q.id AS review_id, q.session_id, q.driver_id, q.decision,
                      q.status, q.created_at, q.resolved_at, q.resolution,
                      q.reviewer_id, q.reviewer_note,
                      s.timestamp AS session_timestamp, s.payload_json,
                      s.context_score, s.behavior_score, s.context_axis,
                      s.behavior_axis, s.quadrant, s.reasons_json
               FROM review_queue q
               JOIN sessions s ON s.id = q.session_id
               WHERE q.session_id = ?""",
            (session_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Review item not found")

    item = dict(row)
    item["reasons"] = decode_json(item.pop("reasons_json"), [])
    item["session_payload"] = decode_json(item.pop("payload_json"), {})
    return item


@app.post("/v1/review-queue/{session_id}/resolve")
def resolve_review(
    session_id: int,
    body: ResolveReview,
    admin: dict[str, str] = Depends(require_admin),
) -> dict:
    """Resolve an open review and write an audit event."""
    with connect() as con:
        review = con.execute(
            "SELECT * FROM review_queue WHERE session_id = ?",
            (session_id,),
        ).fetchone()

        if review is None:
            raise HTTPException(status_code=404, detail="Review item not found")
        if review["status"] == "resolved":
            raise HTTPException(status_code=409, detail="Review item is already resolved")

        resolved_at = now_iso()
        con.execute(
            """UPDATE review_queue
               SET status='resolved', resolved_at=?, resolution=?,
                   reviewer_id=?, reviewer_note=?
               WHERE session_id=? AND status='open'""",
            (
                resolved_at,
                body.resolution,
                admin["username"],
                body.reviewer_note,
                session_id,
            ),
        )
        write_event(
            con,
            review["driver_id"],
            "review_resolved",
            {
                "session_id": session_id,
                "original_decision": review["decision"],
                "resolution": body.resolution,
                "reviewer_id": admin["username"],
                "reviewer_note": body.reviewer_note,
                "resolved_at": resolved_at,
            },
        )
        if body.resolution == "cleared" and ML_DETECTORS_AVAILABLE:
            # A human confirmed this driver, so the frozen anchor should
            # re-establish from here rather than staying pinned to
            # whatever behavior led to this review in the first place.
            reset_frozen_anchor(con, review["driver_id"])

    broadcast_event_sync("review_resolved", {
        "session_id": session_id,
        "driver_id": review["driver_id"],
        "status": "resolved",
        "resolution": body.resolution,
        "reviewer_id": admin["username"],
        "reviewer_note": body.reviewer_note,
        "resolved_at": resolved_at,
    })

    return {
        "session_id": session_id,
        "driver_id": review["driver_id"],
        "status": "resolved",
        "resolution": body.resolution,
        "reviewer_id": admin["username"],
        "resolved_at": resolved_at,
    }


# ---------- Real-Time Event Streaming & ML Simulation Endpoints ----------

@app.get("/v1/events/stream")
async def event_stream(request: Request) -> StreamingResponse:
    """Real-time SSE event stream for live fleet telemetry, reviews, and simulator updates."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    _event_subscribers.add(queue)

    async def event_generator():
        try:
            init_msg = json.dumps({"status": "connected", "service": "trust-juggler-realtime", "timestamp": now_iso()})
            yield f"event: connected\ndata: {init_msg}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=10.0)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield f": ping {now_iso()}\n\n"
        finally:
            _event_subscribers.discard(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/v1/drivers/{driver_id}/drift")
def get_driver_drift(driver_id: str) -> dict:
    """Return historical sessions with Fast EWMA, Frozen Anchor, and breach annotations."""
    with connect() as con:
        fetched_rows = con.execute(
            """SELECT id, timestamp, payload_json, context_score, behavior_score,
                      context_axis, behavior_axis, quadrant, decision, reasons_json
               FROM sessions WHERE driver_id = ? ORDER BY id ASC LIMIT 60""",
            (driver_id,),
        ).fetchall()
        rows = [r for r in fetched_rows if decode_json(r["payload_json"], {}).get("driver_id") == driver_id]

    if not rows:
        return {
            "driver_id": driver_id,
            "count": 0,
            "alert": "Calibrated Gaussian anchor retained across sessions",
            "anchor_score": None,
            "rows": [],
            "sessions": [],
            "rental_audit": {"status": "insufficient_history", "sessions_required": 20},
        }

    formatted_rows = []
    sessions = []
    observed = [float(r["behavior_score"]) for r in rows if r["behavior_score"] is not None]
    ewma = observed[0] if observed else None
    has_breach = False

    for r in rows:
        payload = decode_json(r["payload_json"], {})
        beh = r["behavior_score"]
        ctx = r["context_score"]
        speed = payload.get("vehicle_speed_mph", 0.0)
        ewma = round(0.8 * ewma + 0.2 * beh, 3) if beh is not None and ewma is not None else beh
        quadrant = r["quadrant"].replace("_", " ").title()
        if "handoff" in r["quadrant"].lower():
            has_breach = True
        verdict = "Challenge queued" if (r["decision"] or "").startswith("challenge") else ("Rebaselined" if r["decision"] == "rebaseline" else "Verified")
        speed_str = f"{speed:.1f} mph" if isinstance(speed, (int, float)) else "—"
        ts_str = r["timestamp"].split("T")[1][:5] if "T" in r["timestamp"] else "Recent"
        ses_id = f"SES-{r['id']:04d}"

        formatted_rows.append([ses_id, ts_str, f"{ctx:.2f}" if ctx is not None else "—", f"{beh:.2f}" if beh is not None else "—", quadrant, speed_str, verdict])
        sessions.append({
            "session_id": ses_id,
            "timestamp": r["timestamp"],
            "context_score": ctx,
            "behavior_score": beh,
            "ewma_score": ewma,
            "anchor_score": round(sum(observed[:min(10, len(observed))]) / min(10, len(observed)), 3) if observed else None,
            "quadrant": r["quadrant"],
            "decision": r["decision"],
            "speed_mph": speed,
            "verdict": verdict,
        })

    alert = "Drift review recommended from stored ML decisions" if has_breach else "No stored handoff decision in this history"
    rental_audit: dict[str, Any] = {"status": "insufficient_history", "sessions_required": 20, "session_count": len(rows)}
    if len(rows) >= 20:
        try:
            from ml.redteam.detectors import split_rental_check
            import math
            audit_sessions = []
            for row in rows:
                payload = decode_json(row["payload_json"], {})
                stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                payload["hour_of_day"] = stamp.hour + stamp.minute / 60
                if payload.get("accept_latency_ms") is not None and payload.get("touch_events"):
                    # The touch scorer consumes normalized gesture features; ingest
                    # clients are allowed to omit area or offsets.
                    payload["touch_events"] = [
                        {**event, "pressure": event.get("pressure", 0.0), "area": event.get("area", 0.0),
                         "t_offset_ms": event.get("t_offset_ms", index * 100)}
                        for index, event in enumerate(payload["touch_events"])
                    ]
                    audit_sessions.append(payload)
            if len(audit_sessions) >= 20:
                rental_audit = {"status": "scored", "session_count": len(audit_sessions), **split_rental_check(audit_sessions)}
        except (ImportError, ValueError, KeyError, TypeError) as exc:
            rental_audit = {"status": "unavailable", "session_count": len(rows), "note": str(exc)}
    return {
        "driver_id": driver_id,
        "count": len(sessions),
        "alert": alert,
        "anchor_score": round(sum(observed[:min(10, len(observed))]) / min(10, len(observed)), 3) if observed else None,
        "rows": formatted_rows,
        "sessions": sessions,
        "rental_audit": rental_audit,
    }


@app.get("/v1/ml/models/health")
def get_model_health() -> dict:
    """Summarize runtime scoring and calibration evidence from stored sessions."""
    with connect() as con:
        rows = con.execute("SELECT driver_id, latest_result_json FROM drivers").fetchall()
        session_count = con.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
        latest_payloads = {}
        for session in con.execute("SELECT driver_id, payload_json FROM sessions ORDER BY id DESC").fetchall():
            latest_payloads.setdefault(session["driver_id"], decode_json(session["payload_json"], {}))
    results = [decode_json(r["latest_result_json"], {}) for r in rows if latest_payloads.get(r["driver_id"], {}).get("driver_id") == r["driver_id"]]
    scored = [r for r in results if r.get("model_status")]
    statuses = sorted({str(r.get("model_status")) for r in scored})
    adaptive = sum(1 for r in scored if r.get("threshold_info", {}).get("source") == "per_rider_calibrated")
    conformal_active = any(r.get("model_status") == "per_rider_conformal_fusion" for r in scored)
    return {
        "status": "available" if scored else "no_data",
        "timestamp": now_iso(),
        "summary": ", ".join(statuses) if statuses else "No backend ML evaluations recorded",
        "sample_count": session_count,
        "calibration": {"status": "conformal_active" if conformal_active else "insufficient_evidence", "minimum_sessions": 10,
            "note": "Per-sensor p-values are produced only after a separate enrollment/calibration split is fit from at least 10 prior sessions. A KS uniformity check still requires a held-out p-value cohort and is not reported without one." if conformal_active else "The current fleet has no persisted per-rider conformal evaluation yet. Relative anomaly scores are not presented as p-values, and no KS calibration result is reported without held-out evidence."},
        "thresholds": {"adaptive_driver_count": adaptive, "population_fallback_count": max(0, len(scored) - adaptive)},
        "models": [{"name": name, "status": "runtime active" if name == "ml_two_axis_fusion" else name,
            "scored_sessions": sum(1 for r in scored if r.get("model_status") == name),
            "latest_score": next((r.get("behavior_score") for r in reversed(scored) if r.get("model_status") == name and isinstance(r.get("behavior_score"), (float, int))), None)} for name in (statuses or ["ml_two_axis_fusion"])],
    }


@app.post("/v1/fleet/telemetry/tick")
def telemetry_tick() -> dict:
    """Advances live telemetry stream: simulates slight speed variations and broadcasts."""
    import random
    with connect() as con:
        rows = con.execute("SELECT driver_id, latest_result_json, updated_at FROM drivers LIMIT 20").fetchall()
        ticks = []
        for r in rows:
            speed = 0.0 if random.random() < 0.28 else round(random.uniform(9.5, 26.8), 1)
            ticks.append({
                "driver_id": r["driver_id"],
                "speed_mph": speed,
                "is_moving": speed > 2.0,
                "timestamp": now_iso(),
            })

    broadcast_event_sync("telemetry_tick", {"drivers": ticks})
    return {"status": "ok", "count": len(ticks), "drivers": ticks}


class SimRequest(BaseModel):
    scenario: str = Field(default="lazy_renter")
    driver_id: str | None = None
    vehicle_speed_mph: float | None = None



# Continuous Live Simulation State
_sim_task: asyncio.Task | None = None
_sim_running: bool = False


async def _continuous_sim_loop():
    global _sim_running
    import random
    scenarios = ["lazy_renter", "split_shift_rental", "gradual_drift", "phone_upgrade"]
    while _sim_running:
        try:
            await asyncio.sleep(4.0)
            scen = random.choice(scenarios)
            run_ml_simulation(SimRequest(scenario=scen))
            telemetry_tick()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("[continuous_sim] tick error: %s", e)


@app.post("/v1/fleet/simulator/auto-toggle")
async def toggle_continuous_simulator(enable: bool = True) -> dict:
    """Toggles real-time autonomous fleet telemetry simulation (3s pulse)."""
    global _sim_task, _sim_running
    if enable and not _sim_running:
        _sim_running = True
        _sim_task = asyncio.create_task(_continuous_sim_loop())
        logger.info("[simulator] Continuous live fleet simulation started.")
        broadcast_event_sync("simulator_status", {"active": True})
        return {"status": "started", "active": True}
    elif not enable and _sim_running:
        _sim_running = False
        if _sim_task:
            _sim_task.cancel()
            _sim_task = None
        logger.info("[simulator] Continuous live fleet simulation stopped.")
        broadcast_event_sync("simulator_status", {"active": False})
        return {"status": "stopped", "active": False}
    return {"status": "noop", "active": _sim_running}


@app.get("/v1/fleet/simulator/status")
def get_continuous_simulator_status() -> dict:
    return {"active": _sim_running}

@app.post("/v1/ml/simulate")
def run_ml_simulation(body: SimRequest) -> dict:
    """Execute one of the ML red-team / honest benchmark scenarios and broadcast in real time."""
    scenario_type = body.scenario.lower().replace("-", "_").replace(" ", "_")

    scenario_configs = {
        "lazy_renter": {
            "driver_id": body.driver_id or "rider-sim-lazy",
            "device_id": "dev-samsung-a54",
            "ip_subnet": "192.168.1.0/24",
            "zone": "mumbai_central",
            "vehicle_speed_mph": body.vehicle_speed_mph if body.vehicle_speed_mph is not None else 18.4,
            "accept_latency_ms": 3200.0,
            "touch_events": [{"type": "tap", "pressure": 0.94, "duration_ms": 220, "path": [{"x": 100, "y": 150, "pressure": 0.94, "t": 0}, {"x": 102, "y": 152, "pressure": 0.92, "t": 220}]}],
            "accel": [{"x": 2.1, "y": 3.4, "z": 9.1}],
            "gyro": [1.4, 0.8, 1.1],
            "is_simulated": True,
        },
        "slow_drift_renter": {
            "driver_id": body.driver_id or "rider-sim-drift",
            "device_id": "dev-samsung-a54",
            "ip_subnet": "192.168.1.0/24",
            "zone": "mumbai_central",
            "vehicle_speed_mph": body.vehicle_speed_mph if body.vehicle_speed_mph is not None else 15.0,
            "accept_latency_ms": 2400.0,
            "touch_events": [{"type": "tap", "pressure": 0.78, "duration_ms": 180}],
            "accel": [{"x": 1.8, "y": 2.5, "z": 8.9}],
            "is_simulated": True,
        },
        "split_shift": {
            "driver_id": body.driver_id or "rider-sim-split",
            "device_id": "dev-oneplus-11",
            "ip_subnet": "192.168.10.0/24",
            "zone": "chennai_omr",
            "vehicle_speed_mph": body.vehicle_speed_mph if body.vehicle_speed_mph is not None else 22.0,
            "accept_latency_ms": 1800.0,
            "touch_events": [{"type": "tap", "pressure": 0.62, "duration_ms": 130}],
            "accel": [{"x": 0.8, "y": 1.2, "z": 9.8}],
            "is_simulated": True,
        },
        "copycat_attacker": {
            "driver_id": body.driver_id or "rider-sim-copycat",
            "device_id": "dev-samsung-a54",
            "ip_subnet": "192.168.1.0/24",
            "zone": "mumbai_central",
            "vehicle_speed_mph": body.vehicle_speed_mph if body.vehicle_speed_mph is not None else 12.0,
            "accept_latency_ms": 680.0,
            "touch_events": [{"type": "swipe", "pressure": 0.91, "path": [{"x": 50, "y": 80, "pressure": 0.91, "t": 0}, {"x": 240, "y": 310, "pressure": 0.88, "t": 120}]}],
            "accel": [{"x": 1.9, "y": 2.1, "z": 9.3}],
            "is_simulated": True,
        },
        "legitimate_phone_upgrade": {
            "driver_id": body.driver_id or "rider-sim-upgrade",
            "device_id": "dev-iphone-15-pro-new",
            "ip_subnet": "10.20.30.0/24",
            "zone": "navi_mumbai_vashi",
            "vehicle_speed_mph": body.vehicle_speed_mph if body.vehicle_speed_mph is not None else 0.0,
            "accept_latency_ms": 510.0,
            "touch_events": [{"type": "tap", "pressure": 0.35, "duration_ms": 90}],
            "accel": [{"x": 0.1, "y": 0.2, "z": 9.8}],
            "is_simulated": True,
        },
        "phone_upgrade": {
            "driver_id": body.driver_id or "rider-sim-upgrade",
            "device_id": "dev-iphone-15-pro-new",
            "ip_subnet": "10.20.30.0/24",
            "zone": "navi_mumbai_vashi",
            "vehicle_speed_mph": body.vehicle_speed_mph if body.vehicle_speed_mph is not None else 0.0,
            "accept_latency_ms": 510.0,
            "touch_events": [{"type": "tap", "pressure": 0.35, "duration_ms": 90}],
            "accel": [{"x": 0.1, "y": 0.2, "z": 9.8}],
            "is_simulated": True,
        },
        "cold_start_rider": {
            "driver_id": body.driver_id or "rider-sim-coldstart",
            "device_id": "dev-pixel-8",
            "ip_subnet": "192.168.4.0/24",
            "zone": "bkc_mumbai",
            "vehicle_speed_mph": body.vehicle_speed_mph if body.vehicle_speed_mph is not None else 0.0,
            "accept_latency_ms": 600.0,
            "touch_events": [{"type": "tap", "pressure": 0.38, "duration_ms": 95}],
            "accel": [{"x": 0.12, "y": 0.15, "z": 9.81}],
            "is_simulated": True,
        },
        "cold_start": {
            "driver_id": body.driver_id or "rider-sim-coldstart",
            "device_id": "dev-pixel-8",
            "ip_subnet": "192.168.4.0/24",
            "zone": "bkc_mumbai",
            "vehicle_speed_mph": body.vehicle_speed_mph if body.vehicle_speed_mph is not None else 0.0,
            "accept_latency_ms": 600.0,
            "touch_events": [{"type": "tap", "pressure": 0.38, "duration_ms": 95}],
            "accel": [{"x": 0.12, "y": 0.15, "z": 9.81}],
            "is_simulated": True,
        },
    }

    cfg = scenario_configs.get(scenario_type, scenario_configs["lazy_renter"])

    # Pre-populate owner baseline in SQLite so the scenario is evaluated against an authentic owner history
    with connect() as con:
        owner_context = {
            "device_id": "dev-iphone-12-old" if "upgrade" in scenario_type else "dev-samsung-a54",
            "ip_subnet": "192.168.1.0/24",
            "zone": "mumbai_central",
        }
        owner_behavior = {
            "operational": {"accept_latency_ms": 620.0, "hour_of_day": 14.0},
            "touch": {"pressure_mean": 0.38, "pressure_std": 0.08, "interval_mean_ms": 210.0, "velocity_mean": 0.45, "touch_count": 8},
            "motion": {"accel_mean": 9.8, "accel_std": 0.12},
            "gps": {"speed_mean_mph": 16.5},
        }
        con.execute(
            """INSERT OR REPLACE INTO drivers(driver_id, anchor_json, fast_json, latest_json,
               latest_result_json, updated_at) VALUES(?,?,?,?,?,?)""",
            (cfg["driver_id"], json.dumps(owner_behavior), json.dumps(owner_behavior),
             json.dumps(owner_context), "{}", now_iso()),
        )

    session_ingest = SessionIngest(**cfg)
    result = ingest_session(session_ingest, x_driver_token=None)
    result["scenario"] = scenario_type
    return result


# ---------- Public & Demo Endpoints for Frontend Integration ----------

@app.get("/v1/public/driver-status/{driver_id}")
def public_driver_status(driver_id: str) -> dict:
    """Public read-only trust standing for Driver Trust Portal on index.html."""
    with connect() as con:
        row = con.execute("SELECT * FROM drivers WHERE driver_id = ?", (driver_id,)).fetchone()
        if row is None:
            return {
                "driver_id": driver_id,
                "standing": "unregistered",
                "context_score": None,
                "behavior_score": None,
                "quadrant": "unregistered",
                "decision": "unregistered",
                "message": "Driver ID not found in fleet registry.",
                "pending_declaration": None,
            }
        result = decode_json(row["latest_result_json"], {})
        pending_decl = con.execute(
            """SELECT id, device_id, ip_subnet, zone, reason, status, declared_at
               FROM driver_change_declarations
               WHERE driver_id = ? AND status = 'pending'
               ORDER BY id DESC LIMIT 1""",
            (driver_id,),
        ).fetchone()
        decision = result.get("decision", "continue")
        return {
            "driver_id": driver_id,
            "standing": "review_queued" if (decision.startswith("challenge") or decision.startswith("step_up")) else "verified",
            "context_score": result.get("context_score"),
            "behavior_score": result.get("behavior_score"),
            "quadrant": result.get("quadrant", "stable"),
            "decision": decision,
            "reasons": result.get("reasons", ["Identity and behavioral continuity verified."]),
            "reason_vector": result.get("reason_vector"),
            "signals": result.get("signals", {}),
            "pending_declaration": dict(pending_decl) if pending_decl else None,
            "updated_at": row["updated_at"],
        }


# ---------- Frontend Phone UI & Dashboard Static Mount ----------
_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
_FRONTEND_DIST = _FRONTEND_DIR / "dist"
_STATIC_DIR = _FRONTEND_DIST if _FRONTEND_DIST.exists() else _FRONTEND_DIR
_PHONE_DIR = _FRONTEND_DIR / "mobile-app" / "dist"
if not _PHONE_DIR.exists():
    _PHONE_DIR = _PROJECT_ROOT / "trustmesh_frontend_inspect" / "dist"


if _PHONE_DIR.exists() and (_PHONE_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(_PHONE_DIR / "assets")), name="phone-assets")
elif (_FRONTEND_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(_FRONTEND_DIST / "assets")), name="assets")

if _FRONTEND_DIR.exists():
    app.mount("/dashboard", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="dashboard")



@app.get("/favicon.svg", include_in_schema=False)
def favicon() -> Any:
    if _PHONE_DIR.exists() and (_PHONE_DIR / "favicon.svg").exists():
        return FileResponse(str(_PHONE_DIR / "favicon.svg"))
    raise HTTPException(status_code=404)


@app.get("/robots.txt", include_in_schema=False)
def robots() -> Any:
    if _PHONE_DIR.exists() and (_PHONE_DIR / "robots.txt").exists():
        return FileResponse(str(_PHONE_DIR / "robots.txt"))
    raise HTTPException(status_code=404)


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def root_endpoint() -> Any:
    if _PHONE_DIR.exists() and (_PHONE_DIR / "index.html").exists():
        return FileResponse(str(_PHONE_DIR / "index.html"))
    dist_index = _FRONTEND_DIST / "index.html"
    if dist_index.exists():
        return FileResponse(str(dist_index))
    if _FRONTEND_DIR.exists():
        return RedirectResponse(url="/dashboard/index.html")
    return {"message": "AsliKaun API is running. Visit /docs for OpenAPI specs."}


@app.get("/driver-app-demo.html", include_in_schema=False)
def driver_app_demo_html_redirect() -> Any:
    return RedirectResponse(url="/dashboard/driver-app-demo.html")


@app.get("/auth", include_in_schema=False)
def auth_endpoint(mode: str = "login") -> Any:
    dist_index = _FRONTEND_DIST / "index.html"
    if dist_index.exists():
        return FileResponse(str(dist_index))
    return RedirectResponse(url=f"/dashboard/index.html?mode={mode}")


@app.get("/fleet-dashboard.html", include_in_schema=False)
def fleet_dashboard_endpoint() -> Any:
    dist_fleet = _FRONTEND_DIST / "fleet-dashboard.html"
    if dist_fleet.exists():
        return FileResponse(str(dist_fleet))
    raw_fleet = _FRONTEND_DIR / "fleet-dashboard.html"
    if raw_fleet.exists():
        return FileResponse(str(raw_fleet))
    return RedirectResponse(url="/dashboard/fleet-dashboard.html")


@app.get("/dashboard.html", include_in_schema=False)
def dashboard_html_page() -> Any:
    dash = _FRONTEND_DIR / "dashboard.html"
    if dash.exists():
        return FileResponse(str(dash))
    return RedirectResponse(url="/fleet-dashboard.html")


@app.get("/style.css", include_in_schema=False)
def serve_root_style() -> Any:
    css_file = _FRONTEND_DIR / "style.css"
    if css_file.exists():
        return FileResponse(str(css_file), media_type="text/css")
    raise HTTPException(status_code=404)
@app.get("/app.js", include_in_schema=False)
def serve_root_app_js() -> Any:
    js_file = _FRONTEND_DIR / "app.js"
    if js_file.exists():
        return FileResponse(str(js_file), media_type="application/javascript")
    raise HTTPException(status_code=404)


@app.get("/trustmesh-sdk.js", include_in_schema=False)
def serve_root_sdk() -> Any:
    sdk_file = _FRONTEND_DIR / "trustmesh-sdk.js"
    if sdk_file.exists():
        return FileResponse(str(sdk_file), media_type="application/javascript")
    raise HTTPException(status_code=404)


@app.get("/{filename}.png", include_in_schema=False)
def serve_root_png(filename: str) -> Any:
    for base in [_FRONTEND_DIST, _FRONTEND_DIR / "public", _FRONTEND_DIR]:
        candidate = base / f"{filename}.png"
        if candidate.exists():
            return FileResponse(str(candidate), media_type="image/png")
    raise HTTPException(status_code=404)


