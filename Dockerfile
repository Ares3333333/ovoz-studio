# Ovoz AI Studio — продакшн-образ.
# multi-stage: wheels собираются отдельно, итог — slim runtime + ffmpeg.
FROM python:3.13-slim AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY requirements.txt .
# wheel-таргет — быстрее старт и меньше слоёв; --require-hashes включи,
# когда сгенерируешь `pip-compile --generate-hashes`
RUN pip wheel --no-deps -w /wheels -r requirements.txt \
 && pip wheel -w /wheels uvicorn[standard] fastapi python-multipart httpx edge-tts
# edge-tts в перечислении ВТОРОГО прохода обязателен: первый — --no-deps (пинты),
# а зависимости edge-tts (aiohttp/tabulate) иначе не попадают в /wheels и офлайн-
# установка падает. Ломающийся без этого Docker-сборкой деплой пойман cgroup-тестом R37.

FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OVOZ_DATA_DIR=/data

# ffmpeg/ffprobe: точная оценка длительности + PCM WAV из edge-tts
# curl НЕ ставим — healthcheck через python urllib (меньше поверхность)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
 && rm -rf /wheels

# опциональный доп-контур (R37): OVOZ_EXTRA=asr ставит faster-whisper ПОСЛЕ
# офлайн-ядра, уже по сети — ядро и CI от тяжёлых wheels свободны, а
деплоер на 24GB-машине получает реальный офлайн-ASR в самом образе
ARG OVOZ_EXTRA=""
RUN if [ -n "$OVOZ_EXTRA" ]; then pip install --no-cache-dir -r requirements-${OVOZ_EXTRA}.txt; fi

# код принадлежит root и только для чтения, пишет только в /data
COPY --chown=root:root app ./app
COPY --chown=root:root static ./static
COPY --chown=root:root demo ./demo

RUN groupadd --system --gid 1001 ovoz \
 && useradd  --system --uid 1001 --gid 1001 --no-create-home --home-dir /data ovoz \
 && mkdir -p /data && chown -R ovoz:ovoz /data /tmp

USER 1001:1001
EXPOSE 8080

# healthcheck без curl: python и так в образе
HEALTHCHECK --interval=30s --timeout=5s --start-period=8s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status==200 else 1)"]

# uvicorn без --reload, 1 воркер: SQLite WAL + фоновые пулы потоков живут в процессе
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
