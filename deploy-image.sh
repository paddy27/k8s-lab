#!/bin/bash
# Build an app's image on buildserver, push it to its registry, then
# pre-pull it onto the cluster nodes. Generic across every app in this
# lab (observability-platform, cluster-stats, ...).
#
# Why the explicit pre-pull step: kubelet/crictl's own registry pull
# hits a containerd 2.2.1 bug on this box - it doesn't honor
# config.toml's registry config_path (confirmed: `ctr images pull`
# without --hosts-dir always assumes HTTPS and fails, even though the
# certs.d files are correct and `--hosts-dir` pulls work fine). Rather
# than fight that, this pre-pulls the image directly into containerd's
# content store on each node; kubelet's `imagePullPolicy: IfNotPresent`
# then just uses what's already there instead of pulling itself.
#
# Usage: ./deploy-image.sh <image-name> <tag> <build-dir>
#   e.g. ./deploy-image.sh observability-backend v3 observability-platform/backend
#        ./deploy-image.sh cluster-stats v1 cluster-stats
set -eu
cd "$(dirname "$0")"

IMAGE_NAME="${1:?usage: deploy-image.sh <image-name> <tag> <build-dir>}"
TAG="${2:?usage: deploy-image.sh <image-name> <tag> <build-dir>}"
BUILD_DIR="${3:?usage: deploy-image.sh <image-name> <tag> <build-dir>}"
IMAGE="192.168.56.20:5000/${IMAGE_NAME}:${TAG}"

echo "--- building + pushing ${IMAGE} on buildserver ---"
vagrant ssh buildserver -c "sudo /opt/build-and-push.sh ${IMAGE_NAME} ${TAG} /${BUILD_DIR}"

for node in k8s-master k8s-worker1; do
  echo "--- pre-pulling ${IMAGE} on ${node} ---"
  vagrant ssh "$node" -c "sudo ctr -n k8s.io images pull --hosts-dir /etc/containerd/certs.d ${IMAGE}"
done

echo
echo "Done. Update the relevant image: field in that app's k8s manifests"
echo "to '${IMAGE}' if the tag changed, then kubectl apply them."
