#!/usr/bin/env python3
"""
compute.py — управленческая юнит-экономика PlastiLove, только Ozon.

Сырьё (fetch.py): ozon_accruals.json (/v1/finance/accrual/by-day),
ozon_skumap.json (sku → артикул), ozon_types.json (справочник начислений).

Логика (инструкция проекта):
  чистые выкупы → чистые продажи → выплаты Ozon → опт = выплаты / выкупы
  → − налог 7% (от чистых продаж, «Ваша цена») → − завод (по группам SKU)
  → − транспортный короб (корпусные SKU) → − команда (оклад 70 000;
  KPI — резерв, в команду только при финальном закрытии) = деньги владельца.

Реклама Ozon (CPC/CPO и пр.) уже удержана внутри выплат — повторно не вычитаем,
показываем справочно.

Использование:
  python compute.py --raw raw/2026-09 [--final] [--json out.json]
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

TAX_RATE = 0.07
OZON_SALARY = 70_000
BOX_PER_UNIT = 7          # транспортный короб 45 ₽ / 6 компл, только корпусные SKU
COST_CORPUS = 257         # совместимость со старым run_daily/report

# Группы SKU: себестоимость, план опта для KPI, короб
GROUPS = {
    "россыпь": {"cost": 170, "plan": 290, "box": 0},
    "бежевая": {"cost": 267, "plan": 570, "box": BOX_PER_UNIT},
    "база":    {"cost": 257, "plan": 550, "box": BOX_PER_UNIT},
}
ROSSYP_OFFERS = {"CBH-BLK-WOD-4"}

# Типы начислений, которые считаем рекламой (справочно, уже внутри выплат)
AD_TYPES = {3, 5, 19, 23, 33, 41, 54, 55, 75, 87, 130}
CROSSDOCK_TYPES = {12}

FALLBACK_TYPES = {
    1: "Эквайринг", 12: "Кросс-докинг", 29: "Доставка до места выдачи",
    32: "Логистика", 41: "Оплата за клик", 54: "Продвижение товара",
    59: "Обратная логистика", 74: "Звёздные товары", 52: "Подписка Premium",
}


def group_of(offer_id: str) -> str:
    o = (offer_id or "").upper()
    if o in ROSSYP_OFFERS:
        return "россыпь"
    if o.startswith("CB-BEE-"):
        return "бежевая"
    return "база"


def _amt(x) -> float:
    if not x:
        return 0.0
    try:
        return float(x.get("amount") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _load(p: Path, default):
    if p.exists():
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return default


def load_raw(raw: Path) -> dict:
    types = _load(raw / "ozon_types.json", {})
    return {
        "accruals": _load(raw / "ozon_accruals.json", None),
        "skumap": _load(raw / "ozon_skumap.json", {}),
        "types": {int(k): v for k, v in types.items()} if types else {},
        "manifest": _load(raw / "manifest.json", {}),
    }


# ──────────────────────────────────────────────
# KPI команды Ozon (зафиксированная формула: план-факт по опту + шкала объёма)
# ──────────────────────────────────────────────
def volume_bonus(net_units: int) -> int:
    if net_units < 1200:
        return 0
    return min(3000 * ((net_units - 1200) // 100 + 1), 15000)


def ozon_kpi(net_sales: float, payout: float, sku_rows: list, net_units: int) -> dict:
    expense_rate = 1 - (payout / net_sales) if net_sales else 0
    diff_total = 0.0
    lines = []
    for r in sku_rows:
        u = r["net_units"]
        if u <= 0:
            continue
        fact = (r["net_sales"] / u) * (1 - expense_rate)
        plan = GROUPS[r["group"]]["plan"]
        diff = (fact - plan) * u
        diff_total += diff
        lines.append({"offer": r["offer"], "fact": round(fact, 2), "plan": plan,
                      "units": u, "diff": round(diff, 2)})
    opt_raw = 0.5 * diff_total
    opt_bonus = max(round(opt_raw), 0)
    vol = volume_bonus(net_units)
    reasons = []
    if net_units < 1200:
        reasons.append(f"объём {net_units} шт < 1200 — бонус за объём 0")
    if opt_raw < 0:
        reasons.append(f"опт ниже плана (Σ разница {round(diff_total):,} ₽) — бонус за опт 0".replace(",", " "))
    return {"expense_rate": round(expense_rate, 4), "opt_raw": round(opt_raw, 2),
            "opt": opt_bonus, "volume": vol, "total": opt_bonus + vol,
            "by_sku": lines, "reason": "; ".join(reasons) or "KPI начислен по факту"}


# ──────────────────────────────────────────────
# OZON
# ──────────────────────────────────────────────
def compute_ozon(accruals, skumap=None, types=None, final=False) -> dict:
    if accruals is None:
        return {"error": "нет файла начислений Ozon (выгрузка не удалась)"}
    skumap = skumap or {}
    tname = lambda t: (types or {}).get(t) or FALLBACK_TYPES.get(t) or f"Тип начисления {t}"

    payout = 0.0
    sold_units = ret_units = 0
    gross = ret_sum = 0.0
    lines = defaultdict(float)          # статьи удержаний/начислений
    ads = crossdock = 0.0
    sku = defaultdict(lambda: {"sold": 0, "ret": 0, "net_sales": 0.0, "payout": 0.0})
    schemes = defaultdict(lambda: {"net_units": 0, "net_sales": 0.0})

    def add_line(type_id, value):
        nonlocal ads, crossdock
        lines[tname(type_id)] += value
        if type_id in AD_TYPES:
            ads += value
        if type_id in CROSSDOCK_TYPES:
            crossdock += value

    for a in accruals:
        payout += _amt(a.get("total_amount"))
        p = a.get("posting")
        if p:
            sch = (p.get("delivery_schema") or "?").upper()
            for pr in p.get("products") or []:
                s = str(pr.get("sku"))
                q = pr.get("quantity") or 0
                c = pr.get("commission")
                if c:
                    sa = _amt(c.get("sale_amount"))
                    com = _amt(c.get("commission"))
                    if sa > 0:
                        sold_units += q; gross += sa; sku[s]["sold"] += q
                        schemes[sch]["net_units"] += q
                    elif sa < 0:
                        ret_units += q; ret_sum += -sa; sku[s]["ret"] += q
                        schemes[sch]["net_units"] -= q
                    sku[s]["net_sales"] += sa
                    sku[s]["payout"] += sa + com
                    schemes[sch]["net_sales"] += sa
                    lines["Вознаграждение Ozon (комиссия)"] += com
                d = pr.get("delivery")
                if d:
                    for sv in d.get("services") or []:
                        v = _amt(sv.get("accrued"))
                        add_line(sv.get("type_id"), v)
                        sku[s]["payout"] += v
        itf = a.get("item_fees")
        if itf:
            for it in itf.get("fees") or []:
                s = str(it.get("sku"))
                for fe in it.get("fees") or []:
                    v = _amt(fe.get("accrued"))
                    add_line(fe.get("type_id"), v)
                    sku[s]["payout"] += v
        nif = a.get("non_item_fee")
        if nif:
            add_line(nif.get("type_id"), _amt(nif.get("accrued")))

    net_units = sold_units - ret_units
    net_sales = round(gross - ret_sum, 2)
    payout = round(payout, 2)
    explained = sum(lines.values()) + net_sales
    if abs(payout - explained) >= 1:
        lines["Прочие начисления"] += payout - explained

    # По SKU и группам
    sku_rows = []
    for s, v in sku.items():
        offer = skumap.get(s) or f"SKU {s}"
        g = group_of(skumap.get(s, ""))
        nu = v["sold"] - v["ret"]
        if v["sold"] == 0 and v["ret"] == 0:
            continue
        sku_rows.append({"sku": s, "offer": offer, "group": g, "sold": v["sold"],
                         "ret": v["ret"], "net_units": nu,
                         "net_sales": round(v["net_sales"], 2),
                         "payout": round(v["payout"], 2),
                         "opt": round(v["payout"] / nu, 2) if nu else 0})
    sku_rows.sort(key=lambda r: -r["net_units"])

    # Реклама (CPC/CPO), кросс-докинг, подписки и т.п. Ozon начисляет общей суммой
    # без SKU — распределяем их по артикулам пропорционально чистым продажам,
    # чтобы опт по SKU был сопоставим с общим и сумма сходилась с выплатой.
    common = round(payout, 2) - sum(r["payout"] for r in sku_rows)
    base_sales = sum(max(r["net_sales"], 0) for r in sku_rows)
    for r in sku_rows:
        share = (max(r["net_sales"], 0) / base_sales) if base_sales else 0
        r["payout_direct"] = r["payout"]
        r["payout"] = round(r["payout"] + common * share, 2)
        r["opt"] = round(r["payout"] / r["net_units"], 2) if r["net_units"] else 0

    groups = {}
    factory = pack = 0
    for g, cfg in GROUPS.items():
        u = sum(r["net_units"] for r in sku_rows if r["group"] == g)
        if not u:
            continue
        groups[g] = {"net_units": u, "cost": cfg["cost"], "factory": u * cfg["cost"],
                     "pack": u * cfg["box"]}
        factory += u * cfg["cost"]
        pack += u * cfg["box"]

    opt = round(payout / net_units, 2) if net_units else 0
    tax = round(net_sales * TAX_RATE, 2)
    kpi = ozon_kpi(net_sales, payout, sku_rows, net_units)
    team = OZON_SALARY + (kpi["total"] if final else 0)
    before_team = round(payout - tax - factory - pack, 2)
    owner = round(before_team - team, 2)

    deductions = sorted(((k, round(v, 2)) for k, v in lines.items() if abs(v) >= 0.5),
                        key=lambda kv: kv[1])
    return {
        "sold_units": sold_units, "returns_units": ret_units, "net_units": net_units,
        "gross_sales": round(gross, 2), "returns_sum": round(ret_sum, 2),
        "net_sales": net_sales, "payout": payout, "opt": opt,
        "ads": round(ads, 2), "crossdock": round(crossdock, 2),
        "tax": tax, "factory": factory, "pack": pack,
        "salary": OZON_SALARY, "kpi": kpi, "kpi_in_team": final, "team": team,
        "before_team": before_team, "owner": owner,
        "unit_profit": round(owner / net_units, 2) if net_units else 0,
        "deductions": deductions, "groups": groups, "by_sku": sku_rows,
        "by_scheme": {k: {"net_units": v["net_units"], "net_sales": round(v["net_sales"], 2)}
                      for k, v in schemes.items()},
        "accruals_count": len(accruals),
    }


def compute_all(raw_dir, final=False, **_ignored) -> dict:
    raw = load_raw(Path(raw_dir))
    oz = compute_ozon(raw["accruals"], raw["skumap"], raw["types"], final=final)
    return {"ozon": oz, "params": {"final": final}, "manifest": raw["manifest"]}


def _f(x):
    return f"{round(x):,}".replace(",", " ")


def print_report(res: dict):
    oz = res["ozon"]
    print("═══════════ OZON ═══════════")
    if "error" in oz:
        print(" ", oz["error"]); return
    print(f"  продано / возвраты: {oz['sold_units']} / {oz['returns_units']} шт → чистые {oz['net_units']} шт")
    print(f"  чистые продажи:  {_f(oz['net_sales'])} ₽")
    print(f"  выплаты Ozon:    {_f(oz['payout'])} ₽  (реклама внутри: {_f(-oz['ads'])} ₽, кросс-докинг: {_f(-oz['crossdock'])} ₽)")
    print(f"  ОПТ:             {oz['opt']:.2f} ₽/шт")
    print(f"  − налог 7%:      {_f(oz['tax'])} ₽")
    print(f"  − завод:         {_f(oz['factory'])} ₽  " +
          " · ".join(f"{g} {d['net_units']}×{d['cost']}" for g, d in oz["groups"].items()))
    print(f"  − короб:         {_f(oz['pack'])} ₽")
    print(f"  − команда:       {_f(oz['team'])} ₽")
    print(f"    KPI {'в команде' if oz['kpi_in_team'] else 'резерв'}: {_f(oz['kpi']['total'])} ₽ "
          f"(опт {_f(oz['kpi']['opt'])} + объём {_f(oz['kpi']['volume'])}) — {oz['kpi']['reason']}")
    print(f"  = владелец:      {_f(oz['owner'])} ₽  ({oz['unit_profit']:.0f} ₽/шт)")
    print("  удержания:")
    for k, v in oz["deductions"]:
        print(f"    {k}: {_f(v)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--final", action="store_true")
    ap.add_argument("--json", dest="json_out")
    a = ap.parse_args()
    res = compute_all(a.raw, final=a.final)
    print_report(res)
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
