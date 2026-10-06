"""Streaming git-history indexer.

A single `git log --numstat -z` pass per repository produces every row needed
for all five metric families:

  commits       non-merge commits reachable from HEAD (set H-bar)
  file_changes  per file per commit: added/removed lines (binary + zero-churn skipped)
  dir_changes   the same change rolled up into every ancestor directory
                (including the root "") so directory/repository metrics are
                exact-path index lookups instead of subtree scans
  objects       every path ever touched (H[F] and H[D])

Framing (probed against git 2.43; covered by tests/test_metrics.py):
  - header token: b"<hash>\\x1f<parents>\\x1f<author>\\x1f<email>\\x1f<ct>\\x1f<subject>"
    terminated by NUL; if the commit has entries it is followed by b"\\n"
  - regular entry: b"<adds>\\t<dels>\\t<path>\\x00" (binary entries use "-")
  - rename entry:  b"<adds>\\t<dels>\\t\\x00<old>\\x00<new>\\x00"  (attributed to <new>)
  - renames are detected by git itself with -M50%, so a pure rename yields
    0/0 and is skipped: renaming alone never changes metrics.
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from pathlib import Path

from . import db

LOG_FORMAT = "%H%x1f%P%x1f%aN%x1f%aE%x1f%ct%x1f%s"
BATCH_COMMITS = 2000
CHUNK_SIZE = 1 << 20

_HEADER_RE = re.compile(rb"^[0-9a-f]{40,64}\x1f")
_RENAME_RE = re.compile(rb"^(?P<a>[0-9]+|-)\t(?P<d>[0-9]+|-)\t$")
_ENTRY_RE = re.compile(rb"^(?P<a>[0-9]+|-)\t(?P<d>[0-9]+|-)\t(?P<path>.+)$", re.DOTALL)

# In-memory progress for the currently running index jobs: repo_id -> dict.
progress: dict[int, dict] = {}


def ancestors(path: str) -> list[str]:
    """Every ancestor directory of a file path, most specific first, incl. ""."""
    out: list[str] = []
    idx = path.rfind("/")
    while idx != -1:
        path = path[:idx]
        out.append(path)
        idx = path.rfind("/")
    out.append("")
    return out


def run_git(git_dir: str, args: list[str], timeout: int | None = None, check: bool = False):
    return subprocess.run(
        ["git", "-C", git_dir, *args],
        capture_output=True, text=True, timeout=timeout, check=check,
    )


def count_commits(git_dir: str) -> int:
    r = run_git(git_dir, ["rev-list", "--count", "--no-merges", "HEAD"])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "cannot read repository history")
    return int(r.stdout.strip())


def extract_mailmap(git_dir: str) -> str | None:
    """Materialise HEAD:.mailmap inside the git dir so `-c mailmap.file=` can use it.

    Works for both bare clones and extracted working trees and makes mailmap
    handling identical in both ingestion paths.
    """
    r = run_git(git_dir, ["cat-file", "-e", "HEAD:.mailmap"])
    if r.returncode != 0:
        return None
    blob = subprocess.run(
        ["git", "-C", git_dir, "show", "HEAD:.mailmap"], capture_output=True
    )
    if blob.returncode != 0:
        return None
    r2 = run_git(git_dir, ["rev-parse", "--absolute-git-dir"])
    if r2.returncode != 0:
        return None
    target = Path(r2.stdout.strip()) / "rat-mailmap"
    try:
        target.write_bytes(blob.stdout)
    except OSError:
        return None
    return str(target)


class _Indexer:
    def __init__(self, conn: sqlite3.Connection, repo_id: int, total: int):
        self.conn = conn
        self.repo_id = repo_id
        self.total = total
        self.parsed = 0
        self._author_cache: dict[tuple[str, str], int] = {}
        self._commit_batch: list[tuple[str, str | None, int, int, str]] = []
        self._change_batch: list[tuple[int, list, dict[str, tuple[int, int]]]] = []
        self._obj_batch: dict[tuple[str, str], None] = {}
        # current commit
        self.cur: dict | None = None
        self.cur_files: list[tuple[str, int, int]] = []
        self.cur_objects: set[str] = set()
        # rename lookahead state
        self._rename_state = 0  # 1 = expecting old path, 2 = expecting new path
        self._rename_old: bytes = b""
        self._nums: tuple[int, int] | None = None

    # ------------------------------------------------------------- ingestion

    def run(self, git_dir: str) -> None:
        mailmap = extract_mailmap(git_dir)
        cmd = ["git", "-C", git_dir, "-c", "core.quotepath=false"]
        if mailmap:
            cmd += ["-c", f"mailmap.file={mailmap}"]
        cmd += [
            "log", "--root", "--no-merges", "-M50%", "--numstat", "-z",
            f"--format={LOG_FORMAT}", "HEAD",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert proc.stdout is not None
        buf = b""
        try:
            while True:
                chunk = proc.stdout.read(CHUNK_SIZE)
                if not chunk:
                    break
                buf += chunk
                tokens = buf.split(b"\x00")
                buf = tokens.pop()
                for tok in tokens:
                    self._feed(tok)
        finally:
            proc.stdout.close()
        stderr = proc.stderr.read().decode("utf-8", "replace")
        rc = proc.wait()
        if rc != 0:
            raise RuntimeError(f"git log failed: {stderr.strip()}")
        if buf.strip(b"\n"):
            self._feed(buf)
        self._flush_commit()
        self._flush_batch()
        self._report()

    def _feed(self, tok: bytes) -> None:
        if self._rename_state == 1:
            self._rename_old = tok
            self._rename_state = 2
            return
        if self._rename_state == 2:
            self._rename_state = 0
            if self._nums is not None:
                self._emit_change(tok, self._nums)
            return

        s = tok.lstrip(b"\n")
        if _HEADER_RE.match(s):
            self._flush_commit()
            self._begin_commit(s)
            return
        m = _RENAME_RE.match(s)
        if m is not None:
            self._nums = self._parse_nums(m)
            self._rename_state = 1
            return
        m = _ENTRY_RE.match(s)
        if m is not None:
            nums = self._parse_nums(m)
            if nums is not None:
                self._emit_change(m.group("path"), nums)
            return
        # empty token / stray whitespace -> ignore

    @staticmethod
    def _parse_nums(m: re.Match) -> tuple[int, int] | None:
        a, d = m.group("a"), m.group("d")
        if a == b"-" or d == b"-":  # git's own binary detection
            return None
        return int(a), int(d)

    def _emit_change(self, path: bytes, nums: tuple[int, int]) -> None:
        p = path.decode("utf-8", "replace")
        # every touched path is an object even when the diff is 0/0 (pure
        # renames, empty-file creation) - it belongs to H[F]/H[D]
        self.cur_objects.add(p)
        adds, dels = nums
        if adds + dels == 0:
            return  # e.g. pure rename: metrics unchanged
        self.cur_files.append((p, adds, dels))

    def _begin_commit(self, header: bytes) -> None:
        fields = header.split(b"\x1f", 5)
        if len(fields) < 6:
            self.cur = None
            return
        h, parents, an, ae, ct, subject = fields
        parent = parents.split(b" ")[0].decode() if parents else None
        self.cur = {
            "hash": h.decode(),
            "parent": parent or None,
            "author_id": self._author_id(
                an.decode("utf-8", "replace"), ae.decode("utf-8", "replace")
            ),
            "ts": int(ct),
            "subject": subject.decode("utf-8", "replace"),
        }
        self.cur_files = []
        self.cur_objects = set()
        self.parsed += 1
        if self.parsed % 32 == 0:
            self._report()

    def _author_id(self, name: str, email: str) -> int:
        key = (name, email)
        aid = self._author_cache.get(key)
        if aid is not None:
            return aid
        self.conn.execute(
            "INSERT OR IGNORE INTO authors(repo_id, name, email) VALUES (?, ?, ?)",
            (self.repo_id, name, email),
        )
        row = self.conn.execute(
            "SELECT id FROM authors WHERE repo_id = ? AND name = ? AND email = ?",
            (self.repo_id, name, email),
        ).fetchone()
        aid = row["id"]
        self._author_cache[key] = aid
        return aid

    def _flush_commit(self) -> None:
        if self.cur is None:
            return
        cur = self.cur
        self.cur = None
        idx = len(self._commit_batch)
        self._commit_batch.append(
            (cur["hash"], cur["parent"], cur["author_id"], cur["ts"], cur["subject"])
        )
        dirs: dict[str, tuple[int, int]] = {}
        for path, adds, dels in self.cur_files:
            for d in ancestors(path):
                acc = dirs.get(d)
                dirs[d] = (adds, dels) if acc is None else (acc[0] + adds, acc[1] + dels)
        for path in self.cur_objects:
            self._obj_batch[(path, "file")] = None
            for d in ancestors(path):
                self._obj_batch[(d, "dir")] = None
        self._obj_batch[("", "dir")] = None
        self._change_batch.append((idx, self.cur_files, dirs))
        self.cur_files = []
        if len(self._commit_batch) >= BATCH_COMMITS:
            self._flush_batch()

    def _flush_batch(self) -> None:
        if not self._commit_batch:
            return
        self.conn.executemany(
            "INSERT INTO commits(repo_id, hash, parent_hash, author_id, committer_ts, subject)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(self.repo_id, *row) for row in self._commit_batch],
        )
        # lastrowid is None after executemany on Python 3.12, and ingestion is
        # serialised by the ingest lock, so MAX(id) is exactly our last insert
        # (ids from AUTOINCREMENT are contiguous within the same transaction).
        last_id = self.conn.execute("SELECT MAX(id) FROM commits").fetchone()[0]
        first_id = last_id - len(self._commit_batch) + 1
        file_rows = []
        dir_rows = []
        for idx, files, dirs in self._change_batch:
            cid = first_id + idx
            author_id, ts = self._commit_batch[idx][2], self._commit_batch[idx][3]
            for path, adds, dels in files:
                file_rows.append((self.repo_id, cid, path, adds, dels, author_id, ts))
            for d, (adds, dels) in dirs.items():
                dir_rows.append((self.repo_id, cid, d, adds, dels, author_id, ts))
        self.conn.executemany(
            "INSERT INTO file_changes(repo_id, commit_id, path, adds, dels, author_id, committer_ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            file_rows,
        )
        self.conn.executemany(
            "INSERT INTO dir_changes(repo_id, commit_id, path, adds, dels, author_id, committer_ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            dir_rows,
        )
        self.conn.executemany(
            "INSERT OR IGNORE INTO objects(repo_id, path, kind) VALUES (?, ?, ?)",
            [(self.repo_id, path, kind) for path, kind in self._obj_batch],
        )
        self._commit_batch.clear()
        self._change_batch.clear()
        self._obj_batch.clear()
        self._report()

    def _report(self) -> None:
        progress[self.repo_id] = {
            "phase": "indexing",
            "parsed": self.parsed,
            "total": self.total,
        }


def index_repo(repo_id: int, git_dir: str) -> dict:
    """(Re)build the full index for a repository. Returns summary stats."""
    total = count_commits(git_dir)
    progress[repo_id] = {"phase": "indexing", "parsed": 0, "total": total}
    conn = db.get_conn()
    try:
        db.drop_indexes(conn)
        conn.execute("DELETE FROM file_changes WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM dir_changes WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM commits WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM objects WHERE repo_id = ?", (repo_id,))
        conn.execute("BEGIN")
        try:
            _Indexer(conn, repo_id, total).run(git_dir)
            conn.execute(
                "UPDATE repos SET commit_count = ?, status = 'ready', error = NULL WHERE id = ?",
                (total, repo_id),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        db.create_indexes(conn)
        row = conn.execute(
            "SELECT COUNT(DISTINCT author_id) AS authors,"
            " (SELECT COUNT(*) FROM objects WHERE repo_id = ? AND kind = 'file') AS files,"
            " (SELECT COUNT(*) FROM objects WHERE repo_id = ? AND kind = 'dir') AS dirs"
            " FROM commits WHERE repo_id = ?",
            (repo_id, repo_id, repo_id),
        ).fetchone()
        return {
            "commits": total,
            "authors": row["authors"],
            "files": row["files"],
            "dirs": row["dirs"],
        }
    finally:
        conn.close()
    # caller (ingest) clears progress on success
