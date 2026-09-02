#!/bin/bash

LOG="/shared_folder/logs/buildserver.log"

mkdir -p /shared_folder/logs

exec > >(tee -a "$LOG") 2>&1

set -eux

apt-get update

apt-get install -y \
docker.io \
docker-compose-v2 \
ansible \
openjdk-21-jdk

systemctl enable docker
systemctl start docker

usermod -aG docker vagrant

# Helm
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

mkdir -p /opt/registry

# Local insecure registry so images built here can be pushed once and
# pulled by any cluster node - master/worker1's containerd already
# trusts 192.168.56.20:5000 without TLS (see provisioning/common.sh).
# Push with: docker push 192.168.56.20:5000/<name>:<tag>
if ! docker ps --format '{{.Names}}' | grep -qx registry; then
  docker run -d \
    --name registry \
    --restart=always \
    -p 5000:5000 \
    -v /opt/registry/data:/var/lib/registry \
    registry:2
fi

# Generic build-and-push helper: builds any synced project's image and
# pushes it to the registry above. Pushing to "localhost:5000" (not
# 192.168.56.20:5000) needs no insecure-registry config on this end -
# Docker trusts localhost registries by default. The cluster nodes pull
# the same image via 192.168.56.20:5000, which their containerd is
# already configured to trust (see provisioning/common.sh).
cat <<'EOF' >/opt/build-and-push.sh
#!/bin/bash
set -eu
IMAGE="${1:?usage: build-and-push.sh <image-name> <tag> <build-dir>}"
TAG="${2:?usage: build-and-push.sh <image-name> <tag> <build-dir>}"
BUILD_DIR="${3:?usage: build-and-push.sh <image-name> <tag> <build-dir>}"
cd "$BUILD_DIR"
docker build -t "localhost:5000/${IMAGE}:${TAG}" .
docker push "localhost:5000/${IMAGE}:${TAG}"
echo "Pushed 192.168.56.20:5000/${IMAGE}:${TAG}"
EOF
chmod +x /opt/build-and-push.sh

echo "Build server configured successfully."
echo "To build+push an app image: vagrant ssh buildserver -c 'sudo /opt/build-and-push.sh <image-name> <tag> <build-dir>'"
