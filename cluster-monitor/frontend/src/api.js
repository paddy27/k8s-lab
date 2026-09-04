async function getJSON(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

function withQuery(path, params) {
  const qs = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== "" && v !== null && v !== undefined)
  );
  const s = qs.toString();
  return s ? `${path}?${s}` : path;
}

// Relative (no leading "/") so these resolve against <base href="/monitor/">
// in index.html rather than the gateway's root - see the comment there.
export const getClusterSummary = () => getJSON("api/cluster/summary");
export const getIssuesSummary = () => getJSON("api/issues/summary");
export const getIssues = (filters) => getJSON(withQuery("api/issues", filters));
