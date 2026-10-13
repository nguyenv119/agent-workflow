"""Tests for agent-cli's help, exit codes, `self`, `lib`, `eval lib` and the core helpers project CLIs import."""
import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from helpers import CLAUDE, GIT, agent_cli, make_project, run

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import core, main  # noqa: E402

LEAVES = [[], ["hook"], ["hook", "pretooluse"], ["hook", "test"], ["hook", "rules"], ["eval"], ["eval", "lib"],
          ["self"], ["self", "env"], ["self", "install"], ["lib"], ["lib", "path"],
          ["wt"], ["wt", "new"], ["wt", "list"], ["wt", "audit"], ["wt", "rm"],
          ["wait"], ["lock"], ["lock", "acquire"], ["lock", "release"], ["lock", "status"],
          ["pr"], ["pr", "status"], ["pr", "wait"], ["pr", "land"]]


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="agent-cli-")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class Help(TmpCase):
    def test_every_help_starts_with_its_summary_and_ends_with_examples(self):
        """R2: each layer's --help opens with `<prog>: <summary>` and ends in an Examples: block.

        Project CLIs list themselves via `<cli> --help | head -1`, and agents copy the examples.
        """
        # GIVEN an installed project
        p = make_project(self.tmp / "proj")
        for path in LEAVES:
            # WHEN each layer's help is printed
            r = agent_cli(p, *path, "--help")
            lines = r.stdout.rstrip("\n").splitlines()
            # THEN line 1 is the summary and the last block is Examples with real invocations
            with self.subTest(path=path):
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertTrue(lines[0].startswith(" ".join(["agent-cli", *path]) + ": "), lines[0])
                ex = lines.index("Examples:")
                self.assertTrue(all(ln.startswith("  ") and "agent-cli" in ln for ln in lines[ex + 1:]), lines[ex:])
                self.assertGreater(len(lines), ex + 1)

    def test_bare_command_prints_root_help(self):
        """`agent-cli` alone prints the command list (exit 0), never prompts (R1)."""
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
        self.assertEqual(names, ["hook", "eval", "self", "lib", "wt", "wait", "lock", "pr"])

    def test_group_prints_its_help_when_called_bare(self):
        """core.group: a command group whose bare call prints its own help (R1), with a subparser set."""
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
        """R3/R8: a bad invocation fails fast on stderr with `error:` and a correct example, exit 2."""
        # GIVEN an installed project
        p = make_project(self.tmp / "proj")
        # WHEN eval lib is called without its required flag
        r = agent_cli(p, "eval", "lib")
        # THEN exit 2, error line and an example line
        self.assertEqual(r.returncode, 2)
        self.assertRegex(r.stderr, r"^error: .+\n  example: agent-cli eval lib --sh\n$")


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
        """R4: the first run links bin-dir/agent-cli; a second run reports `exists:` and changes nothing."""
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
        """R5: --dry-run says what it would do and leaves the bin dir alone."""
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
        """R8 exit codes are fixed numbers every caller and eval relies on."""
        # GIVEN / WHEN the enum
        # THEN values match R8
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

    def test_emit_json_and_text(self):
        """R7: --json is one object per line; text mode is `key: value` lines."""
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
        """R3: die writes `error:` and `  example:` to stderr and exits with the given code."""
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
