#!/usr/bin/env python3
"""
xlsx_report.py — управленческий Excel-отчёт PlastiLove (только Ozon).
Файл: reports/<YYYY-MM>/Отчёт_Ozon_<Месяц>_<YYYY>.xlsx
"""

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

RU_MONTHS = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
             "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]

HEAD = PatternFill("solid", fgColor="1F4E78")
SUB = PatternFill("solid", fgColor="2E75B6")
OWNER = PatternFill("solid", fgColor="FFF2CC")
WHITE = Font(color="FFFFFF", bold=True, size=12)
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
MONEY, PCS, RUBU = '#,##0 ₽', '#,##0" шт"', '#,##0.00" ₽/шт"'


def _row(ws, r, label, value, fmt=MONEY, fill=None, bold=False, indent=1):
    a, b = ws.cell(r, 1, label), ws.cell(r, 2, value)
    a.alignment = Alignment(indent=indent); a.border = b.border = BORDER
    b.number_format = fmt if isinstance(value, (int, float)) else "General"
    b.alignment = Alignment(horizontal="right")
    if fill:
        a.fill = b.fill = fill
    if bold:
        a.font = b.font = Font(bold=True)
    if isinstance(value, (int, float)) and value < 0 and fill:
        b.font = Font(color="C00000", bold=True)
    return r + 1


def _section(ws, r, title, cols=2):
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=cols)
    c = ws.cell(r, 1, title); c.fill = SUB; c.font = WHITE
    return r + 1


def _table(ws, r, headers, rows, fmts):
    for j, h in enumerate(headers, 1):
        c = ws.cell(r, j, h); c.fill = SUB; c.font = WHITE; c.border = BORDER
    r += 1
    for row in rows:
        for j, (v, fm) in enumerate(zip(row, fmts), 1):
            c = ws.cell(r, j, v); c.border = BORDER
            if isinstance(v, (int, float)):
                c.number_format = fm
        r += 1
    return r


def build(res: dict, period: str, out_path: Path, asof: str = None):
    y, m = map(int, period.split("-"))
    oz, final = res["ozon"], res["params"]["final"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Отчёт"
    ws.column_dimensions["A"].width = 46
    ws.column_dimensions["B"].width = 22
    ws.sheet_view.showGridLines = False
    ws.merge_cells("A1:B1")
    h = ws.cell(1, 1, f"PlastiLove · Ozon · {RU_MONTHS[m]} {y}")
    h.fill = HEAD; h.font = Font(color="FFFFFF", bold=True, size=14)
    h.alignment = Alignment(horizontal="center")
    ws.merge_cells("A2:B2")
    st = "ФИНАЛЬНЫЙ (KPI в команде)" if final else "ОПЕРАТИВНЫЙ (KPI в резерве)"
    if asof:
        st += f" · данные 01.{m:02d}–{asof[8:10]}.{m:02d}.{y}"
    ws.cell(2, 1, st).font = Font(italic=True, color="808080")

    r = 4
    if "error" in oz:
        _row(ws, r, oz["error"], "")
    else:
        r = _section(ws, r, "ДЕНЬГИ ВЛАДЕЛЬЦА (Ozon)")
        r = _row(ws, r, "Продано, шт", oz["sold_units"], PCS)
        r = _row(ws, r, "Возвраты, шт", oz["returns_units"], PCS)
        r = _row(ws, r, "Чистые выкупы", oz["net_units"], PCS, bold=True)
        r = _row(ws, r, "Продажи (цена продавца)", oz["gross_sales"])
        r = _row(ws, r, "− Возвраты", -oz["returns_sum"])
        r = _row(ws, r, "Чистые продажи", oz["net_sales"], bold=True)
        r = _row(ws, r, "Выплаты Ozon (после всех удержаний)", oz["payout"], bold=True)
        r = _row(ws, r, "   справочно: реклама внутри выплат", oz["ads"], indent=2)
        r = _row(ws, r, "   справочно: кросс-докинг внутри выплат", oz["crossdock"], indent=2)
        r = _row(ws, r, "Опт (выплаты / чистые выкупы)", oz["opt"], RUBU, bold=True)
        r = _row(ws, r, "Налоговая база: реализовано за вычетом возвратов", oz["tax_realized"], indent=2)
        r = _row(ws, r, "Налоговая база: выплаты от партнёров", oz["tax_partners"], indent=2)
        r = _row(ws, r, "− Налог УСН 6% от базы по реализации", -oz["tax"])
        for g, d in oz["groups"].items():
            r = _row(ws, r, f"− Завод: {g} ({d['net_units']} × {d['cost']} ₽)", -d["factory"])
        r = _row(ws, r, "− Транспортный короб (7 ₽/шт, корпусные)", -oz["pack"])
        r = _row(ws, r, "= Остаток до команды", oz["before_team"], bold=True)
        r = _row(ws, r, "− Команда Ozon: оклад", -oz["salary"])
        k = oz["kpi"]
        if final:
            r = _row(ws, r, "− Команда Ozon: KPI", -k["total"])
        else:
            r = _row(ws, r, "   KPI (резерв, не вычтен)", k["total"], indent=2)
        r = _row(ws, r, "= ДЕНЬГИ ВЛАДЕЛЬЦА", oz["owner"], fill=OWNER, bold=True)
        r = _row(ws, r, "Прибыль владельца на 1 шт", oz["unit_profit"], RUBU)
        r += 1
        r = _section(ws, r, "ОБЯЗАТЕЛЬСТВА")
        r = _row(ws, r, "Долг налоговой (УСН 6%)", oz["tax"])
        r = _row(ws, r, "Долг заводу", oz["factory"])

        # По SKU
        s = wb.create_sheet("По SKU")
        for col, w in zip("ABCDEFGH", (22, 12, 10, 10, 12, 16, 16, 14)):
            s.column_dimensions[col].width = w
        _table(s, 1, ["Артикул", "Группа", "Продано", "Возвраты", "Чистые, шт",
                      "Чист. продажи, ₽", "Выплата, ₽", "Опт, ₽/шт"],
               [[x["offer"], x["group"], x["sold"], x["ret"], x["net_units"],
                 x["net_sales"], x["payout"], x["opt"]] for x in oz["by_sku"]],
               ["General", "General", "#,##0", "#,##0", "#,##0", MONEY, MONEY, RUBU])
        s.cell(len(oz["by_sku"]) + 3, 1,
               "Выплата по SKU включает общие удержания Ozon без привязки к SKU (реклама CPC/CPO, кросс-докинг, подписки), распределённые пропорционально продажам.").font = Font(italic=True, color="808080")

        # Удержания
        d = wb.create_sheet("Удержания Ozon")
        d.column_dimensions["A"].width = 52; d.column_dimensions["B"].width = 18
        _table(d, 1, ["Статья", "Сумма, ₽"], [[a, b] for a, b in oz["deductions"]], ["General", MONEY])

        # KPI
        kp = wb.create_sheet("KPI команды")
        kp.column_dimensions["A"].width = 44; kp.column_dimensions["B"].width = 18
        rr = 1
        rr = _row(kp, rr, "% расхода месяца (1 − выплаты / оборот)", k["expense_rate"], "0.00%")
        rr = _row(kp, rr, "KPI по опту (50% × Σ разницы)", k["opt"])
        rr = _row(kp, rr, "   Σ до обнуления отрицательного", k["opt_raw"], indent=2)
        rr = _row(kp, rr, f"KPI по объёму ({oz['net_units']} шт)", k["volume"])
        rr = _row(kp, rr, "ИТОГО KPI", k["total"], fill=OWNER, bold=True)
        rr = _row(kp, rr, "Комментарий", k["reason"])
        rr += 1
        _table(kp, rr, ["Артикул", "Опт факт", "План", "Шт", "Разница, ₽"],
               [[x["offer"], x["fact"], x["plan"], x["units"], x["diff"]] for x in k["by_sku"]],
               ["General", RUBU, RUBU, "#,##0", MONEY])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path


def out_path_for(period: str, base="reports") -> Path:
    y, m = map(int, period.split("-"))
    return Path(base) / period / f"Отчёт_Ozon_{RU_MONTHS[m]}_{y}.xlsx"
