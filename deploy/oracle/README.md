# Деплой на Oracle Always Free — маршрут на 10 минут

Это «своё облако без API»: полноценная ARM-ВМ (до 4 ядер / 24 GB RAM),
бесплатно навсегда (Always Free), Docker, свой домен, WebSocket, реальный
офлайн-ASR. Карта при регистрации просят, список Always Free не тарифицируется.

## Шаг 1 — аккаунт (только твои руки, 3 минуты)

1. register.cloud.oracle.com → создать аккаунт (email, карта для верификации,
   без списаний с Always Free).
2. **Домашний регион выбирай сразу обдуманно — он НЕ меняется потом.**
   Для Ташкента лучший ping: **Mumbai (ap-south-1)**; если при создании машины
   будет «out of capacity» для ARM — вторая по близости **Frankfurt
   (eu-de-frankfurt-1)**.

## Шаг 2 — машина (2 минуты)

Compute → Instances → Create instance:
- Image: **Ubuntu 24.04** (минимальный «Ubuntu with Ubuntu Desktop GNOME» НЕ бери,
  нужен чистый 24.04; форма сама предложит ARM-совместимые образы)
- Shape: **A1.Flex** → 4 OCPU / 24 GB (всегда бесплатно)
- Network: «Assign public IPv4» — включить
- SSH keys: вставить **публичный ключ** (агент выдаёт одной строкой)
- Create. Через ~40 секунд у машины есть Public IP.

## Шаг 3 — домен (1 минута)

duckdns.org → войти через GitHub/Google → «Add Domain», например
`ovozai.duckdns.org` → скопировать **токен домена** со страницы.
(Апгрейд «красивого» домена потом: eu.org — бесплатно, модерация пару дней;
ovoz.uz / .app — покупные, ~$10–15/год; caddy переподпишет TLS за минуту.)

## Шаг 4 — отдать всё агенту (1 сообщение)

Скажи агенту три вещи: **публичный IP**, **токен duckdns** и **домен**.
Дальше всё делает он, ты только читаешь вывод:

```
ssh -i ~/.ssh/ovoz_deploy ubuntu@<IP>
echo '<duckdns-токен>' > ~/.ducktoken
curl -fsSL https://raw.githubusercontent.com/Ares3333333/ovoz-studio/main/deploy/oracle/setup.sh -o setup.sh
bash setup.sh <домен.duckdns.org> <токен_бота>
```

Проверка после: `https://<домен>/healthz` → `{"status":"ok"}` и сайт открывается.

## Шаг 5 — Telegram Mini App (2 клика, один раз)

Бот: **@Ozvozbot** (имя/описание/меню команд уже проставлены через API).
1. В BotFather: **/newapp** → выбрать @Ozvozbot → короткое имя, например `ovoz`
   → в поле **HTTPS URL** вставить `https://<домен.duckdns.org>/#studio`
2. Всё. Дальше у бота появляется ссылка-приложение, а агент добавит
   «Menu Button», открывающий мини-ап одной кнопкой (это уже чистый API).

## Что после деплоя проверяет агент (не на глаз, а тестом)

- `python scripts/verify_2026.py https://<домен>` — 105 живых проверок против публичного сервера
- реальный dubbing-прогон через HTTP (ASR=small на ARM: модель ~460 MB скачается
  один раз на первый cold-start, дальше офлайн)
- браузер-QA сайта + smoke-скрин, затем заход через @Ozvozbot в Telegram

## Известные границы этой конфигурации

- Платежи: sim (ждём ключи Payme/Click — это отдельный шаг, не этот)
- Перевод: sim-словарь (боевой GPT/DeepL — по ключу вендора)
- Oracle ARM-шэпа в некоторых регионах иногда «out of capacity» — тогда Frankfurt
- Always Free сеть: 10 TBay/мес трафика — для тестового домена с запасом
