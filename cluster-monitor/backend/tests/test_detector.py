from app.detector import (
    detect_event_issues,
    detect_node_issues,
    detect_pod_issues,
    detect_scheduling_distribution_issues,
)


def _container_status(name, restart_count=0, waiting_reason=None, waiting_message=None, last_terminated_reason=None):
    cs = {"name": name, "restart_count": restart_count, "state": {}, "last_state": {}}
    if waiting_reason:
        cs["state"] = {"waiting": {"reason": waiting_reason, "message": waiting_message}}
    if last_terminated_reason:
        cs["last_state"] = {"terminated": {"reason": last_terminated_reason}}
    return cs


def _pod(namespace, name, phase="Running", container_statuses=None):
    return {
        "metadata": {"namespace": namespace, "name": name},
        "status": {"phase": phase, "container_statuses": container_statuses or []},
    }


def test_detect_pod_issues_flags_crashloopbackoff():
    """12 restarts is both a CrashLoopBackOff and, independently, a
    FrequentRestarts (>= the 5-restart threshold) - both are correct."""
    pods = [_pod("obs", "backend-1", container_statuses=[
        _container_status("backend", restart_count=12, waiting_reason="CrashLoopBackOff"),
    ])]

    issues = detect_pod_issues(pods)

    assert {i["rule"] for i in issues} == {"CrashLoopBackOff", "FrequentRestarts"}
    crashloop = next(i for i in issues if i["rule"] == "CrashLoopBackOff")
    assert crashloop["severity"] == "critical"
    assert crashloop["namespace"] == "obs"
    assert "12 restarts" in crashloop["message"]


def test_detect_pod_issues_flags_image_pull_errors():
    for reason in ("ImagePullBackOff", "ErrImagePull"):
        pods = [_pod("obs", "backend-1", container_statuses=[
            _container_status("backend", waiting_reason=reason, waiting_message="manifest not found"),
        ])]

        [issue] = detect_pod_issues(pods)

        assert issue["rule"] == "ImagePullBackOff"
        assert issue["severity"] == "critical"
        assert "manifest not found" in issue["message"]


def test_detect_pod_issues_flags_oomkilled():
    pods = [_pod("obs", "worker-1", container_statuses=[
        _container_status("worker", last_terminated_reason="OOMKilled"),
    ])]

    [issue] = detect_pod_issues(pods)

    assert issue["rule"] == "OOMKilled"
    assert issue["severity"] == "critical"


def test_detect_pod_issues_flags_pending():
    pods = [_pod("obs", "backend-1", phase="Pending")]

    [issue] = detect_pod_issues(pods)

    assert issue["rule"] == "PendingPod"
    assert issue["severity"] == "warning"


def test_detect_pod_issues_flags_frequent_restarts():
    pods = [_pod("obs", "backend-1", container_statuses=[
        _container_status("backend", restart_count=6),
    ])]

    [issue] = detect_pod_issues(pods)

    assert issue["rule"] == "FrequentRestarts"
    assert issue["severity"] == "warning"


def test_detect_pod_issues_silent_for_healthy_pod():
    pods = [_pod("obs", "backend-1", container_statuses=[_container_status("backend", restart_count=0)])]

    assert detect_pod_issues(pods) == []


def _pending_pod_with_scheduled_message(namespace, name, message):
    pod = _pod(namespace, name, phase="Pending")
    pod["status"]["conditions"] = [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": message}]
    return pod


def test_detect_pod_issues_pending_names_insufficient_resource_causes():
    """A single scheduler message can name more than one cause at once -
    both should be flagged, alongside the generic PendingPod issue."""
    pods = [_pending_pod_with_scheduled_message(
        "obs", "backend-1", "0/2 nodes are available: 1 Insufficient cpu, 1 Insufficient memory.",
    )]

    issues = detect_pod_issues(pods)

    rules = {i["rule"] for i in issues}
    assert rules == {"PendingPod", "InsufficientCPU", "InsufficientMemory"}
    assert all(i["severity"] == "critical" for i in issues if i["rule"] in ("InsufficientCPU", "InsufficientMemory"))


def test_detect_pod_issues_pending_names_taint_cause():
    pods = [_pending_pod_with_scheduled_message(
        "obs", "backend-1", "0/2 nodes are available: 2 node(s) had taint {dedicated: gpu}, that the pod didn't tolerate.",
    )]

    issues = detect_pod_issues(pods)

    assert any(i["rule"] == "TaintsAndTolerations" for i in issues)


def test_detect_pod_issues_pending_names_node_affinity_cause():
    pods = [_pending_pod_with_scheduled_message(
        "obs", "backend-1", "0/2 nodes are available: 2 node(s) didn't match Pod's node affinity/selector.",
    )]

    issues = detect_pod_issues(pods)

    assert any(i["rule"] == "NodeAffinity" for i in issues)


def test_detect_pod_issues_pending_with_unrecognized_message_only_generic():
    """The scheduler message doesn't match any known pattern - stay
    silent on a specific cause rather than guessing; the generic
    PendingPod issue still carries the visibility."""
    pods = [_pending_pod_with_scheduled_message("obs", "backend-1", "something scheduling-related but novel")]

    issues = detect_pod_issues(pods)

    assert {i["rule"] for i in issues} == {"PendingPod"}


def _node(name, ready="True", disk_pressure="False", memory_pressure="False", cpu="2", memory="2048Mi"):
    return {
        "metadata": {"name": name},
        "status": {
            "conditions": [
                {"type": "Ready", "status": ready},
                {"type": "DiskPressure", "status": disk_pressure},
                {"type": "MemoryPressure", "status": memory_pressure},
            ],
            "allocatable": {"cpu": cpu, "memory": memory},
        },
    }


def _node_metrics(name, cpu, memory):
    return {"metadata": {"name": name}, "usage": {"cpu": cpu, "memory": memory}}


def test_detect_node_issues_flags_not_ready():
    nodes = [_node("k8s-worker1", ready="False")]

    [issue] = detect_node_issues(nodes, [])

    assert issue["rule"] == "NodeNotReady"
    assert issue["severity"] == "critical"
    assert issue["namespace"] is None


def test_detect_node_issues_flags_disk_pressure():
    nodes = [_node("k8s-worker1", disk_pressure="True")]

    [issue] = detect_node_issues(nodes, [])

    assert issue["rule"] == "DiskPressure"


def test_detect_node_issues_flags_high_cpu_and_memory():
    nodes = [_node("k8s-worker1", cpu="2", memory="2048Mi")]
    metrics = [_node_metrics("k8s-worker1", cpu="1900m", memory="2000Mi")]  # ~95% both

    issues = detect_node_issues(nodes, metrics)

    rules = {i["rule"] for i in issues}
    assert "HighCPUUsage" in rules
    assert "HighMemoryUsage" in rules


def test_detect_node_issues_silent_when_healthy_and_no_metrics():
    """No metrics-server data shouldn't itself be treated as a problem -
    the usage checks should just be skipped, not raise or flag anything."""
    nodes = [_node("k8s-worker1")]

    assert detect_node_issues(nodes, []) == []


def _event(namespace, kind, name, reason, message="something happened"):
    return {"involved_object": {"namespace": namespace, "kind": kind, "name": name}, "reason": reason, "message": message}


def test_detect_event_issues_groups_repeats_and_counts_them():
    events = [
        _event("obs", "Pod", "backend-1", "BackOff", "back-off restarting failed container"),
        _event("obs", "Pod", "backend-1", "BackOff", "back-off restarting failed container"),
        _event("obs", "Pod", "backend-1", "BackOff", "back-off restarting failed container"),
    ]

    [issue] = detect_event_issues(events)

    assert issue["message"].endswith("(x3)")


def test_detect_event_issues_known_critical_reason_is_critical():
    events = [_event("obs", "Pod", "backend-1", "FailedScheduling", "0/2 nodes available")]

    [issue] = detect_event_issues(events)

    assert issue["severity"] == "critical"


def test_detect_event_issues_unknown_reason_defaults_to_warning():
    events = [_event("obs", "Pod", "backend-1", "SomeUnrecognizedReason")]

    [issue] = detect_event_issues(events)

    assert issue["severity"] == "warning"


def _running_pod_on_node(namespace, name, node_name, owner_kind="ReplicaSet", owner_name="backend-abc123"):
    return {
        "metadata": {
            "namespace": namespace, "name": name,
            "owner_references": [{"kind": owner_kind, "name": owner_name, "controller": True}],
        },
        "spec": {"node_name": node_name},
        "status": {"phase": "Running"},
    }


def test_detect_scheduling_distribution_issues_flags_all_replicas_on_one_node():
    pods = [_running_pod_on_node("obs", f"backend-{i}", "k8s-worker1") for i in range(3)]
    nodes = [_node("k8s-worker1"), _node("k8s-worker2")]

    [issue] = detect_scheduling_distribution_issues(pods, nodes)

    assert issue["rule"] == "PodDistributionImbalance"
    assert issue["resource_kind"] == "ReplicaSet"
    assert issue["resource_name"] == "backend-abc123"
    assert "k8s-worker1" in issue["message"]


def test_detect_scheduling_distribution_issues_silent_when_spread_across_nodes():
    pods = [
        _running_pod_on_node("obs", "backend-1", "k8s-worker1"),
        _running_pod_on_node("obs", "backend-2", "k8s-worker2"),
        _running_pod_on_node("obs", "backend-3", "k8s-worker1"),
    ]
    nodes = [_node("k8s-worker1"), _node("k8s-worker2")]

    assert detect_scheduling_distribution_issues(pods, nodes) == []


def test_detect_scheduling_distribution_issues_silent_below_replica_threshold():
    """Only 2 replicas on one node - not enough to call it "imbalanced",
    that's just what 2 replicas look like."""
    pods = [_running_pod_on_node("obs", f"backend-{i}", "k8s-worker1") for i in range(2)]
    nodes = [_node("k8s-worker1"), _node("k8s-worker2")]

    assert detect_scheduling_distribution_issues(pods, nodes) == []


def _static_pod(namespace, name, node_name):
    """A kubelet-managed static/mirror pod - owned by the Node itself,
    not a workload controller (etcd, kube-apiserver, kube-scheduler,
    kube-controller-manager all look like this on a kubeadm cluster)."""
    return {
        "metadata": {
            "namespace": namespace, "name": name,
            "owner_references": [{"kind": "Node", "name": node_name, "controller": True}],
        },
        "spec": {"node_name": node_name},
        "status": {"phase": "Running"},
    }


def test_detect_scheduling_distribution_issues_ignores_static_control_plane_pods():
    """Regression test: etcd/kube-apiserver/kube-scheduler/kube-controller-
    manager all share an ownerReference of kind Node on their control-plane
    node - that's 4 pods with an identical "owner", but they're 4 unrelated
    single-instance components, not replicas of one workload, and their
    placement isn't a scheduler decision at all. Must not be flagged."""
    pods = [
        _static_pod("kube-system", "etcd-k8s-master", "k8s-master"),
        _static_pod("kube-system", "kube-apiserver-k8s-master", "k8s-master"),
        _static_pod("kube-system", "kube-scheduler-k8s-master", "k8s-master"),
        _static_pod("kube-system", "kube-controller-manager-k8s-master", "k8s-master"),
    ]
    nodes = [_node("k8s-master"), _node("k8s-worker1")]

    assert detect_scheduling_distribution_issues(pods, nodes) == []


def test_detect_scheduling_distribution_issues_silent_on_single_node_cluster():
    """Nothing to spread across - not a misconfiguration to fix."""
    pods = [_running_pod_on_node("obs", f"backend-{i}", "k8s-master") for i in range(3)]
    nodes = [_node("k8s-master")]

    assert detect_scheduling_distribution_issues(pods, nodes) == []
