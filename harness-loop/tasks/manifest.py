"""Load and validate an exam task manifest.

Usage: python3 manifest.py [--require-cache] <manifest.json>
"""
import json
import re
import shlex
import sys
from collections import Counter
from pathlib import Path

FIELDS = ("id", "harbor_path", "repo", "pr", "merged_at", "language", "kind",
          "workdir", "test_command", "title", "screen")
KINDS = ("bugfix", "feature")
HARNESS_FILES = {"test.sh", "Dockerfile"}  # verifier plumbing, not test content
# `cat > path <<` in test.sh: Monthly-SWEBench verifiers write their tests this way.
HEREDOC_TARGET = re.compile(r"""cat\s*>\s*["']?([^\s"'<>]+)["']?\s*<<""")


def task_dir(manifest_path, harbor_path):
    """Absolute task folder: harbor_path resolved against the manifest's own folder."""
    return Path(manifest_path).resolve().parent / harbor_path


def hidden_paths(tdir):
    """Hidden test paths for a cached task (None if tests/ is not downloaded).

    Files under tests/ (relative to it, harness files skipped), plus files test.sh
    writes from heredocs, with any leading `$VAR/` stripped.
    """
    tests = Path(tdir) / "tests"
    if not tests.is_dir():
        return None
    out = {p.relative_to(tests).as_posix() for p in tests.rglob("*")
           if p.is_file() and p.name not in HARNESS_FILES}
    sh = tests / "test.sh"
    if sh.is_file():
        for target in HEREDOC_TARGET.findall(sh.read_text(errors="replace")):
            out.add(re.sub(r"^\$\{?\w+\}?/", "", target))
    return out


def _tokens(cmd):
    """Path-like tokens of a shell command: `a=b` gives b, `f.py::Test` gives f.py."""
    for tok in shlex.split(cmd):
        tok = tok.split("=")[-1].split("::")[0]
        yield tok[2:] if tok.startswith("./") else tok


def _names(tok, path):
    return tok == path or tok.endswith("/" + path)


def _check_types(tid, t):
    checks = (("screen", isinstance(t["screen"], bool), "a bool"),
              ("test_command", t["test_command"] is None or isinstance(t["test_command"], str),
               "a string or null"),
              ("pr", t["pr"] is None or (isinstance(t["pr"], int) and not isinstance(t["pr"], bool)),
               "an int or null"))
    for field, ok, want in checks:
        if not ok:
            raise ValueError(f"{tid}: {field} must be {want}, got {t[field]!r}")


def load(path, require_cache=False):
    """Return the manifest dict; raise ValueError naming the task id on any problem.

    Adds an absolute `task_dir` to each row. With require_cache, every task folder
    must be downloaded so the hidden-test leak check actually runs.
    """
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: manifest must be a JSON object")
    for key, typ in (("set", str), ("source", str), ("tasks", list)):
        if not isinstance(data.get(key), typ):
            raise ValueError(f"{path}: top-level field {key} missing or not a {typ.__name__}")
    seen = set()
    for i, t in enumerate(data["tasks"]):
        if not isinstance(t, dict):
            raise ValueError(f"#{i}: task row must be an object")
        tid = t.get("id", f"#{i}")
        missing = [f for f in FIELDS if f not in t]
        if missing:
            raise ValueError(f"{tid}: missing field(s) {', '.join(missing)}")
        if tid in seen:
            raise ValueError(f"duplicate id {tid}")
        seen.add(tid)
        if t["kind"] not in KINDS:
            raise ValueError(f"{tid}: kind must be one of {KINDS}, got {t['kind']!r}")
        _check_types(tid, t)
        tdir = task_dir(path, t["harbor_path"])
        t["task_dir"] = str(tdir)
        hidden = hidden_paths(tdir)
        if hidden is None and require_cache:
            raise ValueError(f"{tid}: task folder not downloaded: {tdir}")
        cmd = t["test_command"]
        if cmd is None:
            continue
        toks = list(_tokens(cmd))
        if any(tok == "/tests" or tok.startswith("/tests/") for tok in toks):
            raise ValueError(f"{tid}: test_command uses the hidden /tests/ mount: {cmd}")
        leaked = sorted(h for h in hidden or () if any(_names(tok, h) for tok in toks))
        if leaked:
            raise ValueError(f"{tid}: test_command names hidden test file(s) {', '.join(leaked)}")
    return data


def summary(data):
    def counts(key):
        c = Counter(t[key] for t in data["tasks"])
        return ", ".join(f"{n} {k}" for k, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))
    checked = sum(hidden_paths(t["task_dir"]) is not None for t in data["tasks"])
    n = len(data["tasks"])
    return (f"{data['set']}: {n} tasks valid "
            f"({counts('language')}; {counts('kind')}); leak-checked {checked}/{n}")


if __name__ == "__main__":
    args = sys.argv[1:]
    require = "--require-cache" in args
    args = [a for a in args if a != "--require-cache"]
    if len(args) != 1:
        sys.exit(__doc__)
    try:
        print(summary(load(args[0], require_cache=require)))
    except ValueError as e:
        sys.exit(f"invalid: {e}")
