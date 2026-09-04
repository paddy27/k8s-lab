const SEVERITY_STYLES = {
  critical: "border-l-red-500 bg-red-950/20",
  warning: "border-l-amber-500 bg-amber-950/10",
};

const SEVERITY_BADGE = {
  critical: "bg-red-500/15 text-red-400",
  warning: "bg-amber-500/15 text-amber-400",
};

function timeAgo(iso) {
  if (!iso) return "-";
  const seconds = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export default function IssuesList({ issues, filters, onFiltersChange }) {
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-3 text-sm">
        <select
          className="rounded border border-slate-700 bg-slate-900 px-2 py-1"
          value={filters.severity}
          onChange={(e) => onFiltersChange({ ...filters, severity: e.target.value })}
        >
          <option value="">All severities</option>
          <option value="critical">Critical only</option>
          <option value="warning">Warning only</option>
        </select>
        <label className="flex items-center gap-1.5 text-slate-400">
          <input
            type="checkbox"
            checked={filters.active}
            onChange={(e) => onFiltersChange({ ...filters, active: e.target.checked })}
          />
          Active only
        </label>
        <input
          className="rounded border border-slate-700 bg-slate-900 px-2 py-1"
          placeholder="Filter by namespace..."
          value={filters.namespace}
          onChange={(e) => onFiltersChange({ ...filters, namespace: e.target.value })}
        />
      </div>

      {issues.length === 0 ? (
        <div className="rounded-lg border border-slate-800 bg-slate-900 p-6 text-center text-sm italic text-slate-500">
          No issues match these filters.
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {issues.map((issue) => (
            <div
              key={issue.id}
              className={`rounded-lg border border-slate-800 border-l-4 p-3 ${SEVERITY_STYLES[issue.severity] ?? ""}`}
            >
              <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400">
                <span className={`rounded-full px-2 py-0.5 font-semibold ${SEVERITY_BADGE[issue.severity] ?? ""}`}>
                  {issue.severity}
                </span>
                <span className="font-mono">{issue.rule}</span>
                <span>
                  {issue.namespace ? `${issue.namespace}/` : ""}
                  {issue.resource_kind}/{issue.resource_name}
                </span>
                {!issue.active && (
                  <span className="rounded-full bg-slate-700/50 px-2 py-0.5 text-slate-400">resolved</span>
                )}
                <span className="ml-auto">last seen {timeAgo(issue.last_seen)}</span>
              </div>
              <div className="mt-1 text-sm text-slate-200">{issue.message}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
