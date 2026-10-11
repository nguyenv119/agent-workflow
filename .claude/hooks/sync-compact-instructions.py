#!/usr/bin/env python3
"""Paste the whole handoff skill into a CLAUDE.md "Compact Instructions" section.

Compaction can't run skills or open files, so the skill text has to live in
CLAUDE.md. Rerun this after editing the skill:
  python3 sync-compact-instructions.py <handoff SKILL.md> <CLAUDE.md>
Replaces everything between the start/end markers (or appends the section).
"""
import re, sys

START, END = "<!-- compact-instructions:start -->", "<!-- compact-instructions:end -->"
PREAMBLE = """## Compact Instructions
(Generated from the handoff skill by .claude/hooks/sync-compact-instructions.py;
rerun it after editing the skill. Keep this section if you replace this file.)
Compaction cannot run skills, tools, or open files, so the full handoff skill is
pasted below. Follow it, with these changes for compaction:
- Your summary IS the handoff document. Output the whole document (every section
  the skill calls for), then the filled-in TLDR block at the very end.
- Do not write a file or post anything in chat: a PostCompact hook saves this
  summary to ~/.claude/handoffs/ for you.
- Ignore any step that needs a tool. Everything the skill asks for is already in
  this conversation; review all of it, not just the last turns.
"""

skill_path, target = sys.argv[1], sys.argv[2]
skill = open(skill_path).read()
skill = re.sub(r"\A---\n.*?\n---\n", "", skill, flags=re.S)  # drop frontmatter
block = f"{START}\n{PREAMBLE}\n----- handoff skill text -----\n{skill.strip()}\n{END}"

text = open(target).read()
if START in text:
    text = re.sub(re.escape(START) + r".*?" + re.escape(END), lambda _: block, text, flags=re.S)
else:
    text = text.rstrip() + "\n\n" + block + "\n"
open(target, "w").write(text)
print(f"synced {skill_path} -> {target} ({len(block)} chars)")
