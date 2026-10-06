"""Metric computations: file / directory / repository / commit-set / author.

All aggregations are single-table indexed SQL over the precomputed
file_changes / dir_changes tables (directory values are already rolled up to
every ancestor during ingestion, so no subtree scanning is needed at query
time - only direct-parent-child lookups via the objects cache).

Notation follows the brief:
  l+ l- delta lambda   added / removed / growth / churn
  n eta rho            modifications / modification frequency / churn rate
  n_a lambda_a omega   per-author modifications / churn / ownership
"""
from __future__ import annotations

import bisect
import csv
import io
import sqlite3
import threading

from . import authors as authors_mod

MAX_LIST_COMMITS = 20000
CHUNK = 400

_objects_cache: dict[int, dict] = {}
_objects_lock = threading.Lock()


# --------------------------------------------------------------- commit sets

class CommitFilter:
    """The commit set H: all / time range [from, to) / explicit hash list."""

    def __init__(self, repo_id: int, mode: str = "all", frm: int | None = None,
                 to: int | None = None, hashes: list[str] | None = None):
        self.repo_id = repo_id
        self.mode = mode if mode in ("all", "range", "list") else "all"
        self.frm = frm
        self.to = to
        self.hashes = (hashes or [])[:MAX_LIST_COMMITS]

    def where(self, table: str = "changes") -> tuple[str, list]:
        """SQL fragment (leading AND) + params for filtering a table."""
        if self.mode == "list":
            if not self.hashes:
                return " AND 0", []
            ph = ",".join("?" * len(self.hashes))
            col = "commit_id" if table == "changes" else "id"
            return (
                f" AND {col} IN (SELECT id FROM commits WHERE repo_id = ? AND hash IN ({ph}))",
                [self.repo_id, *self.hashes],
            )
        conds, params = [], []
        if self.mode == "range":
            if self.frm is not None:
                conds.append("committer_ts >= ?")
                params.append(self.frm)
            if self.to is not None:
                conds.append("committer_ts < ?")
                params.append(self.to)
        if not conds:
            return "", []
        return " AND " + " AND ".join(conds), params

    def size(self, conn: sqlite3.Connection) -> int:
        where, params = self.where("commits")
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM commits WHERE repo_id = ?{where}",
            [self.repo_id, *params],
        ).fetchone()
        return row["n"]


def filter_from_params(repo_id: int, params) -> CommitFilter:
    mode = params.get("mode") or "all"
    frm = params.get("from")
    to = params.get("to")
    try:
        frm = int(frm) if frm not in (None, "") else None
        to = int(to) if to not in (None, "") else None
    except (TypeError, ValueError):
        frm = to = None
    raw = params.get("commits") or ""
    hashes = [h.strip() for h in raw.split(",") if h.strip()]
    return CommitFilter(repo_id, mode, frm, to, hashes)


def parse_author_ids(raw) -> list[int] | None:
    if not raw:
        return None
    try:
        ids = [int(x) for x in str(raw).split(",") if x.strip()]
    except ValueError:
        return None
    return ids or None


# ------------------------------------------------------------ objects cache

def invalidate_objects(repo_id: int) -> None:
    with _objects_lock:
        _objects_cache.pop(repo_id, None)


def get_objects(conn: sqlite3.Connection, repo_id: int) -> dict:
    with _objects_lock:
        cached = _objects_cache.get(repo_id)
    if cached is not None:
        return cached
    files: set[str] = set()
    dirs: set[str] = {""}
    for r in conn.execute("SELECT path, kind FROM objects WHERE repo_id = ?", (repo_id,)):
        (files if r["kind"] == "file" else dirs).add(r["path"])
    data = {"files": files, "dirs": dirs, "paths": sorted(files | dirs)}
    with _objects_lock:
        _objects_cache[repo_id] = data
    return data


def resolve_kind(conn: sqlite3.Connection, repo_id: int, path: str, kind: str | None) -> str:
    if kind in ("file", "dir"):
        return kind
    if path == "":
        return "dir"
    objs = get_objects(conn, repo_id)
    if path in objs["dirs"]:
        return "dir"
    if path in objs["files"]:
        return "file"
    i = bisect.bisect_left(objs["paths"], path + "/")
    if i < len(objs["paths"]) and objs["paths"][i].startswith(path + "/"):
        return "dir"
    return "file"


def parent_of(path: str) -> str:
    head, sep, _ = path.rpartition("/")
    return head if sep else ""


def _subtree(prefix: str) -> tuple[str, list]:
    """SQL range bounds selecting every object strictly under directory `prefix`."""
    if not prefix:
        return "", []
    return " AND path >= ? AND path < ?", [prefix + "/", prefix + "0"]


def _author_sql(author_ids: list[int] | None) -> tuple[str, list]:
    if not author_ids:
        return "", []
    ph = ",".join("?" * len(author_ids))
    return f" AND author_id IN ({ph})", list(author_ids)


def _table(kind: str) -> str:
    return "dir_changes" if kind == "dir" else "file_changes"


# ------------------------------------------------------------ core metrics

def object_metrics(conn: sqlite3.Connection, repo_id: int, path: str, kind: str,
                   cf: CommitFilter, author_ids: list[int] | None) -> dict:
    """All metrics for one object over commit set H (optionally scoped to authors)."""
    table = _table(kind)
    where, params = cf.where()
    rows = conn.execute(
        f"SELECT author_id, SUM(adds) AS adds, SUM(dels) AS dels, COUNT(*) AS n"
        f" FROM {table} WHERE repo_id = ? AND path = ?{where} GROUP BY author_id",
        [repo_id, path, *params],
    ).fetchall()

    groups, member_to_group = authors_mod.group_of(conn, repo_id)
    per_group: dict[int, dict] = {}
    totals = {"adds": 0, "dels": 0, "modifications": 0}
    for r in rows:
        gid = member_to_group.get(r["author_id"], r["author_id"])
        g = per_group.setdefault(gid, {"adds": 0, "dels": 0, "modifications": 0})
        g["adds"] += r["adds"]
        g["dels"] += r["dels"]
        g["modifications"] += r["n"]
        totals["adds"] += r["adds"]
        totals["dels"] += r["dels"]
        totals["modifications"] += r["n"]

    h_size = cf.size(conn)
    total_churn = totals["adds"] + totals["dels"]

    author_rows = []
    for gid, g in per_group.items():
        group = groups.get(gid)
        churn = g["adds"] + g["dels"]
        author_rows.append({
            "id": gid,
            "name": group["name"] if group else f"author {gid}",
            "email": group["email"] if group else "",
            "members": group["members"] if group else [gid],
            "adds": g["adds"],
            "dels": g["dels"],
            "growth": g["adds"] - g["dels"],
            "churn": churn,
            "modifications": g["modifications"],
            "ownership": (churn / total_churn) if total_churn else 0.0,
            "selected": bool(author_ids and gid in author_ids),
        })
    author_rows.sort(key=lambda a: (-a["churn"], a["name"].lower()))

    all_authors = author_rows
    selected = [a for a in all_authors if a["selected"]] if author_ids else all_authors
    f_adds = sum(a["adds"] for a in selected)
    f_dels = sum(a["dels"] for a in selected)
    f_mods = sum(a["modifications"] for a in selected)
    f_churn = f_adds + f_dels

    def pack(adds, dels, mods):
        churn = adds + dels
        return {
            "adds": adds, "dels": dels, "growth": adds - dels, "churn": churn,
            "modifications": mods,
            "mod_frequency": (mods / h_size) if h_size else 0.0,
            "churn_rate": (churn / h_size) if h_size else 0.0,
        }

    return {
        "object": {"path": path, "kind": kind},
        "commits_in_set": h_size,
        "filtered_by_authors": bool(author_ids),
        "metrics": pack(f_adds, f_dels, f_mods),
        "totals": pack(totals["adds"], totals["dels"], totals["modifications"]),
        "authors": all_authors,
    }


def _metrics_for_paths(conn: sqlite3.Connection, repo_id: int, table: str,
                       paths: list[str], cf: CommitFilter,
                       author_ids: list[int] | None) -> dict[str, tuple[int, int, int]]:
    out: dict[str, tuple[int, int, int]] = {}
    where, params = cf.where()
    auth, authp = _author_sql(author_ids)
    for i in range(0, len(paths), CHUNK):
        chunk = paths[i:i + CHUNK]
        ph = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT path, SUM(adds) AS a, SUM(dels) AS d, COUNT(*) AS n FROM {table}"
            f" WHERE repo_id = ? AND path IN ({ph}){where}{auth} GROUP BY path",
            [repo_id, *chunk, *params, *authp],
        ).fetchall()
        for r in rows:
            out[r["path"]] = (r["a"], r["d"], r["n"])
    return out


def tree_children(conn: sqlite3.Connection, repo_id: int, path: str, kind: str,
                  cf: CommitFilter, author_ids: list[int] | None, limit: int = 2000) -> dict:
    """Immediate children (files and directories) of `path` with their metrics."""
    objs = get_objects(conn, repo_id)
    dirs = sorted(d for d in objs["dirs"] if d and parent_of(d) == path)
    files = sorted(f for f in objs["files"] if parent_of(f) == path)
    m_dirs = _metrics_for_paths(conn, repo_id, "dir_changes", dirs, cf, author_ids)
    m_files = _metrics_for_paths(conn, repo_id, "file_changes", files, cf, author_ids)
    h_size = cf.size(conn)

    def pack(p: str, k: str, m):
        adds, dels, n = m if m else (0, 0, 0)
        churn = adds + dels
        return {
            "path": p,
            "name": p.rsplit("/", 1)[-1],
            "kind": k,
            "adds": adds, "dels": dels, "growth": adds - dels, "churn": churn,
            "modifications": n,
            "mod_frequency": (n / h_size) if h_size else 0.0,
            "churn_rate": (churn / h_size) if h_size else 0.0,
        }

    children = [pack(d, "dir", m_dirs.get(d)) for d in dirs]
    children += [pack(f, "file", m_files.get(f)) for f in files]
    children.sort(key=lambda c: (c["kind"] == "file", -c["churn"], c["name"].lower()))
    total = len(children)
    truncated = total > limit
    if truncated:
        children = children[:limit]
    return {
        "object": {"path": path, "kind": kind},
        "commits_in_set": h_size,
        "total_children": total,
        "truncated": truncated,
        "children": children,
    }


def top_files(conn: sqlite3.Connection, repo_id: int, path: str, kind: str,
              cf: CommitFilter, author_ids: list[int] | None, limit: int = 20) -> list[dict]:
    """Files with the most churn inside the selected object."""
    where, params = cf.where()
    auth, authp = _author_sql(author_ids)
    if kind == "file":
        sub, subp = " AND path = ?", [path]
    else:
        sub, subp = _subtree(path)
    rows = conn.execute(
        f"SELECT path, SUM(adds) AS a, SUM(dels) AS d, COUNT(*) AS n FROM file_changes"
        f" WHERE repo_id = ?{sub}{where}{auth} GROUP BY path"
        f" ORDER BY SUM(adds + dels) DESC LIMIT ?",
        [repo_id, *subp, *params, *authp, limit],
    ).fetchall()
    return [
        {"path": r["path"], "adds": r["a"], "dels": r["d"], "growth": r["a"] - r["d"],
         "churn": r["a"] + r["d"], "modifications": r["n"]}
        for r in rows
    ]


def top_authors(conn: sqlite3.Connection, repo_id: int, path: str, kind: str,
                cf: CommitFilter, author_ids: list[int] | None, limit: int = 20) -> list[dict]:
    where, params = cf.where()
    auth, authp = _author_sql(author_ids)
    rows = conn.execute(
        f"SELECT author_id, SUM(adds) AS a, SUM(dels) AS d, COUNT(*) AS n FROM {_table(kind)}"
        f" WHERE repo_id = ? AND path = ?{where}{auth} GROUP BY author_id",
        [repo_id, path, *params, *authp],
    ).fetchall()
    groups, member_to_group = authors_mod.group_of(conn, repo_id)
    merged: dict[int, dict] = {}
    for r in rows:
        gid = member_to_group.get(r["author_id"], r["author_id"])
        g = merged.setdefault(gid, {"adds": 0, "dels": 0, "modifications": 0})
        g["adds"] += r["a"]
        g["dels"] += r["d"]
        g["modifications"] += r["n"]
    out = []
    for gid, g in merged.items():
        group = groups.get(gid)
        out.append({
            "id": gid,
            "name": group["name"] if group else f"author {gid}",
            "email": group["email"] if group else "",
            "members": group["members"] if group else [gid],
            "adds": g["adds"], "dels": g["dels"],
            "growth": g["adds"] - g["dels"], "churn": g["adds"] + g["dels"],
            "modifications": g["modifications"],
        })
    out.sort(key=lambda a: (-a["churn"], a["name"].lower()))
    return out[:limit]


DAY = 86400


def timeseries(conn: sqlite3.Connection, repo_id: int, cf: CommitFilter,
               author_ids: list[int] | None) -> dict:
    """Per-day commit counts and added/removed lines (for the activity charts)."""
    where_c, params_c = cf.where("commits")
    auth, authp = _author_sql(author_ids)
    commits = {}
    for r in conn.execute(
        f"SELECT committer_ts / {DAY} AS day, COUNT(*) AS n FROM commits"
        f" WHERE repo_id = ?{where_c}{auth} GROUP BY day",
        [repo_id, *params_c, *authp],
    ):
        commits[r["day"]] = r["n"]
    where, params = cf.where()
    changes = {}
    for r in conn.execute(
        f"SELECT committer_ts / {DAY} AS day, SUM(adds) AS a, SUM(dels) AS d FROM dir_changes"
        f" WHERE repo_id = ? AND path = ''{where}{auth} GROUP BY day",
        [repo_id, *params, *authp],
    ):
        changes[r["day"]] = (r["a"], r["d"])
    days = sorted(set(commits) | set(changes))
    return {
        "days": [
            {
                "day": d,
                "ts": d * DAY,
                "commits": commits.get(d, 0),
                "adds": changes.get(d, (0, 0))[0],
                "dels": changes.get(d, (0, 0))[1],
            }
            for d in days
        ]
    }


def object_history(conn: sqlite3.Connection, repo_id: int, path: str, kind: str,
                   cf: CommitFilter, author_ids: list[int] | None, limit: int = 300) -> list[dict]:
    where, params = cf.where()
    # qualify columns: the query joins commits and authors (both have timestamps)
    where = where.replace("committer_ts", "fc.committer_ts").replace("commit_id", "fc.commit_id")
    auth, authp = _author_sql(author_ids)
    auth = auth.replace("author_id", "fc.author_id")
    rows = conn.execute(
        f"SELECT c.hash, c.committer_ts, c.subject, a.name, a.email,"
        f" SUM(fc.adds) AS a, SUM(fc.dels) AS d"
        f" FROM {_table(kind)} fc"
        f" JOIN commits c ON c.id = fc.commit_id"
        f" JOIN authors a ON a.id = fc.author_id"
        f" WHERE fc.repo_id = ? AND fc.path = ?{where}{auth}"
        f" GROUP BY c.id ORDER BY c.committer_ts DESC LIMIT ?",
        [repo_id, path, *params, *authp, limit],
    ).fetchall()
    return [
        {"hash": r["hash"], "ts": r["committer_ts"], "subject": r["subject"],
         "author": r["name"], "email": r["email"], "adds": r["a"], "dels": r["d"]}
        for r in rows
    ]


def repo_summary(conn: sqlite3.Connection, repo_id: int) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(committer_ts) AS first_ts, MAX(committer_ts) AS last_ts"
        " FROM commits WHERE repo_id = ?", (repo_id,),
    ).fetchone()
    root = conn.execute(
        "SELECT COALESCE(SUM(adds),0) AS a, COALESCE(SUM(dels),0) AS d FROM dir_changes"
        " WHERE repo_id = ? AND path = ''", (repo_id,),
    ).fetchone()
    kinds = {
        r["kind"]: r["n"]
        for r in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM objects WHERE repo_id = ? GROUP BY kind", (repo_id,)
        )
    }
    author_n = conn.execute(
        "SELECT COUNT(DISTINCT author_id) AS n FROM commits WHERE repo_id = ?", (repo_id,)
    ).fetchone()["n"]
    adds, dels = root["a"] or 0, root["d"] or 0
    return {
        "commits": row["n"],
        "authors": author_n,
        "files": kinds.get("file", 0),
        "dirs": kinds.get("dir", 0),
        "first_ts": row["first_ts"],
        "last_ts": row["last_ts"],
        "adds": adds,
        "dels": dels,
        "growth": adds - dels,
        "churn": adds + dels,
    }


def export_csv(conn: sqlite3.Connection, repo_id: int, path: str, kind: str,
               cf: CommitFilter, author_ids: list[int] | None) -> str:
    data = tree_children(conn, repo_id, path, kind, cf, author_ids, limit=100000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["path", "kind", "added_lines", "removed_lines", "growth", "churn",
                "modifications", "modification_frequency", "churn_rate"])
    for c in data["children"]:
        w.writerow([c["path"], c["kind"], c["adds"], c["dels"], c["growth"], c["churn"],
                    c["modifications"], f"{c['mod_frequency']:.6f}", f"{c['churn_rate']:.6f}"])
    return buf.getvalue()
