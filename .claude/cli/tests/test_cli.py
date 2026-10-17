"""Tests for agent-cli's help, exit codes, `self`, `lib`, `eval lib` and the core helpers project CLIs import."""
import argparse
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import CLAUDE, GIT, TmpCase, agent_cli, make_project, run

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import core, main  # noqa: E402

LEAVES = [[], ["hook"], ["hook", "pretooluse"], ["hook", "test"], ["hook", "rules"], ["eval"], ["eval", "lib"],
          ["self"], ["self", "env"], ["self", "install"], ["lib"], ["lib", "path"],
          ["wt"], ["wt", "new"], ["wt", "list"], ["wt", "audit"], ["wt", "rm"],
          ["wait"], ["lock"], ["lock", "acquire"], ["lock", "release"], ["lock", "status"],
          ["pr"], ["pr", "status"], ["pr", "wait"], ["pr", "land"],
          ["harness"], ["harness", "drift"], ["harness", "inventory"]]
EXIT_HEADER = "Exit codes (every agent-cli command):"
# Leaf commands that are not read commands, so house-rules "--json" does not ask them for --json.
NOT_READ = {"hook pretooluse": "a hook: stdout is Claude Code's hook protocol, not ours",
            "hook posttooluse": "a hook: records a command shape, prints nothing",
            "hook posttoolusefailure": "a hook: records a command shape, prints nothing",
            "hook userpromptsubmit": "a hook: stdout is Claude Code's hook protocol, not ours",
            "eval init": "writes a scaffold", "pr land": "merges a PR"}


def leaves(parser, path=()):
    """("verb path", parser) for every leaf command under parser."""
    subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subs:
        return [(" ".join(path), parser)]
    return [x for name, child in subs[0].choices.items() for x in leaves(child, (*path, name))]


class Help(TmpCase):
    def test_every_help_starts_with_its_summary_and_ends_with_examples(self):
        """cli-for-agents "`--help` that works": each layer's --help opens with `<prog>: <summary>` and ends in an
        Examples: block.

        Project CLIs list themselves via `<cli> --help | head -1`, and agents copy the examples.
        """
        # GIVEN an installed project
        p = make_project(self.tmp / "proj")
        for path in LEAVES:
            # WHEN each layer's help is printed
            r = agent_cli(p, *path, "--help")
            lines = r.stdout.rstrip("\n").splitlines()
            # THEN line 1 is the summary and the last block is Examples with real invocations (the root's
            # exit-code table follows its Examples)
            with self.subTest(path=path):
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertTrue(lines[0].startswith(" ".join(["agent-cli", *path]) + ": "), lines[0])
                ex = lines.index("Examples:")
                end = lines.index(EXIT_HEADER) - 1 if not path else len(lines)
                self.assertTrue(all(ln.startswith("  ") and "agent-cli" in ln for ln in lines[ex + 1:end]),
                                lines[ex:end])
                self.assertGreater(end, ex + 1)

    def test_bare_command_prints_root_help(self):
        """`agent-cli` alone prints the command list (exit 0), never prompts (house-rules "Never prompt")."""
        # GIVEN an installed project
        p = make_project(self.tmp / "proj")
        # WHEN run with no args
        r = agent_cli(p)
        # THEN the root help with its commands is shown
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("agent-cli: shared chores for agent sessions", r.stdout)
        for name in ("hook", "eval", "self", "lib"):
            self.assertRegex(r.stdout, rf"\n    {name}\s+\S")

    def test_root_help_lists_project_clis_from_the_path_seam(self):
        """Project CLIs on the seam's dirs appear under Project CLIs with their own first help line."""
        # GIVEN a project whose path seam names tools/ holding one CLI
        p = make_project(self.tmp / "proj", {"path": "tools\n"})
        (p / "tools").mkdir()
        cli = p / "tools/proj-cli"
        cli.write_text("#!/bin/sh\necho 'proj-cli: does project chores'\necho more\n")
        cli.chmod(0o755)
        # WHEN the root help is printed
        r = agent_cli(p, "--help")
        # THEN the CLI is listed with its summary
        self.assertRegex(r.stdout, r"Project CLIs:\n  proj-cli\s+proj-cli: does project chores\n")

    def test_each_command_module_registers_itself(self):
        """build() takes its top-level commands from main.COMMANDS, one register(sub) per module, so a new
        command module is one list entry."""
        # GIVEN the module list
        # WHEN the root parser is built
        root = main.build()
        names = list(next(a for a in root._actions if isinstance(a, argparse._SubParsersAction)).choices)
        # THEN every module registered exactly the commands the root lists, in list order
        self.assertTrue(all(callable(getattr(m, "register", None)) for m in main.COMMANDS))
        self.assertEqual(names, ["hook", "eval", "self", "lib", "wt", "wait", "lock", "pr", "log", "page", "card", "repeats",
                                 "harness"])

    def test_group_prints_its_help_when_called_bare(self):
        """core.group: a command group whose bare call prints its own help (house-rules "Never prompt"), with a
        subparser set."""
        # GIVEN a parser with one group holding one leaf
        root = core.build_parser("demo", "a demo", ["demo grp leaf"])
        sub = root.add_subparsers(title="Commands", metavar="<command>", parser_class=core.Parser)
        grp, gs = core.group(sub, "grp", "a group", ["demo grp leaf"])
        core.subcommand(gs, "leaf", "a leaf", ["demo grp leaf"]).set_defaults(func=lambda a: "leaf")
        out = io.StringIO()
        # WHEN the group is called bare, and the leaf is called
        with redirect_stdout(out):
            root.parse_args(["grp"]).func(None)
        leaf = root.parse_args(["grp", "leaf"]).func(None)
        # THEN the group printed its help and the leaf ran
        self.assertTrue(out.getvalue().startswith("demo grp: a group\n"), out.getvalue())
        self.assertEqual(leaf, "leaf")

    def test_bad_usage_exits_2_with_an_example(self):
        """cli-for-agents "Fail fast with actionable errors" + house-rules "exit-code table": a bad invocation fails fast on
        stderr with `error:` and a correct example, exit 2."""
        # GIVEN an installed project
        p = make_project(self.tmp / "proj")
        # WHEN eval lib is called without its required flag
        r = agent_cli(p, "eval", "lib")
        # THEN exit 2, error line and an example line
        self.assertEqual(r.returncode, 2)
        self.assertRegex(r.stderr, r"^error: .+\n  example: agent-cli eval lib --sh\n$")


class HouseRules(TmpCase):
    def test_root_help_ends_with_the_exit_code_table(self):
        """house-rules "exit-code table": `agent-cli --help` ends with one table naming every core.Exit code.

        Callers and evals branch on these numbers; the table is where an agent learns them without reading code.
        """
        # GIVEN an installed project
        p = make_project(self.tmp / "proj")
        # WHEN the root help is printed
        lines = agent_cli(p, "--help").stdout.rstrip("\n").splitlines()
        # THEN its last block is the header plus one `  <n>  <name>  <meaning>` row per Exit member, in order
        rows = lines[lines.index(EXIT_HEADER) + 1:]
        got = [re.fullmatch(r"  (\d)  ([a-z]+) +\S.*", ln) for ln in rows]
        self.assertTrue(all(got), rows)
        self.assertEqual([(int(m[1]), m[2]) for m in got], [(int(e), e.name.lower()) for e in core.Exit])

    def test_every_read_verb_takes_json(self):
        """house-rules "--json": every leaf command except the named non-read ones accepts --json.

        A new read verb without --json fails here, so the rule cannot drift again.
        """
        # GIVEN the built command tree
        tree = dict(leaves(main.build()))
        # WHEN each leaf's options are read
        missing = [v for v, p in tree.items() if v not in NOT_READ and "--json" not in p._option_string_actions]
        # THEN only the exempt ones lack --json, and every exempt name is still a real command
        self.assertEqual(missing, [])
        self.assertLessEqual(set(NOT_READ), set(tree))

    def test_path_printers_emit_one_json_object(self):
        """`lib path`, `eval lib` and `self env` with --json print one JSON object naming what they print bare."""
        # GIVEN a project with a path seam
        p = make_project(self.tmp / "proj", {"path": "tools\n"})
        cases = [(["lib", "path"], {"path": str(p / ".claude/cli")}),
                 (["eval", "lib", "--sh"], {"path": str(p / ".claude/cli/agent_cli/eval_lib/verdict.sh")}),
                 (["eval", "lib", "--py"], {"path": str(p / ".claude/cli/agent_cli/eval_lib/verdict.py")}),
                 (["self", "env"], {"dirs": [f"{p}/.claude/bin", f"{p}/tools"],
                                    "export": f'export PATH="{p}/.claude/bin:{p}/tools:$PATH"'})]
        for args, want in cases:
            # WHEN each runs with --json
            r = agent_cli(p, *args, "--json")
            # THEN stdout is exactly one JSON object with those fields
            with self.subTest(args=args):
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(len(r.stdout.splitlines()), 1, r.stdout)
                self.assertEqual(json.loads(r.stdout), want)

    def test_self_env_json_when_switched_off_is_empty(self):
        """AGENT_CLI_PATH_OFF=1 with --json still prints one object, with no dirs and no export line."""
        # GIVEN the off switch
        p = make_project(self.tmp / "proj")
        # WHEN self env --json runs
        r = agent_cli(p, "self", "env", "--json", env={"AGENT_CLI_PATH_OFF": "1"})
        # THEN one empty object, exit 0
        self.assertEqual((r.returncode, json.loads(r.stdout)), (0, {"dirs": [], "export": ""}))


class SelfEnv(TmpCase):
    def test_env_prints_bin_dir_then_seam_dirs(self):
        """`self env` puts .claude/bin and the project's seam dirs ahead of $PATH."""
        # GIVEN a project with a relative and an absolute path seam entry
        p = make_project(self.tmp / "proj", {"path": "# tools\ntools\n/opt/x\n"})
        # WHEN self env runs
        r = agent_cli(p, "self", "env")
        # THEN one export line in that order
        self.assertEqual(r.stdout, f'export PATH="{p}/.claude/bin:{p}/tools:/opt/x:$PATH"\n')

    def test_env_prints_nothing_when_switched_off(self):
        """AGENT_CLI_PATH_OFF=1 stops the PATH wiring (the SessionStart hook appends nothing)."""
        # GIVEN the off switch
        p = make_project(self.tmp / "proj")
        # WHEN self env runs
        r = agent_cli(p, "self", "env", env={"AGENT_CLI_PATH_OFF": "1"})
        # THEN no output, exit 0
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_env_escapes_shell_specials(self):
        """A dir holding `$` or a quote cannot run code when the export line is sourced."""
        # GIVEN a path seam dir with $ and "
        p = make_project(self.tmp / "proj", {"path": '/opt/a$b"c\n'})
        # WHEN self env runs
        r = agent_cli(p, "self", "env")
        # THEN they are backslash-escaped inside the double quotes
        self.assertIn(r'/opt/a\$b\"c:', r.stdout)


class SeamLookup(TmpCase):
    """find_seam order, observed through `self env` (which reads the `path` seam)."""

    def env_dirs(self, project, cwd=None, env=None):
        out = agent_cli(project, "self", "env", cwd=cwd, env=env).stdout
        return out.split('"')[1].split(":")[1:-1]

    def test_installed_claude_dir_wins_over_project_dir_env(self):
        """The .claude agent-cli is installed in wins: a project's own seam is never overridden by env."""
        # GIVEN an install with a seam, and another dir with a different seam in CLAUDE_PROJECT_DIR
        p = make_project(self.tmp / "a", {"path": "/from/install\n"})
        other = make_project(self.tmp / "b", {"path": "/from/env\n"}, install=False)
        # WHEN looked up
        # THEN the install's seam is used
        self.assertEqual(self.env_dirs(p, env={"CLAUDE_PROJECT_DIR": str(other)}), ["/from/install"])

    def test_project_dir_env_is_second(self):
        """With no seam in the install, $CLAUDE_PROJECT_DIR/.claude is used."""
        # GIVEN an install without seams and a project dir with one
        p = make_project(self.tmp / "a")
        other = make_project(self.tmp / "b", {"path": "/from/env\n"}, install=False)
        # WHEN looked up
        # THEN the env project's seam is used
        self.assertEqual(self.env_dirs(p, env={"CLAUDE_PROJECT_DIR": str(other)}), ["/from/env"])

    def test_walks_up_from_cwd(self):
        """Without the env var (common in Bash tool calls), the cwd's enclosing project is found."""
        # GIVEN an install elsewhere, and cwd deep inside a project with a seam
        p = make_project(self.tmp / "a")
        proj = make_project(self.tmp / "c", {"path": "/from/cwd\n"}, install=False)
        (proj / "x/y").mkdir(parents=True)
        # WHEN looked up from x/y
        # THEN that project's seam is used
        self.assertEqual(self.env_dirs(p, cwd=proj / "x/y"), ["/from/cwd"])

    def test_falls_back_to_the_git_main_worktree_root(self):
        """A linked worktree without the (untracked) seam still finds it in the main checkout."""
        # GIVEN a main repo with an untracked seam and a linked worktree outside it
        p = make_project(self.tmp / "a")
        main = make_project(self.tmp / "main", install=False)
        run(GIT + ["commit", "-q", "--allow-empty", "-m", "c"], main)
        make_project(main, {"path": "/from/main\n"}, install=False)
        run(GIT + ["worktree", "add", "-q", str(self.tmp / "wt")], main)
        # WHEN looked up from the worktree
        # THEN the main checkout's seam is used
        self.assertEqual(self.env_dirs(p, cwd=self.tmp / "wt"), ["/from/main"])

    def test_never_reads_home_claude(self):
        """The walk stops before $HOME, so a seam in ~/.claude never applies to every repo under it."""
        # GIVEN HOME with a seam and a project under HOME without one
        p = make_project(self.tmp / "a")
        home = make_project(self.tmp / "home", {"path": "/from/home\n"}, install=False)
        (home / "proj").mkdir()
        # WHEN looked up from home/proj
        # THEN nothing is found
        self.assertEqual(self.env_dirs(p, cwd=home / "proj", env={"HOME": str(home)}), [])


class SelfInstall(TmpCase):
    def test_install_links_then_reports_exists(self):
        """cli-for-agents "Idempotency": the first run links bin-dir/agent-cli; a second run reports `exists:` and
        changes nothing."""
        # GIVEN an empty bin dir
        p = make_project(self.tmp / "proj")
        bindir = self.tmp / "bin"
        bindir.mkdir()
        # WHEN installed twice
        first = agent_cli(p, "self", "install", "--bin-dir", str(bindir))
        second = agent_cli(p, "self", "install", "--bin-dir", str(bindir))
        # THEN a link to the shim exists and the second run says so
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(os.readlink(bindir / "agent-cli"), str(p / ".claude/bin/agent-cli"))
        self.assertTrue(second.stdout.startswith("exists: "), second.stdout)

    def test_linked_shim_runs(self):
        """The symlinked shim resolves its own package, so it works from any bin dir."""
        # GIVEN an installed link
        p = make_project(self.tmp / "proj")
        bindir = self.tmp / "bin"
        bindir.mkdir()
        agent_cli(p, "self", "install", "--bin-dir", str(bindir))
        # WHEN run through the link
        r = run([sys.executable, str(bindir / "agent-cli"), "lib", "path"], self.tmp)
        # THEN it answers from the real install
        self.assertEqual(r.stdout.strip(), str(p / ".claude/cli"))

    def test_dry_run_writes_nothing(self):
        """cli-for-agents "Destructive actions": --dry-run says what it would do and leaves the bin dir alone."""
        # GIVEN an empty bin dir
        p = make_project(self.tmp / "proj")
        bindir = self.tmp / "bin"
        bindir.mkdir()
        # WHEN dry-run
        r = agent_cli(p, "self", "install", "--bin-dir", str(bindir), "--dry-run", "--json")
        # THEN nothing is written and the plan is reported
        self.assertFalse((bindir / "agent-cli").exists())
        self.assertEqual(json.loads(r.stdout)["status"], "would-link")

    def test_refuses_to_replace_another_file(self):
        """An unrelated agent-cli already in the bin dir is never overwritten (exit 2)."""
        # GIVEN a different file at the link path
        p = make_project(self.tmp / "proj")
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "agent-cli").write_text("other")
        # WHEN installed
        r = agent_cli(p, "self", "install", "--bin-dir", str(bindir))
        # THEN refused, file intact
        self.assertEqual(r.returncode, 2)
        self.assertEqual((bindir / "agent-cli").read_text(), "other")

    def test_missing_bin_dir_fails(self):
        """A bin dir that does not exist is an error with an example, not a silent mkdir."""
        # GIVEN no such dir
        p = make_project(self.tmp / "proj")
        # WHEN installed
        r = agent_cli(p, "self", "install", "--bin-dir", str(self.tmp / "nope"))
        # THEN exit 1 with an example
        self.assertEqual(r.returncode, 1)
        self.assertIn("  example: agent-cli self install --bin-dir", r.stderr)


class LibAndEvalPaths(TmpCase):
    def test_lib_path_lets_a_project_cli_import_core(self):
        """A project CLI puts `agent-cli lib path` on sys.path and imports the stable helpers."""
        # GIVEN the lib path
        p = make_project(self.tmp / "proj")
        lib = agent_cli(p, "lib", "path").stdout.strip()
        # WHEN a script imports from it
        code = ("import sys; sys.path.insert(0, sys.argv[1]); "
                "from agent_cli.core import build_parser, Exit, emit, die, parse_duration, find_seam; "
                "print(int(Exit.PARTIAL))")
        r = run([sys.executable, "-c", code, lib], self.tmp)
        # THEN the import works
        self.assertEqual(r.stdout.strip(), "5", r.stderr)

    def test_eval_lib_prints_existing_library_paths(self):
        """`eval lib --sh|--py` name the verdict libraries to source / import."""
        # GIVEN an installed project
        p = make_project(self.tmp / "proj")
        # WHEN asked for each
        sh, py = (agent_cli(p, "eval", "lib", f).stdout.strip() for f in ("--sh", "--py"))
        # THEN both files exist with the right names
        self.assertEqual(Path(sh), p / ".claude/cli/agent_cli/eval_lib/verdict.sh")
        self.assertEqual(Path(py), p / ".claude/cli/agent_cli/eval_lib/verdict.py")


class Core(unittest.TestCase):
    def test_exit_codes(self):
        """house-rules "exit-code table": exit codes are fixed numbers every caller and eval relies on."""
        # GIVEN / WHEN the enum
        # THEN values match house-rules "exit-code table"
        self.assertEqual([int(c) for c in core.Exit], [0, 1, 2, 3, 4, 5])
        self.assertEqual(core.Exit.TIMEOUT, 4)

    def test_parse_duration(self):
        """Durations take s/m/h/d suffixes or bare seconds; anything else is an error."""
        # GIVEN / WHEN parsed
        got = [core.parse_duration(s) for s in ("40m", "90", "2h", "1d", "15s")]
        # THEN seconds
        self.assertEqual(got, [2400, 90, 7200, 86400, 15])
        for bad in ("", "5x", "-1m", "m"):
            with self.assertRaises(ValueError):
                core.parse_duration(bad)

    def test_duration_refuses_a_bad_value_with_exit_2_and_the_example(self):
        """core.duration is parse_duration for commands: a bad value exits 2 naming a correct invocation."""
        # GIVEN a good and a bad duration
        # WHEN parsed
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            core.duration("5x", "agent-cli wait --cap 30m")
        # THEN seconds for the good one, exit 2 + example for the bad one
        self.assertEqual(core.duration("2m", "unused"), 120)
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("  example: agent-cli wait --cap 30m", err.getvalue())

    def test_git_returns_none_or_exits_1_when_git_cannot_run(self):
        """core.git never raises: with git missing it returns None, or with check exits 1 and the example."""
        # GIVEN no git on PATH
        with mock.patch.dict(os.environ, {"PATH": ""}):
            # WHEN called without and with check
            quiet = core.git("rev-parse", "HEAD", check=False)
            err = io.StringIO()
            with redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                core.git("rev-parse", "HEAD", example="agent-cli wt list")
        # THEN None, then exit 1 naming the git command and the example
        self.assertIsNone(quiet)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("error: git rev-parse HEAD: ", err.getvalue())
        self.assertIn("  example: agent-cli wt list", err.getvalue())

    def test_child_env_drops_names_and_puts_bin_first_on_path(self):
        """core.child_env is os.environ minus the dropped names, with agent-cli's bin ahead of $PATH."""
        # GIVEN a variable to drop and one to keep
        with mock.patch.dict(os.environ, {"DROP_ME": "1", "KEEP_ME": "2", "PATH": "/usr/bin"}):
            # WHEN the child env is built
            env = core.child_env(drop=("DROP_ME",))
        # THEN dropped, kept, and bin first with the old PATH last
        self.assertNotIn("DROP_ME", env)
        self.assertEqual(env["KEEP_ME"], "2")
        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(core.BIN))
        self.assertEqual(env["PATH"].split(os.pathsep)[-1], "/usr/bin")

    def test_emit_json_and_text(self):
        """house-rules "--json": --json is one object per line; text mode is `key: value` lines."""
        # GIVEN an object
        obj = {"id": 3, "url": "u"}
        # WHEN emitted both ways
        a, b = io.StringIO(), io.StringIO()
        with redirect_stdout(a):
            core.emit(obj, True)
        with redirect_stdout(b):
            core.emit(obj, False)
        # THEN
        self.assertEqual(a.getvalue(), '{"id": 3, "url": "u"}\n')
        self.assertEqual(b.getvalue(), "id: 3\nurl: u\n")

    def test_die_prints_error_and_example_then_exits_with_code(self):
        """cli-for-agents "Fail fast with actionable errors": die writes `error:` and `  example:` to stderr and
        exits with the given code."""
        # GIVEN a captured stderr
        err = io.StringIO()
        # WHEN die runs
        with redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            core.die(core.Exit.BLOCKED, "lock held", "agent-cli lock list")
        # THEN
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(err.getvalue(), "error: lock held\n  example: agent-cli lock list\n")

    def test_run_seam_prepends_self_env_dirs(self):
        """A seam runs with .claude/bin first on PATH, so it can call agent-cli by name."""
        # GIVEN a seam script that prints its PATH
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        seam = tmp / "s"
        seam.write_text('#!/bin/sh\necho "$1 $PATH"\n')
        seam.chmod(0o755)
        # WHEN run with capture asked for (kwargs pass through to subprocess.run)
        r = core.run_seam(seam, ["arg"], capture_output=True, text=True)
        # THEN the args pass through and .claude/bin leads PATH
        self.assertTrue(r.stdout.startswith(f"arg {CLAUDE / 'bin'}:"), r.stdout)

    def test_run_seam_streams_output_by_default(self):
        """A long seam (e.g. a worktree setup) shows its output live: nothing is captured unless asked."""
        # GIVEN a seam script that prints a marker and exits 3, and a file standing in for our stdout
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        seam = tmp / "s"
        seam.write_text('#!/bin/sh\necho streamed-$1\nexit 3\n')
        seam.chmod(0o755)
        # WHEN run with no kwargs, its stdout inherited from ours
        with open(tmp / "out", "w") as out:
            r = _run_inheriting(seam, out)
        # THEN the output went to our stdout, not into the result, and the exit code is kept
        self.assertIsNone(r.stdout)
        self.assertEqual(r.returncode, 3)
        self.assertEqual((tmp / "out").read_text(), "streamed-x\n")


def _run_inheriting(seam, out):
    """core.run_seam(seam, ["x"]) with the process's fd 1 pointed at out, so inheritance is observable."""
    saved = os.dup(1)
    sys.stdout.flush()
    os.dup2(out.fileno(), 1)
    try:
        return core.run_seam(seam, ["x"])
    finally:
        os.dup2(saved, 1)
        os.close(saved)


if __name__ == "__main__":
    unittest.main()
