#!/usr/bin/env python3
"""
report.py — собирает красивое сообщение отчёта и отправляет в Telegram.

Использование:
  python report.py --raw ./raw/2026-06 --period 2026-06            # печать превью (не отправляет)
  python report.py --raw ./raw/2026-06 --period 2026-06 --send     # отправить в Telegram
  python report.py --raw ./raw/2026-06 --period 2026-06 --send --xlsx reports/2026-06/Отчёт_Июнь_2026.xlsx

.env:
  TG_BOT_TOKEN=123456:ABC...        (создать у @BotFather)
  TG_CHAT_ID=123456789              (свой chat_id, узнать у @userinfobot)
"""

import argparse
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

import compute as C
from xlsx_report import RU_MONTHS

load_dotenv()


def _f(x):
    return f"{round(x):,}".replace(",", " ")


def build_message(res: dict, period: str) -> str:
    y, m = map(int, period.split("-"))
    title = f"{RU_MONTHS[m]} {y}"
    oz, wb, t = res["ozon"], res["wb"], res["total"]
    final = res["params"]["final"]
    status = "✅ финальный" if final else "🟡 предварительный"

    L = []
    L.append(f"📊 <b>Отчёт PlastiLove — {title}</b>")
    L.append(f"<i>{status}</i>")
    L.append("")

    # Итог первым делом — деньги владельца
    owner = t["owner"]
    icon = "🟢" if owner >= 0 else "🔴"
    L.append(f"{icon} <b>Деньги владельца: {_f(owner)} ₽</b>")
    L.append(f"Выкупы: {_f(t['net_units'])} шт · Продажи: {_f(t['net_sales'])} ₽")
    L.append("")

    # OZON
    if "error" not in oz:
        oi = "🟢" if oz["owner"] >= 0 else "🔴"
        L.append(f"<b>🟦 OZON</b>  {oi} {_f(oz['owner'])} ₽")
        L.append(f"• выкупы {oz['net_units']} шт · опт <b>{oz['opt']:.0f} ₽/шт</b>")
        L.append(f"• выплаты {_f(oz['payout'])} · налог −{_f(oz['tax'])} · завод −{_f(oz['factory'])}")
        kpi = oz["kpi"]["total"]
        kpi_word = "в команде" if final else "резерв"
        L.append(f"• команда {_f(oz['team'])} (KPI {kpi_word}: {_f(kpi)} ₽)")
        L.append("")

    # WB
    if "error" not in wb:
        wi = "🟢" if wb["owner"] >= 0 else "🔴"
        L.append(f"<b>🟪 WILDBERRIES</b>  {wi} {_f(wb['owner'])} ₽")
        L.append(f"• выкупы {wb['net_units']} шт · опт <b>{wb['opt']:.0f} ₽/шт</b>")
        L.append(f"• выплаты {_f(wb['payout'])} · реклама −{_f(wb['adv'])}")
        d = wb["deductions"]
        L.append(f"• логистика −{_f(d['logistics'])} · хранение −{_f(d['storage'])}")
        if wb["cross_dock_missing"]:
            L.append("• ⚠️ кросс-докинг не передан (в расчёте 0)")
        L.append("")

    # Долги
    L.append(f"💸 Долг налоговой: {_f(t['debt_tax'])} ₽ · заводу: {_f(t['debt_factory'])} ₽")

    # Короткий вердикт
    if "error" not in oz and "error" not in wb:
        winner = "Ozon" if oz["owner"] >= wb["owner"] else "WB"
        L.append("")
        verdict = f"Месяц сделал <b>{winner}</b>."
        if wb["owner"] < 0:
            verdict += " WB в минусе — логистика/хранение на перезатаре; разгружать склад, рекламу резать."
        L.append(verdict)

    return "\n".join(L)


def send_telegram(text: str, xlsx: Path | None = None) -> dict:
    token = os.getenv("TG_BOT_TOKEN")
    chat  = os.getenv("TG_CHAT_ID")
    if not token or not chat:
        raise SystemExit("ERROR: TG_BOT_TOKEN / TG_CHAT_ID не найдены в .env")

    api = f"https://api.telegram.org/bot{token}"
    # 1) текст
    r = requests.post(f"{api}/sendMessage", json={
        "chat_id": chat, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=30)
    r.raise_for_status()
    res = r.json()
    # 2) Excel-файл (если есть)
    if xlsx and Path(xlsx).exists():
        with open(xlsx, "rb") as f:
            rd = requests.post(f"{api}/sendDocument", data={"chat_id": chat},
                               files={"document": f}, timeout=120)
        rd.raise_for_status()
    return res


def main():
    ap = argparse.ArgumentParser(description="Отчёт PlastiLove в Telegram")
    ap.add_argument("--raw", required=True)
    ap.add_argument("--period", required=True, help="YYYY-MM")
    ap.add_argument("--cost", type=int, default=C.COST_CORPUS)
    ap.add_argument("--cross-dock", type=float, default=0)
    ap.add_argument("--final", action="store_true")
    ap.add_argument("--send", action="store_true", help="Отправить в Telegram (иначе только печать)")
    ap.add_argument("--xlsx", help="Приложить Excel-файл к сообщению")
    args = ap.parse_args()

    res = C.compute_all(args.raw, cost=args.cost, cross_dock=args.cross_dock, final=args.final)
    msg = build_message(res, args.period)

    if args.send:
        send_telegram(msg, Path(args.xlsx) if args.xlsx else None)
        print("✓ Отправлено в Telegram")
    else:
        print("─── ПРЕВЬЮ TELEGRAM (HTML) ───")
        print(msg)
        print("──────────────────────────────")
        print("Для отправки добавь --send (нужны TG_BOT_TOKEN и TG_CHAT_ID в .env)")


if __name__ == "__main__":
    main()
