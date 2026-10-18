#!/usr/bin/env python3
"""Fetch the task folders one exam manifest names, so a fresh clone can start an exam.

    python3 harness-loop/tasks/fetch_cache.py <manifest>      (relative to the cwd or to harness-loop/)

Only the `huggingface:<org>/<name>@<40-hex revision>` part of the manifest's `source` is fetched. Each task's
harbor_path must be cache/<name lowercased>/<archive>/<id>: the dataset's <archive>.tar.zst (bugfix,
non_bugfix) is downloaded once from <HARNESS_TASKS_URL, default https://huggingface.co>/datasets/<org>/<name>/
resolve/<revision>/ and only the manifest's <id> folders are unpacked (`tar -xf`). A folder already in the cache
is kept. HARNESS_TASKS_CACHE=<an existing tasks/cache dir> symlinks the folders it holds instead of downloading.
Then `manifest.py --require-cache <manifest>`, and harness-loop/.venv (harbor and swegen from
exam/requirements.txt, `uv venv` + `uv pip install -r`) when it has no bin/harbor.

Exit: 0 ready; 1 a download, unpack, check or venv step failed (the reason printed); 2 usage or a manifest this
script cannot fetch.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

TASKS = Path(__file__).resolve().parent
LOOP_DIR = TASKS.parent
SOURCE = re.compile(r"huggingface:([\w.-]+)/([\w.-]+)@([0-9a-f]{40})\b")
DEFAULT_URL = "https://huggingface.co"


def fail(msg, code=1):
    print(f"fetch_cache: {msg}", file=sys.stderr)
    sys.exit(code)


def wanted(manifest):
    """[(task dir, cache-relative path, archive, id)] of every task, or exit 2 on a path this script cannot fetch."""
    data = json.loads(manifest.read_text())
    m = SOURCE.search(data.get("source", ""))
    if not m:
        fail(f"{manifest}: source has no huggingface:<org>/<name>@<revision>", code=2)
    org, name, rev = m.groups()
    out = []
    for t in data.get("tasks", []):
        parts = Path(t["harbor_path"]).parts
        if len(parts) != 4 or parts[0] != "cache" or parts[1] != name.lower() or parts[3] != t["id"]:
            fail(f"{t['id']}: harbor_path {t['harbor_path']} is not cache/{name.lower()}/<archive>/<id>", code=2)
        out.append((manifest.parent / t["harbor_path"], Path(*parts[1:]), parts[2], t["id"]))
    return (org, name, rev), out


def download(url, dest):
    try:
        with urllib.request.urlopen(url, timeout=600) as r, open(dest, "wb") as fh:
            shutil.copyfileobj(r, fh)
    except (urllib.error.URLError, OSError) as e:
        fail(f"download of {url} failed ({e})")


def fetch(source, tasks):
    org, name, rev = source
    reuse = os.environ.get("HARNESS_TASKS_CACHE")
    todo = {}
    for dest, rel, archive, tid in tasks:
        if dest.is_dir():
            continue
        if reuse and (Path(reuse).expanduser() / rel).is_dir():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.symlink_to((Path(reuse).expanduser() / rel).resolve(), target_is_directory=True)
            print(f"fetch_cache: {tid}: linked from HARNESS_TASKS_CACHE")
            continue
        todo.setdefault(archive, []).append((dest, tid))
    base = os.environ.get("HARNESS_TASKS_URL", DEFAULT_URL).rstrip("/")
    for archive, items in todo.items():
        url = f"{base}/datasets/{org}/{name}/resolve/{rev}/{archive}.tar.zst"
        with tempfile.TemporaryDirectory(prefix="fetch-cache-", dir=TASKS) as tmp:
            print(f"fetch_cache: downloading {url} for {len(items)} task(s)")
            download(url, Path(tmp) / "a.tar.zst")
            p = subprocess.run(["tar", "-xf", "a.tar.zst", *(tid for _, tid in items)], cwd=tmp,
                               capture_output=True, text=True)
            if p.returncode:
                fail(f"unpacking {archive}.tar.zst failed: {p.stderr.strip()[-300:]}")
            for dest, tid in items:
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(Path(tmp) / tid, dest)
                print(f"fetch_cache: {tid}: fetched")


def venv():
    """harness-loop/.venv with harbor, made with uv when it has none."""
    env = LOOP_DIR / ".venv"
    if (env / "bin/harbor").is_file():
        return
    steps = ([] if (env / "bin/python").is_file() else [["uv", "venv", str(env), "--python", "3.12"]])
    steps.append(["uv", "pip", "install", "--python", str(env / "bin/python"),
                  "-r", str(LOOP_DIR / "exam/requirements.txt")])
    for cmd in steps:
        print(f"fetch_cache: {' '.join(cmd[:3])} ...")
        try:
            p = subprocess.run(cmd, capture_output=True, text=True)
        except OSError as e:
            fail(f"{cmd[0]} not found ({e.__class__.__name__}); install uv, then run again")
        if p.returncode:
            fail(f"{' '.join(cmd[:3])} failed: {p.stderr.strip()[-300:]}")


def main(argv):
    if len(argv) != 1 or argv[0].startswith("-"):
        fail("usage: fetch_cache.py <manifest>", code=2)
    manifest = Path(argv[0])
    if not manifest.is_absolute() and not manifest.exists():
        manifest = LOOP_DIR / manifest
    if not manifest.is_file():
        fail(f"no manifest at {manifest}", code=2)
    source, tasks = wanted(manifest.resolve())
    fetch(source, tasks)
    p = subprocess.run([sys.executable, str(TASKS / "manifest.py"), "--require-cache", str(manifest)],
                       capture_output=True, text=True)
    if p.returncode:
        fail(f"manifest check failed: {(p.stderr or p.stdout).strip()}")
    print(p.stdout.strip())
    venv()
    print("fetch_cache: ready")


if __name__ == "__main__":
    main(sys.argv[1:])
