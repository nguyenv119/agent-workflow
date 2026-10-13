#!/usr/bin/env python3
"""Fake `claude -p` for project_check tests: replays a recorded reply, never calls a model.

Reads the prompt from stdin and appends it, with argv and the env keys it saw, to $FAKE_CLAUDE_LOG
(if set). Prints the file $FAKE_CLAUDE_REPLY (a recorded `--output-format json` envelope; default
project_check/clean.json next to this file) and exits $FAKE_CLAUDE_EXIT (default 0).
"""
import json
import os
import sys
from pathlib import Path

prompt = sys.stdin.read()
log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt, "env": sorted(os.environ)}) + "\n")
reply = os.environ.get("FAKE_CLAUDE_REPLY") or str(Path(__file__).resolve().parent / "project_check/clean.json")
sys.stdout.write(Path(reply).read_text())
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
