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

── FBO / FBS ────────────────────────────────────────────────────────────
С сентября 2026 Ozon отключил /v3/finance/transaction/list (см. fetch.py) —
именно это, а не переход на FBO, было причиной нулевых отчётов. Заодно
добавлена разбивка «сколько сделал FBO, сколько FBS» внутри каждого канала:

  • Ozon — операции классифицируются по posting_number: сверяем с уже
    скачанными списками отправлений /v2/posting/fbo/list и
    /v3/posting/fbs/list (ozon_fbo_postings.json / ozon_fbs_postings.json).
    Для найденных отправлений считаем полный P&L (продажи, выплаты, опт,
    налог, завод, упаковка) отдельно по FBO и по FBS. Команда/оклад/KPI
    считаются только на уровне канала Ozon целиком — они не привязаны
    к конкретной поставке, поэтому делить их по схеме нечем.

  • WB — операции склад-продажи классифицируются по srid через
    warehouseType в wb_orders.json / wb_sales.json ("Склад WB" → FBO,
    иначе — FBS/маркетплейс/DBS). Реклама, логистика, хранение и прочие
    удержания у WB не разбиты по схеме на уровне отчёта реализации,
    поэтому по WB показываем разбивку по выручке и штукам, а не по
    полному P&L — это честнее, чем выдумывать точность, которой нет
    в исходных данных.
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

UNSCHEMED = "не определено"    # операция/продажа, которую не удалось привязать к FBO или FBS


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
        "ozon_tx": _load(_find(
            raw,
            "ozon_finance_operations.json",       # новый источник (cash-flow-statement)
            "ozon_transactions.json",              # старый источник (для архивных raw/ до сентября 2026)
            "transactions_*.json", "ozon_transactions*.json",
        )),
        "ozon_fbo_postings": _load(_find(raw, "ozon_fbo_postings.json")) or [],
        "ozon_fbs_postings": _load(_find(raw, "ozon_fbs_postings.json")) or [],
        "wb_report":   _load(_find(raw, "wb_report_detail.json", "report_detail.json", "wb_report*.json")),
        "wb_adv":      _load(_find(raw, "wb_adv.json", "adv_upd.json", "wb_adv*.json")),
        "wb_orders":   _load(_find(raw, "wb_orders.json")) or [],
        "wb_sales":    _load(_find(raw, "wb_sales.json")) or [],
    }


def _sum(rows, field):
    return sum((r.get(field) or 0) for r in rows)


def _get(d: dict, *keys, default=None):
    """
    Достаёт значение по первому найденному ключу. Ключ может быть путём
    через точку ("posting.posting_number") для вложенных словарей.
    Нужно, т.к. точная форма ответа нового финансового API Ozon
    (/v1/finance/cash-flow-statement/list) официально не задокументирована.
    """
    for k in keys:
        cur = d
        ok = True
        for part in k.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return default


def _op_type_name(op: dict) -> str:
    return _get(op, "operation_type_name", "type_name", "operation_name", default="") or ""


def _op_amount(op: dict) -> float:
    return _get(op, "amount", "sum", "total_amount", default=0) or 0


def _op_accrual(op: dict) -> float:
    return _get(op, "accruals_for_sale", "accrual_for_sale", "sale_amount", default=0) or 0


def _op_posting_number(op: dict) -> str:
    return _get(op, "posting.posting_number", "posting_number", default="") or ""


def _op_delivery_schema(op: dict) -> str:
    """
    'FBO' / 'FBS' / 'RFBS' / 'CROSSBORDER' / '' — берём прямо из посылки.
    Подтверждено на реальных данных (raw/2026-06/ozon_transactions.json,
    старый /v3/finance/transaction/list): поле posting.delivery_schema
    у Ozon действительно есть и совпадает с реальной схемой продажи.
    Если новый /v1/finance/cash-flow-statement/list его не отдаёт —
    ничего страшного, ниже есть запасной путь через posting_number.
    """
    return (_get(op, "posting.delivery_schema", "delivery_schema", default="") or "").strip().upper()


# ──────────────────────────────────────────────
# Классификация по схеме (FBO / FBS)
# ──────────────────────────────────────────────
def build_ozon_scheme_map(fbo_postings: list, fbs_postings: list) -> dict:
    """posting_number → 'FBO' / 'FBS', по спискам отправлений из fetch.py."""
    m = {}
    for p in fbo_postings or []:
        pn = p.get("posting_number")
        if pn:
            m[pn] = "FBO"
    for p in fbs_postings or []:
        pn = p.get("posting_number")
        if pn:
            m[pn] = "FBS"
    return m


def build_wb_scheme_map(wb_orders: list, wb_sales: list) -> dict:
    """srid → 'FBO' / 'FBS', по warehouseType из заказов/продаж WB.
    'Склад WB' (и похожие) → FBO. Всё остальное (склад продавца / DBS / маркетплейс) → FBS."""
    m = {}
    for rows in (wb_orders or [], wb_sales or []):
        for r in rows:
            srid = r.get("srid")
            wtype = (r.get("warehouseType") or "").strip()
            if not srid or not wtype:
                continue
            m[srid] = "FBO" if "WB" in wtype.upper() else "FBS"
    return m


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
def _ozon_scheme_slice(ops: list, cost: int) -> dict:
    """Полный P&L (без команды/KPI — они общие на канал) для подмножества операций Ozon."""
    deliv = [t for t in ops if _op_type_name(t) == OZON_DELIVER]
    ret   = [t for t in ops if _op_type_name(t) == OZON_RETURN]

    net_units = len(deliv) - len(ret)
    gross     = sum(_op_accrual(t) for t in deliv)
    ret_sum   = abs(sum(_op_accrual(t) for t in ret))
    net_sales = gross - ret_sum
    payout    = round(sum(_op_amount(t) for t in ops), 2)
    opt       = payout / net_units if net_units else 0

    tax     = round(net_sales * TAX_RATE, 2)
    factory = net_units * cost
    pack    = net_units * BOX_PER_UNIT
    margin_before_team = round(payout - tax - factory - pack, 2)

    return {
        "net_units": net_units, "net_sales": round(net_sales, 2), "payout": payout,
        "opt": round(opt, 2), "tax": tax, "factory": factory, "pack": pack,
        "margin_before_team": margin_before_team,
        "deliveries": len(deliv), "returns_count": len(ret),
    }


def compute_ozon(tx, fbo_postings=None, fbs_postings=None, cost=COST_CORPUS, final=False) -> dict:
    if not tx:
        return {"error": "нет данных начислений Ozon (ozon_finance_operations.json / ozon_transactions.json пуст)"}

    scheme_map = build_ozon_scheme_map(fbo_postings or [], fbs_postings or [])

    by_scheme_ops = defaultdict(list)
    unmatched = 0
    for op in tx:
        if not isinstance(op, dict):
            continue
        # 1) сначала — прямое поле posting.delivery_schema (надёжнее, подтверждено на реальных данных);
        # 2) если пусто — сверяем posting_number со списками /v2/posting/fbo/list и /v3/posting/fbs/list.
        scheme = _op_delivery_schema(op)
        if not scheme:
            pn = _op_posting_number(op)
            scheme = scheme_map.get(pn, UNSCHEMED)
        if scheme == UNSCHEMED:
            unmatched += 1
        by_scheme_ops[scheme].append(op)

    # ── общий расчёт по каналу (как раньше — не зависит от разбивки по схеме) ──
    combined = _ozon_scheme_slice(tx, cost)
    net_units, net_sales, payout, opt = (
        combined["net_units"], combined["net_sales"], combined["payout"], combined["opt"],
    )
    tax, factory, pack = combined["tax"], combined["factory"], combined["pack"]
    kpi   = ozon_kpi(net_units, opt)
    team  = OZON_SALARY + (kpi["total"] if final else 0)     # KPI в команду только при финале
    owner = round(payout - tax - factory - pack - team, 2)

    # Порядок: FBO, FBS вперёд (основные схемы), любые другие схемы Ozon
    # (RFBS/CROSSBORDER/FBP и т.п., если появятся) — посередине, «не определено» — последним.
    priority = {"FBO": 0, "FBS": 1, UNSCHEMED: 99}
    ordered_schemes = sorted(by_scheme_ops.keys(), key=lambda s: priority.get(s, 50))
    by_scheme = {}
    for scheme in ordered_schemes:
        ops = by_scheme_ops.get(scheme)
        if ops:
            by_scheme[scheme] = _ozon_scheme_slice(ops, cost)

    gross_sales = round(sum(_op_accrual(t) for t in tx if _op_type_name(t) == OZON_DELIVER), 2)
    returns_sum = round(abs(sum(_op_accrual(t) for t in tx if _op_type_name(t) == OZON_RETURN)), 2)

    return {
        "net_units": net_units, "gross_sales": gross_sales, "returns_sum": returns_sum,
        "net_sales": net_sales, "payout": payout, "opt": opt,
        "tax": tax, "factory": factory, "pack": pack,
        "salary": OZON_SALARY, "kpi": kpi, "kpi_in_team": final,
        "team": team, "owner": owner,
        "deliveries": combined["deliveries"], "returns_count": combined["returns_count"],
        "by_scheme": by_scheme,
        "unmatched_ops": unmatched,
        "unmatched_note": (
            f"{unmatched} операций не удалось привязать к FBO/FBS по posting_number "
            f"(нет в ozon_fbo_postings.json / ozon_fbs_postings.json за этот период)"
            if unmatched else None
        ),
    }


# ──────────────────────────────────────────────
# WB
# ──────────────────────────────────────────────
def compute_wb(report, adv, wb_orders=None, wb_sales=None, cost=COST_CORPUS, cross_dock=0) -> dict:
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

    # ── разбивка по схеме (FBO/Склад WB vs FBS/маркетплейс-DBS) ──
    # У отчёта реализации WB нет собственного поля со схемой, поэтому
    # классифицируем по srid через warehouseType из wb_orders/wb_sales.
    # Делим только выручку и штуки (продажи минус возвраты) — удержания
    # (логистика/хранение/реклама/штрафы) в отчёте реализации по схеме
    # не разложены, поэтому не делаем вид, что можем их точно поделить.
    scheme_map = build_wb_scheme_map(wb_orders or [], wb_sales or [])
    by_scheme_units = defaultdict(float)
    by_scheme_sales = defaultdict(float)
    unmatched = 0
    if scheme_map:
        for r in sales:
            srid = r.get("srid")
            scheme = scheme_map.get(srid, UNSCHEMED)
            if scheme == UNSCHEMED:
                unmatched += 1
            by_scheme_units[scheme] += (r.get("quantity") or 0)
            by_scheme_sales[scheme] += (r.get("retail_amount") or 0)
        for r in rets:
            srid = r.get("srid")
            scheme = scheme_map.get(srid, UNSCHEMED)
            by_scheme_units[scheme] -= (r.get("quantity") or 0)
            by_scheme_sales[scheme] -= (r.get("retail_amount") or 0)

    by_scheme = {}
    for scheme in ("FBO", "FBS", UNSCHEMED):
        if scheme in by_scheme_units:
            by_scheme[scheme] = {
                "net_units": int(by_scheme_units[scheme]),
                "sales_rub": round(by_scheme_sales[scheme], 2),
            }

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
        "by_scheme": by_scheme,
        "unmatched_units": unmatched if scheme_map else None,
        "scheme_split_available": bool(scheme_map),
    }


# ──────────────────────────────────────────────
# СВОД
# ──────────────────────────────────────────────
def compute_all(raw_dir, cost=COST_CORPUS, cross_dock=0, final=False) -> dict:
    raw = load_raw(Path(raw_dir))
    oz = compute_ozon(raw["ozon_tx"], raw["ozon_fbo_postings"], raw["ozon_fbs_postings"],
                       cost=cost, final=final)
    wb = compute_wb(raw["wb_report"], raw["wb_adv"], raw["wb_orders"], raw["wb_sales"],
                     cost=cost, cross_dock=cross_dock)

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


def _print_scheme_block(title: str, by_scheme: dict, keys):
    if not by_scheme or len(by_scheme) <= 1:
        return
    print(f"  ── по схемам ({title}) ──")
    for scheme, d in by_scheme.items():
        parts = [f"{k}={_f(d[k]) if isinstance(d.get(k), (int, float)) else d.get(k)}" for k in keys if k in d]
        print(f"    {scheme}: " + " · ".join(parts))


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
        _print_scheme_block("Ozon", oz.get("by_scheme", {}),
                             ("net_units", "net_sales", "payout", "opt", "margin_before_team"))
        if oz.get("unmatched_note"):
            print(f"    ⚠️ {oz['unmatched_note']}")
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
        if wb.get("scheme_split_available"):
            _print_scheme_block("WB, только выручка/штуки", wb.get("by_scheme", {}),
                                 ("net_units", "sales_rub"))
        else:
            print("  ── по схемам (WB): нет данных wb_orders.json/wb_sales.json для сопоставления по srid ──")
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
