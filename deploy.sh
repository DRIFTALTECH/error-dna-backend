#!/usr/bin/env bash
#
# Error DNA backend — one-shot A→Z deploy for AWS EC2 (Amazon Linux 2023).
# Installs deps, runs API + MCP under systemd, fronts them with Caddy (auto-HTTPS),
# smoke-tests everything, and prints the public URLs.
#
#   Usage on the box (repo already cloned here):
#     sudo DOMAIN=error-db-driftal.driftal.com bash deploy.sh          # full deploy
#     sudo bash deploy.sh test                                         # re-run smoke tests
#     sudo bash deploy.sh status                                       # service status + URLs
#
#   Prereqs you do ONCE in the AWS console (script can't):
#     1. Elastic IP → associate with the instance (stable IP for DNS).
#     2. Security group inbound: 22 (SSH, your IP), 80 + 443 (0.0.0.0/0).
#     3. DNS → A-record  $DOMAIN  →  <elastic IP>   (in driftal.com's DNS host).
#     4. DB reach: launch this EC2 in Aurora's VPC; Aurora SG inbound 5432 from THIS EC2's SG.
#     5. DB auth (IAM): either attach an instance role with rds-db:connect, OR put an
#        IAM-user's AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY in .env. Keep DB_PASSWORD empty.
#
set -euo pipefail

# ===== config (override via env) ============================================
DOMAIN="${DOMAIN:-error-db-driftal.driftal.com}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_USER="${SUDO_USER:-ec2-user}"
NODE_MAJOR="20"
API_PORT="$(grep -E '^PORT=' "$APP_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2 | tr -d ' "'"'"'' || true)"; API_PORT="${API_PORT:-3000}"
MCP_PORT="$(grep -E '^MCP_PORT=' "$APP_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2 | tr -d ' "'"'"'' || true)"; MCP_PORT="${MCP_PORT:-3333}"
# ============================================================================

log()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '  \033[1;32m✓ %s\033[0m\n' "$*"; }
warn() { printf '  \033[1;33m⚠ %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

need_root() { [ "$(id -u)" = 0 ] || die "run with sudo"; }
arch_tag()  { case "$(uname -m)" in x86_64) echo amd64;; aarch64) echo arm64;; *) echo amd64;; esac; }

# EC2 IMDSv2 helpers (AL2023 defaults to v2 — needs a token).
imds() {
  local tok
  tok="$(curl -fsS --max-time 3 -X PUT http://169.254.169.254/latest/api/token \
        -H 'X-aws-ec2-metadata-token-ttl-seconds: 120' 2>/dev/null || true)"
  [ -n "$tok" ] && curl -fsS --max-time 3 -H "X-aws-ec2-metadata-token: $tok" \
        "http://169.254.169.254/latest/meta-data/$1" 2>/dev/null || true
}
public_ip()   { imds public-ipv4 | grep -E '^[0-9]' || echo "<elastic-ip>"; }
has_role()    { [ -n "$(imds iam/security-credentials/)" ]; }

# ---- preflight -------------------------------------------------------------
preflight() {
  need_root
  command -v dnf >/dev/null || die "not a dnf/Amazon Linux box — this script targets Amazon Linux 2023"
  [ -f "$APP_DIR/.env" ]     || die ".env missing in $APP_DIR — create it (DB_*, LLM_API_KEY) before deploy"
  [ -f "$APP_DIR/main.py" ]  || die "main.py not found — run this from the repo root"
  for k in DB_HOST LLM_API_KEY; do
    grep -qE "^$k=." "$APP_DIR/.env" || warn ".env has no non-empty $k — app may fail to start"
  done

  # DB auth mode. DB_PASSWORD set → static password. Empty → IAM: boto3 mints an
  # RDS token, needing EITHER an attached instance role OR AWS keys in .env.
  if grep -qE '^DB_PASSWORD=.' "$APP_DIR/.env"; then
    ok "DB auth: static password"
  elif grep -qE '^AWS_ACCESS_KEY_ID=.' "$APP_DIR/.env" && grep -qE '^AWS_SECRET_ACCESS_KEY=.' "$APP_DIR/.env"; then
    grep -qE '^AWS_REGION=.' "$APP_DIR/.env" || die "IAM mode: .env has AWS keys but no AWS_REGION (must match Aurora's region)"
    ok "DB auth: IAM via AWS keys in .env"
  elif has_role; then
    ok "DB auth: IAM via attached EC2 instance role ($(imds iam/security-credentials/))"
  else
    die "IAM DB auth (DB_PASSWORD empty) but no AWS_ACCESS_KEY_ID/SECRET in .env and no instance role attached. Attach a role with rds-db:connect, or add the IAM-user keys to .env."
  fi

  chown "$APP_USER":"$APP_USER" "$APP_DIR/.env" 2>/dev/null || true
  chmod 600 "$APP_DIR/.env"
  ok "preflight passed (domain=$DOMAIN, api=$API_PORT, mcp=$MCP_PORT, user=$APP_USER, arch=$(arch_tag))"
}

# ---- system deps -----------------------------------------------------------
install_system() {
  log "dnf: system packages"
  dnf install -y python3 python3-pip python3-devel gcc gcc-c++ make curl git tar >/dev/null
  ok "base packages installed ($(python3 --version))"

  if ! command -v caddy >/dev/null; then
    log "install Caddy (static binary + systemd — no dnf repo on AL2023)"
    curl -fsSL "https://caddyserver.com/api/download?os=linux&arch=$(arch_tag)" -o /usr/bin/caddy
    chmod +x /usr/bin/caddy
    getent group caddy >/dev/null || groupadd --system caddy
    id caddy >/dev/null 2>&1 || useradd --system --gid caddy --home-dir /var/lib/caddy \
        --create-home --shell /sbin/nologin caddy
    mkdir -p /etc/caddy
    cat >/etc/systemd/system/caddy.service <<'EOF'
[Unit]
Description=Caddy
After=network-online.target
Wants=network-online.target

[Service]
User=caddy
Group=caddy
ExecStart=/usr/bin/caddy run --config /etc/caddy/Caddyfile
ExecReload=/usr/bin/caddy reload --config /etc/caddy/Caddyfile --force
TimeoutStopSec=5s
LimitNOFILE=1048576
AmbientCapabilities=CAP_NET_BIND_SERVICE
ProtectSystem=full

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
  fi
  ok "caddy $(caddy version | head -1)"
}

# ---- node + openclaw (scraper/login-test; best-effort) ---------------------
install_openclaw() {
  log "Node $NODE_MAJOR + openclaw (scraper / login-test)"
  if ! command -v node >/dev/null || [ "$(node -v | grep -oE '[0-9]+' | head -1)" -lt "$NODE_MAJOR" ]; then
    curl -fsSL "https://rpm.nodesource.com/setup_${NODE_MAJOR}.x" | bash - >/dev/null 2>&1
    dnf install -y nodejs >/dev/null
  fi
  npm install -g openclaw >/dev/null 2>&1 && ok "openclaw $(openclaw --version 2>/dev/null | head -1)" \
    || warn "openclaw install failed — read APIs + MCP still work; scraper/login-test won't"

  # Google Chrome for openclaw's headless browser (x86_64 only — no arm64 Chrome).
  if ! command -v google-chrome >/dev/null; then
    if [ "$(arch_tag)" = "amd64" ]; then
      dnf install -y https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm >/dev/null 2>&1 \
        || warn "chrome install failed — scraper/login-test degrade"
    else
      warn "arm64 box: Google Chrome has no arm64 build — scraper/login-test won't run (use an x86_64 instance for those)"
    fi
  fi
  sudo -u "$APP_USER" openclaw browser doctor >/dev/null 2>&1 \
    && ok "openclaw browser ready" \
    || warn "openclaw browser not ready on headless box — login-test/scraper degrade, core stays up"
}

# ---- python venv -----------------------------------------------------------
install_python() {
  log "python venv + requirements"
  sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
  sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip -q
  sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
  ok "dependencies installed"

  sudo -u "$APP_USER" bash -c "cd '$APP_DIR' && .venv/bin/python -m services.scraper" >/dev/null \
    && ok "scraper classify() self-check passed" || warn "classify self-check failed"
  sudo -u "$APP_USER" bash -c "cd '$APP_DIR' && .venv/bin/python mcp_server.py selftest" 2>/dev/null \
    && ok "MCP selftest passed (DB reachable + IAM/creds valid)" \
    || warn "MCP selftest failed — check DB_* + AWS creds/role in .env, and Aurora SG allows this EC2's SG on 5432"
}

# ---- systemd services ------------------------------------------------------
write_services() {
  log "systemd units"
  local pathenv="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  for svc in api mcp; do
    local desc exec
    if [ "$svc" = api ]; then desc="Error DNA API (FastAPI :$API_PORT)"; exec="main.py"
    else desc="Error DNA MCP (streamable-http :$MCP_PORT/mcp)"; exec="mcp_server.py"; fi
    cat >/etc/systemd/system/error-dna-$svc.service <<EOF
[Unit]
Description=$desc
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=PATH=$pathenv
ExecStart=$APP_DIR/.venv/bin/python $exec
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  done
  systemctl daemon-reload
  systemctl enable --now error-dna-api.service error-dna-mcp.service
  ok "api + mcp services enabled and started"
}

# ---- caddy reverse proxy ---------------------------------------------------
write_caddy() {
  log "Caddyfile (TLS + routing)"
  cat >/etc/caddy/Caddyfile <<EOF
$DOMAIN {
	encode gzip

	# MCP streamable-http — disable buffering so SSE/streaming flows.
	@mcp path /mcp /mcp/*
	handle @mcp {
		reverse_proxy 127.0.0.1:$MCP_PORT {
			flush_interval -1
		}
	}

	# Everything else → FastAPI (serves /api/*).
	handle {
		reverse_proxy 127.0.0.1:$API_PORT
	}
}
EOF
  chown -R caddy:caddy /etc/caddy
  caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1 || die "Caddyfile invalid"
  systemctl enable --now caddy
  systemctl reload caddy 2>/dev/null || systemctl restart caddy
  ok "caddy serving $DOMAIN (auto-HTTPS via Let's Encrypt)"
}

# ---- smoke tests -----------------------------------------------------------
smoke() {
  log "smoke tests"
  sleep 2
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$API_PORT/api/health" || echo 000)
  [ "$code" = "200" ] && ok "API /api/health → 200 (local)" || warn "API health local → $code (journalctl -u error-dna-api)"
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$MCP_PORT/mcp" || echo 000)
  [ "$code" != "000" ] && ok "MCP :$MCP_PORT/mcp responding (HTTP $code)" || warn "MCP not responding (journalctl -u error-dna-mcp)"
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://$DOMAIN/api/health" || echo 000)
  if [ "$code" = "200" ]; then ok "https://$DOMAIN/api/health → 200 (public + TLS live)"
  else warn "public https → $code — set DNS A-record ($DOMAIN → $(public_ip)) + open 80/443 in the SG, then: sudo bash deploy.sh test"; fi
}

print_urls() {
  local ip; ip="$(public_ip)"
  cat <<EOF

────────────────────────────────────────────────────────────
  Error DNA — deployed (EC2 / Amazon Linux 2023)
────────────────────────────────────────────────────────────
  Public IP        : $ip
  DNS A-record     : $DOMAIN  →  $ip   (add this in driftal.com DNS)

  API base         : https://$DOMAIN/api
  Health           : https://$DOMAIN/api/health
  MCP endpoint     : https://$DOMAIN/mcp   (streamable-http)

  Local (on box)   : http://127.0.0.1:$API_PORT/api   |   http://127.0.0.1:$MCP_PORT/mcp

  Logs             : journalctl -u error-dna-api -f
                     journalctl -u error-dna-mcp -f
                     journalctl -u caddy -f
  Restart          : sudo systemctl restart error-dna-api error-dna-mcp
────────────────────────────────────────────────────────────
EOF
}

status() { systemctl --no-pager status error-dna-api error-dna-mcp caddy || true; print_urls; }

# ---- main ------------------------------------------------------------------
case "${1:-deploy}" in
  deploy)
    preflight
    install_system
    install_openclaw
    install_python
    write_services
    write_caddy
    smoke
    print_urls
    ;;
  test)   need_root; smoke ;;
  status) need_root; status ;;
  *) die "unknown command '$1' (deploy | test | status)" ;;
esac
