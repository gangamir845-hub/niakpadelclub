#!/usr/bin/env python3
"""
دستورهای بات تلگرام (long polling) — /today, /day, /free, /find, /move, /cancel, /help

notify.py فقط یک‌طرفه پیام می‌فرسته (اعلان رزرو جدید). این ماژول جداگانه با
long polling پیام‌های ورودی رو می‌گیره و فقط به چت‌های مجاز (همان
NIAK_TELEGRAM_CHAT_ID که در notify.py استفاده می‌شه) جواب می‌ده — هر چت دیگه‌ای
که به بات پیام بده نادیده گرفته می‌شه، چون این دستورها اطلاعات مشتری (نام،
شماره تلفن) رو نشون می‌دن.

اجرا: از api_server.py توی lifespan به صورت یک ترد پس‌زمینه استارت می‌شه، پس
نیازی به سرویس یا پروسه‌ی جدا نیست و به همون data.db وصل می‌شه.

نکته: اگر می‌خوای این دستورها توی یک گروه هم کار کنن، باید privacy mode بات رو
غیرفعال کنی: به @BotFather پیام بده -> /setprivacy -> ربات رو انتخاب کن -> Disable.
در چت خصوصی (که همین الان استفاده می‌کنی) نیازی به این کار نیست.
"""
import datetime
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import notify

log = logging.getLogger("niak.bot")

TOKEN = os.environ.get("NIAK_TELEGRAM_TOKEN", "").strip()
ALLOWED_CHATS = {c.strip() for c in os.environ.get("NIAK_TELEGRAM_CHAT_ID", "").split(",") if c.strip()}
API = f"https://api.telegram.org/bot{TOKEN}"
POLL_TIMEOUT = 30  # seconds, Telegram long-poll wait


def _api_get(method: str, **params):
    url = f"{API}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=POLL_TIMEOUT + 10) as resp:
        return json.loads(resp.read())


def _send(chat_id: str, text: str) -> None:
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        f"{API}/sendMessage", data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                log.warning("sendMessage responded %s", resp.status)
    except Exception as e:
        log.warning("sendMessage failed: %s", e)


def _connect(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(db_path, timeout=10)


def _fmt_row(date: str, court: int, start: int, dur: int, name: str, phone: str, code: str) -> str:
    end = start + dur
    fa = notify.fa_digits
    return (
        f"📅 {notify.jalali_label(date)} | 🏟 زمین {fa(court + 1)} | "
        f"{hour_label(start)}-{hour_label(end)}\n👤 {name} | 📞 {phone} | 🔖 {code}"
    )


def cmd_today(db_path: str) -> str:
    today = datetime.date.today().isoformat()
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT date, court, start_hour, duration, name, phone, code"
            " FROM bookings WHERE date = ? ORDER BY court, start_hour",
            [today],
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return f"📅 {notify.jalali_label(today)}\n\nهیچ رزروی برای امروز ثبت نشده."
    header = f"📅 رزروهای امروز — {notify.jalali_label(today)} ({notify.fa_digits(len(rows))} مورد)\n"
    return header + "\n\n".join(_fmt_row(*r) for r in rows)


def cmd_find(db_path: str, query: str) -> str:
    digits = query.strip()
    if len(digits) < 3:
        return "برای جست‌وجو حداقل ۳ رقم از شماره تلفن رو بفرست. مثال: /find 0912"
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT date, court, start_hour, duration, name, phone, code"
            " FROM bookings WHERE phone LIKE ? ORDER BY date DESC, start_hour DESC LIMIT 15",
            [f"%{digits}%"],
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return f"چیزی برای «{digits}» پیدا نشد."
    header = f"🔎 نتایج برای «{digits}»:\n"
    return header + "\n\n".join(_fmt_row(*r) for r in rows)


# ——— محدوده‌ی مجاز باشگاه (هم‌راستا با api_server.py) ———
START_HOUR = 7
END_HOUR = 26
hour_label = notify.hour_label
COURTS = (0, 1)

FA_TO_EN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _parse_date(token: str):
    """«امروز»/«فردا»/«today»/«tomorrow»/«-» یا YYYY-MM-DD → تاریخ ISO."""
    t = token.strip().translate(FA_TO_EN_DIGITS).lower()
    today = datetime.date.today()
    if t in ("-", "", "same", "همون", "همان"):
        return ""          # یعنی تاریخ عوض نمی‌شه
    if t in ("today", "امروز"):
        return today.isoformat()
    if t in ("tomorrow", "فردا"):
        return (today + datetime.timedelta(days=1)).isoformat()
    t = t.replace("/", "-").replace(".", "-")
    try:
        return datetime.date.fromisoformat(t).isoformat()
    except ValueError:
        return None


def _parse_int(token: str):
    t = token.strip().translate(FA_TO_EN_DIGITS)
    t = t.split(":")[0]                     # «۱۹:۰۰» → ۱۹
    try:
        return int(t)
    except ValueError:
        return None


def _taken_hours(conn, date: str, court: int, exclude_booking_id=None) -> set:
    sql = "SELECT hour FROM slots WHERE date = ? AND court = ?"
    args = [date, court]
    if exclude_booking_id is not None:
        sql += " AND booking_id != ?"
        args.append(exclude_booking_id)
    return {r[0] for r in conn.execute(sql, args).fetchall()}


def cmd_free(db_path: str, arg: str) -> str:
    """/free [تاریخ] — ساعت‌های خالی هر زمین."""
    parts = arg.split() if arg.strip() else []
    date = _parse_date(parts[0]) if parts else datetime.date.today().isoformat()
    if date is None:
        return "تاریخ نامعتبره. مثال: /free 2026-09-20  یا  /free فردا"
    if not date:
        date = datetime.date.today().isoformat()

    fa = notify.fa_digits
    conn = _connect(db_path)
    try:
        lines = [f"🗓 ساعت‌های خالی — {notify.jalali_label(date)}"]
        for court in COURTS:
            taken = _taken_hours(conn, date, court)
            free = [h for h in range(START_HOUR, END_HOUR) if h not in taken]
            body = "، ".join(hour_label(h) for h in free) if free else "هیچ ساعتی خالی نیست"
            lines.append(f"\n🏟 زمین {fa(court + 1)}:\n{body}")
        return "\n".join(lines)
    finally:
        conn.close()


MOVE_USAGE = (
    "استفاده: /move <کد رزرو> <تاریخ|-> <ساعت شروع> [زمین] [مدت]\n\n"
    "مثال‌ها:\n"
    "/move NIAK-20260920-118 - 19            (همان روز، ساعت ۱۹)\n"
    "/move NIAK-20260920-118 فردا 18 2       (فردا، ساعت ۱۸، زمین ۲)\n"
    "/move NIAK-20260920-118 2026-09-25 20 1 3"
)


def cmd_move(db_path: str, arg: str) -> str:
    """جابه‌جایی رزرو به تاریخ/ساعت/زمین دیگر.

    کل کار داخل یک تراکنش `BEGIN IMMEDIATE` انجام می‌شه: اول ساعت‌های قبلی آزاد
    می‌شن، بعد ساعت‌های جدید درج می‌شن. اگر ساعت جدید گرفته باشه، تراکنش برمی‌گرده
    — یعنی رزرو قبلی دست‌نخورده سر جاش می‌مونه.
    """
    parts = arg.split()
    if len(parts) < 3:
        return MOVE_USAGE

    code = parts[0].strip()
    new_date = _parse_date(parts[1])
    if new_date is None:
        return "تاریخ نامعتبره (فرمت: YYYY-MM-DD یا «امروز»/«فردا» یا «-» برای همان روز).\n\n" + MOVE_USAGE
    new_start = _parse_int(parts[2])
    if new_start is None:
        return "ساعت شروع نامعتبره.\n\n" + MOVE_USAGE
    if new_start in (0, 1):
        # باشگاه ۷ صبح باز می‌شود، پس «۰» و «۱» یعنی بامدادِ روز بعد (۲۴/۲۵)
        new_start += 24

    new_court = None
    if len(parts) > 3:
        c = _parse_int(parts[3])
        if c not in (1, 2):
            return "شماره زمین باید ۱ یا ۲ باشه.\n\n" + MOVE_USAGE
        new_court = c - 1                     # ورودی ۱/۲ → مقدار داخلی ۰/۱

    new_dur = None
    if len(parts) > 4:
        d = _parse_int(parts[4])
        if d not in (1, 2, 3):
            return "مدت باید ۱، ۲ یا ۳ ساعت باشه.\n\n" + MOVE_USAGE
        new_dur = d

    fa = notify.fa_digits
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, date, court, start_hour, duration, name, phone"
            " FROM bookings WHERE code = ?",
            [code],
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return f"رزروی با کد «{code}» پیدا نشد. با /find <شماره> پیداش کن."

        booking_id, old_date, old_court, old_start, old_dur, name, phone = row
        date = new_date or old_date
        court = old_court if new_court is None else new_court
        dur = old_dur if new_dur is None else new_dur

        if not (START_HOUR <= new_start and new_start + dur <= END_HOUR):
            conn.execute("ROLLBACK")
            return f"بازه خارج از ساعت کاری باشگاه است ({hour_label(START_HOUR)} تا {hour_label(END_HOUR)})."

        if (date, court, new_start, dur) == (old_date, old_court, old_start, old_dur):
            conn.execute("ROLLBACK")
            return "این رزرو همین حالا هم دقیقاً همین‌جاست؛ چیزی عوض نشد."

        hours = list(range(new_start, new_start + dur))
        taken = _taken_hours(conn, date, court, exclude_booking_id=booking_id)
        clash = [h for h in hours if h in taken]
        if clash:
            conn.execute("ROLLBACK")
            busy = "، ".join(hour_label(h) for h in clash)
            return (f"⛔️ جابه‌جایی انجام نشد — این ساعت‌ها روی زمین {fa(court + 1)} "
                    f"قبلاً رزرو شده‌اند: {busy}\n\nرزرو قبلی دست‌نخورده سر جاشه. "
                    f"با /free {date} ساعت‌های خالی را ببین.")

        conn.execute("DELETE FROM slots WHERE booking_id = ?", [booking_id])
        conn.executemany(
            "INSERT INTO slots (date, court, hour, booking_id) VALUES (?,?,?,?)",
            [(date, court, h, booking_id) for h in hours],
        )
        conn.execute(
            "UPDATE bookings SET date = ?, court = ?, start_hour = ?, duration = ? WHERE id = ?",
            [date, court, new_start, dur, booking_id],
        )
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        return "⛔️ جابه‌جایی انجام نشد چون یکی از ساعت‌های جدید همین لحظه رزرو شد. رزرو قبلی سر جاشه."
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    return (
        "🔄 رزرو جابه‌جا شد\n\n"
        f"از: {notify.jalali_label(old_date)} | زمین {fa(old_court + 1)} | "
        f"{hour_label(old_start)}-{hour_label(old_start + old_dur)}\n"
        f"به: {notify.jalali_label(date)} | زمین {fa(court + 1)} | "
        f"{hour_label(new_start)}-{hour_label(new_start + dur)}\n\n"
        f"👤 {name} | 📞 {phone} | 🔖 {code}"
    )


def cmd_day(db_path: str, arg: str) -> str:
    """/day <تاریخ> — همه رزروهای یک روز."""
    date = _parse_date(arg) if arg.strip() else datetime.date.today().isoformat()
    if date is None:
        return "تاریخ نامعتبره. مثال: /day 2026-09-20  یا  /day فردا"
    if not date:
        date = datetime.date.today().isoformat()
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT date, court, start_hour, duration, name, phone, code"
            " FROM bookings WHERE date = ? ORDER BY court, start_hour",
            [date],
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return f"📅 {notify.jalali_label(date)}\n\nهیچ رزروی برای این روز ثبت نشده."
    header = f"📅 {notify.jalali_label(date)} ({notify.fa_digits(len(rows))} مورد)\n"
    return header + "\n\n".join(_fmt_row(*r) for r in rows)


def cmd_cancel(db_path: str, code: str) -> str:
    code = code.strip()
    if not code:
        return "استفاده: /cancel <کد رزرو>  — مثال: /cancel NIAK-20260920-118"
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, date, court, start_hour, duration, name, phone"
            " FROM bookings WHERE code = ?",
            [code],
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return f"رزروی با کد «{code}» پیدا نشد."
        booking_id, date, court, start, dur, name, phone = row
        conn.execute("DELETE FROM slots WHERE booking_id = ?", [booking_id])
        conn.execute("DELETE FROM bookings WHERE id = ?", [booking_id])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return "✅ رزرو لغو شد و ساعتش آزاد شد\n\n" + _fmt_row(date, court, start, dur, name, phone, code)


HELP_TEXT = (
    "دستورهای موجود:\n\n"
    "/today — رزروهای امروز\n"
    "/day <تاریخ> — رزروهای یک روز دیگر (مثال: /day فردا)\n"
    "/free [تاریخ] — ساعت‌های خالی هر زمین (مثال: /free 2026-09-20)\n"
    "/find <بخشی از شماره تلفن> — جست‌وجوی رزرو (مثال: /find 0912)\n"
    "/move <کد> <تاریخ|-> <ساعت> [زمین] [مدت] — جابه‌جایی رزرو\n"
    "     مثال: /move NIAK-20260920-118 فردا 19 2\n"
    "/cancel <کد رزرو> — لغو رزرو و آزاد کردن ساعتش (مثال: /cancel NIAK-20260920-118)\n"
    "/help — همین راهنما\n\n"
    "هر تغییری که اینجا بدهی بلافاصله روی سایت هم دیده می‌شود."
)


def handle_update(update: dict, db_path: str) -> None:
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return

    if ALLOWED_CHATS and chat_id not in ALLOWED_CHATS:
        log.warning("ignored command from unauthorized chat_id=%s", chat_id)
        return

    parts = text.split(maxsplit=1)
    cmd = parts[0].split("@")[0].lower()  # /cancel@NiakpadelBot -> /cancel
    arg = parts[1].strip() if len(parts) > 1 else ""

    try:
        if cmd == "/today":
            reply = cmd_today(db_path)
        elif cmd == "/find":
            reply = cmd_find(db_path, arg)
        elif cmd in ("/day", "/date"):
            reply = cmd_day(db_path, arg)
        elif cmd in ("/free", "/empty"):
            reply = cmd_free(db_path, arg)
        elif cmd in ("/move", "/change"):
            reply = cmd_move(db_path, arg)
        elif cmd == "/cancel":
            reply = cmd_cancel(db_path, arg)
        elif cmd in ("/help", "/start"):
            reply = HELP_TEXT
        else:
            reply = "دستور ناشناخته. /help رو بفرست."
    except Exception:
        log.exception("command %s failed", cmd)
        reply = "یه خطا پیش اومد، دوباره امتحان کن."

    _send(chat_id, reply)


def poll_loop(db_path: str, stop_event: threading.Event) -> None:
    if not TOKEN:
        log.warning("NIAK_TELEGRAM_TOKEN not set — bot commands disabled")
        return
    if not ALLOWED_CHATS:
        log.warning("NIAK_TELEGRAM_CHAT_ID not set — bot commands disabled (no authorized chat)")
        return

    # اگر قبلاً webhook تنظیم شده باشه، getUpdates کار نمی‌کنه — پاکش می‌کنیم.
    try:
        _api_get("deleteWebhook")
    except Exception as e:
        log.warning("deleteWebhook failed (may be harmless): %s", e)

    offset = 0
    log.info("telegram command polling started")
    while not stop_event.is_set():
        try:
            data = _api_get("getUpdates", timeout=POLL_TIMEOUT, offset=offset)
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                handle_update(update, db_path)
        except Exception as e:
            log.warning("poll error: %s", e)
            time.sleep(5)


def start_background(db_path: str) -> threading.Event:
    """Starts the polling loop in a daemon thread; returns an event to stop it."""
    stop_event = threading.Event()
    threading.Thread(target=poll_loop, args=(db_path, stop_event), daemon=True).start()
    return stop_event


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")
    ev = start_background(_db)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        ev.set()
