#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OLLAMA_ARCHIVE="${1:-$SCRIPT_DIR/ollama-linux-amd64.tar.zst}"
MODELS_ARCHIVE="${2:-$SCRIPT_DIR/ollama-models.tar}"
MODEL_ROOT="/data/ollama"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo bash $0"
  exit 1
fi

if [[ ! -f "$OLLAMA_ARCHIVE" ]]; then
  echo "Missing file: $OLLAMA_ARCHIVE"
  exit 1
fi

if [[ ! -f "$MODELS_ARCHIVE" ]]; then
  echo "Missing file: $MODELS_ARCHIVE"
  exit 1
fi

install_zstd() {
  if command -v zstd >/dev/null 2>&1; then
    return 0
  fi

  echo "zstd is not installed. Trying the system package manager..."
  if command -v dnf >/dev/null 2>&1; then
    dnf install -y zstd && return 0
  elif command -v yum >/dev/null 2>&1; then
    yum install -y zstd && return 0
  elif command -v apt-get >/dev/null 2>&1; then
    apt-get install -y zstd && return 0
  elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install zstd && return 0
  elif command -v apk >/dev/null 2>&1; then
    apk add zstd && return 0
  fi

  echo "Cannot install zstd automatically."
  echo "Install zstd from the internal offline package repository, then run this script again."
  echo "RPM systems: sudo rpm -Uvh zstd*.rpm"
  echo "Debian systems: sudo dpkg -i zstd*.deb"
  exit 1
}

install_zstd

echo "[1/6] Installing Ollama"
tar --zstd -xf "$OLLAMA_ARCHIVE" -C /usr
if [[ ! -x /usr/bin/ollama ]]; then
  echo "Invalid Ollama archive: /usr/bin/ollama was not installed"
  exit 1
fi

echo "[2/6] Creating service account"
getent group ollama >/dev/null || groupadd --system ollama
id ollama >/dev/null 2>&1 || useradd \
  --system --gid ollama --home-dir /usr/share/ollama \
  --create-home --shell /usr/sbin/nologin ollama

echo "[3/6] Importing models"
mkdir -p "$MODEL_ROOT/models"
systemctl stop ollama 2>/dev/null || true
tar -xf "$MODELS_ARCHIVE" -C "$MODEL_ROOT"
if [[ ! -d "$MODEL_ROOT/models/blobs" || ! -d "$MODEL_ROOT/models/manifests" ]]; then
  echo "Invalid model archive: models/blobs or models/manifests is missing"
  exit 1
fi
chown -R ollama:ollama "$MODEL_ROOT"
chmod -R u=rwX,g=rX,o= "$MODEL_ROOT"

echo "[4/6] Writing systemd service"
cat >/etc/systemd/system/ollama.service <<'EOF'
[Unit]
Description=Ollama Service
After=network-online.target

[Service]
ExecStart=/usr/bin/ollama serve
User=ollama
Group=ollama
Environment="HOME=/usr/share/ollama"
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_MODELS=/data/ollama/models"
Environment="OLLAMA_CONTEXT_LENGTH=4096"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_MAX_LOADED_MODELS=2"
Environment="OLLAMA_MAX_QUEUE=8"
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

echo "[5/6] Starting Ollama"
systemctl daemon-reload
systemctl enable --now ollama

for _ in $(seq 1 30); do
  if ollama list >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! ollama list >/dev/null 2>&1; then
  echo "Ollama failed to start. Check: journalctl -u ollama -n 100 --no-pager"
  exit 1
fi

echo "[6/6] Installation complete"
ollama --version
ollama list
echo "API: http://127.0.0.1:11434"
