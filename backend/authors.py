"""Author identities: .mailmap-aware grouping and manual merges.

The .mailmap is applied by git itself during indexing (%aN/%aE), so the authors
table stores already-canonical identities. Manual merges are recorded as
`merged_into` pointers and resolved at query time, so merging/unmerging never
requires a reindex.
"""
from __future__ import annotations

import sqlite3


def author_groups(conn: sqlite3.Connection, repo_id: int) -> dict[int, dict]:
    """Resolve merged_into chains into groups: canonical id -> group dict."""
    rows = conn.execute(
        "SELECT id, name, email, merged_into FROM authors WHERE repo_id = ?", (repo_id,)
    ).fetchall()
    by_id = {r["id"]: dict(r) for r in rows}

    def root(aid: int) -> int:
        seen: set[int] = set()
        while True:
            m = by_id.get(aid, {}).get("merged_into")
            if m is None or m not in by_id or m in seen:
                return aid
            seen.add(aid)
            aid = m

    groups: dict[int, dict] = {}
    for r in by_id.values():
        gid = root(r["id"])
        g = groups.get(gid)
        if g is None:
            g = groups[gid] = {
                "id": gid,
                "name": by_id[gid]["name"],
                "email": by_id[gid]["email"],
                "members": [],
            }
        g["members"].append(r["id"])
    for g in groups.values():
        g["members"].sort()
    return groups


def group_of(conn: sqlite3.Connection, repo_id: int) -> tuple[dict[int, dict], dict[int, int]]:
    """Convenience: (canonical_id -> group, raw author id -> canonical id)."""
    groups = author_groups(conn, repo_id)
    member_to_group = {m: gid for gid, g in groups.items() for m in g["members"]}
    return groups, member_to_group


def list_authors(conn: sqlite3.Connection, repo_id: int) -> list[dict]:
    groups = author_groups(conn, repo_id)
    commit_counts = {
        r["author_id"]: r["n"]
        for r in conn.execute(
            "SELECT author_id, COUNT(*) AS n FROM commits WHERE repo_id = ? GROUP BY author_id",
            (repo_id,),
        )
    }
    churns = {
        r["author_id"]: r["c"]
        for r in conn.execute(
            "SELECT author_id, SUM(adds + dels) AS c FROM dir_changes"
            " WHERE repo_id = ? AND path = '' GROUP BY author_id",
            (repo_id,),
        )
    }
    info = {
        r["id"]: dict(r)
        for r in conn.execute(
            "SELECT id, name, email, merged_into FROM authors WHERE repo_id = ?", (repo_id,)
        )
    }
    out = []
    for gid, g in groups.items():
        members = []
        for mid in g["members"]:
            m = info[mid]
            members.append(
                {
                    "id": mid,
                    "name": m["name"],
                    "email": m["email"],
                    "commits": commit_counts.get(mid, 0),
                    "churn": churns.get(mid, 0),
                }
            )
        members.sort(key=lambda m: (-m["churn"], m["name"].lower()))
        out.append(
            {
                "id": gid,
                "name": g["name"],
                "email": g["email"],
                "members": members,
                "commits": sum(m["commits"] for m in members),
                "churn": sum(m["churn"] for m in members),
                "merged": len(members) > 1,
            }
        )
    out.sort(key=lambda g: (-g["churn"], g["name"].lower()))
    return out


def merge_authors(conn: sqlite3.Connection, repo_id: int, ids: list[int]) -> dict:
    """Merge identities; ids[0] is kept as the canonical identity (its name wins)."""
    ids = list(dict.fromkeys(ids))
    if len(ids) < 2:
        raise ValueError("select at least two authors to merge")
    groups = author_groups(conn, repo_id)
    for aid in ids:
        contains = any(aid in g["members"] for g in groups.values())
        if not contains:
            raise ValueError(f"author {aid} does not belong to this repository")
    canonical = next(g["id"] for g in groups.values() if ids[0] in g["members"])
    conn.execute("UPDATE authors SET merged_into = NULL WHERE repo_id = ? AND id = ?",
                 (repo_id, canonical))
    for aid in ids:
        if aid == canonical:
            continue
        member_group = next(g["id"] for g in groups.values() if aid in g["members"])
        if member_group == canonical:
            continue
        conn.execute(
            "UPDATE authors SET merged_into = ? WHERE repo_id = ? AND (id = ? OR merged_into = ?)",
            (canonical, repo_id, member_group, member_group),
        )
    return {"canonical": canonical, "merged": [i for i in ids if i != canonical]}


def unmerge_author(conn: sqlite3.Connection, repo_id: int, author_id: int) -> None:
    """Release an identity (and, if canonical, all its members) from the group."""
    conn.execute("UPDATE authors SET merged_into = NULL WHERE repo_id = ? AND id = ?",
                 (repo_id, author_id))
    conn.execute("UPDATE authors SET merged_into = NULL WHERE repo_id = ? AND merged_into = ?",
                 (repo_id, author_id))
