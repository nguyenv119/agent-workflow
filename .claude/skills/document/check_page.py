#!/usr/bin/env python3
"""Check a document page or a folder of to-do cards: a shim over `agent-cli page check` and `agent-cli card check`.

    check_page.py <page.html>                      same as: agent-cli page check <page.html>
    check_page.py cards <dir> [--prefix NAME]...   same as: agent-cli card check <dir> [--prefix NAME]...

The rules live in agent-cli (.claude/cli/agent_cli/page.py and card.py); a project changes them with a
page-rules.json or card-rules.json seam. Prints one line per violation and exits 1, or prints PASS and exits 0.
Exit 2 on bad usage or a card folder with no *.json cards. Stdlib only.
"""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "cli"))
from agent_cli import card, page  # noqa: E402

CARD_FIELDS = tuple(card.DEFAULTS["required_when"]["fields"])


def check_page(html):
    """Return the list of violations for one page's HTML (default rules)."""
    return page.check_page(html, page.DEFAULTS)


def check_cards(folder, prefixes=()):
    """Return the list of violations for every *.json to-do card in folder (default rules)."""
    return card.check_cards(folder, prefixes, card.DEFAULTS)


def main(argv):
    prefixes = argv[3::2]
    if (len(argv) >= 2 and argv[0] == "cards" and Path(argv[1]).is_dir()
            and len(argv) % 2 == 0 and set(argv[2::2]) <= {"--prefix"}):
        if not any(Path(argv[1]).glob("*.json")):
            print(f"no cards found: {argv[1]} has no *.json files", file=sys.stderr)
            return 2
        out = check_cards(argv[1], prefixes)
    elif len(argv) == 1 and Path(argv[0]).is_file():
        out = check_page(Path(argv[0]).read_text(encoding="utf-8"))
    else:
        print("usage: check_page.py <page.html> | check_page.py cards <dir> [--prefix NAME]...  (path must exist)",
              file=sys.stderr)
        return 2
    print("\n".join(out) if out else "PASS")
    return 1 if out else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
