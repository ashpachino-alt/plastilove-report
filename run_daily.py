#!/usr/bin/env python3
"""
run_daily.py — ежедневный прогон: Ozon fetch → compute → Excel → Telegram.

По умолчанию: текущий месяц, 1-е число → сегодня (оперативный).
1-го числа: предыдущий месяц целиком (финальный, KPI в команде).

  python run_daily.py                    # по расписанию
  python run_daily.py --period 2026-08 --final
  python run_daily.py --no-send          # превью без отправки
"""

import argparse
import calendar
import html
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import compute as C
import report as R
import xlsx_report as X

BASE = Path(__file__).resolve().parent


def month_bounds(period: str | None):
    today = date.today()
    if period:
        y, m = map(int, period.split("-"))
    elif today.day == 1:
        prev = today - timedelta(days=1)
        y, m = prev.year, prev.month
    else:
        y, m = today.year, today.month
    last = date(y, m, calendar.monthrange(y, m)[1])
    d_to = min(last, today)
    return f"{y:04d}-{m:02d}", date(y, m, 1).isoformat(), d_to.isoformat(), d_to == last and today > last


def alert(text: str, no_send: bool):
    print(text)
    if not no_send:
        try:
            R.send_telegram(text)
        except Exception as e:
            print(f"не удалось отправить алерт: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period")
    ap.add_argument("--final", action="store_true")
    ap.add_argument("--skip-fetch", action="store_true")
    ap.add_argument("--no-send", action="store_true")
    args, _unknown = ap.parse_known_args()   # старые флаги (--cost, --cross-dock) игнорируем

    period, d_from, d_to, month_closed = month_bounds(args.period)
    final = args.final or (month_closed and not args.period)
    raw = BASE / "raw" / period

    if not args.skip_fetch:
        print(f"[daily] fetch {d_from} → {d_to}")
        r = subprocess.run([sys.executable, str(BASE / "fetch.py"), "--from", d_from,
                            "--to", d_to, "--out", str(raw)], capture_output=True, text=True)
        print(r.stdout[-5000:], r.stderr[-3000:])
        if r.returncode != 0:
            tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
            alert("❌ <b>PlastiLove: отчёт Ozon не сформирован</b>\nОшибка выгрузки данных Ozon:\n"
                  + html.escape("\n".join(tail)[:1500]), args.no_send)
            sys.exit(1)

    res = C.compute_all(raw, final=final)
    C.print_report(res)
    oz = res["ozon"]
    if "error" in oz or oz.get("accruals_count", 0) == 0:
        alert(f"⚠️ <b>PlastiLove: нет данных Ozon за {d_from} – {d_to}</b>\n"
              f"{html.escape(oz.get('error', 'Ozon вернул 0 начислений'))}. Отчёт с нулями не отправляю.", args.no_send)
        sys.exit(1)

    xlsx_path = BASE / X.out_path_for(period)
    X.build(res, period, xlsx_path, asof=d_to)
    msg = R.build_message(res, period, asof=d_to)
    if args.no_send:
        print("─── превью ───\n" + msg)
    else:
        R.send_telegram(msg, xlsx_path)
        print("[daily] ✓ отправлено в Telegram")


if __name__ == "__main__":
    main()
