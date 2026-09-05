from datetime import datetime, timedelta, timezone

from app.root_cause import analyze_incident, workload_for_pod

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _issue(rule, message="", first_seen=T0):
    return {"rule": rule, "message": message, "first_seen": first_seen}


def test_analyze_incident_no_signal_returns_empty_root_causes():
    result = analyze_incident("obs", "backend-1", [_issue("CrashLoopBackOff", "")], [], None)

    # CrashLoopBackOff alone with nothing else IS the "Application Error"
    # fallback - covered by its own test below. This one checks the
    # true-empty case: no rules travel through _score_categories at all.
    result_empty = analyze_incident("obs", "backend-1", [], [], None)
    assert result_empty["root_causes"] == []
    assert "no correlating signal" in result_empty["recommended_action"].lower()


def test_analyze_incident_flags_oomkilled_as_memory_issue():
    related = [_issue("OOMKilled", "Container was OOMKilled")]

    result = analyze_incident("obs", "backend-1", related, [], None)

    assert len(result["root_causes"]) == 1
    assert result["root_causes"][0]["category"] == "Resource Limits (Memory)"
    assert result["root_causes"][0]["probability_pct"] == 100.0
    assert "OOMKilled" in result["root_causes"][0]["evidence"][0]


def test_analyze_incident_combines_multiple_signals_into_normalized_percentages():
    related = [
        _issue("OOMKilled", "Container was OOMKilled"),  # weight 3, Memory
    ]
    node_issues = [_issue("NodeNotReady", "node down")]  # weight 3, Node/Infrastructure

    result = analyze_incident("obs", "backend-1", related, node_issues, None)

    total_pct = sum(r["probability_pct"] for r in result["root_causes"])
    assert abs(total_pct - 100.0) < 0.01
    categories = {r["category"] for r in result["root_causes"]}
    assert categories == {"Resource Limits (Memory)", "Node/Infrastructure"}
    # equal weights (3 and 3) -> equal split
    assert all(abs(r["probability_pct"] - 50.0) < 0.01 for r in result["root_causes"])


def test_analyze_incident_flags_connectivity_from_message_text():
    related = [_issue("Event:Unhealthy", 'Readiness probe failed: dial tcp 10.0.0.1:5432: connect: connection refused')]

    result = analyze_incident("obs", "backend-1", related, [], None)

    assert result["root_causes"][0]["category"] == "Network/Dependency Connectivity"


def test_analyze_incident_crashloop_alone_is_application_error_fallback():
    related = [_issue("CrashLoopBackOff", "crash-looping")]

    result = analyze_incident("obs", "backend-1", related, [], None)

    assert result["root_causes"][0]["category"] == "Application Error"
    assert result["root_causes"][0]["probability_pct"] == 100.0


def test_analyze_incident_correlates_recent_rollout():
    incident_start = T0
    rollout_time = T0 - timedelta(minutes=20)  # 20 min before - within the 1h window
    related = [_issue("CrashLoopBackOff", "crash-looping", first_seen=incident_start)]
    workload = ("Deployment", {
        "status": {"conditions": [{"type": "Progressing", "last_update_time": rollout_time, "reason": "NewReplicaSetAvailable"}]},
    })

    result = analyze_incident("obs", "backend-1", related, [], workload)

    assert result["root_causes"][0]["category"] == "Recent Deployment Rollout"
    assert any("rolled out" in e for r in result["root_causes"] for e in r["evidence"])


def test_analyze_incident_ignores_rollout_that_happened_after_incident_started():
    incident_start = T0
    rollout_time = T0 + timedelta(minutes=5)  # AFTER the incident started - can't be the cause
    related = [_issue("CrashLoopBackOff", "crash-looping", first_seen=incident_start)]
    workload = ("Deployment", {
        "status": {"conditions": [{"type": "Progressing", "last_update_time": rollout_time, "reason": "NewReplicaSetAvailable"}]},
    })

    result = analyze_incident("obs", "backend-1", related, [], workload)

    # Falls back to Application Error since the rollout doesn't correlate
    assert result["root_causes"][0]["category"] == "Application Error"


def test_analyze_incident_ignores_rollout_outside_correlation_window():
    incident_start = T0
    rollout_time = T0 - timedelta(hours=5)  # too long ago
    related = [_issue("CrashLoopBackOff", "crash-looping", first_seen=incident_start)]
    workload = ("Deployment", {
        "status": {"conditions": [{"type": "Progressing", "last_update_time": rollout_time, "reason": "NewReplicaSetAvailable"}]},
    })

    result = analyze_incident("obs", "backend-1", related, [], workload)

    assert not any(r["category"] == "Recent Deployment Rollout" for r in result["root_causes"])


def test_analyze_incident_timeline_is_sorted_ascending():
    related = [
        _issue("CrashLoopBackOff", "later", first_seen=T0 + timedelta(minutes=10)),
        _issue("FrequentRestarts", "earlier", first_seen=T0),
    ]

    result = analyze_incident("obs", "backend-1", related, [], None)

    labels = [e["label"] for e in result["timeline"]]
    assert labels.index("FrequentRestarts: earlier") < labels.index("CrashLoopBackOff: later")


def _pod(namespace, labels):
    return {"metadata": {"namespace": namespace, "labels": labels}}


def _deployment(namespace, name, selector_labels):
    return {"metadata": {"namespace": namespace, "name": name}, "spec": {"selector": {"match_labels": selector_labels}}}


def test_workload_for_pod_matches_by_selector_not_owner_chain():
    pod = _pod("obs", {"app": "backend", "pod-template-hash": "abc123"})
    deployments = [_deployment("obs", "backend", {"app": "backend"})]

    result = workload_for_pod(pod, deployments, [])

    assert result[0] == "Deployment"
    assert result[1]["metadata"]["name"] == "backend"


def test_workload_for_pod_none_when_no_selector_matches():
    pod = _pod("obs", {"app": "other"})
    deployments = [_deployment("obs", "backend", {"app": "backend"})]

    assert workload_for_pod(pod, deployments, []) is None
