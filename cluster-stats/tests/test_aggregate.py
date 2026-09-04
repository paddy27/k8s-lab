import pytest

from app.aggregate import (
    build_recommendations,
    parse_cpu_millicores,
    parse_memory_bytes,
    summarize_cluster,
    summarize_namespaces,
    summarize_nodes,
    summarize_workloads,
)


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, 0.0),
        ("100m", 100.0),
        ("1", 1000.0),
        ("1.5", 1500.0),
        ("500000n", 0.5),
        ("2000u", 2.0),
    ],
)
def test_parse_cpu_millicores(value, expected):
    assert parse_cpu_millicores(value) == pytest.approx(expected)


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, 0),
        ("128Mi", 128 * 1024 * 1024),
        ("1Gi", 1024**3),
        ("500", 500),
        ("1k", 1000),
        ("2Ki", 2048),
    ],
)
def test_parse_memory_bytes(value, expected):
    assert parse_memory_bytes(value) == expected


def _node(name, cpu_cap, mem_cap, ready=True):
    return {
        "metadata": {"name": name, "labels": {"node-role.kubernetes.io/worker": ""}},
        "status": {
            "capacity": {"cpu": cpu_cap, "memory": mem_cap, "pods": "110"},
            "allocatable": {"cpu": cpu_cap, "memory": mem_cap, "pods": "110"},
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "addresses": [{"type": "InternalIP", "address": "192.168.56.11"}],
            "nodeInfo": {
                "kubeletVersion": "v1.30.14",
                "osImage": "Ubuntu 22.04.5 LTS",
                "containerRuntimeVersion": "containerd://2.2.1",
            },
        },
    }


def test_summarize_nodes_reports_usage_pct():
    nodes = [_node("k8s-worker1", "2", "2048Mi")]
    node_metrics = [{"metadata": {"name": "k8s-worker1"}, "usage": {"cpu": "500m", "memory": "1024Mi"}}]

    [summary] = summarize_nodes(nodes, node_metrics)

    assert summary["ready"] is True
    assert summary["roles"] == ["worker"]
    assert summary["cpu_used_millicores"] == 500.0
    assert summary["cpu_used_pct"] == 25.0
    assert summary["memory_used_pct"] == 50.0


def test_summarize_nodes_missing_metrics_reports_zero_not_error():
    """No metrics-server data for this node -> usage defaults to 0, which
    is a legitimate (if uninformative) reading, not None/unknown. None is
    reserved for an actual divide-by-zero (0 allocatable), not "we don't
    know" - see test_summarize_nodes_zero_allocatable_reports_none_pct."""
    nodes = [_node("k8s-master", "2", "4096Mi")]

    [summary] = summarize_nodes(nodes, node_metrics=[])

    assert summary["cpu_used_millicores"] == 0.0
    assert summary["cpu_used_pct"] == 0.0


def test_summarize_nodes_zero_allocatable_reports_none_pct():
    nodes = [_node("k8s-master", "0", "0")]

    [summary] = summarize_nodes(nodes, node_metrics=[])

    assert summary["cpu_used_pct"] is None
    assert summary["memory_used_pct"] is None


def _pod(namespace, name, phase="Running", cpu_req="100m", mem_req="128Mi"):
    return {
        "metadata": {"namespace": namespace, "name": name},
        "spec": {
            "nodeName": "k8s-worker1",
            "containers": [{"resources": {"requests": {"cpu": cpu_req, "memory": mem_req}}}],
        },
        "status": {"phase": phase, "containerStatuses": [{"restartCount": 0}]},
    }


def test_summarize_namespaces_aggregates_pod_resources():
    namespaces = [{"metadata": {"name": "obs"}, "status": {"phase": "Active"}}]
    pods = [
        _pod("obs", "backend", cpu_req="100m", mem_req="128Mi"),
        _pod("obs", "worker", cpu_req="150m", mem_req="256Mi"),
    ]

    [summary] = summarize_namespaces(namespaces, pods, pod_metrics=[])

    assert summary["pod_count"] == 2
    assert summary["cpu_requested_millicores"] == pytest.approx(250.0)
    assert summary["memory_requested_bytes"] == (128 + 256) * 1024 * 1024
    assert summary["pods_by_phase"] == {"Running": 2}


def test_summarize_cluster_counts_pods_by_phase():
    nodes_summary = [{"ready": True, "cpu_capacity_millicores": 2000, "cpu_allocatable_millicores": 2000,
                       "cpu_used_millicores": 500, "memory_capacity_bytes": 0, "memory_allocatable_bytes": 0,
                       "memory_used_bytes": 0}]
    ns_summary = [{"name": "obs"}]
    pods = [_pod("obs", "a"), _pod("obs", "b", phase="Pending")]

    summary = summarize_cluster(nodes_summary, ns_summary, pods)

    assert summary["node_count"] == 1
    assert summary["nodes_ready"] == 1
    assert summary["pod_count"] == 2
    assert summary["pods_by_phase"] == {"Running": 1, "Pending": 1}


def test_summarize_workloads_normalizes_all_three_kinds():
    deployments = [{
        "metadata": {"namespace": "obs", "name": "backend"},
        "spec": {"replicas": 2},
        "status": {"replicas": 2, "readyReplicas": 1},
    }]
    daemonsets = [{
        "metadata": {"namespace": "kube-system", "name": "kube-proxy"},
        "status": {"desiredNumberScheduled": 2, "currentNumberScheduled": 2, "numberReady": 2},
    }]
    statefulsets = [{
        "metadata": {"namespace": "obs", "name": "db"},
        "spec": {"replicas": 1},
        "status": {"replicas": 1, "readyReplicas": 1},
    }]

    result = summarize_workloads(deployments, daemonsets, statefulsets)

    assert result == [
        {"kind": "DaemonSet", "namespace": "kube-system", "name": "kube-proxy", "desired": 2, "current": 2, "ready": 2},
        {"kind": "Deployment", "namespace": "obs", "name": "backend", "desired": 2, "current": 2, "ready": 1},
        {"kind": "StatefulSet", "namespace": "obs", "name": "db", "desired": 1, "current": 1, "ready": 1},
    ]


def test_summarize_workloads_missing_status_defaults_to_zero():
    """A brand-new workload whose controller hasn't reported status yet
    shouldn't crash the summary - just show zeros."""
    deployments = [{"metadata": {"namespace": "obs", "name": "new"}, "spec": {"replicas": 1}, "status": {}}]

    [entry] = summarize_workloads(deployments, [], [])

    assert entry["current"] == 0
    assert entry["ready"] == 0


def test_summarize_namespaces_includes_arbitrary_resource_counts():
    namespaces = [
        {"metadata": {"name": "obs"}, "status": {"phase": "Active"}},
        {"metadata": {"name": "empty-ns"}, "status": {"phase": "Active"}},
    ]
    resource_kinds = {
        "Service": [{"metadata": {"namespace": "obs", "name": "backend"}}],
        "ConfigMap": [
            {"metadata": {"namespace": "obs", "name": "a"}},
            {"metadata": {"namespace": "obs", "name": "b"}},
        ],
    }

    by_name = {n["name"]: n for n in summarize_namespaces(namespaces, [], [], resource_kinds)}

    assert by_name["obs"]["resource_counts"] == {"Service": 1, "ConfigMap": 2}
    # a namespace with none of a given kind still reports 0, not a missing key
    assert by_name["empty-ns"]["resource_counts"] == {"Service": 0, "ConfigMap": 0}


def test_summarize_namespaces_without_resource_kinds_arg_still_works():
    namespaces = [{"metadata": {"name": "obs"}, "status": {"phase": "Active"}}]

    [entry] = summarize_namespaces(namespaces, [], [])

    assert entry["resource_counts"] == {}


def _deployment_with_request(namespace, name, container_name, cpu_req, mem_req):
    return {
        "metadata": {"namespace": namespace, "name": name},
        "spec": {
            "replicas": 1,
            "template": {"spec": {"containers": [
                {"name": container_name, "resources": {"requests": {"cpu": cpu_req, "memory": mem_req}}}
            ]}},
        },
        "status": {},
    }


def _vpa_for(namespace, target_name, container_name, lower_cpu, lower_mem, upper_cpu, upper_mem):
    return {
        "metadata": {"namespace": namespace, "name": f"{target_name}-auto"},
        "spec": {
            "targetRef": {"kind": "Deployment", "name": target_name},
            "updatePolicy": {"updateMode": "Off"},
        },
        "status": {"recommendation": {"containerRecommendations": [{
            "containerName": container_name,
            "target": {"cpu": lower_cpu, "memory": lower_mem},
            "lowerBound": {"cpu": lower_cpu, "memory": lower_mem},
            "upperBound": {"cpu": upper_cpu, "memory": upper_mem},
        }]}},
    }


def test_build_recommendations_flags_under_provisioned_container():
    deployments = [_deployment_with_request("obs", "backend", "backend", "10m", "16Mi")]
    vpas = [_vpa_for("obs", "backend", "backend", "50m", "64Mi", "200m", "256Mi")]

    recs = build_recommendations(deployments, [], [], vpas, [])

    types = {(r["type"], r["container"]) for r in recs}
    assert ("VPA", "backend") in types
    messages = " ".join(r["message"] for r in recs)
    assert "below the VPA-recommended minimum" in messages
    assert all(r["severity"] == "warning" for r in recs if r["type"] == "VPA")


def test_build_recommendations_flags_no_request_set_distinctly():
    deployments = [{
        "metadata": {"namespace": "obs", "name": "backend"},
        "spec": {
            "replicas": 1,
            # no "resources" block at all on the container - a common
            # real-world case, distinct from "has a request, just too low"
            "template": {"spec": {"containers": [{"name": "backend"}]}},
        },
        "status": {},
    }]
    vpas = [_vpa_for("obs", "backend", "backend", "50m", "64Mi", "200m", "256Mi")]

    recs = build_recommendations(deployments, [], [], vpas, [])

    assert any("No CPU request set" in r["message"] for r in recs)
    assert any("No Memory request set" in r["message"] for r in recs)


def test_build_recommendations_flags_over_provisioned_as_info():
    deployments = [_deployment_with_request("obs", "backend", "backend", "500m", "512Mi")]
    vpas = [_vpa_for("obs", "backend", "backend", "50m", "64Mi", "200m", "256Mi")]

    recs = build_recommendations(deployments, [], [], vpas, [])

    assert recs
    assert all(r["severity"] == "info" for r in recs)
    assert all("above the VPA-recommended maximum" in r["message"] for r in recs)


def test_build_recommendations_silent_when_well_sized():
    deployments = [_deployment_with_request("obs", "backend", "backend", "100m", "128Mi")]
    vpas = [_vpa_for("obs", "backend", "backend", "50m", "64Mi", "200m", "256Mi")]

    recs = build_recommendations(deployments, [], [], vpas, [])

    assert recs == []


def test_build_recommendations_ignores_vpa_with_no_matching_workload():
    """A VPA whose target no longer exists (deployment deleted, VPA not
    cleaned up yet) shouldn't crash the recommendation engine."""
    vpas = [_vpa_for("obs", "ghost", "ghost", "50m", "64Mi", "200m", "256Mi")]

    recs = build_recommendations([], [], [], vpas, [])

    assert recs == []


def _hpa(namespace, name, min_replicas, max_replicas, current_replicas):
    return {
        "metadata": {"namespace": namespace, "name": name},
        "spec": {
            "scaleTargetRef": {"kind": "Deployment", "name": name},
            "minReplicas": min_replicas,
            "maxReplicas": max_replicas,
            "metrics": [],
        },
        "status": {"currentReplicas": current_replicas, "desiredReplicas": current_replicas},
    }


def test_build_recommendations_flags_hpa_pinned_at_max():
    hpas = [_hpa("obs", "backend", 1, 3, 3)]

    recs = build_recommendations([], [], [], [], hpas)

    assert any(r["type"] == "HPA" and "max replicas" in r["message"] for r in recs)


def test_build_recommendations_flags_hpa_that_cannot_scale():
    hpas = [_hpa("obs", "backend", 2, 2, 2)]

    recs = build_recommendations([], [], [], [], hpas)

    assert any("can never actually scale" in r["message"] for r in recs)


def test_build_recommendations_no_hpa_warnings_when_healthy():
    hpas = [_hpa("obs", "backend", 1, 5, 2)]

    recs = build_recommendations([], [], [], [], hpas)

    assert recs == []
