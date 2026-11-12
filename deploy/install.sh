#!/usr/bin/env bash
# Set up (or update) an open-slide editor + publisher on Ubuntu/Debian.
# Run as root inside a dedicated container or VM, from a checkout of this repo:
#
#   EDIT_HOST=slides-edit.example.com PUBLIC_HOST=slides.example.com ./deploy/install.sh
#
# Safe to re-run: existing workspaces, configs and tokens are kept.
set -euo pipefail

: "${EDIT_HOST:?set EDIT_HOST, e.g. slides-edit.example.com}"
: "${PUBLIC_HOST:?set PUBLIC_HOST, e.g. slides.example.com}"
SLIDE_USER="${SLIDE_USER:-slide}"
HOME_DIR="/home/${SLIDE_USER}"
WORKSPACE="${HOME_DIR}/deck"
DATA_DIR="${HOME_DIR}/publish"
APP_DIR="/opt/stagedoor"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '\n==> %s\n' "$*"; }
as_user() { su - "${SLIDE_USER}" -c "$1"; }

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive

log "Packages"
apt-get update -qq
apt-get install -y -qq curl ca-certificates gnupg git python3 caddy >/dev/null
if ! node -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit(a>22||(a===22&&b>=12)||(a===20&&b>=19)?0:1)' 2>/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null
  apt-get install -y -qq nodejs >/dev/null
fi
if ! command -v cloudflared >/dev/null; then
  install -d -m 0755 /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
    > /etc/apt/sources.list.d/cloudflared.list
  apt-get update -qq && apt-get install -y -qq cloudflared >/dev/null
fi
echo "node $(node -v), $(caddy version | cut -d' ' -f1) caddy, $(cloudflared --version | cut -d' ' -f3) cloudflared"

log "User and workspace"
id "${SLIDE_USER}" >/dev/null 2>&1 || useradd -m -s /bin/bash "${SLIDE_USER}"
chmod o+x "${HOME_DIR}"   # caddy only needs to traverse into the publish dir
if [[ ! -d "${WORKSPACE}/slides" ]]; then
  as_user "cd ~ && CI=1 npx -y @open-slide/cli@latest init deck </dev/null"
  cat > "${WORKSPACE}/open-slide.config.ts" <<EOF
import type { OpenSlideConfig } from "@open-slide/core";

const openSlideConfig: OpenSlideConfig = {
  port: 5173,
  allowedHosts: ["${EDIT_HOST}"],
  // Only affects the public build; the editor always shows the full UI.
  build: {
    showSlideUi: false,
    showSlideBrowser: false,
  },
};

export default openSlideConfig;
EOF
  chown "${SLIDE_USER}:" "${WORKSPACE}/open-slide.config.ts"
elif ! grep -q "${EDIT_HOST}" "${WORKSPACE}/open-slide.config.ts" 2>/dev/null; then
  echo "warning: ${WORKSPACE}/open-slide.config.ts does not list ${EDIT_HOST} in allowedHosts;"
  echo "         the editor will reject requests through the tunnel until it does."
fi
if [[ ! -d "${WORKSPACE}/.git" ]]; then
  as_user "cd deck && git init -q && git add -A && git -c user.name=${SLIDE_USER} -c user.email=${SLIDE_USER}@localhost commit -qm 'initial workspace'"
fi
as_user "cd deck && git config user.name >/dev/null || { git config user.name ${SLIDE_USER}; git config user.email ${SLIDE_USER}@localhost; }"

log "Publisher"
install -d -m 0755 "${APP_DIR}"
rm -rf "${APP_DIR}/publisher"
cp -r "${REPO_DIR}/publisher" "${APP_DIR}/publisher"
find "${APP_DIR}" -name __pycache__ -prune -exec rm -rf {} +
install -d -m 0755 /etc/stagedoor
cat > /etc/stagedoor/publisher.env <<EOF
STAGEDOOR_WORKSPACE=${WORKSPACE}
STAGEDOOR_DATA_DIR=${DATA_DIR}
STAGEDOOR_LISTEN=127.0.0.1:8090
STAGEDOOR_BASE_PATH=/publish
STAGEDOOR_PUBLIC_URL=https://${PUBLIC_HOST}/s/{id}
EOF
install -d -o "${SLIDE_USER}" -g "${SLIDE_USER}" "${DATA_DIR}"
if [[ ! -e "${DATA_DIR}/current" ]]; then
  echo "no public release yet; building an empty site"
  as_user "set -a; . /etc/stagedoor/publisher.env; cd ${APP_DIR} && python3 -m publisher rebuild"
fi

log "Services"
sed "s#/home/slide#${HOME_DIR}#g; s#^User=slide#User=${SLIDE_USER}#" \
  "${REPO_DIR}/deploy/systemd/openslide-dev.service" > /etc/systemd/system/openslide-dev.service
sed "s#^User=slide#User=${SLIDE_USER}#" \
  "${REPO_DIR}/deploy/systemd/stagedoor.service" > /etc/systemd/system/stagedoor.service
install -m 0644 "${REPO_DIR}/deploy/systemd/cloudflared.service" /etc/systemd/system/cloudflared.service
if [[ -f /etc/caddy/Caddyfile ]] && ! cmp -s "${REPO_DIR}/deploy/caddy/Caddyfile" /etc/caddy/Caddyfile; then
  cp /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.$(date +%Y%m%d%H%M%S).bak"
fi
sed "s#/home/slide#${HOME_DIR}#g" "${REPO_DIR}/deploy/caddy/Caddyfile" > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 \
  || { caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile; exit 1; }

systemctl daemon-reload
systemctl enable -q openslide-dev stagedoor caddy
systemctl restart openslide-dev stagedoor caddy
if [[ -f /etc/cloudflared/tunnel.env ]]; then
  chmod 600 /etc/cloudflared/tunnel.env
  systemctl enable -q cloudflared && systemctl restart cloudflared
  tunnel_note="cloudflared is running."
else
  tunnel_note="Put TUNNEL_TOKEN=... in /etc/cloudflared/tunnel.env (mode 600), then: systemctl enable --now cloudflared"
fi

log "Checks"
sleep 3
for svc in openslide-dev stagedoor caddy; do printf '%-22s %s\n' "$svc" "$(systemctl is-active "$svc")"; done
curl -fsS -o /dev/null -w "editor    %{http_code}\n" -H "Host: ${EDIT_HOST}" http://127.0.0.1:5173/ || true
curl -fsS -o /dev/null -w "publisher %{http_code}\n" http://127.0.0.1:8090/publish/healthz || true
curl -fsS -o /dev/null -w "public    %{http_code}\n" http://127.0.0.1:8080/ || true

cat <<EOF

Done. ${tunnel_note}

Tunnel ingress rules, in this order:
EOF
printf '  %-40s %-20s -> %s\n' \
  "${EDIT_HOST}" "path ^/publish(/|\$)" "http://127.0.0.1:8090" \
  "${EDIT_HOST}" "" "http://127.0.0.1:5173" \
  "${PUBLIC_HOST}" "" "http://127.0.0.1:8080"
echo "Protect ${EDIT_HOST} with an access policy (e.g. Cloudflare Access)."
