"""The exam's plan token from a local env file, for a laptop whose shell does not export it.

with_token(env) returns a copy of env with CLAUDE_CODE_OAUTH_TOKEN taken from the file named by
HARNESS_EXAM_ENV_FILE when env lacks it (or has it empty). Only that one key is read (`KEY=value`, an optional
`export `, optional quotes; every other line is ignored and nothing is evaluated); `~` is expanded. A named file
that cannot be read is an error naming the path, never its contents. Callers pass the result to a subprocess
only: the value never goes into this process's os.environ, a log, a print or run.json.
"""
import re
from pathlib import Path

from trial_export import OAUTH_TOKEN

ENV_FILE = "HARNESS_EXAM_ENV_FILE"
LINE = re.compile(rf"^\s*(?:export\s+)?{OAUTH_TOKEN}\s*=\s*(.*?)\s*$")


def with_token(env):
    out = dict(env)
    if out.get(OAUTH_TOKEN) or not out.get(ENV_FILE):
        return out
    path = Path(out[ENV_FILE]).expanduser()
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as e:
        raise RuntimeError(f"{ENV_FILE} names {path}, which cannot be read ({e.__class__.__name__})") from None
    for line in text.splitlines():  # the last assignment wins, as in a shell
        m = LINE.match(line)
        if m:
            value = m[1]
            if len(value) > 1 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            else:
                value = value.split(" #")[0].strip()
            if value:
                out[OAUTH_TOKEN] = value
    return out
