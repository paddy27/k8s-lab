"""Networking Analysis (beyond the original Top 5).

Covers what's genuinely derivable from the K8s API without a live
network probe:

- **Service/Endpoint health** - an Endpoints object's own ready/
  not-ready address counts already say exactly what the plan doc's
  example asks for ("Service `payment-service` has 5 desired pods, but
  only 3 healthy endpoints are available") - that IS
  `not_ready_addresses`, no pod-selector matching needed (Kubernetes
  itself already did that work to populate the Endpoints object).
- **DNS health** - CoreDNS's own Deployment readiness. If that's at 0
  ready replicas, every Service-name lookup in the cluster is affected,
  not just one workload - worth its own critical signal distinct from
  CoreDNS just happening to also trip the generic per-pod checks.
- **Ingress / LoadBalancer readiness** - whether either has actually
  been assigned an address yet.

Deliberately NOT built: **Connection Errors** (already covered by the
existing `Event:<reason>` catch-all in detector.py plus
root_cause.py's connectivity-marker evidence - a dedicated rule here
would just duplicate that, same reasoning Storage Analysis gave for
skipping VolumeAttachmentIssues) and **Network Latency** (needs a live
probe - ping/curl between pods, a service mesh, or synthetic
monitoring - none of which exist in this lab or are derivable from the
K8s API alone).

Pure functions over plain dicts, same philosophy as every other
analysis module here.
"""
from __future__ import annotations

from app.detector import _issue

DNS_DEPLOYMENT_NAMESPACE = "kube-system"
DNS_DEPLOYMENT_NAME = "coredns"


def _service_has_selector(service: dict) -> bool:
    return bool(service.get("spec", {}).get("selector"))


def build_service_endpoint_issues(services: list[dict], endpoints: list[dict]) -> list[dict]:
    """Endpoints objects are named identically to the Service they back
    (Kubernetes' own convention) and only exist for selector-based
    Services - a headless/ExternalName Service, or one with manually
    managed endpoints, has nothing to check here."""
    endpoints_by_key = {(e["metadata"]["namespace"], e["metadata"]["name"]): e for e in endpoints}
    issues = []
    for svc in services:
        if not _service_has_selector(svc):
            continue
        namespace, name = svc["metadata"]["namespace"], svc["metadata"]["name"]
        ep = endpoints_by_key.get((namespace, name))
        subsets = (ep or {}).get("subsets") or []
        ready = sum(len(s.get("addresses") or []) for s in subsets)
        not_ready = sum(len(s.get("not_ready_addresses") or []) for s in subsets)
        total = ready + not_ready

        if total == 0:
            issues.append(_issue(
                "ServiceWithNoEndpoints", "critical", namespace, "Service", name,
                "Has a selector but zero endpoints - nothing is currently backing this Service "
                "(check the selector matches running pods, and that they're passing readiness).",
            ))
        elif not_ready > 0:
            issues.append(_issue(
                "EndpointAvailabilityDegraded", "warning", namespace, "Service", name,
                f"{total} desired pod(s), but only {ready} healthy endpoint(s) are available "
                f"({not_ready} not ready).",
            ))
    return issues


def build_dns_health_issues(deployments: list[dict]) -> list[dict]:
    for d in deployments:
        if d["metadata"]["namespace"] == DNS_DEPLOYMENT_NAMESPACE and d["metadata"]["name"] == DNS_DEPLOYMENT_NAME:
            desired = d.get("spec", {}).get("replicas") or 0
            ready = d.get("status", {}).get("ready_replicas") or 0
            if desired > 0 and ready == 0:
                return [_issue(
                    "ClusterDNSDown", "critical", DNS_DEPLOYMENT_NAMESPACE, "Deployment", DNS_DEPLOYMENT_NAME,
                    "CoreDNS has 0 ready replicas - cluster-wide DNS resolution is down, affecting every "
                    "Service-name lookup in the cluster.",
                )]
            break
    return []


def build_ingress_issues(ingresses: list[dict]) -> list[dict]:
    issues = []
    for ing in ingresses:
        lb_ingress = (ing.get("status", {}).get("load_balancer") or {}).get("ingress") or []
        if not lb_ingress:
            issues.append(_issue(
                "IngressNotReady", "warning", ing["metadata"]["namespace"], "Ingress", ing["metadata"]["name"],
                "No address assigned yet - check that an Ingress controller is installed and watching this class.",
            ))
    return issues


def build_loadbalancer_issues(services: list[dict]) -> list[dict]:
    issues = []
    for svc in services:
        if svc.get("spec", {}).get("type") != "LoadBalancer":
            continue
        lb_ingress = (svc.get("status", {}).get("load_balancer") or {}).get("ingress") or []
        if not lb_ingress:
            issues.append(_issue(
                "LoadBalancerPending", "warning", svc["metadata"]["namespace"], "Service", svc["metadata"]["name"],
                "type: LoadBalancer but no external address assigned yet - check that a LoadBalancer "
                "provisioner (cloud controller manager, MetalLB, ...) is running.",
            ))
    return issues


def build_all_networking_issues(
    services: list[dict], endpoints: list[dict], deployments: list[dict], ingresses: list[dict],
) -> list[dict]:
    return [
        *build_service_endpoint_issues(services, endpoints),
        *build_dns_health_issues(deployments),
        *build_ingress_issues(ingresses),
        *build_loadbalancer_issues(services),
    ]
