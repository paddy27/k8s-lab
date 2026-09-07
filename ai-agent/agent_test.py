#!/usr/bin/env python3
"""First tool-calling agent loop - one tool, one question, full round-trip.

This is the exact mechanic every future tool reuses:
1. Send the user's question + tool definitions to Ollama.
2. If the model responds with tool_calls instead of an answer, actually
   execute them (an HTTP GET against cluster-monitor, via the gateway -
   this VM isn't a cluster member, so it reaches cluster-monitor the
   same way a browser does: 192.168.56.11:30090, not a ClusterIP).
3. Feed each tool's result back as a "tool" role message.
4. Ask again - now the model has real data and should answer in plain
   English instead of guessing.

Stdlib only (json + urllib) - nothing to pip install on the VM.
"""
import json
import sys
import urllib.request

OLLAMA_URL = "http://localhost:11434/api/chat"
GATEWAY_URL = "http://192.168.56.11:30090"
MODEL = "qwen2.5:3b-instruct"

# Added after the first test run got two things wrong: it read
# "restarts": 1 as "no restarts", and called 6.4% CPU "quite high". Both
# were misreadings of numbers it already had, not missing data - so the
# fix is telling it how to handle numbers, not giving it more of them.
SYSTEM_PROMPT = (
    "You are a Kubernetes cluster status assistant. Answer only using data "
    "returned by tools - never guess or add facts not present in the tool "
    "results. Report every number (a percentage, a count, a restart count) "
    "exactly as given - do not round it, and do not describe it as \"high\", "
    "\"low\", or \"concerning\" unless the data itself labels it that way "
    "(e.g. a severity field). If a field says \"restarts\": 1, say \"1 "
    "restart\", never \"no restarts\"."
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_cluster_health",
            "description": "Get the overall Kubernetes cluster health score, control plane status, resource saturation, and active risk factors.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_resource_optimization",
            "description": "Get resource right-sizing recommendations for the cluster - which containers are CPU/memory over-provisioned, under-provisioned, or practically unused, and the estimated monthly cost savings from fixing them.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_active_issues",
            "description": "Get every currently active issue detected in the cluster (crashes, misconfigurations, security findings, capacity predictions, etc.), with severity and namespace/resource.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_incident_analysis",
            "description": "Root cause analysis for pods that are actively CrashLoopBackOff, OOMKilled, or ImagePullBackOff right now - a timeline plus evidence-weighted likely causes and a recommended action.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_predictions",
            "description": "Forecasts for the cluster - estimated days until cluster-wide CPU/memory capacity is exhausted at current growth rate, and the pod-count growth trend.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_hpa_status",
            "description": "Get every HorizontalPodAutoscaler in the cluster - min/max/current/desired replica counts.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_cost_optimization",
            "description": "Get idle or underutilized nodes in the cluster - candidates for scaling down.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_node_status",
            "description": "Get per-node health and resource usage - readiness, CPU/memory used vs. allocatable, kubelet version.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_events",
            "description": "Get raw Kubernetes events from the last hour - counts by severity, top reasons, and the most recent critical/warning events.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


def call_ollama(messages: list[dict]) -> dict:
    body = json.dumps({"model": MODEL, "messages": messages, "tools": TOOLS, "stream": False}).encode()
    req = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def get_cluster_health() -> dict:
    with urllib.request.urlopen(f"{GATEWAY_URL}/monitor/api/cluster/health") as resp:
        return json.load(resp)


def get_resource_optimization() -> dict:
    # Note the path prefix: this one lives on cluster-stats, not
    # cluster-monitor - /stats/ vs /monitor/ through the same gateway.
    with urllib.request.urlopen(f"{GATEWAY_URL}/stats/api/optimization") as resp:
        return json.load(resp)


def get_active_issues() -> list:
    with urllib.request.urlopen(f"{GATEWAY_URL}/monitor/api/issues?active=true") as resp:
        return json.load(resp)


def get_incident_analysis() -> list:
    with urllib.request.urlopen(f"{GATEWAY_URL}/monitor/api/incidents") as resp:
        return json.load(resp)


def get_predictions() -> dict:
    with urllib.request.urlopen(f"{GATEWAY_URL}/monitor/api/predictions") as resp:
        return json.load(resp)


def get_hpa_status() -> list:
    with urllib.request.urlopen(f"{GATEWAY_URL}/stats/api/autoscaling/hpa") as resp:
        return json.load(resp)


def get_cost_optimization() -> dict:
    with urllib.request.urlopen(f"{GATEWAY_URL}/stats/api/cost-optimization") as resp:
        return json.load(resp)


def get_node_status() -> list:
    with urllib.request.urlopen(f"{GATEWAY_URL}/stats/api/nodes") as resp:
        return json.load(resp)


def get_recent_events() -> dict:
    with urllib.request.urlopen(f"{GATEWAY_URL}/monitor/api/events") as resp:
        return json.load(resp)


# One entry per tool declared above - this dict is what turns "the model
# said call get_cluster_health" into an actual function call. Every new
# tool you add later needs an entry here AND in TOOLS above.
TOOL_IMPLEMENTATIONS = {
    "get_cluster_health": get_cluster_health,
    "get_resource_optimization": get_resource_optimization,
    "get_active_issues": get_active_issues,
    "get_incident_analysis": get_incident_analysis,
    "get_predictions": get_predictions,
    "get_hpa_status": get_hpa_status,
    "get_cost_optimization": get_cost_optimization,
    "get_node_status": get_node_status,
    "get_recent_events": get_recent_events,
}


def _fmt_bytes(b: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if b < 1024 or unit == "TiB":
            return f"{b:.0f}{unit}" if unit == "B" else f"{b:.1f}{unit}"
        b /= 1024
    return f"{b:.1f}TiB"


def format_cluster_health(data: dict) -> str:
    """Flatten the raw JSON into plain text before it ever reaches the
    model - the system prompt fixed how it *phrases* things (no more
    editorializing), but it still misread a number sitting inside a
    nested array. Small models are far more reliable reading flat lines
    than correctly indexing into JSON structure while also composing
    prose - this moves that parsing work into tested Python instead."""
    lines = []

    health = data["health_score"]
    lines.append(f"Health score: {health['score']}/100")
    for d in health["deductions"]:
        lines.append(f"  - {d['reason']} (-{d['points']} points)")

    cap = data["cluster_capacity"]
    lines.append(f"Nodes: {cap['nodes_ready']}/{cap['node_count']} ready")
    lines.append(f"Cluster CPU allocatable: {cap['cpu_allocatable_millicores']:.0f}m")
    lines.append(f"Cluster memory allocatable: {_fmt_bytes(cap['memory_allocatable_bytes'])}")

    sat = data["resource_saturation"]
    lines.append(f"CPU used: {sat['cpu_used_pct']}%")
    lines.append(f"Memory used: {sat['memory_used_pct']}%")

    lines.append("Control plane components:")
    for c in data["control_plane_health"]:
        ready = "ready" if c["ready"] else "NOT ready"
        lines.append(f"  - {c['component']} ({c['pod_name']} on {c['node']}): {ready}, {c['restarts']} restart(s)")

    risks = data["risk_factors"]
    if risks:
        lines.append(f"Risk factors ({len(risks)}):")
        for r in risks:
            lines.append(
                f"  - [{r['severity']}] {r['rule']} on {r['namespace']}/{r['resource_kind']}/{r['resource_name']}: {r['message']}"
            )
    else:
        lines.append("Risk factors: none")

    return "\n".join(lines)


def format_resource_optimization(data: dict) -> str:
    lines = []
    savings = data["potential_savings"]
    lines.append(
        f"Potential savings: {savings['cpu_cores']} CPU core(s) + {savings['memory_gib']} GiB memory "
        f"reclaimable (~${savings['estimated_monthly_cost_usd']}/month estimated)"
    )

    def _bucket(label: str, key: str) -> None:
        items = data[key]
        lines.append(f"{label} ({len(items)}):")
        if not items:
            lines.append("  - none")
            return
        for r in items[:10]:  # cap so this can't explode on a bigger cluster
            lines.append(
                f"  - {r['namespace']}/{r['target_kind']}/{r['target_name']} ({r['container']}): "
                f"CPU {r['cpu_request_millicores']:.0f}m requested -> {r['cpu_recommended_millicores']:.0f}m recommended, "
                f"Mem {_fmt_bytes(r['memory_request_bytes'])} requested -> {_fmt_bytes(r['memory_recommended_bytes'])} recommended"
            )
        if len(items) > 10:
            lines.append(f"  - ...and {len(items) - 10} more, truncated")

    _bucket("CPU over-provisioned", "cpu_over_provisioned")
    _bucket("Memory over-provisioned", "memory_over_provisioned")
    _bucket("Under-provisioned (risk of throttling/OOM)", "under_provisioned")
    _bucket("Practically unused", "unused_resources")

    return "\n".join(lines)


def format_active_issues(data: list) -> str:
    if not data:
        return "No active issues - the cluster is clean right now."
    critical = [i for i in data if i["severity"] == "critical"]
    warning = [i for i in data if i["severity"] == "warning"]
    lines = [f"Active issues: {len(critical)} critical, {len(warning)} warning ({len(data)} total)"]
    shown = (critical + warning)[:15]
    for i in shown:
        lines.append(
            f"  - [{i['severity']}] {i['rule']} on {i['namespace']}/{i['resource_kind']}/{i['resource_name']}: "
            f"{i['message']} (last seen {i['last_seen']})"
        )
    if len(data) > len(shown):
        lines.append(f"  - ...and {len(data) - len(shown)} more, truncated")
    return "\n".join(lines)


def format_incident_analysis(data: list) -> str:
    if not data:
        return "No active incidents right now - no pod is CrashLoopBackOff, OOMKilled, or ImagePullBackOff."
    lines = [f"{len(data)} active incident(s):"]
    for inc in data:
        lines.append(f"\nPod {inc['namespace']}/{inc['pod_name']}:")
        lines.append("  Timeline:")
        for t in inc["timeline"][-5:]:  # most recent few - a long history isn't the point here
            lines.append(f"    {t['time']}: {t['label']}")
        if inc["root_causes"]:
            lines.append("  Root cause probabilities:")
            for rc in inc["root_causes"]:
                lines.append(f"    - {rc['category']}: {rc['probability_pct']}% ({'; '.join(rc['evidence'])})")
        else:
            lines.append("  Root cause: not enough correlating signal yet")
        lines.append(f"  Recommended action: {inc['recommended_action']}")
    return "\n".join(lines)


def format_predictions(data: dict) -> str:
    forecast = data["cluster_capacity_forecast"]
    trend = data["pod_growth_trend"]

    def _days(v):
        return "no upward trend detected, or not enough history yet" if v is None else f"{v:.1f} day(s)"

    lines = [
        f"Cluster-wide CPU capacity forecast: {_days(forecast['cpu_days_to_exhaustion'])}",
        f"Cluster-wide memory capacity forecast: {_days(forecast['memory_days_to_exhaustion'])}",
    ]
    pods_per_day = trend["pods_per_day"]
    lines.append(
        "Pod count trend: not enough history yet" if pods_per_day is None else f"Pod count trend: {pods_per_day:+.2f} pods/day"
    )
    return "\n".join(lines)


def format_hpa_status(data: list) -> str:
    if not data:
        return "No HorizontalPodAutoscalers exist in the cluster."
    lines = [f"{len(data)} HPA(s):"]
    for h in data:
        lines.append(
            f"  - {h['namespace']}/{h['name']} (target {h['target_kind']}/{h['target_name']}): "
            f"min {h['min_replicas']}, max {h['max_replicas']}, current {h['current_replicas']}, desired {h['desired_replicas']}"
        )
    return "\n".join(lines)


def format_cost_optimization(data: dict) -> str:
    idle, underutilized = data["idle_nodes"], data["underutilized_nodes"]
    if not idle and not underutilized:
        return "No idle or underutilized nodes - every node is carrying a reasonable share of load."
    lines = []
    if idle:
        lines.append(f"Idle nodes ({len(idle)}):")
        for n in idle:
            lines.append(f"  - {n['name']}: CPU {n['cpu_used_pct']}%, Memory {n['memory_used_pct']}%")
    if underutilized:
        lines.append(f"Underutilized nodes ({len(underutilized)}):")
        for n in underutilized:
            lines.append(f"  - {n['name']}: CPU {n['cpu_used_pct']}%, Memory {n['memory_used_pct']}%")
    return "\n".join(lines)


def format_node_status(data: list) -> str:
    lines = [f"{len(data)} node(s):"]
    for n in data:
        ready = "Ready" if n["ready"] else "NOT Ready"
        lines.append(
            f"  - {n['name']} ({', '.join(n['roles'])}): {ready}, "
            f"CPU {n['cpu_used_pct']}% ({n['cpu_used_millicores']:.0f}m / {n['cpu_allocatable_millicores']:.0f}m), "
            f"Memory {n['memory_used_pct']}% ({_fmt_bytes(n['memory_used_bytes'])} / {_fmt_bytes(n['memory_allocatable_bytes'])}), "
            f"kubelet {n['kubelet_version']}"
        )
    return "\n".join(lines)


def format_recent_events(data: dict) -> str:
    summary = data["summary"]
    lines = [f"Events in the last hour: {summary['critical']} critical, {summary['warning']} warning, {summary['normal']} normal"]
    if summary["top_reasons"]:
        lines.append("Top reasons:")
        for r in summary["top_reasons"]:
            lines.append(f"  - {r['reason']}: {r['count']}")
    notable = [e for e in data["events"] if e["severity"] in ("critical", "warning")][:10]
    if notable:
        lines.append("Most recent critical/warning events:")
        for e in notable:
            lines.append(
                f"  - [{e['severity']}] {e['reason']} on {e['namespace']}/{e['involved_kind']}/{e['involved_name']}: "
                f"{e['message']} (last seen {e['last_seen']})"
            )
    return "\n".join(lines)


# Optional per-tool formatter: raw JSON in, flat text out. A tool with
# no entry here just falls back to json.dumps - not every tool result
# needs this treatment, only ones with nested structure a small model
# is likely to misread.
TOOL_FORMATTERS = {
    "get_cluster_health": format_cluster_health,
    "get_resource_optimization": format_resource_optimization,
    "get_active_issues": format_active_issues,
    "get_incident_analysis": format_incident_analysis,
    "get_predictions": format_predictions,
    "get_hpa_status": format_hpa_status,
    "get_cost_optimization": format_cost_optimization,
    "get_node_status": format_node_status,
    "get_recent_events": format_recent_events,
}


def main() -> None:
    # Two tools now, so the interesting question is whether the model
    # picks the *right* one - pass your own on the command line to test
    # that, e.g.:
    #   python3 agent_test.py "Are there any pods I should resize to save resources?"
    question = sys.argv[1] if len(sys.argv) > 1 else "Is my kubernetes cluster healthy right now?"
    print(f"Question: {question}\n")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]

    response = call_ollama(messages)
    message = response["message"]

    tool_calls = message.get("tool_calls")
    if not tool_calls:
        print("Model answered directly, without calling a tool:")
        print(message["content"])
        return

    messages.append(message)
    # A real turn can request several tools at once - loop over all of
    # them, not just the first, even though there's only one right now.
    for call in tool_calls:
        name = call["function"]["name"]
        print(f"-> model called: {name}({call['function']['arguments']})")
        result = TOOL_IMPLEMENTATIONS[name]()
        formatted = TOOL_FORMATTERS.get(name, json.dumps)(result)
        print(f"\n--- what the model actually sees for {name} ---\n{formatted}\n---")
        messages.append({"role": "tool", "content": formatted})

    final = call_ollama(messages)
    print("\nFinal answer:")
    print(final["message"]["content"])


if __name__ == "__main__":
    main()
