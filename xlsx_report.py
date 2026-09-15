#!/usr/bin/env python3
"""
xlsx_report.py — строит управленческий Excel-отчёт из результата compute.py.
Кладёт файл в папку месяца: reports/<YYYY-MM>/Отчёт_<MMMM>_<YYYY>.xlsx

Использование:
  python xlsx_report.py --raw ./raw/2026-06 --period 2026-06
  python xlsx_report.py --raw ./raw/2026-06 --period 2026-06 --cross-dock 25000 --final
"""

import argparse
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

import compute as C

RU_MONTHS = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
             "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]

# Стили
HEAD_FILL   = PatternFill("solid", fgColor="1F4E78")   # тёмно-синий
SUB_FILL    = PatternFill("solid", fgColor="2E75B6")   # синий
TOTAL_FILL  = PatternFill("solid", fgColor="548235")   # зелёный
OWNER_FILL  = PatternFill("solid", fgColor="FFF2CC")   # жёлтый акцент
NEG_FONT    = Font(color="C00000", bold=True)
WHITE_BOLD  = Font(color="FFFFFF", bold=True, size=12)
BOLD        = Font(bold=True)
THIN        = Side(style="thin", color="BFBFBF")
BORDER      = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
MONEY       = '#,##0 ₽'
PCS         = '#,##0" шт"'
RUBUNIT     = '#,##0.00" ₽/шт"'


def _row(ws, r, label, value, *, fmt=MONEY, fill=None, bold=False, indent=1, neg_red=False):
    c1 = ws.cell(r, 1, label)
    c1.alignment = Alignment(indent=indent, vertical="center")
    c1.border = BORDER
    c2 = ws.cell(r, 2, value)
    c2.number_format = fmt if isinstance(value, (int, float)) else "General"
    c2.alignment = Alignment(horizontal="right", vertical="center")
    c2.border = BORDER
    if fill:
        c1.fill = fill; c2.fill = fill
    if bold:
        c1.font = Font(bold=True); c2.font = Font(bold=True)
    if neg_red and isinstance(value, (int, float)) and value < 0:
        c2.font = NEG_FONT
    return r + 1


def _section(ws, r, title, fill=SUB_FILL):
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
    c = ws.cell(r, 1, title)
    c.fill = fill; c.font = WHITE_BOLD
    c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[r].height = 22
    return r + 1


def build(res: dict, period: str, out_path: Path, asof: str = None):
    y, m = map(int, period.split("-"))
    title = f"{RU_MONTHS[m]} {y}"
    if asof:
        ay, am, ad = map(int, asof.split("-"))
        asof_txt = f"  ·  данные за 01.{am:02d}–{ad:02d}.{am:02d}.{ay}"
    else:
        asof_txt = ""
    oz, wb, t = res["ozon"], res["wb"], res["total"]
    final = res["params"]["final"]

    wbk = Workbook()
    ws = wbk.active
    ws.title = f"Отчёт {period}"
    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 20
    ws.sheet_view.showGridLines = False

    # Заголовок
    ws.merge_cells("A1:B1")
    h = ws.cell(1, 1, f"Управленческий отчёт — {title.upper()} (PlastiLove)")
    h.fill = HEAD_FILL; h.font = Font(color="FFFFFF", bold=True, size=14)
    h.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 30
    ws.merge_cells("A2:B2")
    status = "ФИНАЛЬНЫЙ (с хвостом, KPI в команде)" if final else "ПРЕДВАРИТЕЛЬНЫЙ / ОПЕРАТИВНЫЙ"
    s = ws.cell(2, 1, f"Статус: {status}{asof_txt}")
    s.font = Font(italic=True, color="808080"); s.alignment = Alignment(horizontal="center")

    r = 4
    # ── OZON ──
    r = _section(ws, r, "БЛОК 1. OZON")
    if "error" in oz:
        r = _row(ws, r, oz["error"], "", fmt="General")
    else:
        r = _row(ws, r, "Чистые выкупы", oz["net_units"], fmt=PCS)
        r = _row(ws, r, "Чистые продажи", oz["net_sales"])
        r = _row(ws, r, "Выплаты Ozon", oz["payout"])
        r = _row(ws, r, "Опт (₽/шт)", oz["opt"], fmt=RUBUNIT, bold=True)
        r = _row(ws, r, "− Налог 7%", -oz["tax"])
        r = _row(ws, r, "− Завод (себестоимость)", -oz["factory"])
        r = _row(ws, r, "− Транспортный короб", -oz["pack"])
        r = _row(ws, r, "− Команда Ozon (оклад)", -oz["salary"])
        kpi_note = f"KPI (резерв): {C._f(oz['kpi']['total'])} ₽" if not final else f"KPI в команде: {C._f(oz['kpi']['total'])} ₽"
        r = _row(ws, r, f"   {kpi_note}", oz["kpi"]["reason"], fmt="General", indent=2)
        r = _row(ws, r, "= Остаток владельца Ozon", oz["owner"], fill=OWNER_FILL, bold=True, neg_red=True)

        by_scheme = oz.get("by_scheme") or {}
        if len(by_scheme) > 1:
            r += 1
            r = _row(ws, r, "Разбивка Ozon по схеме (FBO / FBS)", "", fmt="General", bold=True, indent=0)
            for scheme, d in by_scheme.items():
                r = _row(ws, r, f"   {scheme} — выкупы", d["net_units"], fmt=PCS, indent=2)
                r = _row(ws, r, f"   {scheme} — чистые продажи", d["net_sales"], indent=2)
                r = _row(ws, r, f"   {scheme} — выплаты", d["payout"], indent=2)
                r = _row(ws, r, f"   {scheme} — опт (₽/шт)", d["opt"], fmt=RUBUNIT, indent=2)
                r = _row(ws, r, f"   {scheme} — маржа до команды (−налог−завод−короб)", d["margin_before_team"], indent=2, neg_red=True)
            if oz.get("unmatched_note"):
                r = _row(ws, r, "   ⚠️ примечание", oz["unmatched_note"], fmt="General", indent=2)

    r += 1
    # ── WB ──
    r = _section(ws, r, "БЛОК 2. WILDBERRIES")
    if "error" in wb:
        r = _row(ws, r, wb["error"], "", fmt="General")
    else:
        r = _row(ws, r, "Выкупы", wb["net_units"], fmt=PCS)
        r = _row(ws, r, "Выкупили на сумму", wb["sales_rub"])
        r = _row(ws, r, "Выплаты WB (нетто)", wb["payout"])
        r = _row(ws, r, "− WB-реклама", -wb["adv"])
        r = _row(ws, r, "Остаток после WB и рекламы", wb["after_adv"])
        r = _row(ws, r, "Опт (₽/шт)", wb["opt"], fmt=RUBUNIT, bold=True)
        r = _row(ws, r, "− Налог 7%", -wb["tax"])
        r = _row(ws, r, "− Завод (себестоимость)", -wb["factory"])
        r = _row(ws, r, "− Упаковка", -wb["pack"])
        cd_lbl = "− Кросс-докинг (НЕ ПЕРЕДАН)" if wb["cross_dock_missing"] else "− Кросс-докинг"
        r = _row(ws, r, cd_lbl, -wb["cross_dock"])
        r = _row(ws, r, "= Остаток владельца WB", wb["owner"], fill=OWNER_FILL, bold=True, neg_red=True)
        d = wb["deductions"]
        r = _row(ws, r, "   в т.ч. удержания WB: логистика/хранение/пр.",
                 f"лог {C._f(d['logistics'])} · хран {C._f(d['storage'])} · уд {C._f(d['deduction'])}",
                 fmt="General", indent=2)

        wb_by_scheme = wb.get("by_scheme") or {}
        if wb.get("scheme_split_available") and len(wb_by_scheme) > 1:
            r += 1
            r = _row(ws, r, "Разбивка WB по схеме (только продажи/штуки)", "", fmt="General", bold=True, indent=0)
            for scheme, ds in wb_by_scheme.items():
                r = _row(ws, r, f"   {scheme} — выкупы", ds["net_units"], fmt=PCS, indent=2)
                r = _row(ws, r, f"   {scheme} — продажи", ds["sales_rub"], indent=2)
            r = _row(ws, r, "   ⚠️ примечание",
                     "удержания (логистика/хранение/реклама) в отчёте реализации WB по схеме не разделены",
                     fmt="General", indent=2)
        elif not wb.get("scheme_split_available"):
            r += 1
            r = _row(ws, r, "Разбивка WB по схеме", "нет wb_orders.json/wb_sales.json для сопоставления по srid",
                     fmt="General", indent=0)

    r += 1
    # ── ИТОГ ──
    r = _section(ws, r, "БЛОК 3. ОБЩИЙ ИТОГ", fill=TOTAL_FILL)
    r = _row(ws, r, "Общие чистые выкупы", t["net_units"], fmt=PCS)
    r = _row(ws, r, "Общие чистые продажи", t["net_sales"])
    r = _row(ws, r, "Общие выплаты МП", t["payout"])
    r = _row(ws, r, "Общая реклама (WB)", -t["adv"])
    r = _row(ws, r, "Остаток после МП и рекламы", t["after_mp_adv"])
    r = _row(ws, r, "− Налог всего", -t["tax"])
    r = _row(ws, r, "− Завод всего", -t["factory"])
    r = _row(ws, r, "− Упаковка всего", -t["pack"])
    r = _row(ws, r, "− Команда всего", -t["team"])
    r = _row(ws, r, "= ФИНАЛЬНЫЙ ОСТАТОК ВЛАДЕЛЬЦА", t["owner"], fill=OWNER_FILL, bold=True, neg_red=True)
    ws.row_dimensions[r-1].height = 22

    r += 1
    r = _section(ws, r, "ОБЯЗАТЕЛЬСТВА НА КОНЕЦ МЕСЯЦА")
    r = _row(ws, r, "Долг налоговой (7%)", t["debt_tax"], neg_red=False)
    r = _row(ws, r, "Долг заводу (себестоимость)", t["debt_factory"])

    # Второй лист — выводы
    ws2 = wbk.create_sheet("Выводы")
    ws2.column_dimensions["A"].width = 100
    ws2.cell(1, 1, "ВЫВОДЫ И РЕКОМЕНДАЦИИ").font = Font(bold=True, size=14, color="1F4E78")
    lines = _conclusions(oz, wb, t)
    for i, ln in enumerate(lines, start=3):
        c = ws2.cell(i, 1, ln)
        c.alignment = Alignment(wrap_text=True, vertical="top")
        if ln.startswith("•"):
            c.font = Font(size=11)
        elif ln.endswith(":") or ln.isupper():
            c.font = Font(bold=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wbk.save(out_path)
    return out_path


def _conclusions(oz, wb, t):
    L = []
    if "error" in oz or "error" in wb:
        return ["Недостаточно данных для полного вывода."]
    winner = "Ozon" if oz["owner"] >= wb["owner"] else "WB"
    L.append(f"Кто сделал месяц: {winner}. Ozon +{C._f(oz['owner'])} ₽, WB {C._f(wb['owner'])} ₽.")
    L.append("")
    if wb["owner"] < 0:
        d = wb["deductions"]
        L.append(f"• Слабое место — WB: канал в минусе ({C._f(wb['owner'])} ₽). Логистика {C._f(d['logistics'])} ₽ + "
                 f"хранение {C._f(d['storage'])} ₽ на {wb['net_units']} выкупах съедают маржу — это следствие перезатара склада WB.")
    L.append(f"• Опт Ozon {oz['opt']:.0f} ₽/шт — " +
             ("над санитарным 550, но ниже рабочих 560–570." if oz['opt'] >= 550 else "НИЖЕ санитарного 550 — опасно для прибыли."))
    L.append(f"• Опт WB {wb['opt']:.0f} ₽/шт (до рекламы) — {'рабочий' if wb['opt']>=550 else 'структурно нерабочий, ниже 550'}.")
    L.append(f"• Реклама WB {C._f(wb['adv'])} ₽ на {'убыточном' if wb['owner']<0 else 'рабочем'} канале — "
             f"{'резать до разгрузки склада.' if wb['owner']<0 else 'держать под контролем.'}")
    L.append(f"• Объём Ozon {oz['net_units']} шт — {oz['kpi']['reason']}.")
    L.append("")
    L.append("Что делать дальше:")
    L.append("• Ozon — вернуть объём выше 1 200 шт и опт к 560+, докинуть дефицитные SKU.")
    if wb["owner"] < 0:
        L.append("• WB — не рекламировать и активно разгружать склад (дерево дисконтом), пока канал в минусе.")
    if wb["cross_dock_missing"]:
        L.append("• Передать сумму кросс-докинга WB за период (сейчас в расчёте 0).")
    L.append("• Подтвердить себестоимость по SKU (корпус 250 / без корпуса 170) — сейчас всё по 250 ₽.")
    return L


def out_path_for(period: str, base="reports") -> Path:
    y, m = map(int, period.split("-"))
    return Path(base) / period / f"Отчёт_{RU_MONTHS[m]}_{y}.xlsx"


def main():
    ap = argparse.ArgumentParser(description="Excel-отчёт PlastiLove")
    ap.add_argument("--raw", required=True)
    ap.add_argument("--period", required=True, help="YYYY-MM")
    ap.add_argument("--cost", type=int, default=C.COST_CORPUS)
    ap.add_argument("--cross-dock", type=float, default=0)
    ap.add_argument("--final", action="store_true")
    ap.add_argument("--out", help="Путь к xlsx (по умолчанию reports/<period>/)")
    args = ap.parse_args()

    res = C.compute_all(args.raw, cost=args.cost, cross_dock=args.cross_dock, final=args.final)
    out = Path(args.out) if args.out else out_path_for(args.period)
    path = build(res, args.period, out)
    print(f"→ Excel: {path}")


if __name__ == "__main__":
    main()
