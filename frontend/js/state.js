/* URL-hash router: every filter lives in the hash so views are shareable and
   browser back/forward works. The hash is the single source of truth. */

const EMPTY_FILTERS = { mode: "all", from: null, to: null, commits: [], authors: [] };

function numOrNull(v) {
    if (v == null || v === "") return null;
    const n = parseInt(v, 10);
    return Number.isNaN(n) ? null : n;
}

export function parseHash(hash = location.hash) {
    const h = (hash || "").replace(/^#\/?/, "");
    const [pathPart, query] = h.split("?");
    const seg = pathPart.split("/").filter(Boolean);
    const page = seg[0] === "dash" || seg[0] === "authors" ? seg[0] : "repos";
    const repoId = seg[1] ? parseInt(seg[1], 10) || null : null;
    const params = new URLSearchParams(query || "");
    const filters = {
        mode: ["all", "range", "list"].includes(params.get("mode")) ? params.get("mode") : "all",
        from: numOrNull(params.get("from")),
        to: numOrNull(params.get("to")),
        commits: (params.get("commits") || "").split(",").filter(Boolean),
        authors: (params.get("authors") || "")
            .split(",").map(Number).filter((n) => Number.isInteger(n) && n > 0),
    };
    if (filters.mode === "range" && filters.from == null && filters.to == null) filters.mode = "all";
    if (filters.mode === "list" && filters.commits.length === 0) filters.mode = "all";
    return { page, repoId, path: params.get("path") || "", filters };
}

export function routeHash(r) {
    if (r.page === "repos" || !r.repoId) return "#/repos";
    const q = new URLSearchParams();
    if (r.path) q.set("path", r.path);
    const f = r.filters || EMPTY_FILTERS;
    if (f.mode && f.mode !== "all") q.set("mode", f.mode);
    if (f.mode === "range") {
        if (f.from != null) q.set("from", f.from);
        if (f.to != null) q.set("to", f.to);
    }
    if (f.mode === "list" && f.commits.length) q.set("commits", f.commits.join(","));
    if (f.authors.length) q.set("authors", f.authors.join(","));
    const qs = q.toString();
    return `#/${r.page}/${r.repoId}${qs ? "?" + qs : ""}`;
}

let current = parseHash();

export function route() {
    return current;
}

/* Merge `partial` into the current route (filters merge one level deep) and
   navigate. Returns false when nothing changed. */
export function setRoute(partial) {
    const next = {
        ...current,
        ...partial,
        filters: { ...current.filters, ...(partial.filters || {}) },
    };
    if (!next.repoId && next.page !== "repos") next.page = "repos";
    const hash = routeHash(next);
    if (hash === location.hash) {
        current = next;
        return false;
    }
    location.hash = hash; // hashchange handler calls refresh() + re-render
    return true;
}

/* Re-read the hash (called by the hashchange listener). */
export function refresh() {
    current = parseHash();
    return current;
}

/* API query string for the metric endpoints. `pathOverride` lets the file
   drawer fetch a different object than the one currently drilled into. */
export function queryFromRoute(r = current, pathOverride = null) {
    const q = new URLSearchParams();
    q.set("path", pathOverride !== null ? pathOverride : r.path || "");
    const f = r.filters;
    if (f.mode !== "all") q.set("mode", f.mode);
    if (f.mode === "range") {
        if (f.from != null) q.set("from", f.from);
        if (f.to != null) q.set("to", f.to);
    }
    if (f.mode === "list" && f.commits.length) q.set("commits", f.commits.join(","));
    if (f.authors.length) q.set("authors", f.authors.join(","));
    return "?" + q.toString();
}
