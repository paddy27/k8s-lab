import pytest

from app.aggregate import (
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
