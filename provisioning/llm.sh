#!/bin/bash

LOG="/shared_folder/logs/$(hostname).log"

mkdir -p /shared_folder/logs

exec > >(tee -a "$LOG") 2>&1

set -eux

echo "====================================="
echo "Provisioning $(hostname)"
echo "Started : $(date)"
echo "====================================="

# Ollama's own installer is idempotent - safe to re-run on every
# `vagrant provision`. It already detects there's no GPU on this
# VirtualBox guest (no Metal/NVIDIA/AMD passthrough to a Linux guest)
# and falls back to CPU-only automatically - OLLAMA_LLM_LIBRARY below
# just makes that an explicit, pinned choice instead of an inferred one.
curl -fsSL https://ollama.com/install.sh | sh

# Listen on all interfaces, not just localhost - the rest of the lab
# (cluster-monitor, in particular) reaches this VM over
# 192.168.56.0/24, not loopback.
mkdir -p /etc/systemd/system/ollama.service.d
cat <<EOF >/etc/systemd/system/ollama.service.d/override.conf
[Service]
Environment="OLLAMA_HOST=0.0.0.0"
Environment="OLLAMA_LLM_LIBRARY=cpu"
EOF

systemctl daemon-reload
systemctl restart ollama
systemctl enable ollama

# Pull the model - idempotent, no-op if already present locally.
# Override OLLAMA_MODEL (see the Vagrantfile's `env:` block, same
# pattern as NODE_IP in common.sh) to pull something else without
# editing this script.
ollama pull "${OLLAMA_MODEL:-qwen3:4b}"

echo "Provisioning completed $(date)"
