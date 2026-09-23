// Server API. Keys go only in the body of the request that starts a run.

async function request(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "content-type": "application/json", ...(options.headers || {}) },
  });
  const type = res.headers.get("content-type") || "";
  const body = type.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error(body?.error || `HTTP ${res.status}`);
  return body;
}

let metaCache;
export const api = {
  meta: () => (metaCache ||= request("/api/meta").catch((e) => { metaCache = null; throw e; })),
  preview: (body) => request("/api/preview", { method: "POST", body: JSON.stringify(body) }),
  runs: () => request("/api/runs"),
  run: (id) => request(`/api/runs/${encodeURIComponent(id)}`),
  start: (body) => request("/api/runs", { method: "POST", body: JSON.stringify(body) }),
  cancel: (id) => request(`/api/runs/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  report: (id) => request(`/api/runs/${encodeURIComponent(id)}/report`),
  node: (id, nid) => request(`/api/runs/${encodeURIComponent(id)}/nodes/${encodeURIComponent(nid)}`),
  stream: (id, after) => new EventSource(`/api/runs/${encodeURIComponent(id)}/events?after=${after || 0}`),
};
