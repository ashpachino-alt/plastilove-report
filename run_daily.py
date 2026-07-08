#!/usr/bin/env python3
"""
run_daily.py — ежедневный автоматический прогон: fetch → compute → Excel → Telegram.
Запускается по расписанию в 9:00 (см. deploy/ниже). Период = текущий календарный месяц (1 → сегодня).

Использование:
  python run_daily.py                       # текущий месяц, оперативный
  python run_daily.py --period 2026-06      # конкретный месяц (для пересчёта вручную)
  python run_daily.py --final               # финальное закрытие (KPI Ozon в команду)
  python run_daily.py --skip-fetch          # не тянуть заново, считать по уже скачанному сырью

.env: ключи Ozon/WB (для fetch) + TG_BOT_TOKEN / TG_CHAT_ID (+ опц. CROSS_DOCK, COST).
"""

import argparse
import calendar
import subprocess
import sys
from datetime import date
from pathlib import Path

import compute as C
import report as R
import xlsx_report as X

BASE = Path(__file__).resolve().parent


def month_bounds(period: str | None):
    if period:
        y, m = map(int, period.split("-"))
        d_from = date(y, m, 1)
        # если это текущий месяц — до сегодня, иначе — до конца месяца
        today = date.today()
        if (y, m) == (today.year, today.month):
            d_to = today
        else:
            d_to = date(y, m, calendar.monthrange(y, m)[1])
    else:
        today = date.today()
        y, m = today.year, today.month
        d_from = date(y, m, 1)
        d_to = today
    return f"{y:04d}-{m:02d}", d_from.isoformat(), d_to.isoformat()


def run_fetch(date_from: str, date_to: str, out: Path):
    print(f"[daily] fetch {date_from} → {date_to} → {out}")
    cmd = [sys.executable, str(BASE / "fetch.py"),
           "--from", date_from, "--to", date_to, "--out", str(out)]
    subprocess.run(cmd, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", help="YYYY-MM (по умолчанию текущий месяц)")
    ap.add_argument("--cost", type=int, default=C.COST_CORPUS)
    ap.add_argument("--cross-dock", type=float, default=None,
                    help="Кросс-докинг WB, ₽ (или из env CROSS_DOCK)")
    ap.add_argument("--final", action="store_true")
    ap.add_argument("--skip-fetch", action="store_true", help="Не тянуть сырьё заново")
    ap.add_argument("--no-send", action="store_true", help="Не отправлять в Telegram (тест)")
    args = ap.parse_args()

    import os
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env")

    cross = args.cross_dock if args.cross_dock is not None else float(os.getenv("CROSS_DOCK", "0"))

    period, d_from, d_to = month_bounds(args.period)
    raw = BASE / "raw" / period

    # 1) FETCH
    if not args.skip_fetch:
        run_fetch(d_from, d_to, raw)
    else:
        print(f"[daily] fetch пропущен, считаю по {raw}")

    # 2) COMPUTE
    res = C.compute_all(raw, cost=args.cost, cross_dock=cross, final=args.final)
    C.print_report(res)

    # 3) EXCEL → reports/<period>/
    xlsx_path = BASE / X.out_path_for(period)
    X.build(res, period, xlsx_path)
    print(f"[daily] Excel: {xlsx_path}")

    # 4) TELEGRAM
    msg = R.build_message(res, period)
    if args.no_send:
        print("─── превью (не отправлено) ───")
        print(msg)
    else:
        R.send_telegram(msg, xlsx_path)
        print("[daily] ✓ отправлено в Telegram")


if __name__ == "__main__":
    main()
