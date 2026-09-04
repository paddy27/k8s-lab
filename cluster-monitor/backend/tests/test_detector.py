from app.detector import (
    detect_event_issues,
    detect_node_issues,
    detect_pod_issues,
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
