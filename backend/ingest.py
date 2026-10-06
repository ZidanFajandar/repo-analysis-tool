"""Repository ingestion: zip uploads, remote clones and repo lifecycle.

All indexing jobs are serialised through a single lock: the batch-insert id
mapping in the indexer assumes a single writer, and it also keeps the UI's
progress reporting sane.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path

from . import db, indexer

_URL_RE = re.compile(r"^(https?://|ssh://|git://|file://|git@)[^\s]+$")
_MAX_ZIP_DEPTH = 4

_LOCK = threading.Lock()
_active: set[int] = set()


def repos_dir() -> Path:
    p = db.data_dir() / "repos"
    p.mkdir(parents=True, exist_ok=True)
    return p


def repo_dir(repo_id: int) -> Path:
    return repos_dir() / str(repo_id)


def is_busy(repo_id: int) -> bool:
    return repo_id in _active


# --------------------------------------------------------------------- CRUD

def create_repo(name: str, source: str, origin: str | None) -> int:
    conn = db.get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO repos(name, source, origin, path, status, created_at)"
            " VALUES (?, ?, ?, '', 'pending', ?)",
            (name, source, origin, int(time.time())),
        )
        return cur.lastrowid
    finally:
        conn.close()


def get_repo(conn, repo_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    prog = indexer.progress.get(repo_id)
    if prog is None:
        prog = {"phase": d["status"], "parsed": 0, "total": d["commit_count"], "error": d["error"]}
    d["progress"] = prog
    d["busy"] = is_busy(repo_id)
    return d


def delete_repo(repo_id: int) -> None:
    conn = db.get_conn()
    try:
        conn.execute("DELETE FROM file_changes WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM dir_changes WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM commits WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM objects WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM authors WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
    finally:
        conn.close()
    indexer.progress.pop(repo_id, None)
    shutil.rmtree(repo_dir(repo_id), ignore_errors=True)
    from . import metrics
    metrics.invalidate_objects(repo_id)


def _set_status(repo_id: int, status: str, error: str | None = None, path: str | None = None) -> None:
    conn = db.get_conn()
    try:
        if path is not None:
            conn.execute(
                "UPDATE repos SET status = ?, error = ?, path = ? WHERE id = ?",
                (status, error, path, repo_id),
            )
        else:
            conn.execute(
                "UPDATE repos SET status = ?, error = ? WHERE id = ?",
                (status, error, repo_id),
            )
    finally:
        conn.close()


# ------------------------------------------------------------------- zip

def _find_git_dir(root: Path, depth: int = 0) -> Path | None:
    """Locate the directory to run `git -C` in inside an extracted zip."""
    if (root / ".git").exists():
        return root
    if (root / "HEAD").is_file() and (root / "objects").is_dir():
        return root  # bare repository layout
    if depth >= _MAX_ZIP_DEPTH:
        return None
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        found = _find_git_dir(sub, depth + 1)
        if found is not None:
            return found
    return None


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    dest_real = dest.resolve()
    for info in zf.infolist():
        target = (dest_real / info.filename).resolve()
        if target != dest_real and not str(target).startswith(str(dest_real) + os.sep):
            raise ValueError(f"zip entry escapes extraction directory: {info.filename}")
    zf.extractall(dest_real)


def _prepare_zip(repo_id: int, filename: str, data: bytes) -> str:
    dest = repo_dir(repo_id) / "work"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    tmp = _write_tmp(repo_id, data)
    if not zipfile.is_zipfile(tmp):
        raise ValueError("uploaded file is not a valid zip archive")
    with zipfile.ZipFile(tmp) as zf:
        _safe_extract(zf, dest)
    tmp.unlink(missing_ok=True)
    root = _find_git_dir(dest)
    if root is None:
        raise ValueError(
            "no .git directory found in the zip - upload the repository "
            "including its .git folder"
        )
    r = indexer.run_git(str(root), ["rev-parse", "--git-dir"])
    if r.returncode != 0:
        raise ValueError(
            "found .git but it is not usable (a .git file must point to a git "
            "directory included in the zip)"
        )
    return str(root)


def _write_tmp(repo_id: int, data: bytes) -> Path:
    p = repo_dir(repo_id) / "upload.zip"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


# ------------------------------------------------------------------ clone

def validate_clone_url(url: str) -> None:
    if not _URL_RE.match(url or ""):
        raise ValueError("invalid repository URL (expected https://, ssh://, git://, file:// or git@...)")


def _prepare_clone(repo_id: int, url: str) -> str:
    validate_clone_url(url)
    dest = repo_dir(repo_id) / "repo.git"
    if dest.exists():
        shutil.rmtree(dest)
    try:
        r = subprocess.run(
            ["git", "clone", "--bare", "--quiet", "--", url, str(dest)],
            capture_output=True, text=True, timeout=1800,
        )
    except subprocess.TimeoutExpired:
        raise ValueError("clone timed out after 30 minutes")
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        raise ValueError("git clone failed: " + (msg[-1] if msg else "unknown error"))
    r2 = indexer.run_git(str(dest), ["rev-parse", "HEAD"])
    if r2.returncode != 0:
        raise ValueError("cloned repository has no commits")
    return str(dest)


# ------------------------------------------------------------------- jobs

def _job(repo_id: int, prepare, phase: str) -> None:
    with _LOCK:
        try:
            _set_status(repo_id, phase)
            git_dir = prepare()
            _set_status(repo_id, "indexing", path=git_dir)
            indexer.progress[repo_id] = {"phase": "indexing", "parsed": 0, "total": 0}
            stats = indexer.index_repo(repo_id, git_dir)
            indexer.progress.pop(repo_id, None)
            from . import metrics
            metrics.invalidate_objects(repo_id)
            _ = stats
        except Exception as exc:  # noqa: BLE001 - surface anything to the UI
            indexer.progress.pop(repo_id, None)
            _set_status(repo_id, "failed", str(exc))
        finally:
            _active.discard(repo_id)


def start_zip_ingest(repo_id: int, filename: str, data: bytes) -> None:
    _active.add(repo_id)
    t = threading.Thread(
        target=_job, args=(repo_id, lambda: _prepare_zip(repo_id, filename, data), "extracting"),
        daemon=True,
    )
    t.start()


def start_clone_ingest(repo_id: int, url: str) -> None:
    _active.add(repo_id)
    t = threading.Thread(
        target=_job, args=(repo_id, lambda: _prepare_clone(repo_id, url), "cloning"),
        daemon=True,
    )
    t.start()


def start_reindex(repo_id: int) -> None:
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT path FROM repos WHERE id = ?", (repo_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise KeyError(repo_id)
    git_dir = row["path"]
    if not git_dir or not Path(git_dir).exists():
        raise ValueError("repository files are missing - delete and re-import it")
    _active.add(repo_id)
    t = threading.Thread(target=_job, args=(repo_id, lambda: git_dir, "indexing"), daemon=True)
    t.start()


def derive_name(value: str) -> str:
    """Best-effort repo name from a URL or zip filename."""
    v = (value or "").strip().rstrip("/")
    v = v.split("/")[-1].split("\\")[-1]
    v = re.sub(r"\.(git|zip)$", "", v)
    return v or "repository"
