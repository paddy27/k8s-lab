"""Best Practices & Security analysis (Top 5 priority #4).

Static analysis over already-fetched cluster state - no new metrics or
history needed here, unlike Storage/Scheduling above. Pure functions
over plain dicts, same philosophy as detector.py: pods/workloads/PDBs/
HPAs/NetworkPolicies in as plain dicts, plain issue dicts out.

Covers both trees from the plan doc - they were agreed as one combined
priority, and "missing resource requests/limits" was listed under both
anyway, so it's implemented once here rather than twice. Deliberately
does NOT cover Deprecated Kubernetes API detection - see the README:
a real signal for that exists (the apiserver's own
`apiserver_requested_deprecated_apis` metric), but it comes from
scraping the API server's /metrics, a materially different data source
from everything else this app reads.

A known, expected side effect of running this against a real cluster:
infra components genuinely need what these rules flag as risky -
Calico needs `privileged`/`hostNetwork`, kube-proxy needs `hostNetwork`,
etc. That's not a false positive to special-case away; it's the same
signal a real scanner (kube-bench, Polaris, kubesec) would show for the
same pods, and hiding it would just be a different way of getting it
wrong. See the README for how this plays out on this lab's own cluster.
"""
from __future__ import annotations

from app.detector import _issue

_DANGEROUS_CAPABILITIES = {
    "ALL", "SYS_ADMIN", "NET_ADMIN", "SYS_PTRACE", "SYS_MODULE",
    "SYS_RAWIO", "SYS_BOOT", "NET_RAW", "BPF", "PERFMON",
}

_SKIP_NETWORKPOLICY_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease"}


def _effective_security_context(pod: dict, container: dict) -> dict:
    """Container-level securityContext overrides pod-level, field by
    field - not wholesale, since a pod can set some fields at the pod
    level and others per-container."""
    pod_sc = pod.get("spec", {}).get("security_context") or {}
    c_sc = container.get("security_context") or {}
    return {
        "privileged": c_sc.get("privileged"),
        "run_as_non_root": c_sc.get("run_as_non_root", pod_sc.get("run_as_non_root")),
        "run_as_user": c_sc.get("run_as_user", pod_sc.get("run_as_user")),
        "capabilities": c_sc.get("capabilities") or {},
    }


def _is_unpinned_image(image: str) -> bool:
    """True for no tag at all (defaults to :latest) or an explicit
    :latest - false for a pinned tag or a digest pin (@sha256:...).
    Splits on "/" first so a registry:port prefix (e.g.
    "192.168.56.20:5000/cluster-stats:v6") doesn't get mistaken for a
    missing tag."""
    last_segment = image.rsplit("/", 1)[-1]
    if "@" in last_segment:
        return False
    if ":" not in last_segment:
        return True
    return last_segment.rsplit(":", 1)[-1] == "latest"


def detect_pod_security_issues(pods: list[dict]) -> list[dict]:
    issues = []
    for pod in pods:
        namespace = pod["metadata"]["namespace"]
        name = pod["metadata"]["name"]
        spec = pod.get("spec", {})

        if spec.get("host_network"):
            issues.append(_issue(
                "HostNetwork", "warning", namespace, "Pod", name,
                "Uses the host's network namespace - can see/intercept traffic on the node's own interfaces.",
            ))
        if spec.get("host_pid"):
            issues.append(_issue(
                "HostPID", "warning", namespace, "Pod", name,
                "Uses the host's PID namespace - can see and signal every process on the node.",
            ))
        if spec.get("host_ipc"):
            issues.append(_issue(
                "HostIPC", "warning", namespace, "Pod", name,
                "Uses the host's IPC namespace - shares memory/semaphores with every process on the node.",
            ))

        for vol in spec.get("volumes") or []:
            if vol.get("host_path"):
                issues.append(_issue(
                    "HostPathVolume", "warning", namespace, "Pod", name,
                    f"Volume '{vol.get('name')}' mounts a hostPath ({vol['host_path'].get('path')}) - "
                    "direct access to the node's filesystem.",
                ))
                break  # one issue per pod is enough, not one per hostPath volume

        service_account = spec.get("service_account_name") or "default"
        automount = spec.get("automount_service_account_token")
        if service_account == "default" and automount is not False:
            issues.append(_issue(
                "ServiceAccountAnalysis", "info", namespace, "Pod", name,
                "Runs as the default ServiceAccount with token automount not explicitly disabled - "
                "consider a dedicated ServiceAccount with automountServiceAccountToken: false unless "
                "this pod actually needs to call the API server.",
            ))

        for container in spec.get("containers") or []:
            cname = container.get("name")
            sc = _effective_security_context(pod, container)

            if sc["privileged"]:
                issues.append(_issue(
                    "PrivilegedContainer", "critical", namespace, "Pod", name,
                    f"Container '{cname}' runs privileged - full access to the host, equivalent to root on the node.",
                ))

            if sc["run_as_non_root"] is not True and sc["run_as_user"] in (None, 0):
                issues.append(_issue(
                    "RunningAsRoot", "warning", namespace, "Pod", name,
                    f"Container '{cname}' has nothing ruling out root (no runAsNonRoot: true, no non-zero "
                    "runAsUser) - if the image defaults to root, this container runs as root.",
                ))

            added_caps = {c.upper() for c in (sc["capabilities"].get("add") or [])}
            dangerous = added_caps & _DANGEROUS_CAPABILITIES
            if dangerous:
                issues.append(_issue(
                    "DangerousCapabilities", "critical", namespace, "Pod", name,
                    f"Container '{cname}' adds {sorted(dangerous)} - each is a well-known privilege-escalation vector.",
                ))

            resources = container.get("resources") or {}
            if not resources.get("requests"):
                issues.append(_issue(
                    "MissingResourceRequests", "warning", namespace, "Pod", name,
                    f"Container '{cname}' has no resource requests set - can't be reasoned about for "
                    "scheduling, or by the VPA/HPA recommendation engines in cluster-stats.",
                ))
            if not resources.get("limits"):
                issues.append(_issue(
                    "MissingResourceLimits", "info", namespace, "Pod", name,
                    f"Container '{cname}' has no resource limits set - can consume unbounded CPU/memory on its node.",
                ))

            image = container.get("image") or ""
            if image and _is_unpinned_image(image):
                issues.append(_issue(
                    "ImageSecurityIssues", "warning", namespace, "Pod", name,
                    f"Container '{cname}' image '{image}' has no pinned tag (or uses :latest) - "
                    "unpredictable and unauditable what's actually running.",
                ))

            if not container.get("liveness_probe"):
                issues.append(_issue(
                    "MissingLivenessProbe", "warning", namespace, "Pod", name,
                    f"Container '{cname}' has no livenessProbe - kubelet can't detect and restart it if it hangs.",
                ))
            if not container.get("readiness_probe"):
                issues.append(_issue(
                    "MissingReadinessProbe", "warning", namespace, "Pod", name,
                    f"Container '{cname}' has no readinessProbe - it can receive traffic before it's actually ready.",
                ))
            if not container.get("startup_probe"):
                issues.append(_issue(
                    "MissingStartupProbe", "info", namespace, "Pod", name,
                    f"Container '{cname}' has no startupProbe - only worth adding for slow-starting containers.",
                ))

    return issues


def _pod_template_labels(workload_obj: dict) -> dict:
    return workload_obj.get("spec", {}).get("template", {}).get("metadata", {}).get("labels") or {}


def _pdb_covers(pdb: dict, labels: dict) -> bool:
    selector = pdb.get("spec", {}).get("selector", {}).get("match_labels") or {}
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def _hpa_targets(hpas: list[dict], namespace: str, kind: str, name: str) -> bool:
    return any(
        h["metadata"]["namespace"] == namespace
        and (h.get("spec", {}).get("scale_target_ref") or {}).get("kind") == kind
        and (h.get("spec", {}).get("scale_target_ref") or {}).get("name") == name
        for h in hpas
    )


def detect_workload_best_practice_issues(
    deployments: list[dict], statefulsets: list[dict], pdbs: list[dict], hpas: list[dict],
) -> list[dict]:
    issues = []
    for kind, objs in (("Deployment", deployments), ("StatefulSet", statefulsets)):
        for obj in objs:
            namespace = obj["metadata"]["namespace"]
            name = obj["metadata"]["name"]
            replicas = obj.get("spec", {}).get("replicas") or 0
            labels = _pod_template_labels(obj)

            if replicas == 1:
                issues.append(_issue(
                    "SingleReplicaWorkload", "warning", namespace, kind, name,
                    "Only 1 replica - a single pod restart, eviction, or node failure means full downtime.",
                ))

            if replicas >= 2 and not any(
                pdb["metadata"]["namespace"] == namespace and _pdb_covers(pdb, labels) for pdb in pdbs
            ):
                issues.append(_issue(
                    "MissingPDB", "info", namespace, kind, name,
                    f"{replicas} replicas but no PodDisruptionBudget covers it - a node drain/upgrade "
                    "could take all replicas down at once.",
                ))

            if not _hpa_targets(hpas, namespace, kind, name):
                issues.append(_issue(
                    "MissingHPA", "info", namespace, kind, name,
                    "No HorizontalPodAutoscaler targets this workload - replica count is fixed regardless of load.",
                ))

    return issues


def detect_missing_networkpolicy_issues(pods: list[dict], networkpolicies: list[dict]) -> list[dict]:
    """One issue per namespace, not per pod - "this namespace has zero
    NetworkPolicies" is a namespace-level fact, and flagging it per-pod
    would just be the same message repeated once per pod in it."""
    namespaces_with_pods = {p["metadata"]["namespace"] for p in pods} - _SKIP_NETWORKPOLICY_NAMESPACES
    namespaces_with_policy = {np["metadata"]["namespace"] for np in networkpolicies}
    return [
        _issue(
            "MissingNetworkPolicy", "info", ns, "Namespace", ns,
            "No NetworkPolicy in this namespace - every pod can reach every other pod by default.",
        )
        for ns in sorted(namespaces_with_pods - namespaces_with_policy)
    ]


def detect_all_best_practice_issues(
    pods: list[dict],
    deployments: list[dict],
    statefulsets: list[dict],
    pdbs: list[dict],
    hpas: list[dict],
    networkpolicies: list[dict],
) -> list[dict]:
    return [
        *detect_pod_security_issues(pods),
        *detect_workload_best_practice_issues(deployments, statefulsets, pdbs, hpas),
        *detect_missing_networkpolicy_issues(pods, networkpolicies),
    ]
