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
#
# qwen2.5:3b-instruct, not qwen3:4b - found the hard way, manually,
# before this script was wired up: qwen3:4b's hybrid thinking mode is
# not controllable via the documented `"think": false` API parameter on
# this Ollama version (every response embedded the full reasoning trace
# regardless), and that reasoning is non-deterministic enough in length
# to be unusable for an interactive agent - 3 identical one-line prompts
# produced 330/414/1778 reasoning tokens and 14-124s response times.
# qwen2.5 predates the hybrid-thinking feature entirely (nothing to
# fail to disable) and answered the same prompt in a consistent ~10
# tokens every time.
ollama pull "${OLLAMA_MODEL:-qwen2.5:3b-instruct}"

echo "Provisioning completed $(date)"
