from app.networking import (
    build_dns_health_issues,
    build_ingress_issues,
    build_loadbalancer_issues,
    build_service_endpoint_issues,
)


def _service(namespace, name, selector=None, svc_type="ClusterIP", lb_ingress=None):
    return {
        "metadata": {"namespace": namespace, "name": name},
        "spec": {"selector": selector if selector is not None else {"app": name}, "type": svc_type},
        "status": {"load_balancer": {"ingress": lb_ingress}} if lb_ingress is not None else {"load_balancer": {}},
    }


def _endpoints(namespace, name, ready_count=0, not_ready_count=0):
    return {
        "metadata": {"namespace": namespace, "name": name},
        "subsets": [{
            "addresses": [{"ip": f"10.0.0.{i}"} for i in range(ready_count)],
            "not_ready_addresses": [{"ip": f"10.0.1.{i}"} for i in range(not_ready_count)],
        }] if (ready_count or not_ready_count) else None,
    }


def test_build_service_endpoint_issues_flags_no_endpoints():
    services = [_service("obs", "backend")]
    endpoints = [_endpoints("obs", "backend", ready_count=0, not_ready_count=0)]

    issues = build_service_endpoint_issues(services, endpoints)

    assert len(issues) == 1
    assert issues[0]["rule"] == "ServiceWithNoEndpoints"
    assert issues[0]["severity"] == "critical"


def test_build_service_endpoint_issues_flags_missing_endpoints_object_entirely():
    """A Service can exist with no corresponding Endpoints object at all
    (e.g. right after creation) - same "no endpoints" signal."""
    services = [_service("obs", "backend")]

    issues = build_service_endpoint_issues(services, endpoints=[])

    assert issues[0]["rule"] == "ServiceWithNoEndpoints"


def test_build_service_endpoint_issues_flags_degraded_availability():
    services = [_service("obs", "payment-service")]
    endpoints = [_endpoints("obs", "payment-service", ready_count=3, not_ready_count=2)]

    issues = build_service_endpoint_issues(services, endpoints)

    assert len(issues) == 1
    assert issues[0]["rule"] == "EndpointAvailabilityDegraded"
    assert issues[0]["severity"] == "warning"
    assert "5 desired pod(s), but only 3 healthy" in issues[0]["message"]


def test_build_service_endpoint_issues_silent_when_fully_healthy():
    services = [_service("obs", "backend")]
    endpoints = [_endpoints("obs", "backend", ready_count=3, not_ready_count=0)]

    assert build_service_endpoint_issues(services, endpoints) == []


def test_build_service_endpoint_issues_skips_services_without_a_selector():
    """Headless/ExternalName Services, or the built-in "kubernetes"
    Service in default (manually-managed endpoints) - nothing to check."""
    services = [_service("default", "kubernetes", selector={})]

    assert build_service_endpoint_issues(services, endpoints=[]) == []


def _coredns_deployment(desired=2, ready=2):
    return {
        "metadata": {"namespace": "kube-system", "name": "coredns"},
        "spec": {"replicas": desired},
        "status": {"ready_replicas": ready},
    }


def test_build_dns_health_issues_flags_coredns_fully_down():
    deployments = [_coredns_deployment(desired=2, ready=0)]

    issues = build_dns_health_issues(deployments)

    assert len(issues) == 1
    assert issues[0]["rule"] == "ClusterDNSDown"
    assert issues[0]["severity"] == "critical"


def test_build_dns_health_issues_silent_when_coredns_healthy():
    deployments = [_coredns_deployment(desired=2, ready=1)]  # degraded but not fully down

    assert build_dns_health_issues(deployments) == []


def test_build_dns_health_issues_silent_when_coredns_not_found():
    assert build_dns_health_issues([]) == []


def test_build_ingress_issues_flags_unready_ingress():
    ingresses = [{"metadata": {"namespace": "obs", "name": "web"}, "status": {"load_balancer": {"ingress": []}}}]

    issues = build_ingress_issues(ingresses)

    assert issues[0]["rule"] == "IngressNotReady"


def test_build_ingress_issues_silent_when_address_assigned():
    ingresses = [{"metadata": {"namespace": "obs", "name": "web"}, "status": {"load_balancer": {"ingress": [{"ip": "1.2.3.4"}]}}}]

    assert build_ingress_issues(ingresses) == []


def test_build_ingress_issues_empty_list_is_fine():
    """No Ingress controller installed in this lab - zero Ingress
    objects should mean zero issues, not an error."""
    assert build_ingress_issues([]) == []


def test_build_loadbalancer_issues_flags_pending_lb():
    services = [_service("obs", "frontend", svc_type="LoadBalancer", lb_ingress=[])]

    issues = build_loadbalancer_issues(services)

    assert issues[0]["rule"] == "LoadBalancerPending"


def test_build_loadbalancer_issues_ignores_non_loadbalancer_services():
    services = [_service("obs", "backend", svc_type="ClusterIP")]

    assert build_loadbalancer_issues(services) == []


def test_build_loadbalancer_issues_silent_when_address_assigned():
    services = [_service("obs", "frontend", svc_type="LoadBalancer", lb_ingress=[{"ip": "1.2.3.4"}])]

    assert build_loadbalancer_issues(services) == []
