#!/usr/bin/env python3
"""
fetch.py — выгрузка сырья Ozon для отчёта PlastiLove (только Ozon, WB убран).

Источник денег: POST /v1/finance/accrual/by-day — официальная замена
/v3/finance/transaction/list (отключён Ozon 08.09.2026). Метод отдаёт все
начисления за один день; идём день за днём по периоду, с пагинацией last_id.
Каждое начисление содержит total_amount — чистую сумму, которую Ozon
зачисляет продавцу (продажа минус комиссия/логистика/реклама/прочее).

Справочник артикулов: POST /v3/product/list (sku → offer_id), нужен для
себестоимости по группам SKU и плана опта для KPI.

Использование:
  python fetch.py --from 2026-09-01 --to 2026-09-28 --out raw/2026-09
"""

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

OZON_BASE = "https://api-seller.ozon.ru"
OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")


def log(msg: str):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def ozon_post(endpoint: str, body: dict, tries: int = 5) -> dict:
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        raise RuntimeError("OZON_CLIENT_ID / OZON_API_KEY не заданы")
    headers = {"Client-Id": OZON_CLIENT_ID, "Api-Key": OZON_API_KEY,
               "Content-Type": "application/json"}
    last_err = None
    for attempt in range(1, tries + 1):
        try:
            r = requests.post(OZON_BASE + endpoint, headers=headers, json=body, timeout=60)
        except requests.RequestException as e:
            last_err = f"сеть: {e}"
        else:
            if r.status_code == 200:
                return r.json()
            last_err = f"{r.status_code}: {r.text[:500]}"
            if r.status_code not in (429, 500, 502, 503, 504):
                break  # ошибка запроса — повтор не поможет
        wait = min(2 ** attempt, 30)
        log(f"  {endpoint}: {last_err} — повтор через {wait} с")
        time.sleep(wait)
    raise RuntimeError(f"{endpoint} {body}: {last_err}")


def fetch_accruals_day(ds: str) -> list:
    out, last_id = [], ""
    for _ in range(100):  # страховка от бесконечного цикла
        body = {"date": ds}
        if last_id:
            body["last_id"] = last_id
        data = ozon_post("/v1/finance/accrual/by-day", body)
        rows = data.get("accruals") or []
        out.extend(rows)
        nxt = data.get("last_id") or ""
        if not rows or not nxt or nxt == last_id:
            break
        last_id = nxt
    return out


def fetch_accruals(date_from: str, date_to: str) -> list:
    d, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    allrows = []
    while d <= end:
        rows = fetch_accruals_day(d.isoformat())
        allrows.extend(rows)
        log(f"  {d}: {len(rows)} начислений")
        d += timedelta(days=1)
    return allrows


def fetch_skumap() -> dict:
    """sku (строкой) → offer_id, по активным и архивным товарам."""
    m = {}
    for vis in ("ALL", "ARCHIVED"):
        last_id = ""
        for _ in range(50):
            data = ozon_post("/v3/product/list",
                             {"filter": {"visibility": vis}, "limit": 1000, "last_id": last_id})
            res = data.get("result") or {}
            items = res.get("items") or []
            for it in items:
                if it.get("sku"):
                    m[str(it["sku"])] = it.get("offer_id") or ""
            last_id = res.get("last_id") or ""
            if not items or not last_id:
                break
    return m


def save(out: Path, name: str, obj) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    p = out / name
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    return p


def main():
    ap = argparse.ArgumentParser(description="Выгрузка сырья Ozon")
    ap.add_argument("--from", dest="date_from", required=True)
    ap.add_argument("--to", dest="date_to", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--skip", nargs="*", default=[], help="(совместимость, не используется)")
    args = ap.parse_args()
    out = Path(args.out)

    log(f"OZON: начисления {args.date_from} → {args.date_to} (/v1/finance/accrual/by-day)")
    acc = fetch_accruals(args.date_from, args.date_to)
    save(out, "ozon_accruals.json", acc)
    log(f"  итого начислений: {len(acc)}")

    log("OZON: справочник артикулов (/v3/product/list)")
    try:
        skumap = fetch_skumap()
        log(f"  артикулов: {len(skumap)}")
    except Exception as e:  # справочник не критичен: без него всё уйдёт в базовую группу
        log(f"  ⚠️ справочник не получен: {e}")
        skumap = {}
    save(out, "ozon_skumap.json", skumap)

    try:
        t = ozon_post("/v1/finance/accrual/types", {})
        types = {str(x["id"]): x.get("description") or x.get("name") for x in t.get("accrual_types") or []}
    except Exception as e:
        log(f"  ⚠️ справочник типов начислений не получен: {e}")
        types = {}
    save(out, "ozon_types.json", types)
    save(out, "manifest.json", {"from": args.date_from, "to": args.date_to,
                                "accruals": len(acc), "skus": len(skumap),
                                "fetched_at": datetime.now().isoformat(timespec="seconds")})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"❌ ОШИБКА ВЫГРУЗКИ: {e}")
        sys.exit(1)
