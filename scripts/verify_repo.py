#!/usr/bin/env python3
"""Independent verification of RAT metrics directly from git commands.

This script recomputes metrics with a completely separate implementation
(per-commit `git show --numstat -M50%`, naive Python rollups) and either prints
them or cross-checks them against the tool's own index.

Usage:
  # print naive metrics for one object (compare with provided samples manually)
  python3 scripts/verify_repo.py <git-dir> [--path OBJECT]

  # cross-check against the tool's index in the same data dir
  RAT_DATA_DIR=data python3 scripts/verify_repo.py <git-dir> --repo-id 1

  # restrict the commit set (H_i,j) or use an explicit list
  ... --from TS --to TS   |   --list HASH1,HASH2
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def git(git_dir: str, *args: str) -> bytes:
    r = subprocess.run(["git", "-C", git_dir, *args], capture_output=True)
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {r.stderr.decode(errors='replace')}")
    return r.stdout


def commit_hashes(git_dir: str) -> list[str]:
    return git(git_dir, "rev-list", "--no-merges", "HEAD").decode().split()


def commit_ts(git_dir: str, h: str) -> int:
    return int(git(git_dir, "show", "-s", "--format=%ct", h).decode().strip())


def parse_numstat(data: bytes) -> list[tuple[str, int, int]]:
    """Independent parser for `git show --numstat -z` (rename -> new path)."""
    out: list[tuple[str, int, int]] = []
    tokens = data.split(b"\x00")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        tok_nl = tok.lstrip(b"\n")
        if tok_nl[:41].rstrip(b"\x1f") and b"\x1f" in tok_nl[:64] and i == 0:
            i += 1  # header token (hash line)
            continue
        body = tok_nl
        if body in (b"", b"\n"):
            i += 1
            continue
        parts = body.split(b"\t")
        if len(parts) < 3 and body.endswith(b"\t"):
            # rename: numbers only, next two tokens are old and new paths
            a, d = parts[0], parts[1]
            old, new = tokens[i + 1], tokens[i + 2]
            i += 3
            if a != b"-" and d != b"-":
                out.append((new.decode("utf-8", "replace"), int(a), int(d)))
            else:
                out.append((new.decode("utf-8", "replace"), -1, -1))
            continue
        if len(parts) >= 3:
            a, d, path = parts[0], parts[1], b"\t".join(parts[2:])
            i += 1
            if a == b"-" or d == b"-":
                out.append((path.decode("utf-8", "replace"), -1, -1))  # binary
            else:
                out.append((path.decode("utf-8", "replace"), int(a), int(d)))
            continue
        i += 1
    return out


def naive_metrics(git_dir: str, target: str, frm=None, to=None, hashes=None) -> dict:
    hs = hashes if hashes else commit_hashes(git_dir)
    sel = []
    for h in hs:
        ts = commit_ts(git_dir, h)
        if frm is not None and ts < frm:
            continue
        if to is not None and ts >= to:
            continue
        sel.append((h, ts))
    adds = dels = mods = 0
    for h, _ts in sel:
        data = git(git_dir, "show", "--root", "--numstat", "-z", "-M50%", "--format=%H", h)
        changed = False
        for path, a, d in parse_numstat(data):
            if a == -1:
                continue  # binary: not measured
            if a + d == 0:
                continue
            in_scope = path == target if target else True
            if target and not in_scope:
                # directory scope: any ancestor match means the change rolls up
                parts = path.split("/")
                in_scope = any("/".join(parts[:k]) == target for k in range(1, len(parts)))
            if in_scope:
                adds += a
                dels += d
                changed = True
        if changed:
            mods += 1
    return {
        "object": target or "(repository root)",
        "commits_in_set": len(sel),
        "adds": adds,
        "dels": dels,
        "growth": adds - dels,
        "churn": adds + dels,
        "modifications": mods,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("git_dir")
    ap.add_argument("--path", default="", help="file or directory path ('' = repository)")
    ap.add_argument("--from", dest="frm", type=int, default=None)
    ap.add_argument("--to", type=int, default=None)
    ap.add_argument("--list", dest="hashes", default=None)
    ap.add_argument("--repo-id", type=int, default=None,
                    help="cross-check against the tool index for this repo id")
    args = ap.parse_args()

    hashes = [h for h in (args.hashes or "").split(",") if h] or None
    naive = naive_metrics(args.git_dir, args.path, args.frm, args.to, hashes)

    if args.repo_id is None:
        print(f"naive metrics for {args.path or '(repository root)'}:")
        for k, v in naive.items():
            print(f"  {k}: {v}")
        return 0

    from backend import db, metrics  # noqa: E402

    conn = db.get_conn()
    try:
        kind = metrics.resolve_kind(conn, args.repo_id, args.path, None)
        cf = metrics.CommitFilter(args.repo_id, "list" if hashes else ("range" if (args.frm or args.to) else "all"),
                                  args.frm, args.to, hashes)
        engine = metrics.object_metrics(conn, args.repo_id, args.path, kind, cf, None)
    finally:
        conn.close()
    em = engine["metrics"]
    rows = [
        ("commits_in_set", naive["commits_in_set"], engine["commits_in_set"]),
        ("adds", naive["adds"], em["adds"]),
        ("dels", naive["dels"], em["dels"]),
        ("churn", naive["churn"], em["churn"]),
        ("modifications", naive["modifications"], em["modifications"]),
    ]
    ok = True
    print(f"{'metric':<16} {'naive (git)':>12} {'engine':>12}   status")
    for name, a, b in rows:
        status = "OK" if a == b else "MISMATCH"
        ok = ok and a == b
        print(f"{name:<16} {a:>12} {b:>12}   {status}")
    print("PASS - independent recomputation matches" if ok else "FAIL - mismatches found")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
