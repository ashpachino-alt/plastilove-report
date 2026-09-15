# PlastiLove — ежедневный отчёт в Telegram (деплой на VPS)

Пайплайн из 3 частей, всё запускается одной командой каждый день в 9:00 МСК:

```
run_daily.py → fetch.py (сбор Ozon+WB) → compute.py (расчёт) → xlsx_report.py (Excel) → report.py (Telegram)
```

## Файлы

| Файл | Что делает |
|---|---|
| `fetch.py` | Тянет сырьё Ozon + WB за период → `raw/<YYYY-MM>/` |
| `compute.py` | Считает юнит-экономику по инструкции (Ozon/WB/итог, KPI, долги) |
| `xlsx_report.py` | Строит Excel-отчёт → `reports/<YYYY-MM>/Отчёт_<Месяц>_<Год>.xlsx` |
| `report.py` | Собирает сообщение и шлёт в Telegram (+ прикладывает Excel) |
| `run_daily.py` | Оркестратор: период текущего месяца → всё по цепочке |

## Быстрый старт (локально проверить)

```bash
pip install -r requirements.txt
cp .env.example .env          # вписать ключи Ozon/WB и Telegram
python run_daily.py --skip-fetch --no-send      # расчёт по уже скачанному сырью, без отправки
python report.py --raw raw/2026-06 --period 2026-06   # только превью текста ТГ
```

## Telegram-бот (5 минут)

1. В Telegram напиши **@BotFather** → `/newbot` → получи `TG_BOT_TOKEN`.
2. Напиши своему новому боту любое сообщение (иначе он не сможет тебе писать).
3. Узнай свой `TG_CHAT_ID` у **@userinfobot**.
4. Впиши оба значения в `.env`.
5. Тест: `python report.py --raw raw/2026-06 --period 2026-06 --send`

## Деплой на VPS (Ubuntu, systemd — рекомендуется)

```bash
# 1. Код и окружение
sudo mkdir -p /opt/plastilove && sudo chown $USER /opt/plastilove
# скопировать сюда все .py, requirements.txt, .env, папку deploy/
cd /opt/plastilove
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env         # вписать ключи

# 2. Таймзона сервера — Москва (важно для 9:00)
sudo timedatectl set-timezone Europe/Moscow

# 3. systemd
sudo cp deploy/plastilove-report.service /etc/systemd/system/
sudo cp deploy/plastilove-report.timer   /etc/systemd/system/
#   ⚠️ поправь в .service путь WorkingDirectory/ExecStart и User под себя
sudo systemctl daemon-reload
sudo systemctl enable --now plastilove-report.timer

# 4. Проверка
systemctl list-timers | grep plastilove       # когда следующий запуск
sudo systemctl start plastilove-report.service # прогнать прямо сейчас
journalctl -u plastilove-report.service -f     # смотреть логи
```

### Альтернатива — cron (если не хочешь systemd)

```bash
# crontab -e  (сервер в TZ Europe/Moscow)
0 9 * * *  cd /opt/plastilove && /opt/plastilove/venv/bin/python run_daily.py >> /opt/plastilove/daily.log 2>&1
```

## Почему отчёты обнулились в сентябре 2026 (и что исправлено)

Ozon отключил методы `/v3/finance/transaction/list` и `/v3/finance/transaction/totals`
8 сентября 2026 — именно из-за этого `fetch.py` переставал получать данные о начислениях,
`ozon_transactions.json` оставался пустым, и весь расчёт уходил в 0 (переход на FBO тут
ни при чём, совпадение по времени случайное). `fetch.py` и `compute.py` переведены на
новый метод `/v1/finance/cash-flow-statement/list`. Он официально пока не задокументирован
Ozon в открытом доступе, поэтому разбор ответа сделан максимально гибко — если Ozon снова
поменяет формат, в логе `fetch.py` будут видны сырые ключи ответа для быстрой диагностики.

Заодно добавлена разбивка **FBO / FBS** внутри Ozon (продажи, выплаты, опт и маржа отдельно
по каждой схеме — данные для этого уже собирались в `ozon_fbo_postings.json` /
`ozon_fbs_postings.json`, но раньше не использовались) и разбивка по схеме для WB
(продажи и штуки — по `warehouseType` в `wb_orders.json`/`wb_sales.json`, сопоставленные
по `srid`; удержания WB по схемам не разделены, т.к. в отчёте реализации WB такого поля нет).

## Важные нюансы

- **Время прогона.** WB требует паузу 61 сек между вызовами статистики, поэтому полный `fetch` идёт ~5–8 минут. Это нормально, отчёт придёт чуть позже 9:00.
- **Период.** По умолчанию отчёт за текущий месяц (1 → сегодня), нарастающим итогом каждое утро. Оперативный: KPI Ozon в резерве, не в команде.
- **Финальное закрытие месяца.** После 14-го числа следующего месяца прогнать вручную с хвостом:
  `python run_daily.py --period 2026-06 --final --cross-dock 25000`
- **Кросс-докинг WB** передаётся через `--cross-dock` или `CROSS_DOCK` в `.env` (в API его нет).
- **Себестоимость.** Сейчас всё по 250 ₽/шт (корпус). Если часть SKU без корпуса — считать через `--cost 170` или разнести вручную (дальнейшее улучшение).
