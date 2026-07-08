#!/usr/bin/env python3
"""
fetch.py — сбор сырья с Ozon + WB за период.
Использование:
  python fetch.py --from 2026-05-01 --to 2026-06-16 --out ./raw/2026-05
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY   = os.getenv("OZON_API_KEY")
WB_STATS_TOKEN = os.getenv("WB_STATS_TOKEN") or os.getenv("WB_API_TOKEN")
WB_ADV_TOKEN   = os.getenv("WB_ADV_TOKEN")   or os.getenv("WB_API_TOKEN")

OZON_BASE = "https://api-seller.ozon.ru"
WB_STAT   = "https://statistics-api.wildberries.ru"
WB_ADV    = "https://advert-api.wildberries.ru"
WB_API    = "https://common-api.wildberries.ru"

MANIFEST = {}


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def log(msg: str):
    print(f"[fetch] {msg}", flush=True)


def save(out: Path, name: str, data) -> Path:
    path = out / name
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    size = path.stat().st_size
    log(f"  → {name} ({len(data) if isinstance(data, list) else 'object'} | {size:,} bytes)")
    return path


def ozon_post(endpoint: str, body: dict) -> dict:
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        sys.exit("ERROR: OZON_CLIENT_ID / OZON_API_KEY не найдены в .env")
    r = requests.post(
        OZON_BASE + endpoint,
        headers={
            "Client-Id": OZON_CLIENT_ID,
            "Api-Key": OZON_API_KEY,
            "Content-Type": "application/json",
        },
        json=body,
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def wb_get(url: str, token: str, params: dict = None, pause: bool = True) -> list | dict:
    if not token:
        sys.exit(f"ERROR: WB-токен не найден в .env для {url}")
    if pause:
        log("  (пауза 61 сек — WB rate limit)")
        time.sleep(61)
    r = requests.get(url, headers={"Authorization": token}, params=params, timeout=120)
    if r.status_code == 429:
        log("  WB 429 Too Many Requests — ждём 61 сек и повторяем")
        time.sleep(61)
        r = requests.get(url, headers={"Authorization": token}, params=params, timeout=120)
    r.raise_for_status()
    return r.json()


def wb_post(url: str, token: str, body: dict, pause: bool = True) -> list | dict:
    if not token:
        sys.exit(f"ERROR: WB-токен не найден в .env для {url}")
    if pause:
        log("  (пауза 61 сек — WB rate limit)")
        time.sleep(61)
    r = requests.post(url, headers={"Authorization": token, "Content-Type": "application/json"}, json=body, timeout=120)
    if r.status_code == 429:
        log("  WB 429 Too Many Requests — ждём 61 сек и повторяем")
        time.sleep(61)
        r = requests.post(url, headers={"Authorization": token, "Content-Type": "application/json"}, json=body, timeout=120)
    r.raise_for_status()
    return r.json()


# ──────────────────────────────────────────────
# OZON
# ──────────────────────────────────────────────

def fetch_ozon_transactions(date_from: str, date_to: str, out: Path):
    """POST /v3/finance/transaction/list — полная пагинация."""
    log("OZON: финансовые транзакции")

    # API не принимает период > 1 месяца — разбиваем при необходимости
    from datetime import date, timedelta
    d_from = date.fromisoformat(date_from)
    d_to   = date.fromisoformat(date_to)

    all_ops = []
    total_pages = 0

    # Разбиваем по месяцам
    chunk_start = d_from
    while chunk_start <= d_to:
        # конец чанка — последний день этого месяца или d_to
        import calendar
        last_day = calendar.monthrange(chunk_start.year, chunk_start.month)[1]
        chunk_end = min(date(chunk_start.year, chunk_start.month, last_day), d_to)

        cf = chunk_start.isoformat() + "T00:00:00Z"
        ct = chunk_end.isoformat()   + "T23:59:59Z"
        log(f"  чанк {cf[:10]} — {ct[:10]}")

        page = 1
        while True:
            data = ozon_post("/v3/finance/transaction/list", {
                "filter": {"date": {"from": cf, "to": ct}, "transaction_type": "all"},
                "page": page,
                "page_size": 1000,
            })
            if "result" not in data:
                log(f"  ОШИБКА стр {page}: {data}")
                break
            page_count = data["result"]["page_count"]
            ops = data["result"]["operations"]
            all_ops.extend(ops)
            total_pages += 1
            log(f"    стр {page}/{page_count}: {len(ops)} ops, всего: {len(all_ops)}")
            if page >= page_count:
                break
            page += 1

        chunk_start = chunk_end + timedelta(days=1)

    path = save(out, "ozon_transactions.json", all_ops)
    total_amount = sum(op.get("amount", 0) for op in all_ops)

    MANIFEST["ozon_finance"] = {
        "file": str(path),
        "size_bytes": path.stat().st_size,
        "pages_fetched": total_pages,
        "operations_count": len(all_ops),
        "total_amount": round(total_amount, 2),
    }
    log(f"  Итого: {len(all_ops)} операций, {total_pages} страниц, сумма={total_amount:,.2f} ₽")


def fetch_ozon_transaction_totals(date_from: str, date_to: str, out: Path):
    """POST /v3/finance/transaction/totals — сверка."""
    log("OZON: transaction/totals")
    from datetime import date, timedelta
    import calendar

    d_from = date.fromisoformat(date_from)
    d_to   = date.fromisoformat(date_to)
    all_totals = []

    chunk_start = d_from
    while chunk_start <= d_to:
        last_day = calendar.monthrange(chunk_start.year, chunk_start.month)[1]
        chunk_end = min(date(chunk_start.year, chunk_start.month, last_day), d_to)
        cf = chunk_start.isoformat() + "T00:00:00Z"
        ct = chunk_end.isoformat()   + "T23:59:59Z"
        try:
            data = ozon_post("/v3/finance/transaction/totals", {
                "filter": {"date": {"from": cf, "to": ct}, "transaction_type": "all"}
            })
            all_totals.append({"period": f"{cf[:10]}/{ct[:10]}", "data": data})
            log(f"  totals {cf[:10]}–{ct[:10]}: OK")
        except Exception as e:
            log(f"  totals ОШИБКА {cf[:10]}–{ct[:10]}: {e}")
        from datetime import timedelta
        chunk_start = chunk_end + timedelta(days=1)

    save(out, "ozon_transaction_totals.json", all_totals)


def fetch_ozon_fbo(date_from: str, date_to: str, out: Path):
    """POST /v2/posting/fbo/list — offset-пагинация."""
    log("OZON: FBO отправления")
    all_items = []
    offset = 0
    page = 0
    while True:
        data = ozon_post("/v2/posting/fbo/list", {
            "dir": "asc",
            "filter": {"since": date_from + "T00:00:00Z", "to": date_to + "T23:59:59Z", "status": ""},
            "limit": 50,
            "offset": offset,
            "with": {"financial_data": False},
        })
        if "result" not in data:
            log(f"  FBO ОШИБКА: {data}")
            break
        items = data["result"]
        if not items:
            break
        all_items.extend(items)
        offset += len(items)
        page += 1
        log(f"  стр {page}: +{len(items)}, всего: {len(all_items)}")
        if len(items) < 50:
            break

    path = save(out, "ozon_fbo_postings.json", all_items)
    MANIFEST["ozon_postings_fbo"] = {
        "file": str(path), "size_bytes": path.stat().st_size, "count": len(all_items)
    }

    # Считаем статусы
    statuses = {}
    for p in all_items:
        s = p.get("status", "unknown")
        statuses[s] = statuses.get(s, 0) + 1
    log(f"  Итого FBO: {len(all_items)} отправлений, статусы: {statuses}")


def fetch_ozon_fbs(date_from: str, date_to: str, out: Path):
    """POST /v3/posting/fbs/list — offset-пагинация."""
    log("OZON: FBS отправления")
    all_items = []
    offset = 0
    page = 0
    while True:
        data = ozon_post("/v3/posting/fbs/list", {
            "dir": "asc",
            "filter": {"since": date_from + "T00:00:00Z", "to": date_to + "T23:59:59Z", "status": ""},
            "limit": 50,
            "offset": offset,
            "with": {"financial_data": False, "barcodes": False, "translit": False},
        })
        if "result" not in data:
            log(f"  FBS ОШИБКА: {data}")
            break
        items = data["result"].get("postings", []) if isinstance(data["result"], dict) else data["result"]
        if not items:
            break
        all_items.extend(items)
        offset += len(items)
        page += 1
        log(f"  стр {page}: +{len(items)}, всего: {len(all_items)}")
        if len(items) < 50:
            break

    path = save(out, "ozon_fbs_postings.json", all_items)
    MANIFEST["ozon_postings_fbs"] = {
        "file": str(path), "size_bytes": path.stat().st_size, "count": len(all_items)
    }
    log(f"  Итого FBS: {len(all_items)} отправлений")


def fetch_ozon_returns(out: Path):
    """POST /v1/returns/list — пагинация."""
    log("OZON: возвраты")
    all_items = []
    page = 1
    while True:
        try:
            data = ozon_post("/v1/returns/list", {"page": page, "page_size": 500})
        except Exception as e:
            log(f"  returns ОШИБКА стр {page}: {e}")
            break
        if "result" not in data:
            log(f"  returns ОШИБКА: {data}")
            break
        result = data["result"]
        items = result.get("returns", result) if isinstance(result, dict) else result
        if not isinstance(items, list) or not items:
            break
        all_items.extend(items)
        log(f"  стр {page}: +{len(items)}, всего: {len(all_items)}")
        if len(items) < 500:
            break
        page += 1

    path = save(out, "ozon_returns.json", all_items)
    MANIFEST["ozon_returns"] = {
        "file": str(path), "size_bytes": path.stat().st_size, "count": len(all_items)
    }
    log(f"  Итого возвратов: {len(all_items)}")


def fetch_ozon_stocks(out: Path):
    """POST /v4/product/info/stocks."""
    log("OZON: остатки")
    all_items = []
    cursor = ""
    while True:
        body = {"filter": {}, "limit": 100}
        if cursor:
            body["cursor"] = cursor
        data = ozon_post("/v4/product/info/stocks", body)
        if "items" not in data:
            log(f"  stocks ОШИБКА: {data}")
            break
        items = data["items"]
        all_items.extend(items)
        cursor = data.get("cursor", "")
        log(f"  +{len(items)}, всего: {len(all_items)}, cursor={'...' if cursor else 'end'}")
        if not cursor or len(items) < 100:
            break

    path = save(out, "ozon_stocks.json", all_items)
    MANIFEST["ozon_stocks"] = {
        "file": str(path), "size_bytes": path.stat().st_size, "count": len(all_items)
    }


# ──────────────────────────────────────────────
# WB
# ──────────────────────────────────────────────

def fetch_wb_sales(date_from: str, out: Path):
    """GET /api/v1/supplier/sales — выкупы и возвраты."""
    log("WB: продажи/выкупы")
    data = wb_get(
        f"{WB_STAT}/api/v1/supplier/sales",
        WB_STATS_TOKEN,
        params={"dateFrom": date_from, "flag": "0"},
    )
    if not isinstance(data, list):
        log(f"  ОШИБКА: {data}")
        MANIFEST["wb_sales"] = {"error": str(data)}
        return

    path = save(out, "wb_sales.json", data)
    buyouts = [s for s in data if s.get("saleID", "").startswith("S")]
    returns = [s for s in data if s.get("saleID", "").startswith("R")]
    MANIFEST["wb_sales"] = {
        "file": str(path),
        "size_bytes": path.stat().st_size,
        "total_records": len(data),
        "buyouts_count": len(buyouts),
        "returns_count": len(returns),
        "buyouts_forpay": round(sum(s.get("forPay", 0) for s in buyouts), 2),
        "buyouts_finished_price": round(sum(s.get("finishedPrice", 0) for s in buyouts), 2),
    }
    log(f"  Всего: {len(data)}, выкупов(S): {len(buyouts)}, возвратов(R): {len(returns)}")


def fetch_wb_orders(date_from: str, out: Path):
    """GET /api/v1/supplier/orders."""
    log("WB: заказы")
    data = wb_get(
        f"{WB_STAT}/api/v1/supplier/orders",
        WB_STATS_TOKEN,
        params={"dateFrom": date_from, "flag": "0"},
    )
    if not isinstance(data, list):
        log(f"  ОШИБКА: {data}")
        MANIFEST["wb_orders"] = {"error": str(data)}
        return

    path = save(out, "wb_orders.json", data)
    MANIFEST["wb_orders"] = {
        "file": str(path), "size_bytes": path.stat().st_size, "count": len(data)
    }
    log(f"  Заказов: {len(data)}")


def fetch_wb_report_detail(date_from: str, date_to: str, out: Path):
    """GET /api/v5/supplier/reportDetailByPeriod — пагинация по rrdid."""
    log("WB: реализация (reportDetailByPeriod)")
    all_rows = []
    rrdid = 0
    iteration = 0

    while True:
        data = wb_get(
            f"{WB_STAT}/api/v5/supplier/reportDetailByPeriod",
            WB_STATS_TOKEN,
            params={"dateFrom": date_from, "dateTo": date_to, "rrdid": rrdid, "limit": 100000},
        )
        if not isinstance(data, list):
            log(f"  ОШИБКА итерация {iteration + 1}: {data}")
            break
        if not data:
            log(f"  Итерация {iteration + 1}: пусто — конец пагинации")
            break

        all_rows.extend(data)
        rrdid = data[-1].get("rrd_id", 0)
        iteration += 1
        log(f"  Итерация {iteration}: +{len(data)} строк, всего: {len(all_rows)}, rrdid={rrdid}")

        if len(data) < 100000:
            log("  Строк < 100000 — конец пагинации")
            break

    path = save(out, "wb_report_detail.json", all_rows)

    ppvz_total = round(sum(r.get("ppvz_for_pay", 0) for r in all_rows), 2)
    sales_rows   = [r for r in all_rows if r.get("doc_type_name") == "Продажа"]
    returns_rows = [r for r in all_rows if r.get("doc_type_name") == "Возврат"]

    MANIFEST["wb_report"] = {
        "file": str(path),
        "size_bytes": path.stat().st_size,
        "rrdid_iterations": iteration,
        "total_rows": len(all_rows),
        "sales_rows": len(sales_rows),
        "returns_rows": len(returns_rows),
        "ppvz_for_pay_total": ppvz_total,
    }
    log(f"  Итого: {len(all_rows)} строк, {iteration} итераций rrdid, ppvz_for_pay={ppvz_total:,.2f} ₽")


def fetch_wb_stocks(date_from: str, out: Path):
    """GET /api/v1/supplier/stocks."""
    log("WB: остатки")
    data = wb_get(
        f"{WB_STAT}/api/v1/supplier/stocks",
        WB_STATS_TOKEN,
        params={"dateFrom": date_from},
    )
    if not isinstance(data, list):
        log(f"  ОШИБКА: {data}")
        MANIFEST["wb_stocks"] = {"error": str(data)}
        return

    path = save(out, "wb_stocks.json", data)
    MANIFEST["wb_stocks"] = {
        "file": str(path), "size_bytes": path.stat().st_size, "count": len(data)
    }
    log(f"  Остатков: {len(data)} позиций")


def fetch_wb_adv(date_from: str, date_to: str, out: Path):
    """GET /adv/v1/upd — реклама (max 1 месяц, разбиваем если нужно)."""
    log("WB: реклама")
    from datetime import date, timedelta
    import calendar

    d_from = date.fromisoformat(date_from)
    d_to   = date.fromisoformat(date_to)
    all_adv = []

    chunk_start = d_from
    first = True
    while chunk_start <= d_to:
        last_day = calendar.monthrange(chunk_start.year, chunk_start.month)[1]
        chunk_end = min(date(chunk_start.year, chunk_start.month, last_day), d_to)
        cf = chunk_start.isoformat()
        ct = chunk_end.isoformat()

        data = wb_get(
            f"{WB_ADV}/adv/v1/upd",
            WB_ADV_TOKEN,
            params={"from": cf, "to": ct},
            pause=not first,
        )
        first = False

        if isinstance(data, list):
            all_adv.extend(data)
            total = sum(x.get("updSum", 0) for x in data)
            log(f"  {cf}–{ct}: {len(data)} записей, {total:,} ₽")
        else:
            log(f"  {cf}–{ct}: ОШИБКА {data}")

        chunk_start = chunk_end + timedelta(days=1)

    path = save(out, "wb_adv.json", all_adv)

    from collections import defaultdict
    by_camp = defaultdict(int)
    for x in all_adv:
        by_camp[x.get("campName", str(x.get("advertId", "")))] += x.get("updSum", 0)

    total_adv = sum(all_adv[i].get("updSum", 0) for i in range(len(all_adv)))
    MANIFEST["wb_adv"] = {
        "file": str(path),
        "size_bytes": path.stat().st_size,
        "total_spend": total_adv,
        "by_campaign": dict(by_camp),
    }
    log(f"  Итого реклама: {total_adv:,} ₽ по {len(by_camp)} кампаниям")


def fetch_wb_funnel(date_from: str, date_to: str, out: Path):
    """POST /api/v2/nm-report/detail — воронка."""
    log("WB: воронка (nm-report/detail)")
    try:
        data = wb_post(
            f"https://seller-analytics-api.wildberries.ru/api/v2/nm-report/detail",
            WB_STATS_TOKEN,
            body={
                "brandNames": [], "objectIDs": [], "tagIDs": [], "nmIDs": [],
                "timezone": "Europe/Moscow",
                "period": {"begin": date_from + " 00:00:00", "end": date_to + " 23:59:59"},
                "orderBy": {"field": "openCardCount", "mode": "desc"},
                "page": 1,
            },
            pause=True,
        )
    except Exception as e:
        log(f"  воронка ОШИБКА: {e}")
        MANIFEST["wb_funnel"] = {"error": str(e)}
        return

    if isinstance(data, dict) and "data" in data:
        rows = data["data"].get("cards", [])
    elif isinstance(data, list):
        rows = data
    else:
        log(f"  воронка неожиданный формат: {str(data)[:200]}")
        rows = []

    path = save(out, "wb_funnel.json", rows if rows else data)
    MANIFEST["wb_funnel"] = {
        "file": str(path),
        "size_bytes": path.stat().st_size,
        "nm_count": len(rows),
    }
    log(f"  Воронка: {len(rows)} nmId")


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Выгрузка сырья с Ozon + WB")
    parser.add_argument("--from", dest="date_from", required=True, help="Дата начала YYYY-MM-DD")
    parser.add_argument("--to",   dest="date_to",   required=True, help="Дата конца YYYY-MM-DD")
    parser.add_argument("--out",  dest="out_dir",   required=True, help="Папка для сырья")
    parser.add_argument("--skip", nargs="*", default=[], help="Источники для пропуска (ozon_fbs, wb_funnel, ...)")
    args = parser.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    log(f"Период: {args.date_from} — {args.date_to}")
    log(f"Папка:  {out.resolve()}")
    log(f"Пропускаем: {args.skip or 'ничего'}")
    print()

    skip = set(args.skip)

    # ── OZON ──
    log("━━━ OZON ━━━")
    fetch_ozon_transactions(args.date_from, args.date_to, out)
    fetch_ozon_transaction_totals(args.date_from, args.date_to, out)

    if "ozon_fbo" not in skip:
        fetch_ozon_fbo(args.date_from, args.date_to, out)
    if "ozon_fbs" not in skip:
        fetch_ozon_fbs(args.date_from, args.date_to, out)
    if "ozon_returns" not in skip:
        fetch_ozon_returns(out)

    fetch_ozon_stocks(out)

    # ── WB ──
    print()
    log("━━━ WB ━━━")
    fetch_wb_sales(args.date_from, out)
    fetch_wb_orders(args.date_from, out)
    fetch_wb_report_detail(args.date_from, args.date_to, out)
    fetch_wb_stocks(args.date_from, out)

    if "wb_adv" not in skip:
        fetch_wb_adv(args.date_from, args.date_to, out)
    if "wb_funnel" not in skip:
        fetch_wb_funnel(args.date_from, args.date_to, out)

    # ── Манифест ──
    print()
    log("━━━ МАНИФЕСТ ━━━")
    manifest_path = out / "manifest.json"
    MANIFEST["meta"] = {
        "date_from": args.date_from,
        "date_to": args.date_to,
        "generated_at": __import__("datetime").datetime.now().isoformat(),
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(MANIFEST, f, ensure_ascii=False, indent=2)

    print()
    print("=" * 60)
    print("МАНИФЕСТ")
    print("=" * 60)
    for key, val in MANIFEST.items():
        if key == "meta":
            continue
        if isinstance(val, dict) and "error" in val:
            print(f"  {key}: ОШИБКА — {val['error']}")
            continue
        parts = []
        for k, v in val.items():
            if k in ("file", "size_bytes"):
                continue
            parts.append(f"{k}={v:,}" if isinstance(v, (int, float)) else f"{k}={v}")
        size = val.get("size_bytes", 0)
        fname = Path(val.get("file", "")).name
        print(f"  {key}: {', '.join(parts)}  [{fname}, {size:,} bytes]")
    print("=" * 60)
    print(f"manifest.json → {manifest_path}")


if __name__ == "__main__":
    main()
