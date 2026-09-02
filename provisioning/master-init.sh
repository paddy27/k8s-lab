#!/bin/bash
# Bootstraps the control plane: kubeadm init, Calico CNI, then drops a
# join command + kubeconfig into the shared folder for the worker(s)
# and the Mac host to pick up. Idempotent - safe to re-run via
# `vagrant provision` without tearing anything down.

LOG="/shared_folder/logs/$(hostname)-init.log"
mkdir -p /shared_folder/logs
exec > >(tee -a "$LOG") 2>&1

set -eu

echo "====================================="
echo "master-init.sh on $(hostname) : $(date)"
echo "====================================="

if [ -f /etc/kubernetes/admin.conf ]; then
  echo "admin.conf already exists - control plane already initialized, skipping kubeadm init."
else
  kubeadm init \
    --apiserver-advertise-address="${NODE_IP}" \
    --pod-network-cidr=10.244.0.0/16 \
    --upload-certs

  mkdir -p /home/vagrant/.kube
  cp -i /etc/kubernetes/admin.conf /home/vagrant/.kube/config
  chown vagrant:vagrant /home/vagrant/.kube/config
fi

# Always refresh the copy in the shared folder - cheap, and covers the
# case where this VM's admin.conf predates a shared_folder wipe.
cp /etc/kubernetes/admin.conf /shared_folder/admin.conf

export KUBECONFIG=/etc/kubernetes/admin.conf

echo "--- CNI: Calico (pod CIDR 10.244.0.0/16 - doesn't collide with the 192.168.56.0/24 node network) ---"
if ! kubectl get daemonset -n kube-system calico-node >/dev/null 2>&1; then
  curl -fsSL -o /tmp/calico.yaml \
    https://raw.githubusercontent.com/projectcalico/calico/v3.28.2/manifests/calico.yaml
  sed -i 's/# - name: CALICO_IPV4POOL_CIDR/- name: CALICO_IPV4POOL_CIDR/' /tmp/calico.yaml
  sed -i 's/#   value: "192.168.0.0\/16"/  value: "10.244.0.0\/16"/' /tmp/calico.yaml
  kubectl apply -f /tmp/calico.yaml
else
  echo "Calico already installed, skipping."
fi

echo "--- join command for worker(s) ---"
kubeadm token create --print-join-command >/shared_folder/join-command.sh
chmod +x /shared_folder/join-command.sh

echo "--- metrics-server ---"
if ! kubectl get deployment -n kube-system metrics-server >/dev/null 2>&1; then
  kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
  # kubeadm's kubelet certs have no IP SANs, so metrics-server can't
  # verify them by default. Fine for an isolated local lab.
  kubectl -n kube-system patch deployment metrics-server --type='json' \
    -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
else
  echo "metrics-server already installed, skipping."
fi

echo "--- Kubernetes Dashboard ---"
if ! kubectl get deployment -n kubernetes-dashboard kubernetes-dashboard >/dev/null 2>&1; then
  kubectl apply -f https://raw.githubusercontent.com/kubernetes/dashboard/v2.7.0/aio/deploy/recommended.yaml
else
  echo "Dashboard already installed, skipping."
fi

echo "--- Dashboard RBAC + cluster-observability (node-exporter, kube-state-metrics) ---"
if [ -d /k8s-manifests ]; then
  kubectl apply -f /k8s-manifests/dashboard-admin-user.yaml
  kubectl apply -f /k8s-manifests/cluster-observability.yaml
else
  echo "WARNING: /k8s-manifests not mounted - skipping RBAC + cluster-observability apply." >&2
fi

echo "master-init.sh completed $(date)"
