import { useCallback, useEffect, useState } from "react";
import { getClusterSummary, getIssues, getIssuesSummary } from "./api.js";
import IssuesList from "./components/IssuesList.jsx";
import Overview from "./components/Overview.jsx";

const REFRESH_MS = 15000;

export default function App() {
  const [cluster, setCluster] = useState(null);
  const [issuesSummary, setIssuesSummary] = useState(null);
  const [issues, setIssues] = useState([]);
  const [filters, setFilters] = useState({ severity: "", active: true, namespace: "" });
  const [updatedAt, setUpdatedAt] = useState(null);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const [clusterData, summaryData, issuesData] = await Promise.all([
        getClusterSummary(),
        getIssuesSummary(),
        getIssues(filters),
      ]);
      setCluster(clusterData);
      setIssuesSummary(summaryData);
      setIssues(issuesData);
      setUpdatedAt(new Date());
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  }, [filters]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, REFRESH_MS);
    return () => clearInterval(id);
  }, [refresh]);

  return (
    <div className="mx-auto max-w-6xl px-6 py-6">
      <header className="mb-6 flex items-baseline gap-3">
        <h1 className="text-lg font-semibold">cluster-monitor</h1>
        <span className="text-xs text-slate-500">k8s-lab</span>
        <span className="ml-auto text-xs text-slate-500">
          {error ? <span className="text-red-400">error: {error}</span> : updatedAt && `updated ${updatedAt.toLocaleTimeString()}`}
        </span>
      </header>

      <section className="mb-8">
        <h2 className="mb-3 text-xs font-medium uppercase tracking-wide text-slate-500">Overview</h2>
        <Overview cluster={cluster} issuesSummary={issuesSummary} />
      </section>

      <section>
        <h2 className="mb-3 text-xs font-medium uppercase tracking-wide text-slate-500">Issues</h2>
        <IssuesList issues={issues} filters={filters} onFiltersChange={setFilters} />
      </section>
    </div>
  );
}
