# observability-platform on k8s-lab

Plain manifests (no Helm yet) deploying the same stack as the project's
`docker-compose.yml`, onto the local kubeadm cluster (`k8s-master` +
`k8s-worker1`).

## Prerequisites (one-time)

1. **Storage**: the cluster has no default StorageClass (bare kubeadm has
   no cloud provisioner). Install Rancher's local-path-provisioner once:

   ```bash
   kubectl apply -f https://raw.githubusercontent.com/rancher/local-path-provisioner/v0.0.30/deploy/local-path-storage.yaml
   kubectl patch storageclass local-path -p '{"metadata": {"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
   ```

2. **Image**: built on `buildserver` and pushed to its local registry
   (`192.168.56.20:5000`), then pre-pulled onto the cluster nodes:

   ```bash
   ./deploy-image.sh observability-backend v2 observability-platform/backend
   ```

   Bump the tag for each code change, and update the `image:` field in
   `05-backend.yaml`/`06-worker.yaml` to match before re-applying.

   **Why a pre-pull step instead of just letting kubelet pull it**:
   kubelet/`crictl`'s own registry pull hits a containerd 2.2.1 bug on
   this box - it never honors `config.toml`'s registry `config_path`
   (confirmed: `ctr images pull` without `--hosts-dir` always assumes
   HTTPS and fails, even though the `/etc/containerd/certs.d/...
   /hosts.toml` file is correct and `--hosts-dir` pulls work fine).
   `deploy-image.sh` works around it by pre-pulling with
   `--hosts-dir` directly; `imagePullPolicy: IfNotPresent` on both
   Deployments then means kubelet just uses what's already there
   instead of trying (and failing) to pull it itself.

## Deploy

```bash
kubectl apply -f k8s-manifests/
```

## Verify

```bash
kubectl -n observability-platform get pods -w
kubectl -n observability-platform get svc backend   # NodePort 30080
curl http://192.168.56.11:30080/healthz             # any node's IP works
```

## Cluster observability

Installed cluster-wide (not part of the app namespace):

- **metrics-server** - powers `kubectl top nodes` / `kubectl top pods`.
  Patched with `--kubelet-insecure-tls` since this lab's kubelet certs
  don't have IP SANs (fine for a local cluster, not for production).
- **Kubernetes Dashboard** (`dashboard-admin-user.yaml`) - read-only
  login (`view` ClusterRole, can't edit/delete from the UI). Never
  exposed beyond localhost:

  ```bash
  kubectl proxy --port=8001 &
  kubectl -n kubernetes-dashboard create token dashboard-viewer --duration=24h
  ```

  Then open http://localhost:8001/api/v1/namespaces/kubernetes-dashboard/services/https:kubernetes-dashboard:/proxy/
  and paste the token in.

## Notes / deviations from docker-compose

- `backend` and `worker` share one image (like docker-compose), just a
  different `command`.
- `redis` has no PVC, matching docker-compose - it's a disposable work
  queue, not a source of truth.
- `timescaledb` and `worker` both use `strategy: Recreate` since their
  PVCs are `ReadWriteOnce` - a rolling update would try to start the new
  pod before killing the old one, and the second pod would fail to mount.
