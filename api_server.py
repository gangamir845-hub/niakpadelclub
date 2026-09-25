#!/usr/bin/env python3
"""
NIAK Padel Club — booking API (port 8000)

Design goals:
  * Reservations are stored server-side (SQLite) so every visitor sees the same data.
  * Double-booking is impossible: each reserved hour becomes a row in `slots`
    with a UNIQUE(date, court, hour) index. All hours of a booking are inserted
    inside ONE transaction — if any hour is already taken, the whole insert
    fails atomically and the API answers 409. This is race-safe even if two
    people press "confirm" at the exact same millisecond.
"""
import datetime
import hmac
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import bot_commands
import notify

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")
# No default: if NIAK_ADMIN_TOKEN is not injected, admin endpoints fail closed
# (503) instead of falling back to a guessable shared secret.
ADMIN_TOKEN = os.environ.get("NIAK_ADMIN_TOKEN", "").strip()


# تلاش‌های ورود به پنل هم محدود می‌شن — توکن ۳۲ کاراکتری تصادفیه و با
# compare_digest چک می‌شه، پس brute force عملاً غیرممکنه؛ این فقط یک لایه‌ی
# اضافه‌ست تا کسی نتونه با هزاران درخواست ناموفق سرور رو مشغول کنه.
ADMIN_AUTH_RATE_LIMIT_MAX = 30
ADMIN_AUTH_RATE_LIMIT_WINDOW = 600
_admin_rate_hits: dict[str, deque] = defaultdict(deque)


def require_admin(token: str | None, ip: str = "unknown") -> None:
    """Constant-time admin token check. Fails closed when unconfigured.

    The token arrives in the X-Admin-Token header rather than the query string,
    so it never lands in access logs or browser history.

    فقط تلاش‌های *ناموفق* شمرده می‌شن، پس پنل مدیریت که هر ۱۵ ثانیه
    /api/admin/latest رو صدا می‌زنه هیچ‌وقت به سقف نمی‌خوره.
    """
    if not ADMIN_TOKEN:
        raise HTTPException(503, "admin panel not configured")
    if not token or not hmac.compare_digest(token, ADMIN_TOKEN):
        _check_rate(ip, _admin_rate_hits, ADMIN_AUTH_RATE_LIMIT_MAX,
                    ADMIN_AUTH_RATE_LIMIT_WINDOW,
                    "too many failed admin attempts, please try again later")
        raise HTTPException(401, "bad token")


START_HOUR = 7
# Closing time is 02:00 of the NEXT calendar day. Hours 24 and 25 mean
# 00:00 and 01:00 after midnight but stay attached to the opening day's date,
# so one night is always one `date` and slot uniqueness still holds.
END_HOUR = 26
COURTS = (0, 1)
CLUB_TZ = ZoneInfo("Asia/Tehran")


def now_local() -> datetime.datetime:
    """Current time in the club's own timezone — never the server's, since the
    server may be hosted anywhere (e.g. UTC), which would silently let today's
    already-passed hours (or the wrong 'today') be booked/blocked."""
    return datetime.datetime.now(CLUB_TZ)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BOOKING_WINDOW_DAYS = 90   # how far ahead a customer may book
MAX_BODY_BYTES = 16 * 1024

# Simple in-process rate limit: stops a script from squatting every slot and
# flooding the club's Telegram. Resets when the server restarts, which is fine
# for a club-sized site.
# Kept generous on purpose: Iranian mobile carriers put many real customers
# behind one shared IP, so a tight limit would block genuine bookings.
RATE_LIMIT_MAX = 15        # booking attempts...
RATE_LIMIT_WINDOW = 600    # ...per 10 minutes, per IP
_rate_hits: dict[str, deque] = defaultdict(deque)
_rate_lock = threading.Lock()

# جست‌وجوی «رزروهای من» جداگانه محدود می‌شه (نه با همون سقف ثبت رزرو)، چون
# صرفاً با شماره تلفن انجام می‌شه و باید جلوی امتحان کردن شماره‌های پشت سر هم
# (enumeration) رو بگیریم؛ سقفش سخت‌گیرانه‌تر از ثبت رزروئه.
LOOKUP_RATE_LIMIT_MAX = 20
LOOKUP_RATE_LIMIT_WINDOW = 600
_lookup_rate_hits: dict[str, deque] = defaultdict(deque)


def _check_rate(ip: str, hits_map: dict, max_hits: int, window: int, message: str) -> None:
    now = time.monotonic()
    with _rate_lock:
        hits = hits_map[ip]
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= max_hits:
            raise HTTPException(429, message)
        hits.append(now)
        if len(hits_map) > 5000:      # keep the dict from growing forever
            for k in [k for k, v in hits_map.items() if not v]:
                del hits_map[k]


def check_rate_limit(ip: str) -> None:
    _check_rate(ip, _rate_hits, RATE_LIMIT_MAX, RATE_LIMIT_WINDOW,
                "too many bookings, please try again later")


def check_lookup_rate_limit(ip: str) -> None:
    _check_rate(ip, _lookup_rate_hits, LOOKUP_RATE_LIMIT_MAX, LOOKUP_RATE_LIMIT_WINDOW,
                "too many requests, please try again later")


def normalize_phone(phone: str) -> str:
    """Keeps only digits, so '0912 123 4567' and '0912-123-4567' and
    '09121234567' are treated as the same number for lookup/cancel."""
    return re.sub(r"\D", "", phone)


# همون نرمال‌سازی روی مقدار ذخیره‌شده هم موقع مقایسه اعمال می‌شه، چون phone
# توی دیتابیس همون‌طوری که کاربر تایپ کرده (با فاصله/خط‌تیره‌ی احتمالی) ذخیره
# می‌شه، نه به‌صورت نرمال‌شده.
PHONE_NORMALIZE_SQL = "REPLACE(REPLACE(REPLACE(phone,' ',''),'-',''),'+','')"

_local = threading.local()


def connect() -> sqlite3.Connection:
    """One SQLite connection per thread (sqlite3 connections are not shareable)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=8000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


db = connect()
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS bookings (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        date       TEXT    NOT NULL,
        court      INTEGER NOT NULL,
        start_hour INTEGER NOT NULL,
        duration   INTEGER NOT NULL,
        name       TEXT    NOT NULL,
        phone      TEXT    NOT NULL,
        code       TEXT    NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS slots (
        date       TEXT    NOT NULL,
        court      INTEGER NOT NULL,
        hour       INTEGER NOT NULL,
        booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
        PRIMARY KEY (date, court, hour)
    ) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS idx_bookings_date ON bookings(date);
    """
)


@asynccontextmanager
async def lifespan(app):
    # دستورهای بات (/today, /find, /cancel) با long polling در پس‌زمینه اجرا می‌شن.
    # اگر توکن/چت‌آیدی تنظیم نباشه، خودش لاگ می‌ده و بی‌خطر خاموش می‌مونه.
    # هر توکن تلگرام فقط یک شنونده می‌تواند داشته باشد؛ اگر دو نسخه از سرور
    # هم‌زمان بالا باشند (مثلاً نسخه‌ی منتشرشده + یک نسخه‌ی محلی برای تست)،
    # تلگرام به یکی از آن‌ها خطای 409 می‌دهد. برای نسخه‌ی تستی
    # NIAK_BOT_POLLING=0 بگذار تا فقط اعلان‌ها بفرستد و دستورها را نخواند.
    stop_bot = None
    if os.environ.get("NIAK_BOT_POLLING", "1").strip() not in ("0", "false", "no"):
        stop_bot = bot_commands.start_background(DB_PATH)
    else:
        print("bot polling disabled (NIAK_BOT_POLLING=0)", flush=True)
    yield
    if stop_bot is not None:
        stop_bot.set()


app = FastAPI(title="NIAK Padel Booking API", lifespan=lifespan)

# Only the club's own pages (published site, preview proxy, local dev) may call
# the API from a browser. Set NIAK_ALLOWED_ORIGINS to override.
# "null" is required because the in-thread preview renders the site inside a
# sandboxed iframe, which sends an opaque (null) Origin.
_extra_origins = ["null"] + [o.strip() for o in os.environ.get("NIAK_ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_extra_origins,
    allow_origin_regex=r"https://([a-z0-9-]+\.)*pplx\.app|http://localhost(:\d+)?|http://127\.0\.0\.1(:\d+)?",
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Admin-Token"],
)


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    """Reject oversized bodies before they are buffered into memory."""
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
        return JSONResponse({"detail": "payload too large"}, status_code=413)
    return await call_next(request)


class BookingIn(BaseModel):
    date: str
    court: int = Field(ge=0, le=1)
    start: int = Field(ge=START_HOUR, le=END_HOUR - 1)
    duration: int = Field(ge=1, le=3)
    name: str = Field(min_length=2, max_length=60, pattern=r"^[^<>\"'&\\\x00-\x1f]+$")
    phone: str = Field(pattern=r"^[0-9+\-\s]{7,20}$")


def check_date(date: str) -> datetime.date:
    """Format AND calendar validity — '9999-99-99' must not be storable."""
    if not DATE_RE.match(date):
        raise HTTPException(400, "bad date format, expected YYYY-MM-DD")
    try:
        return datetime.date.fromisoformat(date)
    except ValueError:
        raise HTTPException(400, "not a real calendar date")


def check_bookable_date(date: str) -> datetime.date:
    """A customer may only book from today up to BOOKING_WINDOW_DAYS ahead.

    'Today' is the club's local date (Asia/Tehran), not the server's, so this
    stays correct regardless of where api_server.py happens to be hosted.
    """
    d = check_date(date)
    today = now_local().date()
    if d < today:
        raise HTTPException(400, "date is in the past")
    if d > today + datetime.timedelta(days=BOOKING_WINDOW_DAYS):
        raise HTTPException(400, "date is too far ahead")
    return d


def check_not_past_hour(date: datetime.date, start_hour: int) -> None:
    """Rejects booking an hour of TODAY that has already started/passed.

    Only applies when the booking date is today — future dates have no
    'current hour' to compare against. Uses the club's local time, same as
    check_bookable_date, so it can't be bypassed by hitting the API from a
    machine in a different timezone.
    """
    now = now_local()
    slot_start = datetime.datetime.combine(
        date, datetime.time(0), tzinfo=now.tzinfo
    ) + datetime.timedelta(hours=start_hour)
    if slot_start <= now:
        raise HTTPException(400, "this hour has already passed today")


@app.get("/api/health")
def health():
    return {"ok": True, "notifications": notify.channels_status()}


@app.get("/api/bookings")
def list_occupied(date: str = Query(...)):
    """Public availability for a day. No personal data is exposed."""
    check_date(date)
    rows = connect().execute(
        "SELECT court, hour FROM slots WHERE date = ?", [date]
    ).fetchall()
    occupied = {str(c): [] for c in COURTS}
    for court, hour in rows:
        occupied.setdefault(str(court), []).append(hour)
    for v in occupied.values():
        v.sort()
    return {"date": date, "occupied": occupied}


@app.post("/api/bookings", status_code=201)
def create_booking(b: BookingIn, background: BackgroundTasks, request: Request):
    check_rate_limit(request.client.host if request.client else "unknown")
    booking_date = check_bookable_date(b.date)
    check_not_past_hour(booking_date, b.start)
    if b.start + b.duration > END_HOUR:
        raise HTTPException(400, "booking runs past closing time")

    hours = list(range(b.start, b.start + b.duration))
    code = f"NIAK-{b.date.replace('-', '')}-{b.court + 1}{b.start:02d}"

    conn = connect()
    try:
        # BEGIN IMMEDIATE takes the write lock up-front, so two simultaneous
        # requests are serialised; the UNIQUE key on slots then rejects the loser.
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO bookings (date, court, start_hour, duration, name, phone, code)"
            " VALUES (?,?,?,?,?,?,?)",
            [b.date, b.court, b.start, b.duration, b.name.strip(), b.phone.strip(), code],
        )
        booking_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO slots (date, court, hour, booking_id) VALUES (?,?,?,?)",
            [(b.date, b.court, h, booking_id) for h in hours],
        )
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        raise HTTPException(409, "slot already booked")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise

    # اعلان بعد از COMMIT و در پس‌زمینه: اگر تلگرام قطع بود، رزرو سالم می‌ماند
    background.add_task(notify.notify_new_booking, {
        "date": b.date, "court": b.court, "start": b.start,
        "duration": b.duration, "name": b.name.strip(),
        "phone": b.phone.strip(), "code": code,
    })

    return {"id": booking_id, "code": code, "date": b.date, "court": b.court,
            "start": b.start, "duration": b.duration}


@app.get("/api/mybookings")
def my_bookings(request: Request, phone: str = Query(..., min_length=7, max_length=20)):
    """رزروهای مشتری، فقط با شماره تلفن — بدون نیاز به حساب کاربری.

    محدودیت واقعی: هرکس شماره‌ی تلفن یه نفر دیگه رو بدونه، می‌تونه رزروهای
    اونو هم ببینه (مثل خیلی از سیستم‌های ساده‌ی «پیگیری سفارش با شماره تلفن»).
    برای جلوگیری از امتحان کردن دسته‌جمعی شماره‌ها، این مسیر جداگانه و
    سخت‌گیرانه‌تر از ثبت رزرو محدود شده (check_lookup_rate_limit).
    """
    check_lookup_rate_limit(request.client.host if request.client else "unknown")
    digits = normalize_phone(phone)
    if len(digits) < 7:
        raise HTTPException(400, "phone number looks too short")
    rows = connect().execute(
        f"SELECT date, court, start_hour, duration, name, code FROM bookings"
        f" WHERE {PHONE_NORMALIZE_SQL} = ? ORDER BY date DESC, start_hour DESC",
        [digits],
    ).fetchall()
    cols = ["date", "court", "start", "duration", "name", "code"]
    today = now_local().date()
    out = []
    for r in rows:
        row = dict(zip(cols, r))
        row["past"] = datetime.date.fromisoformat(row["date"]) < today
        out.append(row)
    return out


@app.delete("/api/mybookings/{code}")
def my_cancel(request: Request, code: str, background: BackgroundTasks,
              phone: str = Query(..., min_length=7, max_length=20)):
    """لغو خودِ مشتری — برخلاف جست‌وجو، اینجا هم شماره تلفن هم کد رزرو با هم
    لازمه، چون این یه عملیات تغییردهنده‌ست (نه فقط دیدن): کد رزرو رو فقط
    خودِ مشتری (توی پیام تأیید) و مدیریت باشگاه می‌دونن، پس صرفِ دونستن
    شماره تلفن یه نفر برای لغو کردن رزروش کافی نیست.
    """
    check_lookup_rate_limit(request.client.host if request.client else "unknown")
    digits = normalize_phone(phone)
    if len(digits) < 7:
        raise HTTPException(400, "phone number looks too short")
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        f"SELECT id, date, court, start_hour, duration, name, phone FROM bookings"
        f" WHERE code = ? AND {PHONE_NORMALIZE_SQL} = ?",
        [code, digits],
    ).fetchone()
    if not row:
        conn.execute("ROLLBACK")
        raise HTTPException(404, "no matching booking for this phone and code")
    booking_id, b_date, b_court, b_start, b_dur, b_name, b_phone = row
    conn.execute("DELETE FROM slots WHERE booking_id = ?", [booking_id])
    conn.execute("DELETE FROM bookings WHERE id = ?", [booking_id])
    conn.execute("COMMIT")

    # اعلان بعد از COMMIT و در پس‌زمینه: اگر تلگرام قطع بود، لغو سالم انجام شده
    background.add_task(notify.notify_cancelled_booking, {
        "date": b_date, "court": b_court, "start": b_start, "duration": b_dur,
        "name": b_name, "phone": b_phone, "code": code,
    })
    return {"deleted": code, "date": b_date, "court": b_court,
            "start": b_start, "duration": b_dur}


@app.get("/api/admin/latest")
def admin_latest(request: Request, since_id: int = 0,
                 x_admin_token: str | None = Header(default=None)):
    """شمارش رزروهای تازه — پنل مدیریت هر چند ثانیه این را می‌خواند."""
    require_admin(x_admin_token, request.client.host if request.client else "unknown")
    row = connect().execute(
        "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM bookings WHERE id > ?", [since_id]
    ).fetchone()
    return {"new_count": row[0], "max_id": max(row[1], since_id)}


@app.get("/api/admin/bookings")
def admin_list(request: Request, date: str | None = None,
               x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token, request.client.host if request.client else "unknown")
    sql = ("SELECT id, date, court, start_hour, duration, name, phone, code, created_at"
           " FROM bookings")
    args: list = []
    if date:
        check_date(date)
        sql += " WHERE date = ?"
        args.append(date)
    sql += " ORDER BY id DESC"
    cols = ["id", "date", "court", "start", "duration", "name", "phone", "code", "created_at"]
    return [dict(zip(cols, r)) for r in connect().execute(sql, args).fetchall()]


@app.delete("/api/admin/bookings/{booking_id}")
def admin_delete(booking_id: int, request: Request,
                 x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token, request.client.host if request.client else "unknown")
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM slots WHERE booking_id = ?", [booking_id])
    cur = conn.execute("DELETE FROM bookings WHERE id = ?", [booking_id])
    conn.execute("COMMIT")
    if cur.rowcount == 0:
        raise HTTPException(404, "not found")
    return {"deleted": booking_id}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
