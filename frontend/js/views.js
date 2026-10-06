/* Page renderers: repositories, dashboard (filters + KPIs + charts + table),
   authors (merge UI), commit picker modal, add-repo modal, file drawer. */
import { api, ApiError } from "./api.js";
import { route, setRoute, queryFromRoute } from "./state.js";
import * as charts from "./charts.js";

const DAY = 86400;
const BUSY = ["pending", "cloning", "extracting", "indexing"];

/* ---------------------------------------------------------------- helpers */

export function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g,
        (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
export const fmt = (n) => (n ?? 0).toLocaleString("en-US");
export const fmtF = (x, d = 3) => (x ?? 0).toFixed(d);
export const fmtPct = (x) => ((x ?? 0) * 100).toFixed(1) + "%";
export const fmtTs = (ts) => (ts
    ? new Date(ts * 1000).toLocaleString("en-US", { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
    : "-");
export const fmtDay = (ts) => (ts
    ? new Date(ts * 1000).toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" })
    : "-");
const dayInputValue = (ts) => (ts ? new Date(ts * 1000).toISOString().slice(0, 10) : "");
const inputDayToTs = (v, addDay = false) => {
    if (!v) return null;
    const t = Math.floor(new Date(v + "T00:00:00").getTime() / 1000);
    return addDay ? t + DAY : t;
};
const initial = (name) => (name || "?").trim().split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();

function el(html) {
    const t = document.createElement("template");
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
}

export function toast(msg, kind = "info") {
    const box = document.getElementById("toasts");
    if (!box) return;
    const node = el(`<div class="toast toast-${kind}">${esc(msg)}</div>`);
    box.appendChild(node);
    setTimeout(() => { node.classList.add("out"); setTimeout(() => node.remove(), 320); }, 4200);
    node.addEventListener("click", () => node.remove());
}

function statusBadge(status) {
    const cls = status === "ready" ? "ready" : status === "failed" ? "failed" : BUSY.includes(status) ? "busy" : "pending";
    return `<span class="badge badge-${cls}">${esc(status)}</span>`;
}

function emptyState(title, sub = "") {
    return `<div class="empty"><b>${esc(title)}</b>${sub ? esc(sub) : ""}</div>`;
}

function busyProgress(r) {
    const p = r.progress || {};
    const pct = p.phase === "indexing" && p.total ? Math.round((p.parsed / p.total) * 100) : null;
    return `<div class="progress ${pct == null ? "indet" : ""}"><i style="width:${pct ?? 40}%"></i></div>
        <div class="hint">${esc(p.phase || r.status)}${pct != null ? ` - ${fmt(p.parsed)} / ${fmt(p.total)} commits (${pct}%)` : "..."}</div>`;
}

function bindDoc(cleanups, type, fn) {
    document.addEventListener(type, fn);
    cleanups.push(() => document.removeEventListener(type, fn));
}

/* ============================================================ REPOS PAGES */

let reposEpoch = 0;

export function renderRepos(root) {
    const mine = ++reposEpoch;
    let timer = null;
    let repos = [];

    root.innerHTML = `
    <div class="page">
        <div class="page-title">
            <h2>Repositories</h2>
            <span class="sub">ingest a zip containing .git, or clone a URL - then open the dashboard</span>
            <span class="spacer"></span>
            <button class="btn btn-primary" id="add-repo">Add repository</button>
        </div>
        <div class="repo-grid" id="repo-grid">
            ${Array(3).fill('<div class="skeleton" style="height:190px"></div>').join("")}
        </div>
    </div>`;

    const grid = root.querySelector("#repo-grid");
    root.querySelector("#add-repo").addEventListener("click", () => openAddRepo(() => load()));

    grid.addEventListener("click", async (ev) => {
        const btn = ev.target.closest("button[data-act]");
        if (!btn) return;
        const id = Number(btn.dataset.id);
        const repo = repos.find((x) => x.id === id);
        if (btn.dataset.act === "delete") {
            if (!confirm(`Delete "${repo ? repo.name : id}" and all of its indexed data?`)) return;
            try {
                await api.del(id);
                toast("repository deleted", "success");
                load();
            } catch (e) { toast(e.message, "error"); }
        } else if (btn.dataset.act === "reindex") {
            try {
                await api.reindex(id);
                toast("reindex started");
                load();
            } catch (e) { toast(e.message, "error"); }
        }
    });

    async function load() {
        try {
            repos = await api.repos();
        } catch (e) { toast(e.message, "error"); return; }
        if (mine !== reposEpoch) return;
        grid.innerHTML = repos.length
            ? repos.map(repoCard).join("")
            : emptyState("No repositories yet", "Click \"Add repository\" to upload a zip or clone a URL.");
        const anyBusy = repos.some((r) => BUSY.includes(r.status) || r.busy);
        if (anyBusy && !timer) timer = setInterval(load, 1200);
        if (!anyBusy && timer) { clearInterval(timer); timer = null; }
    }

    load();
    return () => { if (timer) clearInterval(timer); };
}

function repoCard(r) {
    const busy = BUSY.includes(r.status) || r.busy;
    const s = r.summary || null;
    const stats = s
        ? `<div class="rstats">
               <div><b>${fmt(s.commits)}</b><span>commits</span></div>
               <div><b>${fmt(s.authors)}</b><span>authors</span></div>
               <div><b>${fmt(s.files)}</b><span>files</span></div>
               <div><b>${fmt(s.dirs)}</b><span>dirs</span></div>
           </div>
           <div class="hint">l+ ${fmt(s.adds)} - l- ${fmt(s.dels)} - lambda ${fmt(s.churn)}${s.first_ts ? ` - ${fmtDay(s.first_ts)} to ${fmtDay(s.last_ts)}` : ""}</div>`
        : "";
    const err = r.status === "failed" && r.error ? `<div class="form-error">${esc(r.error)}</div>` : "";
    return `
    <article class="card repo-card">
        <div class="rhead">
            <span class="rname" title="${esc(r.name)}">${esc(r.name)}</span>
            <span class="badge badge-src">${esc(r.source)}</span>
            ${statusBadge(r.status)}
        </div>
        <div class="rmeta" title="${esc(r.origin || "")}">${esc(r.origin || "")}</div>
        ${busy ? busyProgress(r) : ""}
        ${err}
        ${stats}
        <div class="actions">
            <a class="btn btn-primary ${r.status === "ready" ? "" : "disabled"}" href="#/dash/${r.id}"
               ${r.status === "ready" ? "" : 'style="pointer-events:none;opacity:.45"'}>Open</a>
            <button class="btn" data-act="reindex" data-id="${r.id}" ${busy ? "disabled" : ""}>Reindex</button>
            <button class="btn btn-danger" data-act="delete" data-id="${r.id}" ${busy ? "disabled" : ""}>Delete</button>
        </div>
    </article>`;
}

/* ------------------------------------------------------------ add modal */

function openAddRepo(onDone) {
    const host = document.getElementById("modal-root");
    let tab = "zip";
    let file = null;

    host.innerHTML = `
    <div class="modal-overlay">
        <div class="modal">
            <h3>Add repository</h3>
            <div class="desc">Zip uploads must contain the .git directory. Clones fetch the full history (bare) from the URL. Indexing starts in the background.</div>
            <div class="tabs">
                <button class="btn" id="tab-zip">Upload zip</button>
                <button class="btn" id="tab-clone">Clone URL</button>
            </div>
            <div id="panel-zip">
                <div class="dropzone" id="dz"><b>Drop a .zip here</b><br>or click to choose a file<br><span class="muted" id="zfilename"></span></div>
                <input type="file" id="zfile" accept=".zip,application/zip" hidden>
            </div>
            <div id="panel-clone" hidden>
                <input class="input" id="curl" style="width:100%" placeholder="https://github.com/user/repo.git">
            </div>
            <div class="form-error" id="add-err"></div>
            <div class="foot">
                <button class="btn" id="add-cancel">Cancel</button>
                <button class="btn btn-primary" id="add-submit">Add</button>
            </div>
        </div>
    </div>`;

    const overlay = host.querySelector(".modal-overlay");
    const errBox = host.querySelector("#add-err");
    const tabZip = host.querySelector("#tab-zip");
    const tabClone = host.querySelector("#tab-clone");
    const panelZip = host.querySelector("#panel-zip");
    const panelClone = host.querySelector("#panel-clone");
    const dz = host.querySelector("#dz");
    const fileInput = host.querySelector("#zfile");
    const close = () => { host.innerHTML = ""; };

    function setTab(t) {
        tab = t;
        tabZip.classList.toggle("btn-primary", t === "zip");
        tabClone.classList.toggle("btn-primary", t === "clone");
        panelZip.hidden = t !== "zip";
        panelClone.hidden = t !== "clone";
        errBox.textContent = "";
    }
    tabZip.addEventListener("click", () => setTab("zip"));
    tabClone.addEventListener("click", () => setTab("clone"));
    setTab("zip");

    dz.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
        file = fileInput.files[0] || null;
        host.querySelector("#zfilename").textContent = file ? file.name : "";
    });
    dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("drag"); });
    dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
    dz.addEventListener("drop", (e) => {
        e.preventDefault();
        dz.classList.remove("drag");
        file = e.dataTransfer.files[0] || null;
        host.querySelector("#zfilename").textContent = file ? file.name : "";
    });

    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) close(); });
    host.querySelector("#add-cancel").addEventListener("click", close);

    const submit = host.querySelector("#add-submit");
    submit.addEventListener("click", async () => {
        errBox.textContent = "";
        submit.disabled = true;
        try {
            if (tab === "zip") {
                if (!file) throw new ApiError("choose a .zip file first", 0);
                await api.addZip(file);
                toast("zip received - extracting and indexing", "success");
            } else {
                const url = host.querySelector("#curl").value.trim();
                if (!url) throw new ApiError("enter a repository URL", 0);
                await api.addClone(url);
                toast("clone started - indexing will follow", "success");
            }
            close();
            if (onDone) onDone();
        } catch (e) {
            errBox.textContent = e.message;
            toast(e.message, "error");
        } finally {
            submit.disabled = false;
        }
    });
}

/* ============================================================= DASHBOARD */

let dashEpoch = 0;

export function renderDashboard(root) {
    const mine = ++dashEpoch;
    const cleanups = [];
    let poll = null;

    root.innerHTML = `<div class="page">
        <div class="skeleton" style="height:52px"></div>
        <div class="skeleton" style="height:150px;margin-top:14px"></div>
        <div class="skeleton" style="height:300px;margin-top:14px"></div>
    </div>`;

    async function load() {
        const r = route();
        if (!r.repoId) { setRoute({ page: "repos" }); return; }
        let repo;
        try {
            repo = await api.repo(r.repoId);
        } catch (e) {
            if (mine !== dashEpoch) return;
            root.innerHTML = `<div class="page">${emptyState("Repository not found", e.message)}<p><a href="#/repos">Back to repositories</a></p></div>`;
            return;
        }
        if (mine !== dashEpoch) return;
        if (repo.status !== "ready") {
            renderStatusPanel(root, repo);
            if (!poll) {
                poll = setInterval(async () => {
                    if (mine !== dashEpoch) return;
                    try {
                        const r2 = await api.repo(route().repoId);
                        if (mine !== dashEpoch) return;
                        if (r2.status === "ready") {
                            clearInterval(poll); poll = null;
                            load();
                        } else {
                            renderStatusPanel(root, r2);
                        }
                    } catch { /* transient */ }
                }, 1500);
            }
            return;
        }
        if (poll) { clearInterval(poll); poll = null; }

        const qs = queryFromRoute(r);
        let data;
        try {
            const [repos, metrics, tree, ts, topFiles, topAuthors, authorsList] = await Promise.all([
                api.repos(),
                api.metrics(r.repoId, qs),
                api.tree(r.repoId, qs),
                api.timeseries(r.repoId, qs),
                api.top(r.repoId, qs + "&by=files&limit=12"),
                api.top(r.repoId, qs + "&by=authors&limit=12"),
                api.authors(r.repoId),
            ]);
            data = { repos, metrics, tree, ts, topFiles, topAuthors, authorsList };
        } catch (e) {
            if (mine !== dashEpoch) return;
            toast(e.message, "error");
            return;
        }
        if (mine !== dashEpoch) return;
        paint(root, repo, data, cleanups);
    }

    load();
    return () => {
        if (poll) clearInterval(poll);
        cleanups.forEach((fn) => fn());
    };
}

function renderStatusPanel(root, repo) {
    const busy = BUSY.includes(repo.status) || repo.busy;
    root.innerHTML = `
    <div class="page">
        <div class="page-title"><h2>${esc(repo.name)}</h2>${statusBadge(repo.status)}</div>
        <div class="card" style="max-width:640px">
            ${busy ? busyProgress(repo) : `<div class="hint">status: ${esc(repo.status)}</div>`}
            ${repo.error ? `<div class="form-error">${esc(repo.error)}</div>` : ""}
            <div class="row" style="margin-top:14px">
                <a class="btn" href="#/repos">Back to repositories</a>
                <button class="btn" id="st-reindex" ${busy ? "disabled" : ""}>Reindex</button>
            </div>
        </div>
    </div>`;
    const btn = root.querySelector("#st-reindex");
    if (btn) {
        btn.addEventListener("click", async () => {
            try { await api.reindex(repo.id); toast("reindex started"); } catch (e) { toast(e.message, "error"); }
        });
    }
}

function paint(root, repo, data, cleanups) {
    const r = route();
    const { repos, metrics, tree, ts, topFiles, topAuthors, authorsList } = data;
    const f = r.filters;
    const days = ts.days || [];

    const repoOptions = repos.map((x) => {
        const dis = x.status === "ready" ? "" : " disabled";
        return `<option value="${x.id}" ${x.id === r.repoId ? "selected" : ""}${dis}>${esc(x.name)}${x.status === "ready" ? "" : ` (${esc(x.status)})`}</option>`;
    }).join("");

    const authorChip = f.authors.length
        ? `<span class="chip"><b>${f.authors.length}</b> author group${f.authors.length > 1 ? "s" : ""} <span class="x" id="clr-authors">x</span></span>`
        : "";
    const modeExtra = f.mode === "range"
        ? `<span class="group">
             <label class="small">from</label><input type="date" class="input" id="f-from" value="${dayInputValue(f.from)}">
             <label class="small">to</label><input type="date" class="input" id="f-to" value="${f.to ? dayInputValue(f.to - DAY) : ""}">
             <span class="chip"><span class="x" id="clr-range">x clear</span></span>
           </span>`
        : f.mode === "list"
            ? `<span class="group">
                 <span class="chip" title="${esc(f.commits.slice(0, 8).join(", "))}${f.commits.length > 8 ? ", ..." : ""}"><b>${fmt(f.commits.length)}</b> commits</span>
                 <button class="btn btn-sm" id="pick-commits">Pick...</button>
                 <span class="chip"><span class="x" id="clr-list">x clear</span></span>
               </span>`
            : "";

    const totalNote = metrics.filtered_by_authors
        ? `<span class="chip" title="Metrics and tables below reflect only the selected authors; ownership omega denominators stay on the full object.">author-filtered</span>`
        : "";

    root.innerHTML = `
    <div class="page">
        <div class="page-title">
            <h2>${esc(repo.name)}</h2>
            <span class="sub">${fmt(repo.summary.commits)} commits - ${fmt(repo.summary.authors)} authors - ${fmt(repo.summary.files)} files - ${fmt(repo.summary.dirs)} dirs</span>
        </div>

        <div class="card filterbar">
            <span class="group"><label class="small">repo</label>
                <select class="select" id="f-repo">${repoOptions}</select></span>
            <span class="divider"></span>
            <span class="seg" id="f-mode">
                <button data-mode="all" class="${f.mode === "all" ? "active" : ""}">All commits</button>
                <button data-mode="range" class="${f.mode === "range" ? "active" : ""}">Date range</button>
                <button data-mode="list" class="${f.mode === "list" ? "active" : ""}">Commit list</button>
            </span>
            ${modeExtra}
            <span class="divider"></span>
            <span class="group dd" id="author-dd">
                <button class="btn" id="author-dd-btn">Authors${f.authors.length ? ` (${f.authors.length})` : ""} v</button>
                <div class="dd-panel" id="author-dd-panel" hidden></div>
            </span>
            ${authorChip}
            <span class="divider"></span>
            <nav class="breadcrumb" id="crumbs"></nav>
            <span class="search-wrap">
                <input class="input" id="f-search" placeholder="find file/dir..." style="width:190px">
                <div class="search-results" id="f-search-results" hidden></div>
            </span>
            <span class="spacer"></span>
            ${totalNote}
            <a class="btn" id="btn-export" href="${api.exportUrl(r.repoId, queryFromRoute(r))}">Export CSV</a>
        </div>

        <div class="kpis">
            ${kpi("l+ added", fmt(metrics.metrics.adds))}
            ${kpi("l- removed", fmt(metrics.metrics.dels))}
            ${kpi("delta growth", signFmt(metrics.metrics.growth))}
            ${kpi("lambda churn", fmt(metrics.metrics.churn))}
            ${kpi("n modifications", fmt(metrics.metrics.modifications))}
            ${kpi("eta mod freq", fmtF(metrics.metrics.mod_frequency))}
            ${kpi("rho churn rate", fmtF(metrics.metrics.churn_rate))}
            ${kpi("|H| commits", fmt(metrics.commits_in_set))}
        </div>
        <div class="hint">object: <code>${esc(r.path || "(repository root)")}</code>${metrics.filtered_by_authors ? " - author-filtered values; totals (all authors): " + fmt(metrics.totals.churn) + " lambda" : ""}</div>

        <div class="grid">
            <section class="card span-8">
                <h3>Activity timeline <span class="muted">drag/zoom the chart to set a date-range filter</span></h3>
                <div id="ch-activity" class="chart-lg">${days.length ? "" : emptyState("No activity in this commit set")}</div>
            </section>
            <section class="card span-4">
                <h3>Author ownership <span class="muted">omega (lambda share)</span></h3>
                <div id="ch-donut" class="chart-lg"></div>
            </section>
            <section class="card span-6">
                <h3>Daily churn heatmap</h3>
                <div id="ch-heat" class="chart-md">${days.length ? "" : emptyState("No activity in this commit set")}</div>
            </section>
            <section class="card span-6">
                <h3>Cumulative growth</h3>
                <div id="ch-growth" class="chart-md">${days.length ? "" : emptyState("No activity in this commit set")}</div>
            </section>
            <section class="card span-6">
                <h3>Churn treemap <span class="muted">click a dir to drill in, a file to open the drawer</span></h3>
                <div id="ch-treemap" class="chart-md"></div>
            </section>
            <section class="card span-6">
                <h3 class="row">Top contributors
                    <span class="seg" id="top-by">
                        <button data-by="files" class="active">Files</button>
                        <button data-by="authors">Authors</button>
                    </span>
                    <select class="select" id="top-metric" style="padding:2px 8px">
                        <option value="churn" selected>churn</option>
                        <option value="adds">l+ added</option>
                        <option value="dels">l- removed</option>
                        <option value="growth">delta growth</option>
                        <option value="modifications">n modifications</option>
                    </select>
                </h3>
                <div id="ch-top" class="chart-md"></div>
            </section>
            <section class="card span-12">
                <h3>Contents of <code>${esc(r.path || "(repository root)")}</code>
                    <span class="muted">${fmt(tree.total_children)} objects${tree.truncated ? " (showing first " + fmt(tree.children.length) + ")" : ""} - click a row to drill in or open the drawer</span>
                </h3>
                <div class="tablewrap" id="children"></div>
            </section>
        </div>
    </div>`;

    /* ---------------------------------------------------- breadcrumb */
    paintCrumbs(root.querySelector("#crumbs"), repo.name, r.path);

    /* ---------------------------------------------------- filter wiring */
    root.querySelector("#f-repo").addEventListener("change", (e) => {
        setRoute({
            page: "dash", repoId: Number(e.target.value), path: "",
            filters: { mode: "all", from: null, to: null, commits: [], authors: [] },
        });
    });

    root.querySelector("#f-mode").addEventListener("click", (e) => {
        const btn = e.target.closest("button[data-mode]");
        if (!btn) return;
        const m = btn.dataset.mode;
        if (m === "all") {
            setRoute({ filters: { mode: "all", from: null, to: null, commits: [] } });
        } else if (m === "range") {
            const from = f.from ?? repo.summary.first_ts ?? null;
            const to = f.to ?? (repo.summary.last_ts != null ? repo.summary.last_ts + DAY : null);
            setRoute({ filters: { mode: "range", from, to, commits: [] } });
        } else if (m === "list") {
            if (f.commits.length) setRoute({ filters: { mode: "list", from: null, to: null } });
            else openCommitPicker(r.repoId, [], (hashes) => {
                if (hashes.length) setRoute({ filters: { mode: "list", commits: hashes, from: null, to: null } });
            });
        }
    });

    const fromInput = root.querySelector("#f-from");
    const toInput = root.querySelector("#f-to");
    if (fromInput) fromInput.addEventListener("change", () => applyRangeDates());
    if (toInput) toInput.addEventListener("change", () => applyRangeDates());
    function applyRangeDates() {
        const from = inputDayToTs(root.querySelector("#f-from").value);
        const to = inputDayToTs(root.querySelector("#f-to").value, true);
        if (from == null && to == null) setRoute({ filters: { mode: "all", from: null, to: null } });
        else setRoute({ filters: { mode: "range", from, to } });
    }
    const clrRange = root.querySelector("#clr-range");
    if (clrRange) clrRange.addEventListener("click", () => setRoute({ filters: { mode: "all", from: null, to: null } }));
    const clrList = root.querySelector("#clr-list");
    if (clrList) clrList.addEventListener("click", () => setRoute({ filters: { mode: "all", commits: [] } }));
    const pickBtn = root.querySelector("#pick-commits");
    if (pickBtn) pickBtn.addEventListener("click", () => openCommitPicker(r.repoId, f.commits, (hashes) => {
        if (hashes.length) setRoute({ filters: { mode: "list", commits: hashes } });
        else setRoute({ filters: { mode: "all", commits: [] } });
    }));
    const clrAuthors = root.querySelector("#clr-authors");
    if (clrAuthors) clrAuthors.addEventListener("click", () => setRoute({ filters: { authors: [] } }));

    /* author multiselect dropdown */
    const dd = root.querySelector("#author-dd");
    const ddPanel = root.querySelector("#author-dd-panel");
    ddPanel.innerHTML = authorsList.length
        ? authorsList.map((a) => `
            <label><input type="checkbox" data-aid="${a.id}" ${f.authors.includes(a.id) ? "checked" : ""}>
                ${esc(a.name)}${a.merged ? ' <span class="badge badge-merged">merged</span>' : ""}
                <span class="meta">${fmt(a.commits)} commits</span></label>`).join("")
        : `<div class="hint" style="padding:8px">no authors</div>`;
    root.querySelector("#author-dd-btn").addEventListener("click", (e) => {
        e.stopPropagation();
        ddPanel.hidden = !ddPanel.hidden;
    });
    ddPanel.addEventListener("change", () => {
        const ids = [...ddPanel.querySelectorAll("input:checked")].map((i) => Number(i.dataset.aid));
        setRoute({ filters: { authors: ids } });
    });
    bindDoc(cleanups, "click", (e) => {
        if (!dd.contains(e.target)) ddPanel.hidden = true;
    });

    /* object search */
    const searchInput = root.querySelector("#f-search");
    const searchRes = root.querySelector("#f-search-results");
    let searchTimer = null;
    searchInput.addEventListener("input", () => {
        clearTimeout(searchTimer);
        const q = searchInput.value.trim();
        if (!q) { searchRes.hidden = true; return; }
        searchTimer = setTimeout(async () => {
            try {
                const hits = await api.search(r.repoId, q);
                searchRes.innerHTML = hits.length
                    ? hits.map((h) => `<div class="hit" data-path="${esc(h.path)}" data-kind="${esc(h.kind)}">${esc(h.path)}<span class="k">${esc(h.kind)}</span></div>`).join("")
                    : `<div class="hint" style="padding:8px">no matches</div>`;
                searchRes.hidden = false;
            } catch (e) { toast(e.message, "error"); }
        }, 220);
    });
    searchRes.addEventListener("click", (e) => {
        const hit = e.target.closest(".hit");
        if (!hit) return;
        searchRes.hidden = true;
        searchInput.value = "";
        if (hit.dataset.kind === "dir") setRoute({ path: hit.dataset.path });
        else openDrawer(hit.dataset.path);
    });
    bindDoc(cleanups, "click", (e) => {
        if (!searchInput.contains(e.target) && !searchRes.contains(e.target)) searchRes.hidden = true;
    });

    /* ------------------------------------------------------------ charts */
    const activityEl = root.querySelector("#ch-activity");
    if (days.length) {
        charts.activity(activityEl, days, (from, to, full) => {
            const cur = route().filters;
            if (full) {
                if (cur.mode === "range") setRoute({ filters: { mode: "all", from: null, to: null } });
                return;
            }
            if (cur.mode === "range" && cur.from === from && cur.to === to) return;
            setRoute({ filters: { mode: "range", from, to, commits: [] } });
        });
    }
    if (days.length) {
        charts.heatmap(root.querySelector("#ch-heat"), days);
        charts.growth(root.querySelector("#ch-growth"), days);
    }
    const treemapHost = root.querySelector("#ch-treemap");
    const tm = charts.treemap(treemapHost, tree.children, (node) => {
        if (node.kind === "dir") setRoute({ path: node.path });
        else openDrawer(node.path);
    });
    if (!tm) treemapHost.innerHTML = emptyState("No churn in this commit set", "rename-only / binary changes do not count");

    charts.donut(root.querySelector("#ch-donut"), metrics.authors);

    /* top-N panel */
    const topState = { by: "files", metric: "churn" };
    const topHost = root.querySelector("#ch-top");
    function renderTop() {
        topHost.innerHTML = "";
        charts.disposeWithin(topHost);
        const source = topState.by === "files" ? topFiles.items : topAuthors.items;
        const items = source.map((it) => ({
            label: topState.by === "files" ? it.path : it.name,
            value: it[topState.metric],
            raw: it,
        })).filter((i) => i.value > 0);
        if (!items.length) { topHost.innerHTML = emptyState("No data for this metric"); return; }
        charts.bars(topHost, items, (raw) => {
            if (topState.by === "authors") setRoute({ filters: { authors: [raw.id] } });
            else openDrawer(raw.path);
        });
    }
    renderTop();
    root.querySelector("#top-by").addEventListener("click", (e) => {
        const btn = e.target.closest("button[data-by]");
        if (!btn) return;
        topState.by = btn.dataset.by;
        root.querySelectorAll("#top-by button").forEach((b) => b.classList.toggle("active", b === btn));
        renderTop();
    });
    root.querySelector("#top-metric").addEventListener("change", (e) => {
        topState.metric = e.target.value;
        renderTop();
    });

    /* ------------------------------------------------------ children table */
    paintTable(root.querySelector("#children"), tree, (child) => {
        if (child.kind === "dir") setRoute({ path: child.path });
        else openDrawer(child.path);
    });
}

function kpi(label, value, cls = "") {
    return `<div class="kpi"><div class="kpi-label">${esc(label)}</div><div class="kpi-value ${cls}">${value}</div></div>`;
}

function signFmt(n) {
    const v = n ?? 0;
    const cls = v > 0 ? "pos" : v < 0 ? "neg" : "";
    return `<span class="${cls}">${v > 0 ? "+" : ""}${fmt(v)}</span>`;
}

function paintCrumbs(nav, repoName, path) {
    const parts = path ? path.split("/") : [];
    const segs = [{ label: repoName, path: "" }];
    parts.forEach((p, i) => segs.push({ label: p, path: parts.slice(0, i + 1).join("/") }));
    nav.innerHTML = segs.map((s, i) => {
        const last = i === segs.length - 1;
        const link = last
            ? `<span>${esc(s.label)}</span>`
            : `<a href="javascript:void 0" class="crumb ${i === 0 ? "root" : ""}" data-path="${esc(s.path)}">${esc(s.label)}</a>`;
        return link + (last ? "" : '<span class="sep">/</span>');
    }).join("");
    nav.querySelectorAll(".crumb").forEach((a) => {
        a.addEventListener("click", () => setRoute({ path: a.dataset.path }));
    });
}

function paintTable(host, tree, onOpen) {
    const sortState = { key: "churn", dir: -1 };
    const COLS = [
        ["name", "Name"], ["kind", "Kind"], ["adds", "l+"], ["dels", "l-"],
        ["growth", "delta"], ["churn", "lambda"], ["modifications", "n"],
        ["mod_frequency", "eta"], ["churn_rate", "rho"],
    ];

    function render() {
        if (!tree.children.length) {
            host.innerHTML = emptyState("No objects in this commit set");
            return;
        }
        const rows = tree.children.slice().sort((a, b) => {
            const x = a[sortState.key], y = b[sortState.key];
            const cmp = typeof x === "string" ? String(x).localeCompare(String(y)) : (x - y);
            return cmp * sortState.dir;
        });
        host.innerHTML = `<table class="data">
            <thead><tr>${COLS.map(([k, label]) =>
                `<th data-key="${k}" class="${sortState.key === k ? "sorted" : ""} ${k === "name" || k === "kind" ? "" : "num"}">${label}${sortState.key === k ? (sortState.dir < 0 ? " v" : " ^") : ""}</th>`).join("")}</tr></thead>
            <tbody>${rows.map((c) => `
                <tr class="clickable" data-path="${esc(c.path)}" data-kind="${esc(c.kind)}">
                    <td class="path" title="${esc(c.path)}">${esc(c.name)}</td>
                    <td>${esc(c.kind)}</td>
                    <td class="num">${fmt(c.adds)}</td>
                    <td class="num">${fmt(c.dels)}</td>
                    <td class="num">${signFmt(c.growth)}</td>
                    <td class="num">${fmt(c.churn)}</td>
                    <td class="num">${fmt(c.modifications)}</td>
                    <td class="num">${fmtF(c.mod_frequency)}</td>
                    <td class="num">${fmtF(c.churn_rate)}</td>
                </tr>`).join("")}</tbody>
        </table>`;
        host.querySelectorAll("th[data-key]").forEach((th) => {
            th.addEventListener("click", () => {
                const k = th.dataset.key;
                if (sortState.key === k) sortState.dir *= -1;
                else { sortState.key = k; sortState.dir = k === "name" || k === "kind" ? 1 : -1; }
                render();
            });
        });
        host.querySelectorAll("tr.clickable").forEach((tr) => {
            tr.addEventListener("click", () => onOpen({ path: tr.dataset.path, kind: tr.dataset.kind }));
        });
    }
    render();
}

/* --------------------------------------------------------- commit picker */

function openCommitPicker(repoId, preselected, onApply) {
    const host = document.getElementById("modal-root");
    const sel = new Set(preselected);
    let page = 1, q = "", total = 0;

    host.innerHTML = `
    <div class="modal-overlay">
        <div class="modal">
            <h3>Pick commits</h3>
            <div class="desc">H becomes exactly the selected commits (single commit = the diff against its parent).</div>
            <input class="input" id="cp-q" placeholder="Search subject or hash..." style="width:100%">
            <div id="cp-list" style="max-height:44vh;overflow:auto;margin-top:10px"></div>
            <div class="row" style="justify-content:space-between;margin-top:10px">
                <span class="muted" id="cp-status"></span>
                <span><button class="btn btn-sm" id="cp-prev">&lt; Prev</button>
                <button class="btn btn-sm" id="cp-next">Next &gt;</button></span>
            </div>
            <div class="foot">
                <button class="btn" id="cp-cancel">Cancel</button>
                <button class="btn btn-primary" id="cp-apply">Apply (0)</button>
            </div>
        </div>
    </div>`;

    const overlay = host.querySelector(".modal-overlay");
    const list = host.querySelector("#cp-list");
    const status = host.querySelector("#cp-status");
    const apply = host.querySelector("#cp-apply");
    const close = () => { host.innerHTML = ""; };

    async function load() {
        try {
            const data = await api.commits(repoId, `?page=${page}&page_size=30&q=${encodeURIComponent(q)}`);
            total = data.total;
            const pages = Math.max(1, Math.ceil(total / 30));
            status.textContent = `${fmt(total)} commits - page ${page}/${pages}`;
            list.innerHTML = data.commits.length
                ? data.commits.map((c) => `
                    <label class="hit" style="cursor:pointer">
                        <input type="checkbox" data-hash="${esc(c.hash)}" ${sel.has(c.hash) ? "checked" : ""}>
                        <code>${esc(c.hash.slice(0, 8))}</code>
                        <span style="flex:1;overflow:hidden;text-overflow:ellipsis">${esc(c.subject)}</span>
                        <span class="k">${esc(c.author)} - ${fmtDay(c.ts)}</span>
                    </label>`).join("")
                : `<div class="hint" style="padding:8px">no commits match</div>`;
            list.querySelectorAll("input[type=checkbox]").forEach((cb) => {
                cb.addEventListener("change", () => {
                    if (cb.checked) sel.add(cb.dataset.hash);
                    else sel.delete(cb.dataset.hash);
                    apply.textContent = `Apply (${sel.size})`;
                });
            });
            apply.textContent = `Apply (${sel.size})`;
        } catch (e) { toast(e.message, "error"); }
    }

    let t = null;
    host.querySelector("#cp-q").addEventListener("input", (e) => {
        clearTimeout(t);
        t = setTimeout(() => { q = e.target.value.trim(); page = 1; load(); }, 250);
    });
    host.querySelector("#cp-prev").addEventListener("click", () => { if (page > 1) { page--; load(); } });
    host.querySelector("#cp-next").addEventListener("click", () => {
        const pages = Math.max(1, Math.ceil(total / 30));
        if (page < pages) { page++; load(); }
    });
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) close(); });
    host.querySelector("#cp-cancel").addEventListener("click", close);
    apply.addEventListener("click", () => { onApply([...sel]); close(); });
    load();
}

/* ---------------------------------------------------------------- drawer */

export async function openDrawer(path) {
    const host = document.getElementById("drawer-root");
    const r = route();
    host.innerHTML = `
        <div class="drawer-backdrop"></div>
        <aside class="drawer">
            <div class="dhead">
                <span class="dpath">${esc(path)}</span>
                <button class="icon-btn" id="dw-close">x</button>
            </div>
            <div class="skeleton" style="height:96px"></div>
            <div class="skeleton" style="height:210px;margin-top:12px"></div>
        </aside>`;
    const close = () => {
        charts.disposeWithin(host);
        host.innerHTML = "";
        document.removeEventListener("keydown", onKey);
    };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    host.querySelector(".drawer-backdrop").addEventListener("click", close);
    host.querySelector("#dw-close").addEventListener("click", close);

    try {
        const qs = queryFromRoute(r, path);
        const [m, h] = await Promise.all([
            api.metrics(r.repoId, qs),
            api.history(r.repoId, qs + "&limit=300"),
        ]);
        if (!host.querySelector(".drawer")) return; // closed while loading
        const mm = m.metrics;
        const aside = host.querySelector(".drawer");
        aside.innerHTML = `
            <div class="dhead">
                <span class="dpath">${esc(path)}</span>
                <span class="badge badge-src">${esc(m.object.kind)}</span>
                <button class="icon-btn" id="dw-close">x</button>
            </div>
            <div class="kpis" style="grid-template-columns:repeat(4,1fr);margin-top:0">
                ${kpi("l+ added", fmt(mm.adds))}
                ${kpi("l- removed", fmt(mm.dels))}
                ${kpi("delta growth", signFmt(mm.growth))}
                ${kpi("lambda churn", fmt(mm.churn))}
            </div>
            <div class="hint">n ${fmt(mm.modifications)} - eta ${fmtF(mm.mod_frequency)} - rho ${fmtF(mm.churn_rate)}${m.filtered_by_authors ? " - author-filtered" : ""}</div>
            <h3 style="margin:16px 0 6px">Per-commit history <span class="muted">latest ${fmt(h.history.length)}</span></h3>
            <div class="chart-sm" id="dw-history"></div>
            <h3 style="margin:16px 0 6px">Author breakdown <span class="muted">click to filter by that author</span></h3>
            <div class="chart-sm" id="dw-authors"></div>
            <h3 style="margin:16px 0 6px">Recent commits</h3>
            <div class="tablewrap" style="max-height:300px">
                <table class="data"><thead><tr><th>hash</th><th>date</th><th>author</th><th class="num">l+</th><th class="num">l-</th><th>subject</th></tr></thead>
                <tbody>${h.history.slice(0, 40).map((c) => `
                    <tr><td><code>${esc(c.hash.slice(0, 8))}</code></td>
                    <td>${fmtDay(c.ts)}</td><td>${esc(c.author)}</td>
                    <td class="num">${fmt(c.adds)}</td><td class="num">${fmt(c.dels)}</td>
                    <td class="path" title="${esc(c.subject)}">${esc(c.subject)}</td></tr>`).join("")}</tbody></table>
            </div>`;
        host.querySelector("#dw-close").addEventListener("click", close);
        if (h.history.length) charts.historyBars(host.querySelector("#dw-history"), h.history);
        const authorItems = m.authors.filter((a) => a.churn > 0)
            .map((a) => ({ label: a.name, value: a.churn, raw: a }));
        if (authorItems.length) {
            charts.bars(host.querySelector("#dw-authors"), authorItems, (raw) => {
                close();
                setRoute({ filters: { authors: [raw.id] } });
            });
        } else if (host.querySelector("#dw-authors")) {
            host.querySelector("#dw-authors").innerHTML = emptyState("No churn attributed");
        }
    } catch (e) {
        toast(e.message, "error");
        close();
    }
}

/* =============================================================== AUTHORS */

let authorsEpoch = 0;

export function renderAuthors(root) {
    const mine = ++authorsEpoch;
    const r = route();
    const cleanups = [];
    let selected = [];

    async function load() {
        let repos;
        try {
            repos = await api.repos();
        } catch (e) { toast(e.message, "error"); return; }
        if (mine !== authorsEpoch) return;

        const ready = repos.filter((x) => x.status === "ready");
        const repoId = r.repoId && ready.some((x) => x.id === r.repoId) ? r.repoId : (ready[0] ? ready[0].id : null);

        root.innerHTML = `
        <div class="page">
            <div class="page-title">
                <h2>Authors</h2>
                <span class="sub">identities are already .mailmap-corrected at index time - merge duplicates manually below</span>
            </div>
            <div class="card filterbar">
                <span class="group"><label class="small">repo</label>
                    <select class="select" id="a-repo">
                        ${ready.map((x) => `<option value="${x.id}" ${x.id === repoId ? "selected" : ""}>${esc(x.name)}</option>`).join("")}
                    </select>
                </span>
                <span class="muted" id="a-note"></span>
            </div>
            <div class="authors-list" id="a-list"><div class="skeleton" style="height:64px"></div></div>
            <div class="mergebar" id="a-mergebar" hidden>
                <span id="a-selcount" class="muted"></span>
                <span class="spacer"></span>
                <button class="btn btn-primary" id="a-merge" disabled>Merge selected</button>
            </div>
        </div>`;

        const sel = root.querySelector("#a-repo");
        if (sel && ready.length) {
            sel.addEventListener("change", (e) => setRoute({ page: "authors", repoId: Number(e.target.value) }));
        }

        if (!repoId) {
            root.querySelector("#a-list").innerHTML = emptyState("No indexed repositories yet", "Add a repository first, then come back to manage authors.");
            return;
        }
        if (repoId !== r.repoId) { setRoute({ page: "authors", repoId }); return; }

        let authors;
        try {
            authors = await api.authors(repoId);
        } catch (e) { toast(e.message, "error"); return; }
        if (mine !== authorsEpoch) return;

        root.querySelector("#a-note").textContent =
            `${fmt(authors.length)} author groups - ${fmt(authors.reduce((s, a) => s + a.commits, 0))} commits`;
        renderList(authors);

        function renderList(list) {
            const listEl = root.querySelector("#a-list");
            const bar = root.querySelector("#a-mergebar");
            listEl.innerHTML = list.map((a) => `
                <div class="author-row ${selected.includes(a.id) ? "selected" : ""}" data-id="${a.id}">
                    <input type="checkbox" data-sel="${a.id}" ${selected.includes(a.id) ? "checked" : ""}>
                    <div class="avatar">${esc(initial(a.name))}</div>
                    <div class="author-main">
                        <div class="an">${esc(a.name)} ${a.merged ? `<span class="badge badge-merged">merged (${a.members.length})</span>` : ""}</div>
                        <div class="ae">${esc(a.email)}</div>
                        ${a.merged ? `<div class="members">${a.members.map((mem) => `
                            <div class="mrow"><code>${esc(mem.email)}</code> ${esc(mem.name)}
                                <span class="muted">${fmt(mem.commits)} commits</span>
                                ${mem.id !== a.id ? `<button class="btn btn-sm" data-unmerge="${mem.id}">release</button>` : "<span class=\"muted\">(canonical)</span>"}
                            </div>`).join("")}
                            <button class="btn btn-sm" data-unmerge-group="${a.id}" style="margin-top:6px">Unmerge all</button>
                        </div>` : ""}
                    </div>
                    <div class="author-stats"><b>${fmt(a.commits)}</b>commits<b>${fmt(a.churn)}</b>lambda</div>
                </div>`).join("");

            listEl.querySelectorAll("input[data-sel]").forEach((cb) => {
                cb.addEventListener("change", () => {
                    const id = Number(cb.dataset.sel);
                    if (cb.checked) { if (!selected.includes(id)) selected.push(id); }
                    else selected = selected.filter((x) => x !== id);
                    renderList(list);
                });
            });
            listEl.querySelectorAll("button[data-unmerge]").forEach((btn) => {
                btn.addEventListener("click", async () => {
                    try {
                        await api.unmerge(repoId, Number(btn.dataset.unmerge));
                        toast("author released from group", "success");
                        load();
                    } catch (e) { toast(e.message, "error"); }
                });
            });
            listEl.querySelectorAll("button[data-unmerge-group]").forEach((btn) => {
                btn.addEventListener("click", async () => {
                    try {
                        await api.unmerge(repoId, Number(btn.dataset.unmergeGroup));
                        toast("group unmerged", "success");
                        load();
                    } catch (e) { toast(e.message, "error"); }
                });
            });

            bar.hidden = selected.length === 0;
            root.querySelector("#a-selcount").textContent =
                `${selected.length} selected - first selected (${nameOf(selected[0])}) becomes the group`;
            const mergeBtn = root.querySelector("#a-merge");
            mergeBtn.disabled = selected.length < 2;
            mergeBtn.textContent = `Merge selected (${selected.length})`;
        }

        function nameOf(id) {
            const a = authors.find((x) => x.id === id);
            return a ? a.name : id;
        }

        root.querySelector("#a-merge").addEventListener("click", async () => {
            if (selected.length < 2) return;
            try {
                await api.merge(repoId, selected);
                toast(`merged ${selected.length} identities`, "success");
                selected = [];
                load();
            } catch (e) { toast(e.message, "error"); }
        });
    }

    load();
    return () => cleanups.forEach((fn) => fn());
}
