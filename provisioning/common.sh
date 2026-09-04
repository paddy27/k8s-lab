#!/bin/bash

LOG="/shared_folder/logs/$(hostname).log"

mkdir -p /shared_folder/logs

exec > >(tee -a "$LOG") 2>&1

set -eux

echo "====================================="
echo "Provisioning $(hostname)"
echo "Started : $(date)"
echo "====================================="

# Disable swap - kubelet refuses to start with swap on. swapoff+fstab
# alone isn't enough on this box: the bento/ubuntu-22.04 image sets up
# /swap.img via its own systemd unit (swap.img.swap), independent of
# fstab, which re-activates it on every cold boot regardless of the
# fstab edit below - confirmed the hard way after a VM crash/cold-reboot
# left kubelet crash-looping with "running with swap on is not
# supported". Masking the unit is what actually makes it stick.
swapoff -a
sed -i '/ swap / s/^/#/' /etc/fstab
if systemctl list-unit-files 'swap.img.swap' --no-legend | grep -q swap.img.swap; then
  systemctl mask swap.img.swap
fi

# Kernel modules
cat <<EOF >/etc/modules-load.d/k8s.conf
overlay
br_netfilter
EOF

modprobe overlay
modprobe br_netfilter

# Sysctl
cat <<EOF >/etc/sysctl.d/k8s.conf
net.bridge.bridge-nf-call-iptables=1
net.bridge.bridge-nf-call-ip6tables=1
net.ipv4.ip_forward=1
EOF

sysctl --system

apt-get update

apt-get install -y \
curl \
wget \
vim \
git \
unzip \
zip \
make \
apt-transport-https \
ca-certificates \
gnupg \
lsb-release \
containerd

mkdir -p /etc/containerd

containerd config default >/etc/containerd/config.toml

sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' \
/etc/containerd/config.toml

# Trust the local registry on buildserver (192.168.56.20:5000) without
# TLS - it's an internal-only lab registry, never exposed beyond the
# 192.168.56.0/24 host-only network.
sed -i 's#config_path = ""#config_path = "/etc/containerd/certs.d"#' \
/etc/containerd/config.toml

mkdir -p /etc/containerd/certs.d/192.168.56.20:5000
cat <<EOF >/etc/containerd/certs.d/192.168.56.20:5000/hosts.toml
server = "http://192.168.56.20:5000"

[host."http://192.168.56.20:5000"]
  capabilities = ["pull", "resolve", "push"]
  skip_verify = true
EOF

systemctl restart containerd
systemctl enable containerd

mkdir -p /etc/apt/keyrings

curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.30/deb/Release.key \
| gpg --batch --yes --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg

echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] \
https://pkgs.k8s.io/core:/stable:/v1.30/deb/ /" \
> /etc/apt/sources.list.d/kubernetes.list

apt-get update

apt-get install -y kubelet kubeadm kubectl

apt-mark hold kubelet kubeadm kubectl

# kubelet defaults to advertising the first-detected interface's IP,
# which on these VMs is always the NAT adapter (10.0.2.15 - IDENTICAL
# on every node, since each VM has its own isolated NAT). Force it to
# advertise this node's real private-network IP instead. Durable
# (survives kubeadm init/join/reset, unlike the auto-generated
# /var/lib/kubelet/kubeadm-flags.env).
if [ -n "${NODE_IP:-}" ]; then
  echo "KUBELET_EXTRA_ARGS=--node-ip=${NODE_IP}" >/etc/default/kubelet
fi

systemctl enable kubelet
systemctl restart kubelet

echo "Provisioning completed $(date)"
