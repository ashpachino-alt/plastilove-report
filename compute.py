#!/usr/bin/env python3
"""
compute.py — расчёт управленческой юнит-экономики по сырью fetch.py.
Только чтение сырья и расчёт. Без Telegram, без Excel (это report.py / xlsx_report.py).

Использование:
  python compute.py --raw ./raw/2026-06
  python compute.py --raw ./raw/2026-06 --final          # финальное закрытие: KPI Ozon в команду
  python compute.py --raw ./raw/2026-06 --cross-dock 25000
  python compute.py --raw ./raw/2026-06 --json out.json   # результат в JSON

Логика строго по инструкции проекта PlastiLove (Ozon + WB).
"""

import argparse
import glob
import json
import os
from collections import defaultdict
from pathlib import Path

# ──────────────────────────────────────────────
# КОНСТАНТЫ (справочник инструкции)
# ──────────────────────────────────────────────
TAX_RATE       = 0.07          # УСН 7% от чистых продаж
COST_CORPUS    = 250           # себестоимость с корпусом, ₽/шт (короб 15 ₽ уже внутри)
COST_NOCORPUS  = 170           # себестоимость без корпуса, ₽/шт
BOX_PER_UNIT   = 7             # транспортный короб (корпусные SKU): 45 ₽ / 6 компл ≈ 7 ₽/шт
OZON_SALARY    = 70_000        # оклад команды Ozon, ₽/мес
WB_ASSISTANT   = 0             # ассистент WB, ₽/мес (больше не платим)

OZON_DELIVER   = "Доставка покупателю"
OZON_RETURN    = "Получение возврата, отмены, невыкупа от покупателя"


# ──────────────────────────────────────────────
# Загрузка сырья (гибко под разные имена файлов)
# ──────────────────────────────────────────────
def _find(raw: Path, *candidates) -> Path | None:
    """Ищет первый существующий файл по списку шаблонов (glob), рекурсивно."""
    for pat in candidates:
        # прямой путь
        p = raw / pat
        if p.exists():
            return p
        # glob по всему дереву
        hits = sorted(glob.glob(str(raw / "**" / pat), recursive=True))
        if hits:
            return Path(hits[0])
    return None


def _load(path: Path | None):
    if not path or not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_raw(raw: Path) -> dict:
    """Возвращает словарь с исходными данными по известным источникам."""
    return {
        "ozon_tx":     _load(_find(raw, "ozon_transactions.json", "transactions_*.json", "ozon_transactions*.json")),
        "wb_report":   _load(_find(raw, "wb_report_detail.json", "report_detail.json", "wb_report*.json")),
        "wb_adv":      _load(_find(raw, "wb_adv.json", "adv_upd.json", "wb_adv*.json")),
    }


def _sum(rows, field):
    return sum((r.get(field) or 0) for r in rows)


# ──────────────────────────────────────────────
# KPI Ozon (схема п.10 инструкции)
# ──────────────────────────────────────────────
def ozon_kpi(net_units: int, opt: float) -> dict:
    """Возвращает {volume, opt, total, reason} — бонус KPI Ozon, ₽."""
    reasons = []
    # Вход
    if net_units < 1200:
        return {"volume": 0, "opt": 0, "total": 0,
                "reason": f"не добрали объём: {net_units} < 1200 (порог входа) → KPI 0"}

    # Ось объёма (только если опт ≥ 550 — защита от демпинга)
    vol_bonus = 0
    if opt >= 550:
        for thr, bonus in ((1800, 15000), (1500, 10000), (1300, 5000)):
            if net_units >= thr:
                vol_bonus = bonus
                break
    else:
        reasons.append(f"объёмный бонус не начислен: опт {opt:.0f} < 550 (демпинг-защита)")

    # Ось опта (только если объём ≥ 1200; санитарная граница 550)
    opt_bonus = 0
    if opt < 550:
        reasons.append(f"опт-бонус 0: опт {opt:.0f} < 550 (санитарная граница)")
    else:
        for thr, bonus in ((580, 15000), (570, 10000), (560, 5000)):
            if opt >= thr:
                opt_bonus = bonus
                break
        if opt_bonus == 0:
            reasons.append(f"опт {opt:.0f} ниже ступени 560 → опт-бонус 0")

    total = vol_bonus + opt_bonus
    return {"volume": vol_bonus, "opt": opt_bonus, "total": total,
            "reason": "; ".join(reasons) if reasons else "KPI начислен по факту"}


# ──────────────────────────────────────────────
# OZON
# ──────────────────────────────────────────────
def compute_ozon(tx, cost=COST_CORPUS, final=False) -> dict:
    if not tx:
        return {"error": "нет ozon_transactions.json"}
    deliv = [t for t in tx if t.get("operation_type_name") == OZON_DELIVER]
    ret   = [t for t in tx if t.get("operation_type_name") == OZON_RETURN]

    net_units  = len(deliv) - len(ret)
    gross      = _sum(deliv, "accruals_for_sale")
    ret_sum    = abs(_sum(ret, "accruals_for_sale"))
    net_sales  = gross - ret_sum
    payout     = round(_sum(tx, "amount"), 2)                 # выплаты Ozon (нетто, реклама уже внутри)
    opt        = payout / net_units if net_units else 0

    tax     = round(net_sales * TAX_RATE, 2)
    factory = net_units * cost
    pack    = net_units * BOX_PER_UNIT
    kpi     = ozon_kpi(net_units, opt)
    team    = OZON_SALARY + (kpi["total"] if final else 0)     # KPI в команду только при финале
    owner   = round(payout - tax - factory - pack - team, 2)

    return {
        "net_units": net_units, "gross_sales": round(gross, 2), "returns_sum": round(ret_sum, 2),
        "net_sales": round(net_sales, 2), "payout": payout, "opt": round(opt, 2),
        "tax": tax, "factory": factory, "pack": pack,
        "salary": OZON_SALARY, "kpi": kpi, "kpi_in_team": final,
        "team": team, "owner": owner,
        "deliveries": len(deliv), "returns_count": len(ret),
    }


# ──────────────────────────────────────────────
# WB
# ──────────────────────────────────────────────
def compute_wb(report, adv, cost=COST_CORPUS, cross_dock=0) -> dict:
    if not report:
        return {"error": "нет wb_report_detail.json"}
    byop = defaultdict(list)
    for r in report:
        byop[r.get("supplier_oper_name")].append(r)
    sales = byop.get("Продажа", [])
    rets  = byop.get("Возврат", [])

    net_units = int(_sum(sales, "quantity") - _sum(rets, "quantity"))
    sales_rub = _sum(sales, "retail_amount") - _sum(rets, "retail_amount")

    logistics = _sum(report, "delivery_rub")
    storage   = _sum(report, "storage_fee")
    deduction = _sum(report, "deduction")
    penalty   = _sum(report, "penalty")
    rebill    = _sum(report, "rebill_logistic_cost")
    payout    = round(_sum(sales, "ppvz_for_pay") - _sum(rets, "ppvz_for_pay")
                      - logistics - storage - deduction - penalty - rebill, 2)

    adv_spend = round(sum((a.get("updSum") or 0) for a in adv), 2) if adv else 0
    after_adv = round(payout - adv_spend, 2)
    opt       = payout / net_units if net_units else 0

    tax     = round(sales_rub * TAX_RATE, 2)
    factory = net_units * cost
    pack    = net_units * BOX_PER_UNIT
    owner   = round(after_adv - tax - factory - pack - WB_ASSISTANT - cross_dock, 2)

    return {
        "net_units": net_units, "sales_rub": round(sales_rub, 2), "payout": payout,
        "adv": adv_spend, "after_adv": after_adv, "opt": round(opt, 2),
        "tax": tax, "factory": factory, "pack": pack,
        "assistant": WB_ASSISTANT, "cross_dock": cross_dock, "cross_dock_missing": cross_dock == 0,
        "owner": owner,
        "sales_count": len(sales), "returns_count": len(rets),
        "deductions": {"logistics": round(logistics, 2), "storage": round(storage, 2),
                       "deduction": round(deduction, 2), "penalty": round(penalty, 2),
                       "rebill": round(rebill, 2)},
    }


# ──────────────────────────────────────────────
# СВОД
# ──────────────────────────────────────────────
def compute_all(raw_dir, cost=COST_CORPUS, cross_dock=0, final=False) -> dict:
    raw = load_raw(Path(raw_dir))
    oz = compute_ozon(raw["ozon_tx"], cost=cost, final=final)
    wb = compute_wb(raw["wb_report"], raw["wb_adv"], cost=cost, cross_dock=cross_dock)

    def g(d, k):
        return 0 if "error" in d else d.get(k, 0)

    total = {
        "net_units": g(oz, "net_units") + g(wb, "net_units"),
        "net_sales": g(oz, "net_sales") + g(wb, "sales_rub"),
        "payout":    g(oz, "payout") + g(wb, "payout"),
        "adv":       g(wb, "adv"),
        "tax":       g(oz, "tax") + g(wb, "tax"),
        "factory":   g(oz, "factory") + g(wb, "factory"),
        "pack":      g(oz, "pack") + g(wb, "pack"),
        "team":      g(oz, "team") + g(wb, "assistant") + g(wb, "cross_dock"),
        "owner":     g(oz, "owner") + g(wb, "owner"),
    }
    total["after_mp_adv"] = round(total["payout"] - total["adv"], 2)
    total["debt_tax"]     = total["tax"]
    total["debt_factory"] = total["factory"]

    return {"ozon": oz, "wb": wb, "total": total,
            "params": {"cost": cost, "cross_dock": cross_dock, "final": final}}


# ──────────────────────────────────────────────
# Печать (для проверки в консоли)
# ──────────────────────────────────────────────
def _f(x):
    return f"{round(x):,}".replace(",", " ")


def print_report(res: dict):
    oz, wb, t = res["ozon"], res["wb"], res["total"]
    print("═══════════ OZON ═══════════")
    if "error" in oz:
        print(" ", oz["error"])
    else:
        print(f"  чистые выкупы:   {oz['net_units']} шт")
        print(f"  чистые продажи:  {_f(oz['net_sales'])} ₽")
        print(f"  выплаты Ozon:    {_f(oz['payout'])} ₽")
        print(f"  ОПТ:             {oz['opt']:.2f} ₽/шт")
        print(f"  − налог 7%:      {_f(oz['tax'])} ₽")
        print(f"  − завод:         {_f(oz['factory'])} ₽")
        print(f"  − короб:         {_f(oz['pack'])} ₽")
        print(f"  − команда:       {_f(oz['team'])} ₽  (KPI: {oz['kpi']['reason']})")
        print(f"  = владелец Ozon: {_f(oz['owner'])} ₽")
    print("═══════════ WB ═══════════")
    if "error" in wb:
        print(" ", wb["error"])
    else:
        print(f"  выкупы:          {wb['net_units']} шт")
        print(f"  выкупили на:     {_f(wb['sales_rub'])} ₽")
        print(f"  выплаты WB:      {_f(wb['payout'])} ₽")
        print(f"  − реклама:       {_f(wb['adv'])} ₽")
        print(f"  ОПТ:             {wb['opt']:.2f} ₽/шт")
        print(f"  − налог 7%:      {_f(wb['tax'])} ₽")
        print(f"  − завод:         {_f(wb['factory'])} ₽")
        print(f"  − упаковка:      {_f(wb['pack'])} ₽")
        print(f"  − ассистент:     {_f(wb['assistant'])} ₽")
        cd = "НЕ ПЕРЕДАН (0)" if wb["cross_dock_missing"] else _f(wb["cross_dock"]) + " ₽"
        print(f"  − кросс-докинг:  {cd}")
        print(f"  = владелец WB:   {_f(wb['owner'])} ₽")
    print("═══════════ ИТОГ ═══════════")
    print(f"  выкупы:          {t['net_units']} шт")
    print(f"  продажи:         {_f(t['net_sales'])} ₽")
    print(f"  выплаты МП:      {_f(t['payout'])} ₽")
    print(f"  ФИНАЛ ВЛАДЕЛЕЦ:  {_f(t['owner'])} ₽")
    print(f"  долг налоговой:  {_f(t['debt_tax'])} ₽ | долг заводу: {_f(t['debt_factory'])} ₽")


def main():
    ap = argparse.ArgumentParser(description="Расчёт юнит-экономики PlastiLove")
    ap.add_argument("--raw", required=True, help="Папка с сырьём (raw/YYYY-MM)")
    ap.add_argument("--cost", type=int, default=COST_CORPUS, help="Себестоимость ₽/шт (250 корпус / 170 без)")
    ap.add_argument("--cross-dock", type=float, default=0, help="Кросс-докинг WB за период, ₽")
    ap.add_argument("--final", action="store_true", help="Финальное закрытие (KPI Ozon в команду)")
    ap.add_argument("--json", dest="json_out", help="Сохранить результат в JSON")
    args = ap.parse_args()

    res = compute_all(args.raw, cost=args.cost, cross_dock=args.cross_dock, final=args.final)
    print_report(res)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print(f"\n→ JSON: {args.json_out}")


if __name__ == "__main__":
    main()
