"""Exact-value metric tests against a deterministic fixture repository.

The fixture (scripts/make_fixture.py) has hand-computable metrics covering:
renames (pure and with edits), deletions (file and directory), binary files,
.mailmap author merging, a merge commit that must be excluded, and an initial
commit diffed against the empty tree.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import authors as authors_mod  # noqa: E402
from backend import db, indexer, ingest, metrics  # noqa: E402

T0 = 1704067200
STEP = 600
C = {i: T0 + (i - 1) * STEP for i in range(1, 9)}  # c1..c8 timestamps


@pytest.fixture(scope="session")
def env(tmp_path_factory):
    data = tmp_path_factory.mktemp("rat-data")
    db.configure(data)
    db.init_db()
    fixture = data / "fixture_repo"
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "make_fixture.py"), str(fixture)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    repo_id = ingest.create_repo("fixture", "zip", None)
    stats = indexer.index_repo(repo_id, str(fixture))
    return {"repo_id": repo_id, "stats": stats}


def obj(env, path, kind="dir", mode="all", frm=None, to=None, hashes=None,
        author_ids=None):
    conn = db.get_conn()
    try:
        cf = metrics.CommitFilter(env["repo_id"], mode, frm, to, hashes)
        return metrics.object_metrics(conn, env["repo_id"], path, kind, cf, author_ids)
    finally:
        conn.close()


def num(result):
    m = result["metrics"]
    return (m["adds"], m["dels"], m["modifications"])


def commit_hash(env, subject_prefix):
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT hash FROM commits WHERE repo_id = ? AND subject LIKE ?",
            (env["repo_id"], f"{subject_prefix}%"),
        ).fetchone()
        return row["hash"]
    finally:
        conn.close()


# --------------------------------------------------------------- ingestion

def test_history_covers_non_merge_commits(env):
    # 8 non-merge commits; the merge commit is excluded from H-bar
    assert env["stats"]["commits"] == 8
    assert env["stats"]["authors"] == 3
    assert env["stats"]["files"] == 8  # bin.dat is binary -> not measured
    assert env["stats"]["dirs"] == 3  # root, b, d


def test_repo_summary(env):
    conn = db.get_conn()
    try:
        s = metrics.repo_summary(conn, env["repo_id"])
    finally:
        conn.close()
    assert s["commits"] == 8
    assert s["authors"] == 3
    assert s["files"] == 8
    assert s["first_ts"] == C[1]
    assert s["last_ts"] == C[8]  # merge commit (later ts) excluded
    assert (s["adds"], s["dels"], s["growth"], s["churn"]) == (18, 12, 6, 30)


# --------------------------------------------------------- file metrics

@pytest.mark.parametrize("path,expected", [
    (".mailmap", (1, 0, 1)),
    ("a.txt", (5, 1, 2)),        # c1 creation (+3) and c2 edit (+2/-1)
    ("a2.txt", (0, 0, 0)),       # pure-renamed target: no changes of its own
    ("b/c.txt", (4, 0, 1)),
    ("c.txt", (1, 5, 2)),        # c4 rename+edit (+1/-1) and c5 deletion (-4)
    ("d/e.txt", (4, 4, 2)),
    ("d/f.txt", (2, 2, 2)),
    ("x.txt", (1, 0, 1)),
])
def test_file_metrics_all_history(env, path, expected):
    r = obj(env, path, kind="file")
    assert num(r) == expected
    assert r["commits_in_set"] == 8


def test_pure_rename_does_not_change_metrics(env):
    # c3 renamed a.txt -> a2.txt with no edits: neither path records a change
    assert num(obj(env, "a.txt", kind="file")) == (5, 1, 2)
    assert num(obj(env, "a2.txt", kind="file")) == (0, 0, 0)
    # the renamed target still exists as an object (H[F]) with zero metrics
    conn = db.get_conn()
    try:
        objs = metrics.get_objects(conn, env["repo_id"])
    finally:
        conn.close()
    assert "a2.txt" in objs["files"]


def test_rename_with_edit_attributed_to_new_path(env):
    # c4: b/c.txt -> c.txt with +1/-1 must be recorded on c.txt ("b" sees nothing)
    assert num(obj(env, "c.txt", kind="file")) == (1, 5, 2)
    assert num(obj(env, "b", kind="dir")) == (4, 0, 1)


# ---------------------------------------------------- directory metrics

def test_directory_metrics_rollup(env):
    assert num(obj(env, "d", kind="dir")) == (6, 6, 2)
    assert num(obj(env, "b", kind="dir")) == (4, 0, 1)
    r = obj(env, "d", kind="dir")
    assert r["metrics"]["growth"] == 0
    assert r["metrics"]["churn"] == 12


def test_root_metrics(env):
    r = obj(env, "", kind="dir")
    assert num(r) == (18, 12, 7)
    m = r["metrics"]
    assert m["growth"] == 6
    assert m["churn"] == 30
    assert m["mod_frequency"] == pytest.approx(7 / 8)
    assert m["churn_rate"] == pytest.approx(30 / 8)


def test_deleted_directory_recorded_on_paths(env):
    # directory d existed only during c6; its files' deletion is recorded on d
    conn = db.get_conn()
    try:
        cf = metrics.CommitFilter(env["repo_id"], "list", None, None, [commit_hash(env, "c7")])
        r = metrics.object_metrics(conn, env["repo_id"], "d", "dir", cf, None)
        assert (r["metrics"]["adds"], r["metrics"]["dels"]) == (0, 6)
        r_root = metrics.object_metrics(conn, env["repo_id"], "", "dir", cf, None)
        assert (r_root["metrics"]["adds"], r_root["metrics"]["dels"]) == (0, 6)
    finally:
        conn.close()


# --------------------------------------------------------- commit sets

def test_time_range_metrics(env):
    # H_i,j = [c3, c5): c3 (rename only), c4 (rename+edit)
    r = obj(env, "", kind="dir", mode="range", frm=C[3], to=C[5])
    assert r["commits_in_set"] == 2
    assert num(r) == (1, 1, 1)
    assert r["metrics"]["mod_frequency"] == pytest.approx(0.5)
    assert r["metrics"]["churn_rate"] == pytest.approx(1.0)
    # upper bound is exclusive: c5's deletion is not included
    assert num(obj(env, "c.txt", kind="file", mode="range", frm=C[3], to=C[5])) == (1, 1, 1)


def test_open_ended_time_range(env):
    # H_t = commits from c6 to present: c6, c7, c8
    r = obj(env, "", kind="dir", mode="range", frm=C[6])
    assert r["commits_in_set"] == 3
    assert num(r) == (7, 6, 3)


def test_manual_commit_list(env):
    h5 = commit_hash(env, "c5")
    r = obj(env, "", kind="dir", mode="list", hashes=[h5])
    assert r["commits_in_set"] == 1
    assert num(r) == (0, 4, 1)
    r2 = obj(env, "c.txt", kind="file", mode="list", hashes=[h5])
    assert num(r2) == (0, 4, 1)


def test_empty_commit_set_gives_zero_rates(env):
    r = obj(env, "", kind="dir", mode="list", hashes=["0" * 40])
    assert r["commits_in_set"] == 0
    assert r["metrics"]["mod_frequency"] == 0
    assert r["metrics"]["churn_rate"] == 0


# ------------------------------------------------------ author metrics

def test_authors_after_mailmap(env):
    conn = db.get_conn()
    try:
        groups = authors_mod.list_authors(conn, env["repo_id"])
    finally:
        conn.close()
    by_name = {g["name"]: g for g in groups}
    assert set(by_name) == {"Alice Adams", "Robert Smith", "Carol Chen"}
    # Bob <bob@x.com> commits (c2, c5) were mailmapped onto Robert Smith (c8)
    assert by_name["Robert Smith"]["commits"] == 3
    assert by_name["Robert Smith"]["churn"] == 8
    assert by_name["Alice Adams"]["churn"] == 14
    assert by_name["Carol Chen"]["churn"] == 8
    assert len(by_name) == 3 and not any(g["merged"] for g in groups)


def test_object_author_metrics_and_ownership(env):
    r = obj(env, "", kind="dir")
    aur = {a["name"]: a for a in r["authors"]}
    assert aur["Alice Adams"]["churn"] == 14
    assert aur["Alice Adams"]["modifications"] == 2
    assert aur["Robert Smith"]["churn"] == 8
    assert aur["Robert Smith"]["modifications"] == 3
    assert aur["Carol Chen"]["churn"] == 8
    assert aur["Alice Adams"]["ownership"] == pytest.approx(14 / 30)
    assert aur["Robert Smith"]["ownership"] == pytest.approx(8 / 30)


def test_author_filter_reports_author_metrics(env):
    conn = db.get_conn()
    try:
        groups = authors_mod.list_authors(conn, env["repo_id"])
        carol = next(g["id"] for g in groups if g["name"] == "Carol Chen")
        r = metrics.object_metrics(conn, env["repo_id"], "", "dir",
                                   metrics.CommitFilter(env["repo_id"]), [carol])
    finally:
        conn.close()
    assert num(r) == (1, 7, 2)  # c4 (+1/-1) + c7 (-6)
    # ownership denominators stay the full commit set
    assert r["totals"]["churn"] == 30
    selected = [a for a in r["authors"] if a["selected"]]
    assert [a["name"] for a in selected] == ["Carol Chen"]
    assert selected[0]["ownership"] == pytest.approx(8 / 30)


def test_manual_merge_and_unmerge(env):
    conn = db.get_conn()
    try:
        groups = authors_mod.list_authors(conn, env["repo_id"])
        alice = next(g["id"] for g in groups if g["name"] == "Alice Adams")
        carol = next(g["id"] for g in groups if g["name"] == "Carol Chen")
        authors_mod.merge_authors(conn, env["repo_id"], [alice, carol])
        r = metrics.object_metrics(conn, env["repo_id"], "", "dir",
                                   metrics.CommitFilter(env["repo_id"]), None)
        merged = {a["name"]: a for a in r["authors"]}
        assert merged["Alice Adams"]["churn"] == 22
        assert merged["Alice Adams"]["modifications"] == 4
        assert merged["Alice Adams"]["ownership"] == pytest.approx(22 / 30)
        assert len(r["authors"]) == 2
        # filtering by the merged group selects both members' contributions
        r2 = metrics.object_metrics(conn, env["repo_id"], "", "dir",
                                    metrics.CommitFilter(env["repo_id"]), [alice])
        assert num(r2) == (15, 7, 4)
        authors_mod.unmerge_author(conn, env["repo_id"], alice)
        groups2 = authors_mod.list_authors(conn, env["repo_id"])
        assert len(groups2) == 3
    finally:
        conn.close()


# ---------------------------------------------------- tree / timeseries

def test_tree_children(env):
    conn = db.get_conn()
    try:
        t = metrics.tree_children(conn, env["repo_id"], "", "dir",
                                  metrics.CommitFilter(env["repo_id"]), None)
    finally:
        conn.close()
    names = {(c["name"], c["kind"]) for c in t["children"]}
    assert names == {
        (".mailmap", "file"), ("a.txt", "file"), ("a2.txt", "file"),
        ("c.txt", "file"), ("x.txt", "file"), ("b", "dir"), ("d", "dir"),
    }
    d = next(c for c in t["children"] if c["name"] == "d")
    assert (d["adds"], d["dels"], d["churn"]) == (6, 6, 12)


def test_timeseries(env):
    conn = db.get_conn()
    try:
        ts = metrics.timeseries(conn, env["repo_id"], metrics.CommitFilter(env["repo_id"]), None)
    finally:
        conn.close()
    assert len(ts["days"]) == 1  # all fixture commits share one day
    day = ts["days"][0]
    assert day["commits"] == 8
    assert (day["adds"], day["dels"]) == (18, 12)


def test_history_for_file(env):
    conn = db.get_conn()
    try:
        h = metrics.object_history(conn, env["repo_id"], "c.txt", "file",
                                   metrics.CommitFilter(env["repo_id"]), None)
    finally:
        conn.close()
    assert [x["subject"].split()[0] for x in h] == ["c5", "c4"]  # newest first
    assert h[0]["author"] == "Robert Smith"  # c5 was mailmapped from Bob
    assert (h[1]["adds"], h[1]["dels"]) == (1, 1)


def test_top_files(env):
    conn = db.get_conn()
    try:
        top = metrics.top_files(conn, env["repo_id"], "", "dir",
                                metrics.CommitFilter(env["repo_id"]), None, limit=10)
    finally:
        conn.close()
    names = [t["path"] for t in top]
    assert names[0] == "d/e.txt"  # churn 8 is the highest
    assert "d/f.txt" in names and "c.txt" in names
