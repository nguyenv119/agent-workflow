"""`agent-cli harness startsize`: how many bytes of text a session loaded before its first reply, per piece.

Reads a saved transcript (.jsonl) and adds up the stored text of every attachment record that comes before the
first assistant record: instruction files (instructions; the auto-memory file is its own piece, memory), the skill
listing (skills), each hook's output as the model got it (hooks, one row per hook), and the tool, agent and MCP
listings plus the output style and session context (other). Bytes are UTF-8 bytes, so a `wc -c` on the same text
agrees. Attachments after the first reply (a compaction re-sends the skill listing and hook text) are not counted,
and neither are the environment, model and date notes or the base system prompt, which no file of ours controls.

--budget FILE is a JSON object of caps in KB (1 KB = 1024 bytes): total_kb, skills_kb, memory_kb, instructions_kb,
hooks_kb; a missing key leaves that piece uncapped. Exit 1 when any cap is exceeded. Stdlib only.
"""
import json
from pathlib import Path

from . import core
from .core import Exit

EXAMPLE = "agent-cli harness startsize session.jsonl --budget start-budget.json"
PIECES = ("instructions", "memory", "skills", "hooks", "other")
CAPPED = ("total", "skills", "memory", "instructions", "hooks")
LABEL = 70


def register(hs):
    p = core.subcommand(hs, "startsize", "bytes a session loaded before its first reply, per piece, from its "
                        "transcript (exit 1 over a --budget cap)",
                        [EXAMPLE, "agent-cli harness startsize session.jsonl --json"])
    p.add_argument("transcript", help="a session transcript, one JSON record per line")
    p.add_argument("--budget", metavar="FILE", help="JSON caps in KB: total_kb, skills_kb, memory_kb, "
                   "instructions_kb, hooks_kb")
    p.add_argument("--json", action="store_true", help="one JSON object")
    p.set_defaults(func=startsize_cmd)


def parts_of(a):
    """[(piece, label, text)] for one attachment (a dict with a `type`); [] for a kind that is not counted."""
    kind = a.get("type")
    if kind == "instructions":
        return [("memory" if f.get("type") == "AutoMem" else "instructions", f.get("path", "?"),
                 f.get("content") or "") for f in a.get("files") or []]
    if kind == "skill_listing":
        return [("skills", f"skill listing ({a.get('skillCount', '?')} skills)", a.get("content") or "")]
    if kind == "hook_success":
        label = f"{a.get('hookName', '?')} {' '.join(str(a.get('command', '')).split())}"
        return [("hooks", label[:LABEL], a.get("content") or "")]
    if kind == "hook_additional_context":
        return [("hooks", f"{a.get('hookName', '?')} (context)"[:LABEL], "\n".join(a.get("content") or []))]
    if kind == "deferred_tools_delta":
        return [("other", "deferred tool names", "".join(a.get("addedLines") or []))]
    if kind == "agent_listing_delta":
        return [("other", "agent types", "".join(a.get("addedLines") or []))]
    if kind == "mcp_instructions_delta":
        return [("other", "MCP instructions", "".join(a.get("addedBlocks") or []))]
    if kind == "output_style_instructions":
        return [("other", "output style", (a.get("style") or {}).get("prompt") or "")]
    if kind == "session_context":
        return [("other", "session context", "".join((a.get("context") or {}).values()))]
    return []


def measure(records):
    """{"total", "pieces": {name: {"bytes"}}, "parts": [{piece, label, bytes}]} for the records up to the first
    assistant record."""
    parts = []
    for r in records:
        if r.get("type") == "assistant":
            break
        if r.get("type") == "attachment" and isinstance(r.get("attachment"), dict):
            parts += [{"piece": p, "label": label, "bytes": len(text.encode("utf-8"))}
                      for p, label, text in parts_of(r["attachment"])]
    pieces = {p: {"bytes": sum(x["bytes"] for x in parts if x["piece"] == p)} for p in PIECES}
    return {"total": sum(v["bytes"] for v in pieces.values()), "pieces": pieces, "parts": parts}


def load_budget(path):
    """{name: cap in bytes} from a budget file; exit 2 on a bad file, an unknown key or a non-number."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        core.die(Exit.REFUSED, f"--budget {path}: {e}", EXAMPLE)
    allowed = {f"{k}_kb" for k in CAPPED}
    bad = [k for k in raw if k not in allowed] if isinstance(raw, dict) else None
    if bad is None or bad:
        core.die(Exit.REFUSED, f"--budget {path}: " + (f"unknown key {bad[0]!r}; allowed: {', '.join(sorted(allowed))}"
                                                       if bad else "want a JSON object of caps in KB"), EXAMPLE)
    for k, v in raw.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            core.die(Exit.REFUSED, f"--budget {path}: {k} must be a number of KB, got {v!r}", EXAMPLE)
    return {k[:-3]: int(v * 1024) for k, v in raw.items()}


def read_records(path):
    f = Path(path)
    if not f.is_file():
        core.die(Exit.REFUSED, f"{path} is not a file", EXAMPLE)
    records = []
    for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError as e:
            core.die(Exit.REFUSED, f"{path} line {n} is not JSON: {e}", EXAMPLE)
        if isinstance(r, dict):
            records.append(r)
    return records


def startsize_cmd(a):
    m = measure(read_records(a.transcript))
    if not m["parts"]:
        core.die(Exit.REFUSED, f"{a.transcript} has no start attachments before a reply: not a session transcript?",
                 EXAMPLE)
    budget = load_budget(a.budget) if a.budget else {}
    sizes = {"total": m["total"], **{k: v["bytes"] for k, v in m["pieces"].items()}}
    over = [k for k in CAPPED if k in budget and sizes[k] > budget[k]]
    for k, row in m["pieces"].items():
        if k in budget:
            row.update(budget=budget[k], over_by=max(0, row["bytes"] - budget[k]))
    if a.json:
        print(json.dumps({"total": m["total"], "budget_total": budget.get("total"), "pieces": m["pieces"],
                          "parts": m["parts"], "over": over}))
        return Exit.FAIL if over else Exit.OK
    for k in (*PIECES, "total"):
        line = f"{k}: {sizes[k]} bytes"
        if k in budget:
            line += f", budget {budget[k]}" + (f", over by {sizes[k] - budget[k]}" if k in over else "")
        print(line)
        for x in (x for x in m["parts"] if x["piece"] == k):
            print(f"  {x['bytes']:>7} {x['label']}")
    print("startsize: " + (f"over budget: {', '.join(over)}" if over else "ok"))
    return Exit.FAIL if over else Exit.OK
