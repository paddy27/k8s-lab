from app.best_practices import (
    detect_missing_networkpolicy_issues,
    detect_pod_security_issues,
    detect_workload_best_practice_issues,
)


def _container(name="app", image="myrepo/app:v1", resources=None, security_context=None,
                liveness_probe=True, readiness_probe=True, startup_probe=False):
    return {
        "name": name,
        "image": image,
        "resources": resources if resources is not None else {"requests": {"cpu": "100m"}, "limits": {"cpu": "200m"}},
        "security_context": security_context,
        "liveness_probe": {"http_get": {}} if liveness_probe else None,
        "readiness_probe": {"http_get": {}} if readiness_probe else None,
        "startup_probe": {"http_get": {}} if startup_probe else None,
    }


def _pod(namespace, name, containers=None, spec_extra=None):
    spec = {"containers": containers or [_container()]}
    spec.update(spec_extra or {})
    return {"metadata": {"namespace": namespace, "name": name}, "spec": spec}


def test_detect_pod_security_issues_silent_for_well_configured_pod():
    pod = _pod("obs", "backend-1", containers=[_container(
        security_context={"run_as_non_root": True, "run_as_user": 1000},
        startup_probe=True,
    )], spec_extra={"service_account_name": "backend", "automount_service_account_token": False})

    assert detect_pod_security_issues([pod]) == []


def test_detect_pod_security_issues_flags_privileged_container():
    pod = _pod("obs", "backend-1", containers=[_container(security_context={"privileged": True})])

    issues = detect_pod_security_issues([pod])

    [issue] = [i for i in issues if i["rule"] == "PrivilegedContainer"]
    assert issue["severity"] == "critical"


def test_detect_pod_security_issues_flags_running_as_root_by_default():
    """No securityContext at all - nothing rules out root."""
    pod = _pod("obs", "backend-1", containers=[_container(security_context=None)])

    issues = detect_pod_security_issues([pod])

    assert any(i["rule"] == "RunningAsRoot" for i in issues)


def test_detect_pod_security_issues_silent_when_pod_level_run_as_non_root_set():
    """Pod-level securityContext should apply when the container doesn't override it."""
    pod = _pod("obs", "backend-1",
               containers=[_container(security_context=None)],
               spec_extra={"security_context": {"run_as_non_root": True, "run_as_user": 1000}})

    issues = detect_pod_security_issues([pod])

    assert not any(i["rule"] == "RunningAsRoot" for i in issues)


def test_detect_pod_security_issues_flags_dangerous_capabilities():
    pod = _pod("obs", "backend-1", containers=[_container(
        security_context={"capabilities": {"add": ["NET_ADMIN"]}},
    )])

    issues = detect_pod_security_issues([pod])

    [issue] = [i for i in issues if i["rule"] == "DangerousCapabilities"]
    assert issue["severity"] == "critical"
    assert "NET_ADMIN" in issue["message"]


def test_detect_pod_security_issues_flags_host_namespaces_and_hostpath():
    pod = _pod("obs", "backend-1", spec_extra={
        "host_network": True, "host_pid": True, "host_ipc": True,
        "volumes": [{"name": "data", "host_path": {"path": "/var/lib/data"}}],
    })

    issues = detect_pod_security_issues([pod])

    rules = {i["rule"] for i in issues}
    assert {"HostNetwork", "HostPID", "HostIPC", "HostPathVolume"} <= rules


def test_detect_pod_security_issues_flags_default_service_account_with_automount():
    pod = _pod("obs", "backend-1")  # no service_account_name, no automount override

    issues = detect_pod_security_issues([pod])

    [issue] = [i for i in issues if i["rule"] == "ServiceAccountAnalysis"]
    assert issue["severity"] == "info"


def test_detect_pod_security_issues_silent_when_automount_explicitly_disabled():
    pod = _pod("obs", "backend-1", spec_extra={"automount_service_account_token": False})

    issues = detect_pod_security_issues([pod])

    assert not any(i["rule"] == "ServiceAccountAnalysis" for i in issues)


def test_detect_pod_security_issues_flags_missing_requests_and_limits_separately():
    pod = _pod("obs", "backend-1", containers=[_container(resources={})])

    issues = detect_pod_security_issues([pod])

    rules = {i["rule"]: i["severity"] for i in issues}
    assert rules["MissingResourceRequests"] == "warning"
    assert rules["MissingResourceLimits"] == "info"


def test_detect_pod_security_issues_flags_unpinned_image_but_not_pinned_registry_port():
    unpinned = _pod("obs", "a", containers=[_container(image="myrepo/app")])
    latest = _pod("obs", "b", containers=[_container(image="myrepo/app:latest")])
    pinned_with_registry_port = _pod("obs", "c", containers=[_container(image="192.168.56.20:5000/cluster-stats:v6")])
    digest_pinned = _pod("obs", "d", containers=[_container(image="myrepo/app@sha256:" + "a" * 64)])

    assert any(i["rule"] == "ImageSecurityIssues" for i in detect_pod_security_issues([unpinned]))
    assert any(i["rule"] == "ImageSecurityIssues" for i in detect_pod_security_issues([latest]))
    assert not any(i["rule"] == "ImageSecurityIssues" for i in detect_pod_security_issues([pinned_with_registry_port]))
    assert not any(i["rule"] == "ImageSecurityIssues" for i in detect_pod_security_issues([digest_pinned]))


def test_detect_pod_security_issues_flags_missing_probes():
    pod = _pod("obs", "backend-1", containers=[_container(liveness_probe=False, readiness_probe=False, startup_probe=False)])

    issues = detect_pod_security_issues([pod])

    rules = {i["rule"]: i["severity"] for i in issues}
    assert rules["MissingLivenessProbe"] == "warning"
    assert rules["MissingReadinessProbe"] == "warning"
    assert rules["MissingStartupProbe"] == "info"


def _deployment(namespace, name, replicas, labels=None):
    return {
        "metadata": {"namespace": namespace, "name": name},
        "spec": {"replicas": replicas, "template": {"metadata": {"labels": labels or {"app": name}}}},
    }


def _pdb(namespace, match_labels):
    return {"metadata": {"namespace": namespace}, "spec": {"selector": {"match_labels": match_labels}}}


def _hpa(namespace, kind, name):
    return {"metadata": {"namespace": namespace}, "spec": {"scale_target_ref": {"kind": kind, "name": name}}}


def test_detect_workload_best_practice_issues_flags_single_replica():
    deployments = [_deployment("obs", "backend", replicas=1)]

    issues = detect_workload_best_practice_issues(deployments, [], [], [])

    assert any(i["rule"] == "SingleReplicaWorkload" for i in issues)


def test_detect_workload_best_practice_issues_flags_missing_pdb_only_at_2plus_replicas():
    deployments = [_deployment("obs", "backend", replicas=3)]

    issues = detect_workload_best_practice_issues(deployments, [], [], [])

    assert any(i["rule"] == "MissingPDB" for i in issues)


def test_detect_workload_best_practice_issues_silent_when_pdb_covers_it():
    deployments = [_deployment("obs", "backend", replicas=3, labels={"app": "backend"})]
    pdbs = [_pdb("obs", {"app": "backend"})]
    hpas = [_hpa("obs", "Deployment", "backend")]

    issues = detect_workload_best_practice_issues(deployments, [], pdbs, hpas)

    assert not any(i["rule"] in ("MissingPDB", "MissingHPA") for i in issues)


def test_detect_workload_best_practice_issues_flags_missing_hpa():
    deployments = [_deployment("obs", "backend", replicas=3, labels={"app": "backend"})]

    issues = detect_workload_best_practice_issues(deployments, [], [], [])

    assert any(i["rule"] == "MissingHPA" for i in issues)


def test_detect_missing_networkpolicy_issues_flags_namespace_without_one():
    pods = [{"metadata": {"namespace": "obs"}}]

    issues = detect_missing_networkpolicy_issues(pods, [])

    assert len(issues) == 1
    assert issues[0]["rule"] == "MissingNetworkPolicy"
    assert issues[0]["namespace"] == "obs"


def test_detect_missing_networkpolicy_issues_silent_when_covered():
    pods = [{"metadata": {"namespace": "obs"}}]
    networkpolicies = [{"metadata": {"namespace": "obs", "name": "default-deny"}}]

    assert detect_missing_networkpolicy_issues(pods, networkpolicies) == []


def test_detect_missing_networkpolicy_issues_skips_system_namespaces():
    pods = [{"metadata": {"namespace": "kube-system"}}]

    assert detect_missing_networkpolicy_issues(pods, []) == []
