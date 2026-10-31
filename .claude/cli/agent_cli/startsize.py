"""`agent-cli harness startsize`: how many bytes of text a session loaded before its first reply, per piece.

Reads a saved transcript (.jsonl) and adds up the stored text of every attachment record that comes before the
first assistant record: instruction files (instructions; the auto-memory file is its own piece, memory), the skill
listing (skills), each hook's output as the model got it (hooks, one row per hook), and the tool, agent and MCP
listings (listings), the output style and session context (other). Bytes are UTF-8 bytes, so a `wc -c` on the same text
agrees. Attachments after the first reply (a compaction re-sends the skill listing and hook text) are not counted,
and neither are the environment, model and date notes, the base system prompt, or any attachment kind not named
above (edited files, permissions, snapshots, cancelled hooks), which no file of ours controls. Reading stops at the
first reply, so a long or half-written tail is never parsed.

--budget FILE is a JSON object of caps in KB (1 KB = 1024 bytes): total_kb, skills_kb, memory_kb, instructions_kb,
hooks_kb; a missing key leaves that piece uncapped. Exit 1 when any cap is exceeded. --exclude PIECE[,PIECE] drops
those pieces from the total (the pieces are still reported), e.g. `--exclude listings` when the tool, agent and MCP
listings are not ours to trim. Stdlib only.
"""
import json
from pathlib import Path

from . import core
from .core import Exit

EXAMPLE = "agent-cli harness startsize session.jsonl --budget start-budget.json"
PIECES = ("instructions", "memory", "skills", "hooks", "listings", "other")
CAPPED = ("total", "skills", "memory", "instructions", "hooks")
LABEL = 70


def register(hs):
    p = core.subcommand(hs, "startsize", "bytes a session loaded before its first reply, per piece, from its "
                        "transcript (exit 1 over a --budget cap)",
                        [EXAMPLE, "agent-cli harness startsize session.jsonl --json",
                         "agent-cli harness startsize session.jsonl --budget start-budget.json --exclude listings"])
    p.add_argument("transcript", help="a session transcript, one JSON record per line")
    p.add_argument("--budget", metavar="FILE", help="JSON caps in KB: total_kb, skills_kb, memory_kb, "
                   "instructions_kb, hooks_kb")
    p.add_argument("--exclude", metavar="PIECES", default="", help="comma list of pieces left out of the total "
                   f"({', '.join(PIECES)}); default none")
    p.add_argument("--json", action="store_true", help="one JSON object")
    p.set_defaults(func=startsize_cmd)


def txt(v, sep=""):
    """A stored field as text: a string as is, a list of strings joined, anything else empty (a record of an
    unexpected shape counts zero bytes; it never crashes the count)."""
    if isinstance(v, str):
        return v
    return sep.join(x for x in v if isinstance(x, str)) if isinstance(v, list) else ""


def sub(v):
    """A field that should be a dict, as one."""
    return v if isinstance(v, dict) else {}


def parts_of(a):
    """[(piece, label, text)] for one attachment (a dict with a `type`); [] for a kind that is not counted."""
    kind = a.get("type")
    if kind == "instructions":
        files = [f for f in a.get("files") if isinstance(f, dict)] if isinstance(a.get("files"), list) else []
        return [("memory" if f.get("type") == "AutoMem" else "instructions", str(f.get("path", "?")),
                 txt(f.get("content"))) for f in files]
    if kind == "skill_listing":
        return [("skills", f"skill listing ({a.get('skillCount', '?')} skills)", txt(a.get("content")))]
    if kind == "hook_success":
        label = f"{a.get('hookName', '?')} {' '.join(str(a.get('command', '')).split())}"
        return [("hooks", label[:LABEL], txt(a.get("content")))]
    if kind == "hook_additional_context":
        return [("hooks", f"{a.get('hookName', '?')} (context)"[:LABEL], txt(a.get("content"), "\n"))]
    if kind == "deferred_tools_delta":
        return [("listings", "deferred tool names", txt(a.get("addedLines")))]
    if kind == "agent_listing_delta":
        return [("listings", "agent types", txt(a.get("addedLines")))]
    if kind == "mcp_instructions_delta":
        return [("listings", "MCP instructions", txt(a.get("addedBlocks")))]
    if kind == "output_style_instructions":
        return [("other", "output style", txt(sub(a.get("style")).get("prompt")))]
    if kind == "session_context":
        return [("other", "session context", "".join(txt(v) for v in sub(a.get("context")).values()))]
    return []


def measure(records, exclude=()):
    """{"total", "pieces": {name: {"bytes"}}, "parts": [{piece, label, bytes}]} for the records up to the first
    assistant record; `total` leaves out the pieces named in `exclude`."""
    parts = []
    for r in records:
        if r.get("type") == "assistant":
            break
        if r.get("type") == "attachment" and isinstance(r.get("attachment"), dict):
            parts += [{"piece": p, "label": label, "bytes": len(text.encode("utf-8"))}
                      for p, label, text in parts_of(r["attachment"])]
    pieces = {p: {"bytes": sum(x["bytes"] for x in parts if x["piece"] == p)} for p in PIECES}
    return {"total": sum(v["bytes"] for k, v in pieces.items() if k not in exclude),
            "pieces": pieces, "parts": parts}


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
    try:
        with f.open(encoding="utf-8", errors="replace", newline="\n") as fh:
            for n, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except ValueError as e:
                    core.die(Exit.REFUSED, f"{path} line {n} is not JSON: {e}", EXAMPLE)
                if isinstance(r, dict):
                    records.append(r)
                    if r.get("type") == "assistant":
                        break  # nothing after the first reply is counted, so a long or half-written tail is not read
    except OSError as e:
        core.die(Exit.REFUSED, f"{path}: {e}", EXAMPLE)
    return records


def startsize_cmd(a):
    exclude = tuple(x for x in a.exclude.split(",") if x)
    unknown = [x for x in exclude if x not in PIECES]
    if unknown:
        core.die(Exit.REFUSED, f"--exclude {unknown[0]!r} is not a piece; pieces: {', '.join(PIECES)}", EXAMPLE)
    m = measure(read_records(a.transcript), exclude)
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
        print(json.dumps({"total": m["total"], "budget_total": budget.get("total"),
                          "total_over_by": max(0, m["total"] - budget["total"]) if "total" in budget else None,
                          "total_excludes": list(exclude), "pieces": m["pieces"],
                          "parts": m["parts"], "over": over}))
        return Exit.FAIL if over else Exit.OK
    for k in (*PIECES, "total"):
        line = f"{k}: {sizes[k]} bytes"
        if k in budget:
            line += f", budget {budget[k]}" + (f", over by {sizes[k] - budget[k]}" if k in over else "")
        print(line)
        for x in (x for x in m["parts"] if x["piece"] == k):
            print(f"  {x['bytes']:>7} {x['label']}")
    if exclude:
        print(f"total leaves out: {', '.join(exclude)}")
    print("startsize: " + (f"over budget: {', '.join(over)}" if over else "ok"))
    return Exit.FAIL if over else Exit.OK
