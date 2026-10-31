"""Tests for textrules.redact, redact_file and `agent-cli redact`.

Every secret here is a made-up value built from pieces at run time, so no real-looking credential sits in the
source. A redactor that misses a shape leaks a credential into a saved transcript or a pushed handoff; one that
over-reaches destroys git shas and corrupts JSON lines, so both directions are tested.
"""
import json
import os
import re
import stat
import sys
import unittest

from helpers import CLAUDE, TmpCase, agent_cli, make_project

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli.redact import redact_file  # noqa: E402
from agent_cli.textrules import redact  # noqa: E402

# Copy of the SECRET regex a desk sync script uses to withhold files (copied, not imported, so this repo stands alone). Redacted
# output must not match it, or a pushed handoff would still be withheld.
SECRET = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[bap]-[A-Za-z0-9-]{10,}"
                    r"|AKIA[A-Z0-9]{16}|PRIVATE KEY|Bearer (?![A-Z0-9_]+(?![A-Za-z0-9._-]))[A-Za-z0-9._-]{20,}|ntn_[A-Za-z0-9]{20,}"
                    r"|NEON_[A-Z_]*=\S+|postgres(ql)?://[^:\s]+:(?![${<*])[^@\s]+@")

BODY = "QUJDREVG" * 6 + "R0hJSktM"  # fake base64 filler, 56 chars
PEM = f"-----BEGIN PRIVATE KEY-----\n{BODY}\n{BODY}\n-----END PRIVATE KEY-----"
AWS_ID = "AKIA" + "ABCDEFGH12345678"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKE"  # 40 chars, mixed case
SAMPLES = {  # one per alternative of SECRET
    "sk-ant": "key sk-ant-" + "api03-abcDEF123_456" * 2,
    "ghp": "token ghp_" + "A1b2C3d4E5" * 3,
    "github_pat": "GH=github_pat_" + "11AAAA0Y0_abcDEF" * 2,
    "xoxb": "slack xoxb-" + "123456-789012-abc",
    "xoxa": "slack xoxa-" + "123456-789012-abc",
    "xoxp": "slack xoxp-" + "123456-789012-abc",
    "AKIA": f"id {AWS_ID} secret {AWS_SECRET}",
    "PRIVATE KEY": PEM,
    "Bearer": "Authorization: Bearer " + "eyJhbGciOi.payload_part-1234567",
    "ntn": "notion ntn_" + "a1B2c3D4e5" * 3,
    "NEON": "NEON_API_KEY=" + "napi_" + "x9y8z7" * 4,
    "postgres": "postgres://app_user:" + "hunter2pw" + "@db.example.com:5432/app",
    "postgresql": "postgresql://app_user:" + "hunter2pw" + "@db.example.com/app?sslmode=require",
}
KEPT = [  # names and ids that look secret-ish but are not: must come back unchanged
    "commit " + "0123456789abcdef" * 2 + "01234567",
    "path/to/file.py@" + "9fceb02d0ae598e95dc970b74767f19372d61af8",
    "sha256 " + "ab" * 32,
    "a base64ish run " + "Zm9vYmFyQmF6" * 4 + "x1",
    "postgres://app_user@db.example.com/app",
    "postgresql://user:${DB_PASS}@host/db",
    "Authorization: Bearer BLOB_READ_WRITE_TOKEN",
    "Bearer abc",
    "the disk-usage-report-for-the-quarter file and task-assessment-for-the-whole-team",
    "export NEON_PROJECT_ID and NEON_API_KEY by name only",
]


class Redact(unittest.TestCase):
    def test_each_secret_shape_is_gone_and_the_desk_check_no_longer_fires(self):
        """
        Every shape the desk's secret check withholds files for is replaced, and the output passes that check.

        A pushed handoff that still matches the desk's check is withheld forever, and a credential that is not
        replaced is published. The fail control: every sample must match the copy of the check to begin with.
        """
        for name, sample in SAMPLES.items():
            with self.subTest(name):
                # GIVEN a sample the desk check flags
                self.assertTrue(SECRET.search(sample), "sample is not a secret shape")
                # WHEN redacting it
                out, n = redact(sample)
                # THEN something was replaced and the desk check passes
                self.assertGreaterEqual(n, 1)
                self.assertFalse(SECRET.search(out), out)
                self.assertIn("REDACTED", out)

    def test_postgres_url_keeps_scheme_user_and_host(self):
        """
        Only the password goes; the rest of the URL still says which database was meant.

        A transcript stripped of the host is useless for working out what happened.
        """
        out, n = redact(SAMPLES["postgres"])
        self.assertEqual((out, n), ("postgres://app_user:<REDACTED>@db.example.com:5432/app", 1))

    def test_names_ids_and_hashes_survive_unchanged(self):
        """
        Git shas, path@sha lines, bare hex and base64 runs, placeholders and env var names are not credentials.

        Redacting them would corrupt the commands and hashes a saved attempt exists to preserve.
        """
        for text in KEPT:
            with self.subTest(text):
                self.assertEqual(redact(text), (text, 0))

    def test_pem_block_spanning_lines_goes_whole(self):
        """
        A multi-line private key is replaced from BEGIN to END, whatever surrounds it stays.

        Leaving the base64 body behind would leak the key even with its header gone.
        """
        out, n = redact(f"before\n{PEM}\nafter {BODY[:8]}")
        self.assertEqual((out, n), (f"before\n[REDACTED]\nafter {BODY[:8]}", 1))

    def test_truncated_pem_loses_its_body_up_to_the_first_foreign_character(self):
        """
        A key cut off before its END marker still loses its body.

        Transcripts are truncated by tools; the header alone would match the desk's check forever.
        """
        out, n = redact(f"x -----BEGIN RSA PRIVATE KEY-----\n{BODY}\n and then text")
        self.assertFalse(BODY in out)
        self.assertTrue(out.endswith("and then text") and n == 1)

    def test_aws_secret_is_removed_only_when_it_follows_a_key_id_on_the_same_line(self):
        """
        The 40-char secret after an AKIA id goes; a 40-hex sha near the id, or a secret on another line, stays.

        The secret has no prefix to key on, so the id is the anchor; widening it would eat git shas.
        """
        sha = "9fceb02d0ae598e95dc970b74767f19372d61af8"
        self.assertEqual(redact(f"{AWS_ID}={AWS_SECRET}"), ("[REDACTED]=[REDACTED]", 2))
        self.assertEqual(redact(f"{AWS_ID} at {sha}"), (f"[REDACTED] at {sha}", 1))
        self.assertEqual(redact(f"{AWS_ID}\n{AWS_SECRET}"), (f"[REDACTED]\n{AWS_SECRET}", 1))

    def test_a_second_pass_finds_nothing(self):
        """
        Redacting already-redacted text changes nothing and counts 0.

        A tool that redacts on every save would otherwise grow the count and the file each time.
        """
        once, _ = redact("\n".join(SAMPLES.values()))
        self.assertEqual(redact(once), (once, 0))


class RedactFile(TmpCase):
    def jsonl(self, *records):
        path = self.tmp / "t.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        return path

    def test_every_jsonl_line_still_parses_with_escaped_quotes_and_pem_newlines(self):
        """
        Lines holding a URL inside escaped quotes, a PEM with \\n escapes and a truncated key all stay valid JSON.

        A saved transcript that no longer parses is lost; the redactor must never eat a quote or a backslash.
        """
        # GIVEN records whose strings carry the secrets next to JSON escapes
        recs = [{"cmd": 'psql "' + SAMPLES["postgres"].split(" ")[-1] + '" -c "select 1"', "n": 1},
                {"text": f"key:\n{PEM}\nend", "n": 2},
                {"text": f"cut -----BEGIN PRIVATE KEY-----\n{BODY}\\ then", "n": 3},
                {"text": "clean line " + "9fceb02d0ae598e95dc970b74767f19372d61af8", "n": 4}]
        path = self.jsonl(*recs)
        clean = path.read_text().splitlines()[3]
        # WHEN redacting the file
        n = redact_file(path)
        # THEN each line parses, the clean one is byte-identical, and no secret remains
        lines = path.read_text().splitlines()
        self.assertEqual([json.loads(ln)["n"] for ln in lines], [1, 2, 3, 4])
        self.assertEqual(lines[3], clean)
        self.assertEqual(n, 3)
        self.assertFalse(SECRET.search(path.read_text()))
        self.assertIn("hunter2pw", json.dumps(recs))  # control: the password was in the input
        self.assertNotIn("hunter2pw", path.read_text())

    def test_size_changes_only_by_the_redacted_spans(self):
        """
        The file shrinks or grows by exactly the replaced spans' difference.

        Proves nothing else was touched (no re-encoding, no newline changes).
        """
        text = f'{{"a": "ghp_{"A1b2C3d4E5" * 3}"}}\r\nplain\xe9 \udcff line\n'
        path = self.tmp / "t.jsonl"
        path.write_bytes(text.encode("utf-8", "surrogateescape"))
        before = path.stat().st_size
        redact_file(path)
        self.assertEqual(path.stat().st_size, before - len("ghp_" + "A1b2C3d4E5" * 3) + len("[REDACTED]"))
        self.assertTrue(path.read_bytes().endswith("\r\nplain\xe9 \udcff line\n".encode("utf-8", "surrogateescape")))

    def test_file_is_mode_600_after_redaction_and_when_nothing_matched(self):
        """
        The result is readable by its owner only, whether or not anything was replaced.

        A transcript copied to a temp dir keeps its 644 mode otherwise and is world-readable.
        """
        for body in (SAMPLES["ghp"], "nothing here"):
            with self.subTest(body):
                path = self.tmp / "f.txt"
                path.write_text(body)
                os.chmod(path, 0o644)
                redact_file(path)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_a_non_jsonl_file_is_redacted_as_one_text_so_a_pem_spans_lines(self):
        """
        Only .jsonl is split by line; a markdown file's multi-line key is still removed whole.

        Splitting every file by line would leave the key body behind.
        """
        path = self.tmp / "n.md"
        path.write_text(f"a\n{PEM}\nb\n")
        self.assertEqual(redact_file(path), 1)
        self.assertEqual(path.read_text(), "a\n[REDACTED]\nb\n")

    def test_dry_run_counts_and_writes_nothing(self):
        """
        write=False reports the same count and leaves the content and mode alone.

        Callers preview before they rewrite a transcript they cannot recover.
        """
        path = self.tmp / "f.txt"
        path.write_text(SAMPLES["ghp"])
        os.chmod(path, 0o644)
        self.assertEqual(redact_file(path, write=False), 1)
        self.assertEqual((path.read_text(), stat.S_IMODE(path.stat().st_mode)), (SAMPLES["ghp"], 0o644))


class Hardening(TmpCase):
    def test_bare_sk_key_and_escaped_neon_value_go(self):
        """
        A bare sk- key goes, and a NEON_ value inside a JSON-escaped string leaves the line valid JSON.

        The desk check has no bare sk- alternative, so only these tests guard those two paths.
        """
        self.assertEqual(redact("k sk-proj" + "abcDEF123456" * 3), ("k [REDACTED]", 1))
        line = json.dumps({"e": 'NEON_API_KEY="' + "x9y8z7" * 4 + '" ok'})
        out, n = redact(line)
        self.assertEqual((json.loads(out)["e"], n), ('[REDACTED]" ok', 1))

    def test_many_unterminated_pem_headers_finish_fast(self):
        """
        Thousands of BEGIN headers with no END must not make redact quadratic.

        A transcript quoting key headers over and over would otherwise hang the saver.
        """
        import time
        text = "-----BEGIN PRIVATE KEY----- x\n" * 4000
        t = time.time()
        redact(text)
        self.assertLess(time.time() - t, 3)

    def test_symlink_target_is_cleaned_and_the_link_kept(self):
        """
        Redacting a symlink cleans the file it points to and leaves the link in place.

        Replacing the link would report success while the secret stayed in the target.
        """
        real, link = self.tmp / "real.txt", self.tmp / "link.txt"
        real.write_text(SAMPLES["ghp"])
        link.symlink_to(real)
        self.assertEqual(redact_file(link), 1)
        self.assertTrue(link.is_symlink())
        self.assertFalse(SECRET.search(real.read_text()))

    def test_empty_file_and_second_run(self):
        """
        An empty file returns 0, and a second run over a cleaned file returns 0 and leaves no temp file.

        Idempotency lets writers call it on every save.
        """
        e = self.tmp / "e.jsonl"
        e.write_text("")
        self.assertEqual(redact_file(e), 0)
        f = self.tmp / "f.jsonl"
        f.write_text(SAMPLES["ghp"] + "\n")
        self.assertEqual((redact_file(f), redact_file(f)), (1, 0))
        self.assertEqual(sorted(os.listdir(self.tmp)), ["e.jsonl", "f.jsonl"])


class Command(TmpCase):
    def setUp(self):
        super().setUp()
        self.p = make_project(self.tmp / "proj")
        self.f = self.tmp / "planted.txt"
        self.f.write_text("\n".join(SAMPLES.values()) + "\n")

    def test_prints_only_a_count_and_leaves_no_match(self):
        """
        `agent-cli redact <file>` cleans the file and prints a number, never a matched value.

        The command runs in transcripts' sessions; echoing a secret would put it back in the log.
        """
        r = agent_cli(self.p, "redact", str(self.f))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"^redactions: \d+\n$")
        for secret in ("hunter2pw", AWS_SECRET, BODY):
            self.assertNotIn(secret, r.stdout + r.stderr + self.f.read_text())
        self.assertFalse(SECRET.search(self.f.read_text()))

    def test_json_and_dry_run_report_the_count_and_keep_the_file(self):
        """
        --json prints one object; --dry-run leaves the file as it was.

        House rule: every command takes --json, and a destructive one takes --dry-run.
        """
        before = self.f.read_text()
        r = agent_cli(self.p, "redact", str(self.f), "--dry-run", "--json")
        row = json.loads(r.stdout)
        self.assertEqual((r.returncode, row["dry_run"], row["redactions"] > 0), (0, True, True))
        self.assertEqual(self.f.read_text(), before)

    def test_a_missing_file_exits_2_with_an_example(self):
        """
        A path that is not a file is a usage error with a correct invocation, not a traceback.

        House rule: exit 2 on usage errors, and the error says what to type.
        """
        r = agent_cli(self.p, "redact", str(self.tmp / "nope.jsonl"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("example: agent-cli redact", r.stderr)

    def test_an_unreadable_file_exits_2_without_a_traceback(self):
        """
        A file the user cannot read is refused with exit 2 and an example.

        House rule: errors are one line plus a correct invocation, never a traceback.
        """
        bad = self.tmp / "locked.txt"
        bad.write_text("x")
        bad.chmod(0)
        r = agent_cli(self.p, "redact", str(bad))
        bad.chmod(0o600)
        if os.geteuid() != 0:
            self.assertEqual(r.returncode, 2)
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
