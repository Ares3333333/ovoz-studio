#!/usr/bin/env bash
# Ovoz AI Studio — bootstrap для Oracle Always Free (Ubuntu 24.04 aarch64).
# Делает всё: docker + caddy(автоматический HTTPS) + секреты на машине + build/up.
# Запуск с VM:  bash setup.sh <домен.duckdns.org> <токен-бота>
set -euo pipefail

DOMAIN="${1:?использование: bash setup.sh <domain.duckdns.org> <telegram-bot-token>}"
BOT_TOKEN="${2:?нужен TELEGRAM_BOT_TOKEN от @BotFather}"

export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -y
# docker-compose-v2 — плагин `docker compose`; Ubuntu docker.io его НЕ включает
sudo apt-get install -y git curl docker.io docker-compose-v2 caddy
sudo usermod -aG docker "$USER"

# Классическая ловушка Oracle: образ Ubuntu прикладывает iptables, блокирующие 80/443.
sudo iptables -I INPUT 1 -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 1 -p tcp --dport 443 -j ACCEPT
if ! command -v netfilter-persistent >/dev/null; then
  sudo apt-get install -y iptables-persistent
fi
sudo netfilter-persistent save

# репозиторий
if [ ! -d "$HOME/ovoz-studio/.git" ]; then
  git clone https://github.com/Ares3333333/ovoz-studio.git
fi
cd "$HOME/ovoz-studio"
git pull --ff-only

# секреты генерятся НА МАШИНЕ и в git никогда не попадают
if [ ! -f .env ]; then
  umask 077
  cat > .env <<EOF
OVOZ_SECRET=$(openssl rand -hex 32)
OVOZ_ADMIN_SECRET=$(openssl rand -hex 32)
OVOZ_WEBHOOK_SECRET=$(openssl rand -hex 32)
TELEGRAM_BOT_TOKEN=$BOT_TOKEN
OVOZ_ASR_MODEL=small
OVOZ_TTS_PROVIDER=edge
OVOZ_TRANSLATE_PROVIDER=sim
EOF
  echo ".env создан (секреты сгенерированы локально)"
fi

# duckdns: домен должен показывать на IP этой машины. Токен от duckdns.org —
# в ~/.ducktoken (одной строкой); без него пропускаем (ручной разовый апдейт тоже ок).
if [ -f "$HOME/.ducktoken" ]; then
  SUB=$(cut -d. -f1 <<<"$DOMAIN")
  TOK=$(tr -d '[:space:]' < "$HOME/.ducktoken")
  curl -fsS "https://www.duckdns.org/update?domain=$SUB&token=$TOK" >/dev/null && echo "duckdns обновлён"
  ( crontab -l 2>/dev/null | grep -v duckdns; \
    echo "*/10 * * * * curl -fsS 'https://www.duckdns.org/update?domain=$SUB&token=$TOK' >/dev/null" ) | crontab -
fi

# caddy: автоматический Let's Encrypt поверх 8080
sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
$DOMAIN {
	reverse_proxy 127.0.0.1:8080
}
EOF
sudo systemctl enable --now caddy docker
sudo systemctl reload caddy || sudo systemctl restart caddy

# сборка с реальным офлайн-ASR в образе (OVOZ_EXTRA=asr из prod-оверрайда)
docker compose -f docker-compose.yml -f deploy/oracle/docker-compose.prod.yml up -d --build

sleep 10
curl -fsS http://127.0.0.1:8080/healthz >/dev/null && echo "локально: OK"
echo "публично: https://$DOMAIN/healthz   (после DNS-прописки; первый ASR job скачает модель ~460MB)"
