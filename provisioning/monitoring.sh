#!/bin/bash
# Prometheus + Grafana via docker-compose, scraping the k8s cluster
# from OUTSIDE it - deliberately, so monitoring survives even if the
# cluster itself is having a bad day. Scrape targets are node-exporter
# (hostNetwork DaemonSet, one per node) and kube-state-metrics
# (NodePort 30099) - see k8s-manifests/cluster-observability.yaml,
# applied automatically by provisioning/master-init.sh.
#
# Datasource + dashboards are both provisioned from files, so Grafana
# comes up fully configured - no manual "Add datasource"/"Import
# dashboard" clicking required.

LOG="/shared_folder/logs/monitoring.log"

exec > >(tee -a "$LOG") 2>&1

set -eux

apt-get update

apt-get install -y docker.io docker-compose-v2

systemctl enable docker
systemctl start docker

usermod -aG docker vagrant

mkdir -p /opt/monitoring/grafana/provisioning/datasources
mkdir -p /opt/monitoring/grafana/provisioning/dashboards/json

cat <<'EOF' >/opt/monitoring/prometheus.yml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: node-exporter
    static_configs:
      - targets: ["192.168.56.10:9100", "192.168.56.11:9100"]
        labels: { cluster: k8s-lab }

  - job_name: kube-state-metrics
    static_configs:
      - targets: ["192.168.56.10:30099"]
        labels: { cluster: k8s-lab }
EOF

cat <<'EOF' >/opt/monitoring/grafana/provisioning/datasources/prometheus.yml
apiVersion: 1
datasources:
  - name: Prometheus
    uid: prometheus
    type: prometheus
    access: proxy
    url: http://localhost:9090
    isDefault: true
EOF

cat <<'EOF' >/opt/monitoring/grafana/provisioning/dashboards/dashboards.yml
apiVersion: 1
providers:
  - name: default
    orgId: 1
    folder: ""
    type: file
    disableDeletion: false
    updateIntervalSeconds: 30
    allowUiUpdates: true
    options:
      path: /etc/grafana/provisioning/dashboards/json
EOF

cat <<'EOF' >/opt/monitoring/grafana/provisioning/dashboards/json/node-health.json
{
  "title": "Node Health",
  "uid": "node-health",
  "schemaVersion": 39,
  "version": 1,
  "editable": true,
  "timezone": "browser",
  "time": { "from": "now-1h", "to": "now" },
  "refresh": "30s",
  "panels": [
    {
      "id": 1, "title": "CPU Usage %", "type": "timeseries",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 0 },
      "fieldConfig": { "defaults": { "unit": "percent", "min": 0, "max": 100 }, "overrides": [] },
      "targets": [
        { "expr": "100 - (avg by (instance) (rate(node_cpu_seconds_total{mode=\"idle\"}[$__rate_interval])) * 100)", "legendFormat": "{{instance}}" }
      ]
    },
    {
      "id": 2, "title": "Memory Usage %", "type": "timeseries",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 0 },
      "fieldConfig": { "defaults": { "unit": "percent", "min": 0, "max": 100 }, "overrides": [] },
      "targets": [
        { "expr": "100 * (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))", "legendFormat": "{{instance}}" }
      ]
    },
    {
      "id": 3, "title": "Disk Usage % (root)", "type": "timeseries",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 8 },
      "fieldConfig": { "defaults": { "unit": "percent", "min": 0, "max": 100 }, "overrides": [] },
      "targets": [
        { "expr": "100 * (1 - (node_filesystem_avail_bytes{mountpoint=\"/\",fstype!=\"tmpfs\"} / node_filesystem_size_bytes{mountpoint=\"/\",fstype!=\"tmpfs\"}))", "legendFormat": "{{instance}}" }
      ]
    },
    {
      "id": 4, "title": "Network I/O (bytes/sec)", "type": "timeseries",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 8 },
      "fieldConfig": { "defaults": { "unit": "Bps" }, "overrides": [] },
      "targets": [
        { "expr": "rate(node_network_receive_bytes_total{device!~\"lo|veth.*|cali.*\"}[$__rate_interval])", "legendFormat": "{{instance}} rx {{device}}" },
        { "expr": "rate(node_network_transmit_bytes_total{device!~\"lo|veth.*|cali.*\"}[$__rate_interval])", "legendFormat": "{{instance}} tx {{device}}" }
      ]
    },
    {
      "id": 5, "title": "Load Average", "type": "timeseries",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 24, "x": 0, "y": 16 },
      "fieldConfig": { "defaults": {}, "overrides": [] },
      "targets": [
        { "expr": "node_load1", "legendFormat": "{{instance}} load1" },
        { "expr": "node_load5", "legendFormat": "{{instance}} load5" },
        { "expr": "node_load15", "legendFormat": "{{instance}} load15" }
      ]
    }
  ]
}
EOF

cat <<'EOF' >/opt/monitoring/grafana/provisioning/dashboards/json/cluster-state.json
{
  "title": "Kubernetes Cluster State",
  "uid": "cluster-state",
  "schemaVersion": 39,
  "version": 1,
  "editable": true,
  "timezone": "browser",
  "time": { "from": "now-1h", "to": "now" },
  "refresh": "30s",
  "panels": [
    {
      "id": 1, "title": "Pods by Phase", "type": "stat",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 6, "w": 24, "x": 0, "y": 0 },
      "fieldConfig": { "defaults": {}, "overrides": [] },
      "targets": [
        { "expr": "sum by (phase) (kube_pod_status_phase == 1)", "legendFormat": "{{phase}}" }
      ]
    },
    {
      "id": 2, "title": "Pod Restarts (rate, 15m)", "type": "timeseries",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 24, "x": 0, "y": 6 },
      "fieldConfig": { "defaults": {}, "overrides": [] },
      "targets": [
        { "expr": "sum by (namespace, pod) (rate(kube_pod_container_status_restarts_total[15m])) > 0", "legendFormat": "{{namespace}}/{{pod}}" }
      ]
    },
    {
      "id": 3, "title": "Deployment Replicas: Desired vs Available", "type": "timeseries",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 14 },
      "fieldConfig": { "defaults": {}, "overrides": [] },
      "targets": [
        { "expr": "kube_deployment_spec_replicas", "legendFormat": "{{namespace}}/{{deployment}} desired" },
        { "expr": "kube_deployment_status_replicas_available", "legendFormat": "{{namespace}}/{{deployment}} available" }
      ]
    },
    {
      "id": 4, "title": "Node Ready", "type": "stat",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 14 },
      "fieldConfig": { "defaults": {}, "overrides": [] },
      "targets": [
        { "expr": "kube_node_status_condition{condition=\"Ready\",status=\"true\"}", "legendFormat": "{{node}}" }
      ]
    },
    {
      "id": 5, "title": "Pods per Namespace", "type": "piechart",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 24, "x": 0, "y": 22 },
      "fieldConfig": { "defaults": {}, "overrides": [] },
      "targets": [
        { "expr": "count by (namespace) (kube_pod_info)", "legendFormat": "{{namespace}}" }
      ]
    }
  ]
}
EOF

cat <<'EOF' >/opt/monitoring/docker-compose.yml
services:
  prometheus:
    image: prom/prometheus:v2.55.1
    container_name: prometheus
    restart: unless-stopped
    network_mode: host # needs to reach node-exporter/ksm on the 192.168.56.0/24 host-only network
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus_data:/prometheus
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --web.listen-address=:9090

  grafana:
    image: grafana/grafana:11.3.0
    container_name: grafana
    restart: unless-stopped
    network_mode: host # so it can reach prometheus at localhost:9090 under host networking
    environment:
      GF_SERVER_HTTP_PORT: "3000"
      GF_SECURITY_ADMIN_PASSWORD: admin
    volumes:
      - ./grafana/provisioning:/etc/grafana/provisioning:ro
      - grafana_data:/var/lib/grafana

volumes:
  prometheus_data:
  grafana_data:
EOF

cd /opt/monitoring
docker compose up -d

echo "Monitoring node configured successfully."
echo "Prometheus: http://192.168.56.21:9090"
echo "Grafana:    http://192.168.56.21:3000  (admin/admin, change on first login)"
echo "Dashboards: Node Health, Kubernetes Cluster State"
