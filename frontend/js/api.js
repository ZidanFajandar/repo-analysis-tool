/* Thin fetch wrappers around the REST API. Every failure surfaces as ApiError. */

export class ApiError extends Error {
    constructor(detail, status) {
        super(detail);
        this.status = status;
    }
}

async function request(method, url, body, isForm = false) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
        if (isForm) {
            opts.body = body;
        } else {
            opts.headers["Content-Type"] = "application/json";
            opts.body = JSON.stringify(body);
        }
    }
    let res;
    try {
        res = await fetch(url, opts);
    } catch (err) {
        throw new ApiError("network error: " + err.message, 0);
    }
    const ct = res.headers.get("content-type") || "";
    const data = ct.includes("application/json") ? await res.json() : await res.text();
    if (!res.ok) {
        const detail = data && typeof data === "object" ? data.detail : data;
        throw new ApiError(detail || res.statusText || "request failed", res.status);
    }
    return data;
}

export const api = {
    repos: () => request("GET", "/api/repos"),
    repo: (id) => request("GET", `/api/repos/${id}`),
    addZip: (file) => {
        const fd = new FormData();
        fd.append("file", file, file.name);
        return request("POST", "/api/repos/zip", fd, true);
    },
    addClone: (url) => request("POST", "/api/repos/clone", { url }),
    del: (id) => request("DELETE", `/api/repos/${id}`),
    reindex: (id) => request("POST", `/api/repos/${id}/reindex`),

    authors: (id) => request("GET", `/api/repos/${id}/authors`),
    merge: (id, ids) => request("POST", `/api/repos/${id}/authors/merge`, { ids }),
    unmerge: (id, authorId) => request("POST", `/api/repos/${id}/authors/unmerge`, { id: authorId }),

    commits: (id, qs = "") => request("GET", `/api/repos/${id}/commits${qs}`),
    search: (id, q) => request("GET", `/api/repos/${id}/search?q=${encodeURIComponent(q)}`),

    metrics: (id, qs = "") => request("GET", `/api/repos/${id}/metrics${qs}`),
    tree: (id, qs = "") => request("GET", `/api/repos/${id}/tree${qs}`),
    timeseries: (id, qs = "") => request("GET", `/api/repos/${id}/timeseries${qs}`),
    top: (id, qs = "") => request("GET", `/api/repos/${id}/top${qs}`),
    history: (id, qs = "") => request("GET", `/api/repos/${id}/history${qs}`),
    exportUrl: (id, qs = "") => `/api/repos/${id}/export.csv${qs}`,
};
