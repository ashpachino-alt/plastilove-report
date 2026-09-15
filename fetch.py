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
    if r.status_code >= 400:
        # requests.raise_for_status() прячет тело ответа — а у Ozon именно в теле
        # обычно лежит понятная причина (например "code":3, "message":"..."),
        # без него 400/404 ничего не говорят о том, что чинить.
        raise RuntimeError(f"{r.status_code} {endpoint}: {r.text[:1500]}")
    return r.json()


def _wb_retry_wait(r, default=61) -> int:
    """
    WB на 429 сама говорит, сколько ждать (заголовок X-Ratelimit-Retry,
    иногда Retry-After) — используем это значение вместо угаданных 61 сек:
    если бакет реально опустел раньше (например, от параллельного скрипта/
    скилла, использующего тот же токен), фиксированная пауза может
    оказаться короче настоящего окна и все 4 попытки уйдут в те же 429.
    """
    for h in ("X-Ratelimit-Retry", "Retry-After"):
        v = r.headers.get(h)
        if v:
            try:
                return max(int(float(v)), 1)
            except ValueError:
                pass
    return default


def wb_get(url: str, token: str, params: dict = None, pause: bool = True) -> list | dict:
    if not token:
        sys.exit(f"ERROR: WB-токен не найден в .env для {url}")
    if pause:
        log("  (пауза 61 сек — WB rate limit)")
        time.sleep(61)
    r = requests.get(url, headers={"Authorization": token}, params=params, timeout=120)
    for attempt in range(4):
        if r.status_code != 429:
            break
        wait = _wb_retry_wait(r)
        log(f"  WB 429 Too Many Requests (X-Ratelimit-Retry={r.headers.get('X-Ratelimit-Retry')!r}) — ждём {wait} сек и повторяем (попытка {attempt + 1}/4)")
        time.sleep(wait)
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

def _extract_ozon_ops(data):
    """
    Гибко достаёт список операций из ответа /v1/finance/cash-flow-statement/list.
    Точная форма ответа этого метода официально не задокументирована (метод новый,
    пришёл на замену /v3/finance/transaction/list — см. комментарий к
    fetch_ozon_finance ниже), поэтому пробуем несколько известных путей.
    Возвращает список операций, либо None, если ни один путь не подошёл
    (тогда вызывающий код печатает сырые ключи ответа для диагностики).
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        candidate_paths = (
            ("result", "operations"), ("result", "cash_flows"), ("result", "items"),
            ("result", "rows"), ("result",),
            ("operations",), ("cash_flows",), ("items",), ("rows",),
        )
        for path in candidate_paths:
            cur = data
            ok = True
            for p in path:
                if isinstance(cur, dict) and p in cur:
                    cur = cur[p]
                else:
                    ok = False
                    break
            if ok and isinstance(cur, list):
                return cur
    return None


def fetch_ozon_finance(date_from: str, date_to: str, out: Path):
    """
    Начисления и движение денег Ozon.

    ⚠️ ВАЖНО: раньше здесь использовались /v3/finance/transaction/list и
    /v3/finance/transaction/totals. Ozon отключил оба метода 08.09.2026
    (анонс в @OzonSellerAPI, см. https://dev.ozon.ru/news/699-Novye-metody-dlia-finansovykh-otchetov-v-Seller-API/).
    Именно это — а не переход продавца на FBO — было причиной нулевых отчётов
    в сентябре 2026.

    Первая попытка замены (/v1/finance/cash-flow-statement/list, тело
    {"date":{"from","to"},"with_details":true,"page","page_size"} — как в
    неофициальном ozon-mcp-server) вернула 400 Bad Request 15.09.2026 —
    точная форма запроса этого метода нигде официально не задокументирована,
    и чужая реализация угадала её неверно (или метод с тех пор поменялся).

    Поэтому здесь — НЕ один жёстко зашитый запрос, а перебор кандидатов
    (эндпоинт + тело) по порядку убывания вероятности правильности:
    1) POST /v1/finance/accrual/postings — официально анонсированная замена
       для начислений по отправлениям (нужна именно она — только она может
       дать разбивку по posting_number для FBO/FBS).
    2) POST /v1/finance/accrual/by-day — тоже официально анонсирован,
       но это агрегат по дню БЕЗ отправлений: разбивку по FBO/FBS он дать
       не может, зато формат его тела ({"date":"YYYY-MM-DD"}) подтверждён
       рабочей реализацией (ozon-mcp-server), поэтому это надёжный
       запасной вариант если (1) продолжит не работать.
    Для КАЖДОЙ попытки в лог пишется код ответа и ПОЛНОЕ тело (обрезано до
    2000 симв.) — и при успехе, и при ошибке. Так при следующей неудаче
    сразу видно точную причину и реальную форму ответа, без гаданий.
    """
    from datetime import date, timedelta

    d_from = date.fromisoformat(date_from)
    d_to   = date.fromisoformat(date_to)
    days = [d_from + timedelta(days=i) for i in range((d_to - d_from).days + 1)]

    log("OZON: начисления — пробуем /v1/finance/accrual/postings")
    all_ops = []
    postings_ok = True
    try:
        page = 1
        while True:
            body = {
                "filter": {"date": {"from": date_from, "to": date_to}},
                "page": page,
                "page_size": 1000,
            }
            data = ozon_post("/v1/finance/accrual/postings", body)
            log(f"  стр {page} — сырой ответ (обрезан 2000): {json.dumps(data, ensure_ascii=False)[:2000]}")
            ops = _extract_ozon_ops(data)
            if not ops:
                break
            all_ops.extend(ops)
            log(f"    +{len(ops)}, всего: {len(all_ops)}")
            if len(ops) < 1000:
                break
            page += 1
    except Exception as e:
        log(f"  ⚠️ accrual/postings не сработал: {e}")
        postings_ok = False
        all_ops = []

    source = "accrual_postings"
    if not postings_ok or not all_ops:
        log("OZON: пробуем запасной вариант /v1/finance/accrual/by-day (день за днём, без разбивки по отправлениям)")
        source = "accrual_by_day"
        all_ops = []
        logged_sample = False
        for i, d in enumerate(days):
            ds = d.isoformat()
            try:
                data = ozon_post("/v1/finance/accrual/by-day", {"date": ds})
            except Exception as e:
                log(f"  ⚠️ {ds}: {e}")
                continue
            if not logged_sample:
                log(f"  сырой ответ за {ds} (обрезан 2000): {json.dumps(data, ensure_ascii=False)[:2000]}")
                logged_sample = True
            # Не знаем заранее форму — сохраняем сырой ответ как есть,
            # с добавленной датой, чтобы компьют мог агрегировать хоть что-то.
            rec = data if isinstance(data, dict) else {"raw": data}
            rec["_date"] = ds
            all_ops.append(rec)

    path = save(out, "ozon_finance_operations.json", all_ops)
    total_amount = sum((op.get("amount") or op.get("sum") or 0) for op in all_ops if isinstance(op, dict))

    MANIFEST["ozon_finance"] = {
        "file": str(path),
        "size_bytes": path.stat().st_size,
        "operations_count": len(all_ops),
        "total_amount": round(total_amount, 2),
        "source": source,
    }
    log(f"  Итого ({source}): {len(all_ops)} записей, сумма(эвристика)≈{total_amount:,.2f} ₽")
    if not all_ops:
        log("  ⚠️ записей 0 — оба метода не дали данных, см. ⚠️ выше по каждому")


def fetch_ozon_realization(month: int, year: int, out: Path):
    """
    POST /v2/finance/realization — официальный месячный отчёт о реализации
    (аналог xlsx «Отчёт по начислениям», который раньше выгружали вручную
    из личного кабинета). Отдаёт данные только по уже закрытым месяцам,
    поэтому используется как сверка/задел на будущее для --final закрытия,
    а не для оперативного дневного отчёта. compute.py пока эти данные
    не использует напрямую — файл сохраняется для ручной сверки и
    для последующего расширения расчёта.
    """
    log(f"OZON: отчёт о реализации за {month:02d}.{year}")
    try:
        data = ozon_post("/v2/finance/realization", {"month": month, "year": year})
    except Exception as e:
        log(f"  реализация ОШИБКА: {e}")
        MANIFEST["ozon_realization"] = {"error": str(e)}
        return
    path = save(out, "ozon_realization.json", data)
    MANIFEST["ozon_realization"] = {"file": str(path), "size_bytes": path.stat().st_size}


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

    # Устойчивость: ошибка одного источника НЕ роняет весь сбор.
    # Критичные источники (транзакции Ozon, реализация WB) нужны для расчёта —
    # остальное best-effort. Отчёт соберётся даже если что-то отвалилось.
    def safe(name, fn, *a):
        try:
            fn(*a)
        except Exception as e:
            log(f"  ⚠️ источник '{name}' пропущен из-за ошибки: {e}")
            MANIFEST[name] = {"error": str(e)}

    # ── OZON ──
    log("━━━ OZON ━━━")
    safe("ozon_finance", fetch_ozon_finance, args.date_from, args.date_to, out)

    if "ozon_realization" not in skip and args.date_from.endswith("-01"):
        y, m = map(int, args.date_from.split("-")[:2])
        safe("ozon_realization", fetch_ozon_realization, m, y, out)

    if "ozon_fbo" not in skip:
        safe("ozon_postings_fbo", fetch_ozon_fbo, args.date_from, args.date_to, out)
    if "ozon_fbs" not in skip:
        safe("ozon_postings_fbs", fetch_ozon_fbs, args.date_from, args.date_to, out)
    if "ozon_returns" not in skip:
        safe("ozon_returns", fetch_ozon_returns, out)

    safe("ozon_stocks", fetch_ozon_stocks, out)

    # ── WB ──
    print()
    log("━━━ WB ━━━")
    if "wb_sales" not in skip:
        safe("wb_sales", fetch_wb_sales, args.date_from, out)
    if "wb_orders" not in skip:
        safe("wb_orders", fetch_wb_orders, args.date_from, out)
    if "wb_report" not in skip:
        safe("wb_report", fetch_wb_report_detail, args.date_from, args.date_to, out)
    if "wb_stocks" not in skip:
        safe("wb_stocks", fetch_wb_stocks, args.date_from, out)

    if "wb_adv" not in skip:
        safe("wb_adv", fetch_wb_adv, args.date_from, args.date_to, out)
    if "wb_funnel" not in skip:
        safe("wb_funnel", fetch_wb_funnel, args.date_from, args.date_to, out)

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
