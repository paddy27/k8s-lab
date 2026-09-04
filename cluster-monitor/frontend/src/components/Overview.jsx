function Card({ label, value, sub, accent }) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900 p-4">
      <div className="text-xs text-slate-400">{label}</div>
      <div className={`mt-1 text-2xl font-semibold ${accent ?? "text-slate-100"}`}>{value}</div>
      {sub && <div className="mt-1 text-xs text-slate-500">{sub}</div>}
    </div>
  );
}

export default function Overview({ cluster, issuesSummary }) {
  if (!cluster || !issuesSummary) {
    return <div className="text-sm text-slate-500 italic">Loading...</div>;
  }

  const phaseText = Object.entries(cluster.pods_by_phase)
    .map(([k, v]) => `${k}: ${v}`)
    .join(", ");

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
      <Card label="Nodes Ready" value={`${cluster.nodes_ready}/${cluster.node_count}`} />
      <Card label="Pods" value={cluster.pod_count} sub={phaseText} />
      <Card
        label="Critical Issues"
        value={issuesSummary.critical}
        accent={issuesSummary.critical > 0 ? "text-red-400" : "text-emerald-400"}
      />
      <Card
        label="Warning Issues"
        value={issuesSummary.warning}
        accent={issuesSummary.warning > 0 ? "text-amber-400" : "text-emerald-400"}
      />
      <Card label="Total Active Issues" value={issuesSummary.total_active} />
    </div>
  );
}
