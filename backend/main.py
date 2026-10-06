"""Repo Analysis Tool - FastAPI application (REST API + static dashboard)."""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import authors as authors_mod
from . import db, ingest, metrics

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    conn = db.get_conn()
    try:
        conn.execute(
            "UPDATE repos SET status = 'failed',"
            " error = COALESCE(error, 'interrupted by a server restart - use reindex')"
            " WHERE status IN ('pending', 'cloning', 'extracting', 'indexing')"
        )
    finally:
        conn.close()
    yield


app = FastAPI(title="Repo Analysis Tool", lifespan=lifespan)


class CloneBody(BaseModel):
    url: str


class MergeBody(BaseModel):
    ids: list[int]


class UnmergeBody(BaseModel):
    id: int


def _repo_or_404(conn, repo_id: int) -> dict:
    repo = ingest.get_repo(conn, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="repository not found")
    return repo


def _require_ready(repo: dict) -> None:
    if repo["status"] != "ready":
        raise HTTPException(
            status_code=409,
            detail=f"repository is not ready yet (status: {repo['status']})",
        )


def _query_ctx(repo_id: int, mode: str, from_: int | None, to: int | None,
               commits: str | None, authors: str | None):
    conn = db.get_conn()
    repo = _repo_or_404(conn, repo_id)
    _require_ready(repo)
    cf = metrics.filter_from_params(
        repo_id, {"mode": mode, "from": from_, "to": to, "commits": commits}
    )
    author_ids = metrics.parse_author_ids(authors)
    return conn, repo, cf, author_ids


# ------------------------------------------------------------------ repos

@app.get("/api/repos")
def list_repos():
    conn = db.get_conn()
    try:
        out = []
        for r in conn.execute("SELECT id FROM repos ORDER BY created_at DESC"):
            repo = ingest.get_repo(conn, r["id"])
            if repo["status"] == "ready":
                repo["summary"] = metrics.repo_summary(conn, repo["id"])
            out.append(repo)
        return out
    finally:
        conn.close()


@app.post("/api/repos/zip")
async def upload_zip(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="zip is larger than 2 GB")
    filename = file.filename or "repository.zip"
    repo_id = ingest.create_repo(ingest.derive_name(filename), "zip", filename)
    ingest.start_zip_ingest(repo_id, filename, data)
    conn = db.get_conn()
    try:
        return ingest.get_repo(conn, repo_id)
    finally:
        conn.close()


@app.post("/api/repos/clone")
def clone_repo(body: CloneBody):
    url = (body.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="repository URL is required")
    try:
        ingest.validate_clone_url(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    repo_id = ingest.create_repo(ingest.derive_name(url), "url", url)
    ingest.start_clone_ingest(repo_id, url)
    conn = db.get_conn()
    try:
        return ingest.get_repo(conn, repo_id)
    finally:
        conn.close()


@app.get("/api/repos/{repo_id}")
def get_repo(repo_id: int):
    conn = db.get_conn()
    try:
        repo = _repo_or_404(conn, repo_id)
        if repo["status"] == "ready":
            repo["summary"] = metrics.repo_summary(conn, repo_id)
        return repo
    finally:
        conn.close()


@app.delete("/api/repos/{repo_id}")
def remove_repo(repo_id: int):
    conn = db.get_conn()
    try:
        _repo_or_404(conn, repo_id)
        if ingest.is_busy(repo_id):
            raise HTTPException(status_code=409, detail="repository is busy indexing")
    finally:
        conn.close()
    ingest.delete_repo(repo_id)
    return {"deleted": repo_id}


@app.post("/api/repos/{repo_id}/reindex")
def reindex(repo_id: int):
    conn = db.get_conn()
    try:
        _repo_or_404(conn, repo_id)
        if ingest.is_busy(repo_id):
            raise HTTPException(status_code=409, detail="repository is already indexing")
    finally:
        conn.close()
    try:
        ingest.start_reindex(repo_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    conn = db.get_conn()
    try:
        return ingest.get_repo(conn, repo_id)
    finally:
        conn.close()


# ----------------------------------------------------------------- authors

@app.get("/api/repos/{repo_id}/authors")
def list_authors(repo_id: int):
    conn = db.get_conn()
    try:
        _repo_or_404(conn, repo_id)
        return authors_mod.list_authors(conn, repo_id)
    finally:
        conn.close()


@app.post("/api/repos/{repo_id}/authors/merge")
def merge_authors(repo_id: int, body: MergeBody):
    conn = db.get_conn()
    try:
        _repo_or_404(conn, repo_id)
        try:
            return authors_mod.merge_authors(conn, repo_id, body.ids)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    finally:
        conn.close()


@app.post("/api/repos/{repo_id}/authors/unmerge")
def unmerge_author(repo_id: int, body: UnmergeBody):
    conn = db.get_conn()
    try:
        _repo_or_404(conn, repo_id)
        authors_mod.unmerge_author(conn, repo_id, body.id)
        return {"unmerged": body.id}
    finally:
        conn.close()


# ----------------------------------------------------------------- commits

@app.get("/api/repos/{repo_id}/commits")
def list_commits(
    repo_id: int,
    page: int = 1,
    page_size: int = Query(50, ge=1, le=200),
    q: str | None = None,
    author: int | None = None,
    from_: int | None = Query(None, alias="from"),
    to: int | None = None,
):
    conn = db.get_conn()
    try:
        _repo_or_404(conn, repo_id)
        conds = ["repo_id = ?"]
        params: list = [repo_id]
        if q:
            conds.append("(subject LIKE ? OR hash LIKE ?)")
            params += [f"%{q}%", f"{q}%"]
        if author is not None:
            groups = authors_mod.author_groups(conn, repo_id)
            ids = groups.get(author, {}).get("members", [author])
            conds.append(f"author_id IN ({','.join('?' * len(ids))})")
            params += ids
        if from_ is not None:
            conds.append("committer_ts >= ?")
            params.append(from_)
        if to is not None:
            conds.append("committer_ts < ?")
            params.append(to)
        where = " AND ".join(conds)
        total = conn.execute(f"SELECT COUNT(*) AS n FROM commits WHERE {where}", params).fetchone()["n"]
        rows = conn.execute(
            f"SELECT hash, committer_ts, subject, author_id FROM commits WHERE {where}"
            f" ORDER BY committer_ts DESC LIMIT ? OFFSET ?",
            [*params, page_size, (max(page, 1) - 1) * page_size],
        ).fetchall()
        amap = {
            r["author_id"]: r["name"]
            for r in conn.execute(
                "SELECT DISTINCT a.id AS author_id, a.name FROM authors a WHERE a.repo_id = ?",
                (repo_id,),
            )
        }
        return {
            "total": total,
            "page": max(page, 1),
            "page_size": page_size,
            "commits": [
                {
                    "hash": r["hash"], "ts": r["committer_ts"],
                    "subject": r["subject"], "author": amap.get(r["author_id"], "?"),
                    "author_id": r["author_id"],
                }
                for r in rows
            ],
        }
    finally:
        conn.close()


@app.get("/api/repos/{repo_id}/search")
def search_objects(repo_id: int, q: str = ""):
    conn = db.get_conn()
    try:
        _repo_or_404(conn, repo_id)
        objs = metrics.get_objects(conn, repo_id)
        needle = q.lower().strip()
        hits = []
        for p in objs["paths"]:
            if not p:
                continue
            if needle in p.lower():
                kind = "file" if p in objs["files"] else "dir"
                hits.append({"path": p, "kind": kind})
                if len(hits) >= 40:
                    break
        hits.sort(key=lambda h: (len(h["path"]), h["path"]))
        return hits
    finally:
        conn.close()


# ----------------------------------------------------------------- metrics

@app.get("/api/repos/{repo_id}/metrics")
def get_metrics(
    repo_id: int,
    path: str = "",
    kind: str | None = None,
    mode: str = "all",
    from_: int | None = Query(None, alias="from"),
    to: int | None = None,
    commits: str | None = None,
    authors: str | None = None,
):
    conn, _repo, cf, author_ids = _query_ctx(repo_id, mode, from_, to, commits, authors)
    try:
        k = metrics.resolve_kind(conn, repo_id, path, kind)
        return metrics.object_metrics(conn, repo_id, path, k, cf, author_ids)
    finally:
        conn.close()


@app.get("/api/repos/{repo_id}/tree")
def get_tree(
    repo_id: int,
    path: str = "",
    kind: str | None = None,
    mode: str = "all",
    from_: int | None = Query(None, alias="from"),
    to: int | None = None,
    commits: str | None = None,
    authors: str | None = None,
    limit: int = Query(2000, ge=1, le=10000),
):
    conn, _repo, cf, author_ids = _query_ctx(repo_id, mode, from_, to, commits, authors)
    try:
        k = metrics.resolve_kind(conn, repo_id, path, kind)
        return metrics.tree_children(conn, repo_id, path, k, cf, author_ids, limit=limit)
    finally:
        conn.close()


@app.get("/api/repos/{repo_id}/timeseries")
def get_timeseries(
    repo_id: int,
    mode: str = "all",
    from_: int | None = Query(None, alias="from"),
    to: int | None = None,
    commits: str | None = None,
    authors: str | None = None,
):
    conn, _repo, cf, author_ids = _query_ctx(repo_id, mode, from_, to, commits, authors)
    try:
        return metrics.timeseries(conn, repo_id, cf, author_ids)
    finally:
        conn.close()


@app.get("/api/repos/{repo_id}/top")
def get_top(
    repo_id: int,
    path: str = "",
    kind: str | None = None,
    by: str = "files",
    mode: str = "all",
    from_: int | None = Query(None, alias="from"),
    to: int | None = None,
    commits: str | None = None,
    authors: str | None = None,
    limit: int = Query(20, ge=1, le=100),
):
    conn, _repo, cf, author_ids = _query_ctx(repo_id, mode, from_, to, commits, authors)
    try:
        k = metrics.resolve_kind(conn, repo_id, path, kind)
        if by == "authors":
            return {"items": metrics.top_authors(conn, repo_id, path, k, cf, author_ids, limit)}
        return {"items": metrics.top_files(conn, repo_id, path, k, cf, author_ids, limit)}
    finally:
        conn.close()


@app.get("/api/repos/{repo_id}/history")
def get_history(
    repo_id: int,
    path: str = "",
    kind: str | None = None,
    mode: str = "all",
    from_: int | None = Query(None, alias="from"),
    to: int | None = None,
    commits: str | None = None,
    authors: str | None = None,
    limit: int = Query(300, ge=1, le=5000),
):
    conn, _repo, cf, author_ids = _query_ctx(repo_id, mode, from_, to, commits, authors)
    try:
        k = metrics.resolve_kind(conn, repo_id, path, kind)
        return {"history": metrics.object_history(conn, repo_id, path, k, cf, author_ids, limit)}
    finally:
        conn.close()


@app.get("/api/repos/{repo_id}/export.csv")
def export_csv(
    repo_id: int,
    path: str = "",
    kind: str | None = None,
    mode: str = "all",
    from_: int | None = Query(None, alias="from"),
    to: int | None = None,
    commits: str | None = None,
    authors: str | None = None,
):
    conn, repo, cf, author_ids = _query_ctx(repo_id, mode, from_, to, commits, authors)
    try:
        k = metrics.resolve_kind(conn, repo_id, path, kind)
        body = metrics.export_csv(conn, repo_id, path, k, cf, author_ids)
        name = f"{repo['name']}-{path or 'root'}".replace("/", "_")
        return PlainTextResponse(
            body, media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
        )
    finally:
        conn.close()


# ------------------------------------------------------------------ static

app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
