/* Bootstrap: theme toggle, hash router, topbar navigation. */
import { refresh, routeHash } from "./state.js";
import * as views from "./views.js";
import * as charts from "./charts.js";

const view = document.getElementById("view");
const THEME_KEY = "rat-theme";
let cleanup = () => {};

function renderNav(r) {
    const nav = document.getElementById("nav");
    const items = [`<a href="#/repos" class="${r.page === "repos" ? "active" : ""}">Repositories</a>`];
    if (r.repoId) {
        items.push(`<a href="${routeHash({ ...r, page: "dash" })}" class="${r.page === "dash" ? "active" : ""}">Dashboard</a>`);
        items.push(`<a href="${routeHash({ ...r, page: "authors" })}" class="${r.page === "authors" ? "active" : ""}">Authors</a>`);
    }
    nav.innerHTML = items.join("");
}

function render() {
    charts.disposeAll();
    cleanup();
    cleanup = () => {};
    const r = refresh();
    renderNav(r);
    document.title = r.repoId && r.page !== "repos" ? `RAT - repo #${r.repoId}` : "Repo Analysis Tool";
    if (r.page === "dash" && r.repoId) cleanup = views.renderDashboard(view) || cleanup;
    else if (r.page === "authors") cleanup = views.renderAuthors(view) || cleanup;
    else cleanup = views.renderRepos(view) || cleanup;
}

/* theme */
const saved = localStorage.getItem(THEME_KEY);
document.documentElement.dataset.theme = saved === "light" ? "light" : "dark";
document.getElementById("theme-toggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    localStorage.setItem(THEME_KEY, next);
    render(); // repaint charts with the new palette
});

/* router */
if (!location.hash) history.replaceState(null, "", "#/repos");
window.addEventListener("hashchange", render);
window.addEventListener("resize", () => charts.resizeAll());
render();
