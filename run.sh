#!/bin/bash
# راه‌انداز سرور رزرو — تنظیمات از فایل .env خوانده می‌شود
cd "$(dirname "$0")"
set -a; [ -f .env ] && . ./.env; set +a
exec python api_server.py
