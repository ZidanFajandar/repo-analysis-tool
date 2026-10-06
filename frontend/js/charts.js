/* ECharts wrappers. Every chart created here is registered so a view can
   dispose them all before re-rendering (prevents canvas leaks). */

const ec = window.echarts;
const DAY = 86400;
const COLORS = ["#5b8def", "#2ecc71", "#e74c3c", "#f1c40f", "#9b59b6",
    "#1abc9c", "#e67e22", "#e84393", "#00b894", "#0984e3"];

let registry = [];

export function disposeAll() {
    registry.forEach((c) => { try { c.dispose(); } catch { /* already gone */ } });
    registry = [];
}

/* Dispose the chart(s) inside `root` (used by the drawer and the top-N panel,
   whose charts must not kill the dashboard's ones). */
export function disposeWithin(root) {
    const nodes = [root, ...root.querySelectorAll(".chart-lg, .chart-md, .chart-sm")];
    nodes.forEach((dom) => {
        const c = ec.getInstanceByDom(dom);
        if (c) {
            c.dispose();
            const i = registry.indexOf(c);
            if (i >= 0) registry.splice(i, 1);
        }
    });
}

export function resizeAll() {
    registry.forEach((c) => { try { c.resize(); } catch { /* disposed */ } });
}

function theme() {
    const light = document.documentElement.dataset.theme === "light";
    return light
        ? { text: "#64748b", line: "#e2e8f0", cell: "#eef2f8", low: "#e8eef7", high: "#3b82f6" }
        : { text: "#8b98ab", line: "#25304a", cell: "#141c2e", low: "#17223a", high: "#5b8def" };
}

function init(dom) {
    const c = ec.init(dom);
    registry.push(c);
    return c;
}

function axis(t) {
    return {
        axisLine: { lineStyle: { color: t.line } },
        axisLabel: { color: t.text },
        splitLine: { lineStyle: { color: t.line, opacity: 0.45 } },
    };
}

function tip(t) {
    return { backgroundColor: "rgba(15,22,38,.94)", borderColor: t.line, textStyle: { color: "#e8eef8", fontSize: 12 } };
}

const fmtN = (n) => (n || 0).toLocaleString("en-US");
const dayLabel = (ts) => new Date(ts * 1000).toISOString().slice(0, 10);

/* ------------------------------------------------------------ activity */
/* Bars = commits/day, lines = added/removed lines/day. Zooming the chart
   fires onBrush(fromTs, toTs, isFullRange) so it can drive the date filter. */
export function activity(dom, days, onBrush) {
    const c = init(dom);
    const t = theme();
    const labels = days.map((d) => dayLabel(d.ts));
    c.setOption({
        color: COLORS,
        tooltip: { ...tip(t), trigger: "axis" },
        legend: { top: 0, textStyle: { color: t.text }, data: ["commits", "added", "removed"] },
        grid: { left: 48, right: 20, top: 34, bottom: 58 },
        xAxis: { type: "category", data: labels, ...axis(t), splitLine: { show: false } },
        yAxis: [
            { type: "value", name: "lines", nameTextStyle: { color: t.text }, ...axis(t) },
            { type: "value", name: "commits", nameTextStyle: { color: t.text }, ...axis(t), splitLine: { show: false } },
        ],
        dataZoom: [
            { type: "inside" },
            { type: "slider", height: 16, bottom: 14, brushSelect: false },
        ],
        series: [
            { name: "commits", type: "bar", yAxisIndex: 1, data: days.map((d) => d.commits), itemStyle: { opacity: 0.4, borderRadius: [2, 2, 0, 0] } },
            { name: "added", type: "line", symbol: "none", smooth: 0.2, lineStyle: { width: 2 }, data: days.map((d) => d.adds) },
            { name: "removed", type: "line", symbol: "none", smooth: 0.2, lineStyle: { width: 2 }, data: days.map((d) => d.dels) },
        ],
    }, true);

    if (onBrush) {
        let timer = null;
        c.on("datazoom", () => {
            clearTimeout(timer);
            timer = setTimeout(() => {
                const dz = (c.getOption().dataZoom || [])[0];
                if (!dz) return;
                const n = days.length;
                let i0 = dz.startValue != null ? Math.round(dz.startValue) : Math.round(((dz.start || 0) / 100) * (n - 1));
                let i1 = dz.endValue != null ? Math.round(dz.endValue) : Math.round(((dz.end || 100) / 100) * (n - 1));
                i0 = Math.max(0, Math.min(n - 1, i0));
                i1 = Math.max(0, Math.min(n - 1, i1));
                if (i1 < i0) [i0, i1] = [i1, i0];
                onBrush(days[i0].ts, days[i1].ts + DAY, i0 === 0 && i1 === n - 1);
            }, 650);
        });
    }
    return c;
}

/* ------------------------------------------------------------- heatmap */
export function heatmap(dom, days) {
    const c = init(dom);
    const t = theme();
    if (!days.length) return c;
    const max = Math.max(1, ...days.map((d) => d.adds + d.dels));
    c.setOption({
        tooltip: {
            ...tip(t),
            formatter: (p) => `${p.value[0]}<br>churn λ: <b>${fmtN(p.value[1])}</b>`,
        },
        visualMap: {
            min: 0, max, orient: "horizontal", left: 10, bottom: 0, itemWidth: 12, itemHeight: 90,
            textStyle: { color: t.text }, inRange: { color: [t.low, t.high] },
        },
        calendar: {
            range: [dayLabel(days[0].ts), dayLabel(days[days.length - 1].ts)],
            cellSize: ["auto", 13], top: 24, left: 42, right: 14, bottom: 44,
            itemStyle: { color: t.cell, borderColor: "transparent" },
            dayLabel: { color: t.text, fontSize: 10 },
            monthLabel: { color: t.text, fontSize: 11 },
            yearLabel: { show: false },
        },
        series: [{
            type: "heatmap", coordinateSystem: "calendar",
            data: days.map((d) => [dayLabel(d.ts), d.adds + d.dels]),
        }],
    }, true);
    return c;
}

/* -------------------------------------------------------------- growth */
export function growth(dom, days) {
    const c = init(dom);
    const t = theme();
    let a = 0, d = 0;
    const cumAdds = [], cumDels = [], net = [];
    days.forEach((day) => {
        a += day.adds; d += day.dels;
        cumAdds.push(a); cumDels.push(d); net.push(a - d);
    });
    c.setOption({
        color: [COLORS[1], COLORS[2], COLORS[0]],
        tooltip: { ...tip(t), trigger: "axis" },
        legend: { top: 0, textStyle: { color: t.text }, data: ["cumulative added", "cumulative removed", "net growth"] },
        grid: { left: 54, right: 20, top: 34, bottom: 30 },
        xAxis: { type: "category", data: days.map((x) => dayLabel(x.ts)), ...axis(t), splitLine: { show: false } },
        yAxis: { type: "value", ...axis(t), axisLabel: { color: t.text, formatter: (v) => fmtN(v) } },
        series: [
            { name: "cumulative added", type: "line", symbol: "none", smooth: 0.15, data: cumAdds },
            { name: "cumulative removed", type: "line", symbol: "none", smooth: 0.15, data: cumDels },
            { name: "net growth", type: "line", symbol: "none", smooth: 0.15, areaStyle: { opacity: 0.12 }, data: net },
        ],
    }, true);
    return c;
}

/* ------------------------------------------------------------- treemap */
export function treemap(dom, children, onClick) {
    const items = children
        .filter((ch) => ch.churn > 0)
        .map((ch) => ({ name: ch.name, value: ch.churn, path: ch.path, kind: ch.kind }));
    if (!items.length) return null;
    const c = init(dom);
    const t = theme();
    c.setOption({
        color: COLORS,
        tooltip: { ...tip(t), formatter: (p) => `${p.name}<br>churn λ: <b>${fmtN(p.value)}</b>` },
        series: [{
            type: "treemap",
            data: items,
            roam: false,
            nodeClick: false,
            breadcrumb: { show: false },
            label: { show: true, formatter: "{b}", fontSize: 12, color: "#fff" },
            upperLabel: { show: false },
            itemStyle: { borderColor: t.cell, borderWidth: 2, gapWidth: 2 },
        }],
    }, true);
    if (onClick) c.on("click", (p) => { if (p.data && p.data.path != null) onClick(p.data); });
    return c;
}

/* ---------------------------------------------------------------- bars */
/* items: [{ label, value, raw }] - rendered horizontally, biggest at the top. */
export function bars(dom, items, onClickItem) {
    const c = init(dom);
    const t = theme();
    const data = items.slice().reverse();
    if (!data.length) return c;
    c.setOption({
        tooltip: { ...tip(t), trigger: "axis", formatter: (ps) => `${ps[0].name}<br>${fmtN(ps[0].value)}` },
        grid: { left: 8, right: 56, top: 8, bottom: 8, containLabel: true },
        xAxis: { type: "value", ...axis(t) },
        yAxis: {
            type: "category", data: data.map((i) => i.label),
            axisLine: { lineStyle: { color: t.line } },
            axisLabel: { color: t.text, width: 190, overflow: "truncate" },
        },
        series: [{
            type: "bar", data: data.map((i) => i.value), barMaxWidth: 15,
            itemStyle: { borderRadius: [0, 3, 3, 0], color: COLORS[0] },
            label: { show: true, position: "right", color: t.text, fontSize: 11 },
        }],
    }, true);
    if (onClickItem) c.on("click", (p) => { if (data[p.dataIndex]) onClickItem(data[p.dataIndex].raw); });
    return c;
}

/* --------------------------------------------------------------- donut */
export function donut(dom, authors) {
    const c = init(dom);
    const t = theme();
    const rows = authors.filter((a) => a.churn > 0).map((a) => ({ name: a.name, value: a.churn }));
    if (!rows.length) return c;
    const top = rows.slice(0, 8);
    const rest = rows.slice(8);
    if (rest.length) top.push({ name: `others (${rest.length})`, value: rest.reduce((s, r) => s + r.value, 0) });
    c.setOption({
        color: COLORS,
        tooltip: { ...tip(t), formatter: (p) => `${p.name}<br>churn λ: <b>${fmtN(p.value)}</b> (${p.percent}%)` },
        series: [{
            type: "pie", radius: ["48%", "76%"], center: ["50%", "52%"],
            itemStyle: { borderColor: t.cell, borderWidth: 2 },
            label: { color: t.text, fontSize: 11, formatter: "{b}\n{d}%" },
            data: top,
        }],
    }, true);
    return c;
}

/* -------------------------------------------------------- history mini */
export function historyBars(dom, history) {
    const c = init(dom);
    const t = theme();
    const rows = history.slice(0, 120).reverse();
    c.setOption({
        color: [COLORS[1], COLORS[2]],
        tooltip: { ...tip(t), trigger: "axis" },
        legend: { top: 0, textStyle: { color: t.text }, data: ["added", "removed"] },
        grid: { left: 44, right: 12, top: 30, bottom: 26 },
        xAxis: { type: "category", data: rows.map((x) => x.hash.slice(0, 7)), ...axis(t), splitLine: { show: false }, axisLabel: { color: t.text, fontSize: 9 } },
        yAxis: { type: "value", ...axis(t) },
        series: [
            { name: "added", type: "bar", stack: "chg", data: rows.map((x) => x.adds) },
            { name: "removed", type: "bar", stack: "chg", data: rows.map((x) => -x.dels) },
        ],
    }, true);
    return c;
}
