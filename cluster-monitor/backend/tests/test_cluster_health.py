from app.cluster_health import (
    compute_cluster_capacity,
    compute_health_score,
    compute_resource_saturation,
    control_plane_pod_health,
)


def _node(name, ready=True, cpu="2", memory="4096Mi"):
    return {
        "metadata": {"name": name},
        "status": {
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "allocatable": {"cpu": cpu, "memory": memory},
        },
    }


def _control_plane_pod(name, node, ready=True, restarts=0):
    return {
        "metadata": {"namespace": "kube-system", "name": name},
        "spec": {"node_name": node},
        "status": {"container_statuses": [{"ready": ready, "restart_count": restarts}]},
    }


def test_control_plane_pod_health_finds_all_four_components():
    pods = [
        _control_plane_pod("etcd-k8s-master", "k8s-master"),
        _control_plane_pod("kube-apiserver-k8s-master", "k8s-master"),
        _control_plane_pod("kube-scheduler-k8s-master", "k8s-master"),
        _control_plane_pod("kube-controller-manager-k8s-master", "k8s-master"),
        _control_plane_pod("kube-proxy-abcde", "k8s-worker1"),  # not a control-plane component
    ]

    health = control_plane_pod_health(pods)

    components = {h["component"] for h in health}
    assert components == {"etcd", "API Server", "Scheduler", "Controller Manager"}


def test_control_plane_pod_health_flags_not_ready():
    pods = [_control_plane_pod("etcd-k8s-master", "k8s-master", ready=False, restarts=3)]

    [health] = control_plane_pod_health(pods)

    assert health["ready"] is False
    assert health["restarts"] == 3


def test_control_plane_pod_health_ignores_non_kube_system_namespace():
    pod = _control_plane_pod("etcd-k8s-master", "k8s-master")
    pod["metadata"]["namespace"] = "default"

    assert control_plane_pod_health([pod]) == []


def test_compute_cluster_capacity_sums_allocatable_across_nodes():
    nodes = [_node("a", cpu="2", memory="4096Mi"), _node("b", cpu="4", memory="8192Mi", ready=False)]

    capacity = compute_cluster_capacity(nodes)

    assert capacity["node_count"] == 2
    assert capacity["nodes_ready"] == 1
    assert capacity["cpu_allocatable_millicores"] == 6000
    assert capacity["memory_allocatable_bytes"] == (4096 + 8192) * 1024 * 1024


def test_compute_resource_saturation_percentages():
    nodes = [_node("a", cpu="2", memory="2048Mi")]
    node_resource_stats = [{"node_name": "a", "cpu_used_millicores": 1000, "memory_used_bytes": 1024 * 1024 * 1024}]

    saturation = compute_resource_saturation(nodes, node_resource_stats)

    assert saturation["cpu_used_pct"] == 50.0
    assert saturation["memory_used_pct"] == 50.0


def test_compute_resource_saturation_zero_pct_when_no_usage_stats_yet():
    """No node_resource_stats sample yet (e.g. right after startup) reads
    as 0% used against a known allocatable - a legitimate reading, not
    an unknown one (unlike zero *allocatable*, which would be None)."""
    nodes = [_node("a", cpu="2", memory="2048Mi")]

    saturation = compute_resource_saturation(nodes, [])

    assert saturation["cpu_used_pct"] == 0.0


def test_compute_health_score_perfect_when_everything_healthy():
    nodes = [_node("a")]
    control_plane_health = [{"component": "etcd", "ready": True}]
    saturation = {"cpu_used_pct": 10.0, "memory_used_pct": 10.0}

    result = compute_health_score(nodes, control_plane_health, saturation, active_critical_count=0, active_warning_count=0)

    assert result["score"] == 100
    assert result["deductions"] == []


def test_compute_health_score_deducts_for_not_ready_node():
    nodes = [_node("a", ready=False)]

    result = compute_health_score(nodes, [], {}, 0, 0)

    assert result["score"] == 80
    assert "not Ready" in result["deductions"][0]["reason"]


def test_compute_health_score_deducts_for_unhealthy_control_plane():
    nodes = [_node("a")]
    control_plane_health = [{"component": "etcd", "ready": False}]

    result = compute_health_score(nodes, control_plane_health, {}, 0, 0)

    assert result["score"] == 85
    assert "etcd" in result["deductions"][0]["reason"]


def test_compute_health_score_deducts_for_active_issues_capped():
    nodes = [_node("a")]

    result = compute_health_score(nodes, [], {}, active_critical_count=100, active_warning_count=0)

    # 100 * 8 = 800, capped at MAX_ISSUE_PENALTY (50)
    assert result["deductions"][0]["points"] == 50
    assert result["score"] == 50


def test_compute_health_score_deducts_for_high_saturation():
    nodes = [_node("a")]
    saturation = {"cpu_used_pct": 95.0, "memory_used_pct": 40.0}

    result = compute_health_score(nodes, [], saturation, 0, 0)

    assert any("CPU" in d["reason"] for d in result["deductions"])
    assert not any("memory" in d["reason"] for d in result["deductions"])


def test_compute_health_score_never_goes_below_zero():
    nodes = [_node("a", ready=False), _node("b", ready=False), _node("c", ready=False), _node("d", ready=False), _node("e", ready=False), _node("f", ready=False)]

    result = compute_health_score(nodes, [], {}, active_critical_count=1000, active_warning_count=1000)

    assert result["score"] == 0
