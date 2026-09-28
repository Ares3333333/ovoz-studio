# Архитектура MVP — Ovoz AI Studio

## Общая схема

```
 Frontend (SPA uz/ru/en)      Telegram Mini App (следующая итерация)
        │  fetch + Bearer token
        ▼
 FastAPI (app/main.py) ── auth / jobs / glossary / payments / downloads
        │
        ▼
 Billing (app/billing.py) ── кредиты = минуты; ledger-first; идемпотентные webhook
        │
        ▼
 Pipeline (app/pipeline.py) ── state machine: queued → running → done|failed (+refund)
        │
 ├─ Providers (адаптерный слой, sim/real переключается конфигом):
 │    ASR       sim (транскрипт/sidecar)   | faster-whisper (локально, офлайн) | whisper CLI
 │    Translate demo-dict                  | OpenAI-совместимый LLM (ключ)
 │    TTS       stub-tone (честный офлайн) | edge-tts (uz-UZ-MadinaNeural)
 │
 └─ Ling-ядро (собственный модуль = «ров» продукта):
      romanizer  узбекская кириллица ↔ латиница, нормализация апострофа
      glossary   mask/unmask терминов клиента вокруг переводчика
      srt        parse/format/shift/ASS-экспорт (burn-in через ffmpeg)
        │
        ▼
 SQLite (schema-first, потоковые соединения) + файловые артефакты по job_id
```

## Ключевые решения

1. **Единица биллинга = минута обработки** — совпадает с ценностью клиента
   (минуты видео), упрощает юнит-экономику и апгрейд планов.
2. **Ledger-first**: баланс — это сумма проводок, никогда mutable-поле;
   аудит и разбор спорных платежей решаются одним SELECT.
3. **Идемпотентность платежей**: уникальный `external_id`; повторный webhook
   Payme/Click не начисляет кредиты дважды (проверено тестом).
4. **Charge-then-refund**: минуты списываются при приёмке job, возвращаются при
   падении — воркер не может «съесть» деньги молча.
5. **Адаптерный слой провайдеров**: демо-режим (sim) прогоняет весь пайплайн
   end-to-end офлайн; реальные модели/ключи подключаются env-переменными без
   изменения кода. Для продакшена: узбекский TTS — Edge `uz-UZ-MadinaNeural`, а
   ASR — локальный `faster-whisper` (без ключа, офлайн после загрузки модели) или
   внешний whisper CLI.
6. **Языковой слой отдельно от ML**: транслитерация/глоссарии/SRT работают
   детерминированно и покрыты юнит-тестами — это то, что «съедают» все
   наивные обёртки над Whisper.
7. **Timeline событий job** (`job_events`) — наблюдаемость для поддержки:
   видно, на каком этапе умер платёж/задача.

## API (MVP)

| Метод | Путь | Назначение |
|---|---|---|
| POST | /api/auth/register · /login | контакт (телефон/email) → Bearer token |
| GET | /api/me | профиль + баланс минут |
| GET | /api/plans | каталог тарифов |
| POST | /api/jobs | upload (аудио/видео/текст) + jtype/src/tgt → job |
| GET | /api/jobs · /api/jobs/{id} | список / статус + timeline |
| GET | /api/jobs/{id}/download/{kind} | srt, srt_bilingual, ass, dubbing.wav, transcript, document |
| POST/GET | /api/glossary | защита терминов клиента |
| POST | /api/payments/webhook/{provider} | payme / click / telegram_stars (идемпотентно) |
| GET | /healthz | версия + активные провайдеры |

## Что дальше (после MVP)

- Telegram Mini App (WebApp над этим же API) + Stars-оплата — главный канал роста.
- Реальный uz-ASR: дообученный Whisper/Faster-Whisper + дата-контур
  (каждый ручной fix клиента → обучающий пример).
- OCR документов (фото → текст) перед переводчиком; конвейер «черновик →
  верификатор-человек → нотариус-партнёр».
- Очередь задач (RQ/Celery) + object storage; rate limit по IP; метрики.
