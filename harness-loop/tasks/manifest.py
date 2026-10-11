"""Load and validate an exam task manifest. Usage: python3 manifest.py <manifest.json>"""
import json
import sys
from collections import Counter
from pathlib import Path

FIELDS = ("id", "harbor_path", "repo", "pr", "merged_at", "language", "kind",
          "workdir", "test_command", "title", "screen")
KINDS = ("bugfix", "feature")


def _hidden_names(task_dir):
    """File names under the cached task's hidden tests/ folder (empty if not downloaded)."""
    tests = task_dir / "tests"
    return {p.name for p in tests.rglob("*") if p.is_file()} if tests.is_dir() else set()


def load(path):
    """Return the manifest dict; raise ValueError naming the task id on any problem."""
    path = Path(path)
    data = json.loads(path.read_text())
    for key in ("set", "source", "tasks"):
        if key not in data:
            raise ValueError(f"{path}: missing top-level field {key}")
    seen = set()
    for i, t in enumerate(data["tasks"]):
        tid = t.get("id", f"#{i}")
        missing = [f for f in FIELDS if f not in t]
        if missing:
            raise ValueError(f"{tid}: missing field(s) {', '.join(missing)}")
        if tid in seen:
            raise ValueError(f"duplicate id {tid}")
        seen.add(tid)
        if t["kind"] not in KINDS:
            raise ValueError(f"{tid}: kind must be one of {KINDS}, got {t['kind']!r}")
        cmd = t["test_command"]
        if cmd is None:
            continue
        if "/tests/" in cmd:
            raise ValueError(f"{tid}: test_command uses the hidden /tests/ mount: {cmd}")
        leaked = sorted(n for n in _hidden_names(path.parent / t["harbor_path"]) if n in cmd)
        if leaked:
            raise ValueError(f"{tid}: test_command names hidden test file(s) {', '.join(leaked)}")
    return data


def summary(data):
    def counts(key):
        c = Counter(t[key] for t in data["tasks"])
        return ", ".join(f"{n} {k}" for k, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))
    return (f"{data['set']}: {len(data['tasks'])} tasks valid "
            f"({counts('language')}; {counts('kind')})")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(summary(load(sys.argv[1])))
