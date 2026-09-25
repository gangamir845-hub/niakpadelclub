#!/usr/bin/env python3
"""
اعلان رزرو جدید — تلگرام و ایمیل.

هیچ‌کدام از این توابع نباید ثبت رزرو را خراب کند، پس همه خطاها گرفته و فقط
لاگ می‌شوند. اعلان‌ها در پس‌زمینه (BackgroundTasks) اجرا می‌شوند.

تنظیمات با متغیرهای محیطی:
    NIAK_TELEGRAM_TOKEN    توکنی که @BotFather می‌دهد
    NIAK_TELEGRAM_CHAT_ID  آیدی چت خودت یا گروه باشگاه (چند مورد با کاما)
    NIAK_SMTP_HOST / NIAK_SMTP_PORT / NIAK_SMTP_USER / NIAK_SMTP_PASS
    NIAK_MAIL_TO           گیرنده ایمیل (چند مورد با کاما)
"""
import json
import logging
import os
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

log = logging.getLogger("niak.notify")

TELEGRAM_TOKEN = os.environ.get("NIAK_TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHATS = [c.strip() for c in os.environ.get("NIAK_TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
SMTP_HOST = os.environ.get("NIAK_SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("NIAK_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("NIAK_SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("NIAK_SMTP_PASS", "")
MAIL_TO = [m.strip() for m in os.environ.get("NIAK_MAIL_TO", "").split(",") if m.strip()]

PERSIAN_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
                  "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
PERSIAN_WEEKDAYS = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]

_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa_digits(value) -> str:
    return str(value).translate(_DIGITS)


def hour_label(hour: int) -> str:
    """برچسب ساعت باشگاه.

    ساعت‌های ۲۴ و ۲۵ یعنی ۰۰:۰۰ و ۰۱:۰۰ بامدادِ روزِ بعد، ولی به تاریخِ همان
    شبِ کاری بسته‌اند؛ پس برای نمایش به ۰۰/۰۱ برگردانده می‌شوند.
    """
    if hour >= 24:
        return "۰" + fa_digits(hour - 24) + ":۰۰"
    return fa_digits(hour) + ":۰۰"


def g2j(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """تبدیل تاریخ میلادی به شمسی (الگوریتم jalaali-js)."""
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy - 1600
    gm2 = gm - 1
    gd2 = gd - 1
    g_day_no = (365 * gy2 + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
                + gd2 + g_d_m[gm2])
    if gm > 2 and ((gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0):
        g_day_no += 1

    j_day_no = g_day_no - 79
    j_np = j_day_no // 12053
    j_day_no %= 12053
    jy = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365
    for i in range(11):
        month_len = 31 if i < 6 else 30
        if j_day_no < month_len:
            return jy, i + 1, j_day_no + 1
        j_day_no -= month_len
    return jy, 12, j_day_no + 1


def jalali_label(iso_date: str) -> str:
    """'2026-09-20' -> 'یکشنبه ۲۹ شهریور ۱۴۰۵'"""
    try:
        import datetime
        gy, gm, gd = (int(p) for p in iso_date.split("-"))
        jy, jm, jd = g2j(gy, gm, gd)
        weekday = PERSIAN_WEEKDAYS[datetime.date(gy, gm, gd).weekday()]
        return f"{weekday} {fa_digits(jd)} {PERSIAN_MONTHS[jm - 1]} {fa_digits(jy)}"
    except Exception:
        return iso_date


def build_message(b: dict) -> str:
    """متن اعلان برای یک رزرو تازه."""
    end = b["start"] + b["duration"]
    return (
        "🎾 رزرو جدید ثبت شد\n\n"
        f"📅 {jalali_label(b['date'])}\n"
        f"🏟 زمین {fa_digits(b['court'] + 1)}\n"
        f"⏰ {hour_label(b['start'])} تا {hour_label(end)}"
        f" ({fa_digits(b['duration'])} ساعته)\n"
        f"👤 {b['name']}\n"
        f"📞 {b['phone']}\n"
        f"🔖 کد: {b['code']}"
    )


def build_cancel_message(b: dict) -> str:
    """متن اعلان وقتی مشتری خودش رزرو را از سایت لغو می‌کند."""
    end = b["start"] + b["duration"]
    return (
        "❌ یک رزرو از سایت لغو شد\n\n"
        f"📅 {jalali_label(b['date'])}\n"
        f"🏟 زمین {fa_digits(b['court'] + 1)}\n"
        f"⏰ {hour_label(b['start'])} تا {hour_label(end)}"
        f" ({fa_digits(b['duration'])} ساعته)\n"
        f"👤 {b['name']}\n"
        f"📞 {b['phone']}\n"
        f"🔖 کد: {b['code']}\n\n"
        "این ساعت‌ها دوباره آزاد شدند.\n"
        "یادآوری: با همین شماره تماس بگیر و ۹۲ درصد مبلغ را برگردان."
    )


def _send_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHATS:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for chat_id in TELEGRAM_CHATS:
        payload = json.dumps({
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }).encode()
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    log.warning("telegram responded %s", resp.status)
        except urllib.error.HTTPError as e:
            log.warning("telegram error %s: %s", e.code, e.read()[:200])
        except Exception as e:  # network down, DNS, timeout…
            log.warning("telegram failed: %s", e)


def _send_email(subject: str, text: str) -> None:
    if not (SMTP_HOST and MAIL_TO):
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_USER or "niak@localhost"
    msg["To"] = ", ".join(MAIL_TO)
    msg.set_content(text)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.starttls()
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    except Exception as e:
        log.warning("email failed: %s", e)


def notify_new_booking(booking: dict) -> None:
    """در پس‌زمینه صدا زده می‌شود؛ هیچ خطایی به بالا پرت نمی‌شود."""
    try:
        text = build_message(booking)
        _send_telegram(text)
        _send_email("رزرو جدید — NIAK Padel Club", text)
    except Exception as e:
        log.warning("notify_new_booking failed: %s", e)


def notify_cancelled_booking(booking: dict) -> None:
    """در پس‌زمینه صدا زده می‌شود؛ هیچ خطایی به بالا پرت نمی‌شود."""
    try:
        text = build_cancel_message(booking)
        _send_telegram(text)
        _send_email("لغو رزرو — NIAK Padel Club", text)
    except Exception as e:
        log.warning("notify_cancelled_booking failed: %s", e)


def channels_status() -> dict:
    return {
        "telegram": bool(TELEGRAM_TOKEN and TELEGRAM_CHATS),
        "email": bool(SMTP_HOST and MAIL_TO),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sample = {"date": "2026-09-20", "court": 0, "start": 18, "duration": 2,
              "name": "تست اعلان", "phone": "09120000000", "code": "NIAK-TEST"}
    print(build_message(sample))
    print("channels:", channels_status())
    notify_new_booking(sample)
