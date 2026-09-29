#!/usr/bin/env python3
"""
report.py — сообщение отчёта (только Ozon) и отправка в Telegram.

  python report.py --raw raw/2026-09 --period 2026-09            # превью
  python report.py --raw raw/2026-09 --period 2026-09 --send     # отправить
"""

import argparse
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

import compute as C
from xlsx_report import RU_MONTHS

load_dotenv(Path(__file__).resolve().parent / ".env")

_f = C._f


def _opt_zone(opt: float) -> str:
    if opt >= 570:
        return "рабочая зона ✅"
    if opt >= 550:
        return "у нижней границы 550–570 ⚠️"
    return "ниже санитарных 550 ❌"


def build_message(res: dict, period: str, asof: str = None) -> str:
    y, m = map(int, period.split("-"))
    oz = res["ozon"]
    final = res["params"]["final"]
    msk = timezone(timedelta(hours=3))
    L = [f"📊 <b>PlastiLove · Ozon · {RU_MONTHS[m]} {y}</b>",
         f"<i>{'✅ финальный' if final else '🟡 оперативный'}</i>"]
    if asof:
        ay, am, ad = map(int, asof.split("-"))
        L.append(f"📅 Данные за 01.{am:02d}–{ad:02d}.{am:02d}.{ay} (по дате начисления Ozon)")
    L.append(f"🕘 Сформирован: {datetime.now(msk):%d.%m.%Y %H:%M} МСК")
    L.append("")

    if "error" in oz:
        L.append(f"❌ {oz['error']}")
        return "\n".join(L)

    icon = "🟢" if oz["owner"] >= 0 else "🔴"
    L.append(f"{icon} <b>Деньги владельца: {_f(oz['owner'])} ₽</b> ({_f(oz['unit_profit'])} ₽/шт)")
    L.append("")
    L.append(f"• Выкупы: {oz['sold_units']} − возвраты {oz['returns_units']} = <b>{oz['net_units']} шт</b>")
    L.append(f"• Чистые продажи: {_f(oz['net_sales'])} ₽")
    L.append(f"• Выплаты Ozon: {_f(oz['payout'])} ₽")
    L.append(f"   в т.ч. удержано: реклама {_f(-oz['ads'])} · кросс-докинг {_f(-oz['crossdock'])}")
    L.append(f"• <b>Опт: {oz['opt']:.0f} ₽/шт</b> — {_opt_zone(oz['opt'])}")
    L.append("")
    L.append(f"− налог УСН 6%: {_f(oz['tax'])} ₽ (база по реализации {_f(oz['tax_base'])})")
    L.append(f"− завод: {_f(oz['factory'])} ₽")
    L.append(f"− короб: {_f(oz['pack'])} ₽")
    L.append(f"= до команды: {_f(oz['before_team'])} ₽")
    kpi = oz["kpi"]
    if final:
        L.append(f"− команда: {_f(oz['team'])} ₽ (оклад 70 000 + KPI {_f(kpi['total'])})")
    else:
        L.append(f"− команда: {_f(oz['team'])} ₽ (оклад)")
        L.append(f"   KPI резерв: {_f(kpi['total'])} ₽ (опт {_f(kpi['opt'])} + объём {_f(kpi['volume'])})")
    L.append("")
    L.append(f"💸 Долг налоговой: {_f(oz['tax'])} ₽ · заводу: {_f(oz['factory'])} ₽")

    top = [r for r in oz["by_sku"] if r["net_units"] > 0][:5]
    if top:
        L.append("")
        L.append("<b>Топ SKU</b> (шт · опт):")
        for r in top:
            L.append(f"   {r['offer']}: {r['net_units']} · {r['opt']:.0f} ₽")

    L.append("")
    notes = []
    if oz["unit_profit"] < 150:
        notes.append(f"прибыль {oz['unit_profit']:.0f} ₽/шт ниже ориентира 150")
    if oz["opt"] < 550:
        notes.append("опт ниже 550 — проверить рекламу и цены")
    if kpi["reason"] != "KPI начислен по факту":
        notes.append(f"KPI: {kpi['reason']}")
    if notes:
        L.append("⚠️ " + "; ".join(notes))
    return "\n".join(L)


def send_telegram(text: str, xlsx: Path | None = None) -> dict:
    token, chat = os.getenv("TG_BOT_TOKEN"), os.getenv("TG_CHAT_ID")
    if not token or not chat:
        raise SystemExit("ERROR: TG_BOT_TOKEN / TG_CHAT_ID не заданы")
    api = f"https://api.telegram.org/bot{token}"
    r = requests.post(f"{api}/sendMessage", json={
        "chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
        "disable_web_page_preview": True}, timeout=30)
    r.raise_for_status()
    if xlsx and Path(xlsx).exists():
        with open(xlsx, "rb") as f:
            requests.post(f"{api}/sendDocument", data={"chat_id": chat},
                          files={"document": f}, timeout=120).raise_for_status()
    return r.json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--period", required=True)
    ap.add_argument("--final", action="store_true")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--xlsx")
    ap.add_argument("--asof")
    a = ap.parse_args()
    res = C.compute_all(a.raw, final=a.final)
    msg = build_message(res, a.period, asof=a.asof)
    if a.send:
        send_telegram(msg, Path(a.xlsx) if a.xlsx else None)
        print("✓ Отправлено в Telegram")
    else:
        print(msg)


if __name__ == "__main__":
    main()
