#!/usr/bin/env python3
"""Build a deterministic fixture git repository with hand-computable metrics.

History (all timestamps fixed, UTC; base = 2024-01-01T00:00:00Z = 1704067200):

  c1  +0m   Alice          initial: a.txt (3 lines), b/c.txt (4 lines), .mailmap
  c2  +10m  Bob            a.txt -> l1,l2b,l3,l4 (+2/-1); add bin.dat (binary)
  c3  +20m  Alice          pure rename a.txt -> a2.txt (no metric change)
  c4  +30m  Carol          rename b/c.txt -> c.txt and edit c4 -> c4x (+1/-1)
  c5  +40m  Bob            delete c.txt (-4)
  c6  +50m  Alice          add d/e.txt (4 lines), d/f.txt (2 lines) (+6)
  c7  +60m  Carol          delete d/e.txt, d/f.txt (-6, dir d removed)
  c8  +70m  Robert Smith   [branch side] modify bin.dat (binary), add x.txt (1)
  merge +80m Alice         merge side into main with --no-ff (EXCLUDED from metrics)

.mailmap maps Bob <bob@x.com> -> Robert Smith <robert@example.com>, so c2, c5 and
c8 must collapse to one author identity after mailmap application.

Usage: python3 scripts/make_fixture.py [output_dir]
Default output dir: data/fixture_repo
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

T0 = 1704067200
STEP = 600  # 10 minutes


def git(repo: Path, *args, env_extra=None):
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env, check=True, capture_output=True, text=True,
    )


def write(repo: Path, rel: str, content: bytes):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)


def commit(repo: Path, msg: str, ts: int, name: str, email: str):
    env = {
        "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
        "GIT_AUTHOR_DATE": f"{ts} +0000", "GIT_COMMITTER_DATE": f"{ts} +0000",
    }
    git(repo, "add", "-A")
    git(repo, "commit", "-m", msg, env_extra=env)


def build(dest: Path):
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    git(dest, "init", "-q", "-b", "main")
    git(dest, "config", "user.name", "Fixture")
    git(dest, "config", "user.email", "fixture@example.com")

    alice = ("Alice Adams", "alice@example.com")
    bob = ("Bob", "bob@x.com")
    carol = ("Carol Chen", "carol@example.com")
    robert = ("Robert Smith", "robert@example.com")

    # c1: initial commit
    write(dest, ".mailmap", b"Robert Smith <robert@example.com> Bob <bob@x.com>\n")
    write(dest, "a.txt", b"l1\nl2\nl3\n")
    write(dest, "b/c.txt", b"c1\nc2\nc3\nc4\n")
    commit(dest, "c1 initial", T0 + 0 * STEP, *alice)

    # c2: modify a.txt (+2/-1), add binary
    write(dest, "a.txt", b"l1\nl2b\nl3\nl4\n")
    write(dest, "bin.dat", bytes([0, 1, 2, 3, 255, 10, 0, 5]))
    commit(dest, "c2 modify and binary", T0 + 1 * STEP, *bob)

    # c3: pure rename a.txt -> a2.txt
    git(dest, "mv", "a.txt", "a2.txt")
    commit(dest, "c3 rename only", T0 + 2 * STEP, *alice)

    # c4: rename b/c.txt -> c.txt while editing (c4 -> c4x)
    git(dest, "mv", "b/c.txt", "c.txt")
    write(dest, "c.txt", b"c1\nc2\nc3\nc4x\n")
    commit(dest, "c4 rename and edit", T0 + 3 * STEP, *carol)

    # c5: delete c.txt
    (dest / "c.txt").unlink()
    commit(dest, "c5 delete file", T0 + 4 * STEP, *bob)

    # c6: add directory d with two files
    write(dest, "d/e.txt", b"e1\ne2\ne3\ne4\n")
    write(dest, "d/f.txt", b"f1\nf2\n")
    commit(dest, "c6 add dir d", T0 + 5 * STEP, *alice)

    # c7: delete directory d
    (dest / "d/e.txt").unlink()
    (dest / "d/f.txt").unlink()
    commit(dest, "c7 delete dir d", T0 + 6 * STEP, *carol)

    # c8: side branch: modify binary, add x.txt
    git(dest, "checkout", "-q", "-b", "side-feature")
    write(dest, "bin.dat", bytes([0, 9, 9, 9, 255, 10]))
    write(dest, "x.txt", b"x1\n")
    commit(dest, "c8 side changes", T0 + 7 * STEP, *robert)

    # merge back (excluded from metrics)
    git(dest, "checkout", "-q", "main")
    env = {
        "GIT_AUTHOR_NAME": alice[0], "GIT_AUTHOR_EMAIL": alice[1],
        "GIT_COMMITTER_NAME": alice[0], "GIT_COMMITTER_EMAIL": alice[1],
        "GIT_AUTHOR_DATE": f"{T0 + 8 * STEP} +0000",
        "GIT_COMMITTER_DATE": f"{T0 + 8 * STEP} +0000",
    }
    git(dest, "merge", "--no-ff", "-m", "merge side-feature", "side-feature", env_extra=env)

    out = subprocess.run(
        ["git", "-C", str(dest), "log", "--oneline", "--graph"],
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
        capture_output=True, text=True, check=True,
    )
    print(out.stdout)
    print("Fixture repo built at:", dest)


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/fixture_repo")
    build(target.resolve())
