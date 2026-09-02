#!/bin/bash
# Waits for the master's join command to show up in the shared folder,
# then joins. Idempotent - safe to re-run via `vagrant provision`.

LOG="/shared_folder/logs/$(hostname)-init.log"
mkdir -p /shared_folder/logs
exec > >(tee -a "$LOG") 2>&1

set -eu

echo "====================================="
echo "worker-join.sh on $(hostname) : $(date)"
echo "====================================="

if [ -f /etc/kubernetes/kubelet.conf ]; then
  echo "kubelet.conf already exists - already joined, skipping."
  exit 0
fi

echo "Waiting for /shared_folder/join-command.sh from the master..."
for _ in $(seq 1 60); do
  if [ -s /shared_folder/join-command.sh ]; then
    break
  fi
  sleep 5
done

if [ ! -s /shared_folder/join-command.sh ]; then
  echo "ERROR: no join command appeared after 5 minutes - is k8s-master up and initialized?" >&2
  exit 1
fi

bash /shared_folder/join-command.sh

echo "worker-join.sh completed $(date)"
