"""Tests for harness-loop/install_harness.py, run against real temp git repos."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import make_repo, run, tree_hashes

SCRIPT = Path(__file__).resolve().parent.parent / "install_harness.py"

COMMIT1 = {
    ".claude/skills/coordinator/SKILL.md": "coordinator v1\n",
    ".claude/skills/old/SKILL.md": "old skill\n",
    ".claude/settings.json": "{}\n",
    ".claude/loop-evals/run.json": "eval\n",
    "AGENTS.md": "agents\n",
    "CLAUDE.md": "repo claude\n",
    "harness-loop/loop.py": "print(1)\n",
    "templates/CLAUDE.md": "template\n",
}


def addon_hash(addon):
    """Independent re-statement of the documented add-on hash formula."""
    h = hashlib.sha256()
    files = sorted(p for p in Path(addon).rglob("*") if p.is_file())
    for rel, p in sorted((p.relative_to(addon).as_posix(), p) for p in files):
        data = p.read_bytes()
        h.update(rel.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return h.hexdigest()


class InstallHarnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo, self.sha1 = make_repo(self.tmp / "aw", COMMIT1)
        _, self.sha2 = make_repo(self.tmp / "aw", {
            ".claude/skills/coordinator/SKILL.md": "coordinator v2\n",
            ".claude/skills/old/SKILL.md": None,
        })

    def tearDown(self):
        run(["rm", "-rf", str(self.tmp)], "/")

    def install(self, commit, target, *extra, check=True):
        cmd = [sys.executable, str(SCRIPT), "--repo", str(self.repo),
               "--commit", commit, "--target", str(target), *extra]
        return run(cmd, self.tmp, check=check)

    def test_installs_requested_commit_content_not_later_commit(self):
        """
        Installing commit 1 must give commit 1's bytes even though commit 2
        changed the file. Exam results are labelled with a harness commit; if
        the installer took HEAD or the worktree, the label would be a lie and
        results could not be compared across versions.
        """
        # GIVEN a repo where commit 2 rewrote the coordinator skill
        target = self.tmp / "t"
        # WHEN commit 1 is installed
        self.install(self.sha1[:7], target)
        # THEN the installed file holds commit 1's content
        self.assertEqual(
            (target / ".claude/skills/coordinator/SKILL.md").read_text(), "coordinator v1\n")

    def test_dirty_worktree_never_leaks_into_install(self):
        """
        Only committed content is installed. An uncommitted edit in the
        agent-workflow checkout must not reach the target, or an exam would
        run a harness that matches no commit.
        """
        # GIVEN an uncommitted edit to the coordinator skill
        (self.repo / ".claude/skills/coordinator/SKILL.md").write_text("dirty\n")
        target = self.tmp / "t"
        # WHEN HEAD is installed
        self.install("HEAD", target)
        # THEN the committed text is installed, not the dirty one
        self.assertEqual(
            (target / ".claude/skills/coordinator/SKILL.md").read_text(), "coordinator v2\n")

    def test_same_commit_into_two_dirs_gives_byte_identical_trees(self):
        """
        Two installs of one commit must be byte-identical, so a harness
        version is reproducible on any machine or sandbox. If installs drift
        (timestamps baked into files, order-dependent overlays), two exam runs
        labelled with the same commit would test different harnesses and
        their scores could not be compared.
        """
        # GIVEN two empty targets
        a, b = self.tmp / "a", self.tmp / "b"
        # WHEN the same commit is installed into both
        self.install(self.sha1, a)
        self.install(self.sha1, b)
        # THEN every file has the same sha256
        self.assertEqual(tree_hashes(a), tree_hashes(b))
        self.assertTrue(tree_hashes(a))

    def test_only_claude_dir_and_agents_md_are_installed(self):
        """
        Only `.claude/` (minus loop-evals) and AGENTS.md are the product.
        Copying CLAUDE.md, templates/ or harness-loop/ would overwrite the
        target's own CLAUDE.md or plant the self-improvement loop in client
        repos; loop-evals is local eval data.
        """
        # GIVEN commit 1, which also holds CLAUDE.md, templates/, harness-loop/, loop-evals
        target = self.tmp / "t"
        # WHEN it is installed
        self.install(self.sha1, target)
        # THEN only the product files, version and manifest are present
        self.assertEqual(sorted(tree_hashes(target)), [
            ".claude/HARNESS_MANIFEST", ".claude/HARNESS_VERSION", ".claude/settings.json",
            ".claude/skills/coordinator/SKILL.md", ".claude/skills/old/SKILL.md", "AGENTS.md",
        ])

    def test_manifest_lists_every_file_the_install_wrote(self):
        """
        The manifest is what a later --force install deletes, so it must list
        exactly the files written (including itself and HARNESS_VERSION).
        A missing entry would leave stale files behind on upgrade.
        """
        # GIVEN an add-on with one new file
        addon = self.tmp / "addon"
        (addon / "skills/extra").mkdir(parents=True)
        (addon / "skills/extra/SKILL.md").write_text("extra\n")
        target = self.tmp / "t"
        # WHEN installed with the add-on
        self.install(self.sha1, target, "--addon", str(addon))
        # THEN the manifest equals the set of files in the target
        manifest = (target / ".claude/HARNESS_MANIFEST").read_text().splitlines()
        self.assertEqual(sorted(manifest), sorted(tree_hashes(target)))

    def test_addon_file_replaces_core_file_at_same_path(self):
        """
        An add-on file overrides the core file at the same relative path
        under .claude/, and project-rules.md lands at .claude/project-rules.md.
        That is how a project customises the harness without forking it.
        """
        # GIVEN an add-on overriding the coordinator and adding project rules
        addon = self.tmp / "addon"
        (addon / "skills/coordinator").mkdir(parents=True)
        (addon / "skills/coordinator/SKILL.md").write_text("X")
        (addon / "project-rules.md").write_text("rules\n")
        target = self.tmp / "t"
        # WHEN installed with the add-on
        self.install(self.sha1, target, "--addon", str(addon))
        # THEN the add-on versions are installed
        self.assertEqual((target / ".claude/skills/coordinator/SKILL.md").read_text(), "X")
        self.assertEqual((target / ".claude/project-rules.md").read_text(), "rules\n")

    def test_harness_version_records_commit_and_addon_hash(self):
        """
        HARNESS_VERSION records the full commit sha, the add-on path and the
        add-on hash, so every exam result names exactly what ran.
        """
        # GIVEN an add-on with one file
        addon = self.tmp / "addon"
        (addon / "skills/coordinator").mkdir(parents=True)
        (addon / "skills/coordinator/SKILL.md").write_text("X")
        target = self.tmp / "t"
        # WHEN installed by short sha with the add-on
        self.install(self.sha1[:7], target, "--addon", str(addon))
        # THEN the version file holds the full sha, abs add-on path and its hash
        version = json.loads((target / ".claude/HARNESS_VERSION").read_text())
        self.assertEqual(version, {"commit": self.sha1, "addon": str(addon.resolve()),
                                   "addon_sha256": addon_hash(addon)})

    def test_harness_version_without_addon_has_nulls(self):
        """
        With no add-on, addon and addon_sha256 are JSON null, not missing or
        empty strings. Readers of HARNESS_VERSION tell "no add-on" from "add-on"
        by these fields; a missing key would crash a strict reader, and an
        empty string would be mistaken for a real (empty) add-on hash.
        """
        # GIVEN an empty target
        target = self.tmp / "t"
        # WHEN installed without an add-on
        out = self.install(self.sha1, target).stdout
        # THEN both add-on fields are null and the summary line says so
        version = json.loads((target / ".claude/HARNESS_VERSION").read_text())
        self.assertEqual(version, {"commit": self.sha1, "addon": None, "addon_sha256": None})
        self.assertIn("addon: none", out)

    def test_changing_one_addon_byte_changes_hash(self):
        """
        One changed byte in the add-on must change addon_sha256, or two
        different harnesses would carry the same version label.
        """
        # GIVEN two add-ons differing by one byte
        hashes = []
        for i, text in enumerate(["X", "Y"]):
            addon = self.tmp / f"addon{i}"
            (addon / "skills").mkdir(parents=True)
            (addon / "skills/a.md").write_text(text)
            target = self.tmp / f"t{i}"
            # WHEN each is installed
            self.install(self.sha1, target, "--addon", str(addon))
            hashes.append(json.loads((target / ".claude/HARNESS_VERSION").read_text())["addon_sha256"])
        # THEN the recorded hashes differ
        self.assertNotEqual(hashes[0], hashes[1])

    def test_second_install_without_force_exits_2_and_leaves_tree_untouched(self):
        """
        A target that already has a managed install is not overwritten unless
        --force is given, so a stray command cannot silently swap the harness
        under a running exam.
        """
        # GIVEN a target with commit 1 installed
        target = self.tmp / "t"
        self.install(self.sha1, target)
        before = tree_hashes(target)
        # WHEN commit 2 is installed without --force
        result = self.install(self.sha2, target, check=False)
        # THEN it exits 2, explains, and nothing changed
        self.assertEqual(result.returncode, 2)
        self.assertIn("--force", result.stderr)
        self.assertEqual(tree_hashes(target), before)

    def test_force_reinstall_keeps_unmanaged_files_and_removes_dropped_core_file(self):
        """
        --force deletes only files listed in the previous manifest. Real
        targets keep .env, settings.local.json and discover-kb/ in .claude/;
        deleting those would destroy secrets and research. A core file removed
        between commits must disappear, or old skills linger after upgrade.
        """
        # GIVEN a target holding unmanaged files, with commit 1 installed
        target = self.tmp / "t"
        unmanaged = {".claude/.env": "SECRET=1\n", ".claude/settings.local.json": "{}\n",
                     ".claude/discover-kb/x.md": "note\n"}
        for rel, text in unmanaged.items():
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            (target / rel).write_text(text)
        self.install(self.sha1, target)
        # WHEN commit 2 is installed with --force
        self.install(self.sha2, target, "--force")
        # THEN unmanaged files are byte-identical and the dropped skill is gone
        for rel, text in unmanaged.items():
            self.assertEqual((target / rel).read_text(), text)
        self.assertFalse((target / ".claude/skills/old").exists())
        self.assertEqual(
            (target / ".claude/skills/coordinator/SKILL.md").read_text(), "coordinator v2\n")

    def test_force_reinstall_with_addon_inside_target_leaves_addon_untouched(self):
        """
        The add-on may live inside the target's .claude/ (e.g.
        .claude/harness-addon/). A --force reinstall must succeed and leave the
        add-on dir byte-identical; deleting it would destroy the project's
        customisations, which exist nowhere else.
        """
        # GIVEN an add-on inside the target's .claude/, installed once
        target = self.tmp / "t"
        addon = target / ".claude/harness-addon"
        (addon / "skills/coordinator").mkdir(parents=True)
        (addon / "skills/coordinator/SKILL.md").write_text("X")
        (addon / "project-rules.md").write_text("rules\n")
        self.install(self.sha1, target, "--addon", str(addon))
        before = tree_hashes(addon)
        # WHEN reinstalled with --force
        self.install(self.sha2, target, "--addon", str(addon), "--force")
        # THEN the add-on dir is byte-identical and still applied
        self.assertEqual(tree_hashes(addon), before)
        self.assertEqual((target / ".claude/skills/coordinator/SKILL.md").read_text(), "X")

    def test_missing_addon_exits_2_and_writes_nothing(self):
        """
        A typo in --addon must fail loudly rather than install a bare core
        harness. Otherwise the project silently runs without its rules and
        overrides, and the exam result is labelled as if the add-on applied.
        """
        # GIVEN an add-on path that does not exist
        target = self.tmp / "t"
        # WHEN installing with it
        result = self.install(self.sha1, target, "--addon", str(self.tmp / "nope"), check=False)
        # THEN it exits 2 and the target was not created
        self.assertEqual(result.returncode, 2)
        self.assertFalse(target.exists())

    def test_previous_manifest_path_inside_addon_is_refused(self):
        """
        If a previous manifest lists a file inside the add-on dir, --force
        would delete add-on files. The install must refuse before deleting.
        """
        # GIVEN a managed install whose manifest was edited to list an add-on file
        target = self.tmp / "t"
        addon = target / ".claude/harness-addon"
        addon.mkdir(parents=True)
        (addon / "project-rules.md").write_text("rules\n")
        self.install(self.sha1, target, "--addon", str(addon))
        manifest = target / ".claude/HARNESS_MANIFEST"
        manifest.write_text(manifest.read_text() + ".claude/harness-addon/project-rules.md\n")
        before = tree_hashes(target)
        # WHEN reinstalled with --force
        result = self.install(self.sha2, target, "--addon", str(addon), "--force", check=False)
        # THEN it exits 2 and nothing was deleted or changed
        self.assertEqual(result.returncode, 2)
        self.assertEqual(tree_hashes(target), before)

    def test_previous_manifest_path_outside_target_is_refused(self):
        """
        A manifest line like ../outside.txt must never be deleted: --force
        may only remove files inside the target.
        """
        # GIVEN a managed install whose manifest points outside the target
        target = self.tmp / "t"
        outside = self.tmp / "outside.txt"
        outside.write_text("keep\n")
        self.install(self.sha1, target)
        manifest = target / ".claude/HARNESS_MANIFEST"
        manifest.write_text(manifest.read_text() + "../outside.txt\n")
        # WHEN reinstalled with --force
        result = self.install(self.sha2, target, "--force", check=False)
        # THEN it exits 2 and the outside file survives
        self.assertEqual(result.returncode, 2)
        self.assertEqual(outside.read_text(), "keep\n")

    def test_overlay_without_manifest_replaces_same_path_files_and_keeps_others(self):
        """
        A target .claude/ with no manifest (hand-copied harness) is overlaid:
        same-path files are replaced and reported, nothing else is deleted.
        """
        # GIVEN a hand-copied .claude/ with one core path and one local file
        target = self.tmp / "t"
        (target / ".claude/skills/coordinator").mkdir(parents=True)
        (target / ".claude/skills/coordinator/SKILL.md").write_text("hand copied\n")
        (target / ".claude/local.md").write_text("mine\n")
        # WHEN installed
        out = self.install(self.sha1, target).stdout
        # THEN the core file is replaced and reported, the local file kept
        self.assertEqual(
            (target / ".claude/skills/coordinator/SKILL.md").read_text(), "coordinator v1\n")
        self.assertIn("replaced .claude/skills/coordinator/SKILL.md", out)
        self.assertEqual((target / ".claude/local.md").read_text(), "mine\n")

    def test_unknown_commit_exits_2(self):
        """
        A ref that does not resolve must exit 2 with a message, not a
        traceback. Callers (the loop, the exam runner) branch on exit 2 as
        "refused, nothing changed"; a traceback exit 1 reads as a crash and
        leaves them unsure whether the target was half-written.
        """
        # GIVEN a ref that does not exist
        target = self.tmp / "t"
        # WHEN installing it
        result = self.install("no-such-ref", target, check=False)
        # THEN it exits 2 and wrote nothing
        self.assertEqual(result.returncode, 2)
        self.assertFalse(target.exists())

    def link_coordinator(self, target, content):
        """Make target/.claude/skills/coordinator a symlink to a nested dir holding SKILL.md = content."""
        nested = target / "nested/coordinator"
        nested.mkdir(parents=True)
        (nested / "SKILL.md").write_text(content)
        (target / ".claude/skills").mkdir(parents=True)
        (target / ".claude/skills/coordinator").symlink_to(nested)
        return nested

    def test_symlinked_dir_in_target_is_skipped_and_left_in_place(self):
        """
        Real targets link some .claude/skills/* dirs into a nested repo, which
        must stay the single copy. Writing through the link would overwrite
        files no manifest owns, so the install skips the linked path: the link
        stays a link, the files behind it are byte-identical, the skipped paths
        are not in the manifest, and everything else installs.
        """
        # GIVEN .claude/skills/coordinator is a symlink to a nested copy equal to commit 1's
        target = self.tmp / "t"
        nested = self.link_coordinator(target, "coordinator v1\n")
        before = tree_hashes(nested)
        # WHEN commit 1 is installed
        result = self.install(self.sha1, target)
        # THEN the link and its target are untouched, the note says identical,
        # the manifest omits the skipped file, and the other files are installed
        self.assertTrue((target / ".claude/skills/coordinator").is_symlink())
        self.assertEqual(tree_hashes(nested), before)
        self.assertIn("skipped symlink .claude/skills/coordinator", result.stdout)
        self.assertIn("all 1 files identical", result.stdout)
        manifest = (target / ".claude/HARNESS_MANIFEST").read_text().splitlines()
        self.assertNotIn(".claude/skills/coordinator/SKILL.md", manifest)
        self.assertEqual((target / ".claude/skills/old/SKILL.md").read_text(), "old skill\n")

    def test_symlinked_dir_whose_content_differs_is_skipped_with_the_differing_files_named(self):
        """
        When the base copy differs from what is behind the link, the install
        still must not write through it, but the drift has to be visible so a
        human can reconcile the nested copy: the note names each differing file.
        """
        # GIVEN the linked nested copy differs from commit 1's coordinator
        target = self.tmp / "t"
        nested = self.link_coordinator(target, "nested repo copy\n")
        # WHEN commit 1 is installed
        result = self.install(self.sha1, target)
        # THEN the nested file is unchanged and the note lists it as differing
        self.assertEqual((nested / "SKILL.md").read_text(), "nested repo copy\n")
        self.assertIn("1 of 1 files differ", result.stdout)
        self.assertIn(".claude/skills/coordinator/SKILL.md", result.stdout)

    def test_version_files_through_a_symlink_are_refused(self):
        """
        HARNESS_VERSION and HARNESS_MANIFEST are the installer's own record;
        skipping them would leave an install with no label, so if .claude itself
        is a link the install refuses (exit 2) and writes nothing.
        """
        # GIVEN target/.claude is a symlink to a dir elsewhere
        target = self.tmp / "t"
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        target.mkdir()
        (target / ".claude").symlink_to(elsewhere)
        # WHEN installed / THEN exit 2 naming the symlink, nothing written behind it
        result = self.install(self.sha1, target, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("symlink", result.stderr)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_force_refuses_to_delete_previous_manifest_path_under_symlink(self):
        """
        --force deletes previous-manifest paths. If a dir on that path is now a
        symlink, the delete would remove a file in the linked repo (and the
        empty-dir cleanup would crash on the link). It must refuse before any
        delete, leaving the whole target and the link target unchanged.
        """
        # GIVEN commit 1 installed, then .claude/skills/old replaced by a link
        # (old is listed in the previous manifest but not in commit 2)
        target = self.tmp / "t"
        self.install(self.sha1, target)
        nested = target / "nested/old"
        nested.mkdir(parents=True)
        (nested / "SKILL.md").write_text("nested repo copy\n")
        run(["rm", "-rf", str(target / ".claude/skills/old")], "/")
        (target / ".claude/skills/old").symlink_to(nested)
        before = tree_hashes(target)
        # WHEN commit 2 is installed with --force
        result = self.install(self.sha2, target, "--force", check=False)
        # THEN it exits 2 and nothing was deleted or changed
        self.assertEqual(result.returncode, 2)
        self.assertIn("symlink", result.stderr)
        self.assertEqual(tree_hashes(target), before)
        self.assertEqual((nested / "SKILL.md").read_text(), "nested repo copy\n")

    def test_failed_force_install_exits_2_and_tracks_every_path_it_may_have_written(self):
        """
        If an install fails part-way (here: an unmanaged regular file sits where
        an add-on needs a directory), it must exit 2 with a message, not a
        traceback, and the manifest must list every old path plus every path
        the failed run may have written. A file written before the failure but
        missing from the manifest would be orphaned for good: no later --force
        run would ever delete it.
        """
        # GIVEN commit 1 installed, and an unmanaged file blocking an add-on dir
        _, sha3 = make_repo(self.repo, {".claude/skills/b-only/SKILL.md": "b only\n"})
        target = self.tmp / "t"
        self.install(self.sha1, target)
        old_manifest = set((target / ".claude/HARNESS_MANIFEST").read_text().splitlines())
        (target / ".claude/skills/newskill").write_text("unmanaged file\n")
        addon = self.tmp / "addon"
        (addon / "skills/newskill").mkdir(parents=True)
        (addon / "skills/newskill/SKILL.md").write_text("new\n")
        # WHEN commit 3 is installed with --force and that add-on
        result = self.install(sha3, target, "--addon", str(addon), "--force", check=False)
        # THEN it exits 2 cleanly and the manifest tracks old and new paths
        self.assertEqual(result.returncode, 2)
        self.assertIn("install failed", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        manifest = set((target / ".claude/HARNESS_MANIFEST").read_text().splitlines())
        self.assertTrue(old_manifest <= manifest)
        self.assertIn(".claude/skills/b-only/SKILL.md", manifest)
        self.assertIn(".claude/skills/newskill/SKILL.md", manifest)

    def test_force_rollback_after_failed_install_removes_files_the_failed_run_wrote(self):
        """
        After a failed --force install of commit B, a --force rollback to
        commit A must leave no B-only files behind. Otherwise the rolled-back
        harness still carries skills from B, and the exam result labelled
        "commit A" actually ran a mix of A and B.
        """
        # GIVEN a failed --force install of commit 3 (adds b-only) over commit 1
        _, sha3 = make_repo(self.repo, {".claude/skills/b-only/SKILL.md": "b only\n"})
        target = self.tmp / "t"
        self.install(self.sha1, target)
        blocker = target / ".claude/skills/newskill"
        blocker.write_text("unmanaged file\n")
        addon = self.tmp / "addon"
        (addon / "skills/aaa").mkdir(parents=True)
        (addon / "skills/aaa/SKILL.md").write_text("copied before failure\n")
        (addon / "skills/newskill").mkdir(parents=True)
        (addon / "skills/newskill/SKILL.md").write_text("new\n")
        failed = self.install(sha3, target, "--addon", str(addon), "--force", check=False)
        self.assertEqual(failed.returncode, 2)
        self.assertTrue((target / ".claude/skills/b-only/SKILL.md").exists())
        blocker.unlink()
        # WHEN rolled back to commit 1 with --force
        self.install(self.sha1, target, "--force")
        # THEN no commit-3 or add-on file remains and the manifest is exactly commit 1's
        self.assertFalse((target / ".claude/skills/b-only").exists())
        self.assertFalse((target / ".claude/skills/aaa").exists())
        manifest = (target / ".claude/HARNESS_MANIFEST").read_text().splitlines()
        self.assertEqual(sorted(manifest), sorted(tree_hashes(target)))

    def test_executable_addon_file_stays_executable(self):
        """
        An add-on may ship its own hook script. Claude Code runs hooks as
        executables, so dropping the executable bit on copy would make the
        hook fail with "permission denied" at runtime.
        """
        # GIVEN an add-on with an executable hook
        addon = self.tmp / "addon"
        (addon / "hooks").mkdir(parents=True)
        (addon / "hooks/x.sh").write_text("#!/bin/sh\necho hi\n")
        (addon / "hooks/x.sh").chmod(0o755)
        target = self.tmp / "t"
        # WHEN installed with the add-on
        self.install(self.sha1, target, "--addon", str(addon))
        # THEN the installed hook is executable
        self.assertTrue(os.access(target / ".claude/hooks/x.sh", os.X_OK))

    def test_core_file_landing_inside_addon_dir_is_refused(self):
        """
        If the harness commit itself contains a file under the add-on's
        location (.claude/harness-addon/), installing would overwrite the
        project's add-on. It must refuse with exit 2 and leave the target and
        the add-on byte-identical.
        """
        # GIVEN a core commit with .claude/harness-addon/x.md and the add-on at that path
        _, sha3 = make_repo(self.repo, {".claude/harness-addon/x.md": "core\n"})
        target = self.tmp / "t"
        addon = target / ".claude/harness-addon"
        addon.mkdir(parents=True)
        (addon / "x.md").write_text("project addon\n")
        before = tree_hashes(target)
        # WHEN that commit is installed with the add-on
        result = self.install(sha3, target, "--addon", str(addon), check=False)
        # THEN it exits 2 and nothing changed
        self.assertEqual(result.returncode, 2)
        self.assertIn("add-on dir", result.stderr)
        self.assertEqual(tree_hashes(target), before)

    def test_installignore_keeps_private_task_folders_out_of_install_and_hash(self):
        """
        A private add-on dir may also hold exam task folders (hidden tests plus
        the reference fix). Prefixes listed in .installignore, and the file
        itself, must not be installed into the agent's sandbox and must not
        change addon_sha256; otherwise the agent could read its own grader and
        rebuilding a task would change the harness version label.
        """
        # GIVEN an add-on with a skill, a private task dir, a private file and an .installignore
        addon = self.tmp / "addon"
        (addon / "skills").mkdir(parents=True)
        (addon / "skills/a.md").write_text("A")
        (addon / "sidecheck/task1/tests").mkdir(parents=True)
        (addon / "sidecheck/task1/tests/hidden.test.js").write_text("secret test")
        (addon / "notes.md").write_text("private")
        (addon / "sidecheck2").mkdir()
        (addon / "sidecheck2/kept.md").write_text("kept: prefix match is per path part")
        (addon / ".installignore").write_text("# private tasks\n\nsidecheck/\nnotes.md\n")
        target = self.tmp / "t"
        # WHEN installed with the add-on
        self.install(self.sha1, target, "--addon", str(addon))
        # THEN the ignored files and .installignore are absent from the target and the manifest
        manifest = (target / ".claude/HARNESS_MANIFEST").read_text()
        for rel in ("sidecheck/task1/tests/hidden.test.js", "notes.md", ".installignore"):
            self.assertFalse((target / ".claude" / rel).exists(), rel)
            self.assertNotIn(f".claude/{rel}\n", manifest)
        self.assertTrue((target / ".claude/skills/a.md").is_file())
        self.assertTrue((target / ".claude/sidecheck2/kept.md").is_file())
        # AND the hash equals the hash of the add-on without the ignored files
        bare = self.tmp / "bare"
        (bare / "skills").mkdir(parents=True)
        (bare / "skills/a.md").write_text("A")
        (bare / "sidecheck2").mkdir()
        (bare / "sidecheck2/kept.md").write_text("kept: prefix match is per path part")
        version = json.loads((target / ".claude/HARNESS_VERSION").read_text())
        self.assertEqual(version["addon_sha256"], addon_hash(bare))


    def addon_with_ignore(self, ignore, extra=()):
        addon = self.tmp / "addon"
        (addon / "skills").mkdir(parents=True)
        (addon / "skills/a.md").write_text("A")
        for rel in extra:
            (addon / rel).parent.mkdir(parents=True, exist_ok=True)
            (addon / rel).write_text("x")
        (addon / ".installignore").write_text(ignore)
        return addon

    def test_unignored_private_task_folder_is_refused(self):
        """
        A private task folder (it has a task.toml) that .installignore does not
        cover would ship its hidden tests and reference fix into the agent's
        sandbox. Refuse rather than install it.
        """
        # GIVEN an add-on whose task folder sits outside every ignored prefix
        addon = self.addon_with_ignore("private/\n", extra=("private/t0/task.toml", "tasks/t1/task.toml",
                                                             "tasks/t1/tests/hidden.test.js"))
        target = self.tmp / "t"
        # WHEN installed
        result = self.install(self.sha1, target, "--addon", str(addon), check=False)
        # THEN exit 2 naming the file, and nothing written
        self.assertEqual(result.returncode, 2)
        self.assertIn("tasks/t1/task.toml", result.stderr)
        self.assertFalse(target.exists())

    def test_installignore_line_matching_nothing_is_refused(self):
        """A typo in .installignore (e.g. `sidechek/`) would silently ignore nothing; refuse it."""
        # GIVEN an ignore line naming no add-on file
        addon = self.addon_with_ignore("private/\nsidechek/\n", extra=("private/x.md",))
        target = self.tmp / "t"
        # WHEN installed / THEN exit 2 naming the line, nothing written
        result = self.install(self.sha1, target, "--addon", str(addon), check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("sidechek", result.stderr)
        self.assertFalse(target.exists())

    def test_installignore_absolute_or_parent_line_is_refused(self):
        """Ignore lines are add-on-relative prefixes; `/x` or `..` cannot mean that."""
        for line in ("/private", "private/../skills", ".."):
            with self.subTest(line=line):
                # GIVEN an ignore line that is absolute or climbs out
                addon = self.addon_with_ignore(f"{line}\n", extra=("private/x.md",))
                target = self.tmp / "t"
                # WHEN installed / THEN exit 2 naming the line, nothing written
                result = self.install(self.sha1, target, "--addon", str(addon), check=False)
                self.assertEqual(result.returncode, 2)
                self.assertIn(line, result.stderr)
                self.assertIn("add-on-relative", result.stderr)
                self.assertFalse(target.exists())
                run(["rm", "-rf", str(addon)], "/")


SETTINGS = {
    "permissions": {"allow": ["Bash(git:*)"], "deny": ["Bash(rm:*)"]},
    "hooks": {
        "SessionStart": [
            {"hooks": [{"type": "command", "command": "cat AGENTS.md"}]},
            {"matcher": "compact", "hooks": [{"type": "command", "command": "echo compacted"}]},
        ],
        "PostCompact": [{"hooks": [{"type": "command", "command": "save.sh"}]}],
    },
    "autoCompactWindow": 256000,
}


class SkipbaseTest(unittest.TestCase):
    """The add-on's .skipbase opts a target out of base paths and settings entries, failing closed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo, self.sha = make_repo(self.tmp / "aw", {
            ".claude/settings.json": json.dumps(SETTINGS, indent=2) + "\n",
            ".claude/skills/doc/SKILL.md": "doc\n",
            ".claude/skills/doc/template.html": "tpl\n",
            ".claude/skills/coordinator/SKILL.md": "coordinator\n",
            ".claude/hooks/save.sh": "echo save\n",
            "AGENTS.md": "agents\n",
        })
        self.addon = self.tmp / "addon"
        (self.addon / "skills/doc").mkdir(parents=True)
        (self.addon / "skills/doc/example.html").write_text("private example\n")
        self.target = self.tmp / "t"

    def tearDown(self):
        run(["rm", "-rf", str(self.tmp)], "/")

    def install(self, skipbase, *extra, check=True):
        (self.addon / ".skipbase").write_text(skipbase)
        cmd = [sys.executable, str(SCRIPT), "--repo", str(self.repo), "--commit", self.sha,
               "--target", str(self.target), "--addon", str(self.addon), *extra]
        return run(cmd, self.tmp, check=check)

    def settings(self):
        return json.loads((self.target / ".claude/settings.json").read_text())

    def test_path_opts_out_base_files_but_keeps_addon_files_there(self):
        """
        A target that already gets a skill from elsewhere (e.g. a user-level
        copy) must not get the base copy too. `path` drops every base file under
        the prefix; an add-on file under it is still installed because the
        add-on asked for it explicitly. .skipbase itself is never installed.
        """
        # GIVEN .skipbase opting out of the base doc skill and the root AGENTS.md
        # WHEN installed
        result = self.install("# keep the user's own copy\npath .claude/skills/doc/\npath AGENTS.md\n")
        # THEN the base doc files and AGENTS.md are absent from disk and manifest, the add-on file is there
        manifest = (self.target / ".claude/HARNESS_MANIFEST").read_text().splitlines()
        for rel in (".claude/skills/doc/SKILL.md", ".claude/skills/doc/template.html", "AGENTS.md",
                    ".claude/.skipbase"):
            self.assertFalse((self.target / rel).exists(), rel)
            self.assertNotIn(rel, manifest)
        self.assertEqual((self.target / ".claude/skills/doc/example.html").read_text(), "private example\n")
        self.assertEqual((self.target / ".claude/skills/coordinator/SKILL.md").read_text(), "coordinator\n")
        self.assertIn("opted out of base .claude/skills/doc (2 files)", result.stdout)

    def test_skipbase_is_hashed_into_the_addon_hash(self):
        """
        The version label must change when the opt-outs change, or two
        installs with different harness content would carry the same label.
        The hash includes .skipbase by the documented formula.
        """
        # GIVEN two installs that differ only in one .skipbase line
        self.install("path AGENTS.md\n")
        first = json.loads((self.target / ".claude/HARNESS_VERSION").read_text())["addon_sha256"]
        run(["rm", "-rf", str(self.target)], "/")
        self.install("path .claude/skills/doc/\n")
        second = json.loads((self.target / ".claude/HARNESS_VERSION").read_text())["addon_sha256"]
        # THEN the hashes differ and the second equals the independent formula over the add-on (incl. .skipbase)
        self.assertNotEqual(first, second)
        self.assertEqual(second, addon_hash(self.addon))

    def test_setting_lines_delete_top_level_and_nested_keys_and_keep_the_rest(self):
        """
        `setting a.b` removes one key from the installed settings.json so a
        target can keep a user-level value (e.g. its own compact window)
        instead of the base one. Everything else stays as in the base.
        """
        # WHEN installed opting out of a top-level and a nested key
        self.install("setting autoCompactWindow\nsetting permissions.deny\n")
        # THEN exactly those keys are gone
        data = self.settings()
        self.assertNotIn("autoCompactWindow", data)
        self.assertEqual(data["permissions"], {"allow": ["Bash(git:*)"]})
        self.assertEqual(data["hooks"], SETTINGS["hooks"])

    def test_hook_lines_delete_groups_by_matcher_and_drop_emptied_events(self):
        """
        `hook <Event> <matcher>` removes only the groups with that matcher, so a
        target can drop one base hook it already runs at user level without
        losing the event's other hooks; `-` means groups with no matcher, and
        an event left with no groups is deleted rather than kept empty.
        """
        # WHEN installed opting out of the compact SessionStart group and the matcher-less PostCompact group
        self.install("hook SessionStart compact\nhook PostCompact -\n")
        # THEN SessionStart keeps only its matcher-less group and PostCompact is gone
        hooks = self.settings()["hooks"]
        self.assertEqual(hooks["SessionStart"], [SETTINGS["hooks"]["SessionStart"][0]])
        self.assertNotIn("PostCompact", hooks)

    def test_force_reinstall_removes_a_base_file_newly_opted_out(self):
        """
        Opting out after an install must take effect on the next --force
        reinstall: the previously installed base file is deleted (it is in the
        old manifest) and not written again.
        """
        # GIVEN an install with no opt-outs
        self.install("")
        self.assertTrue((self.target / ".claude/hooks/save.sh").exists())
        # WHEN reinstalled with --force opting out of the hook script
        self.install("path .claude/hooks/save.sh\n", "--force")
        # THEN the file is gone and not in the manifest
        self.assertFalse((self.target / ".claude/hooks/save.sh").exists())
        self.assertNotIn(".claude/hooks/save.sh",
                         (self.target / ".claude/HARNESS_MANIFEST").read_text().splitlines())

    def test_entries_that_match_nothing_or_do_not_parse_are_refused_before_writing(self):
        """
        Fail closed, like .installignore: a typo or a stale entry (the base
        renamed the key or the skill) would otherwise silently install what
        the target opted out of. Each case exits 2 naming the line, writing nothing.
        """
        cases = {
            "path .claude/skills/dco/\n": "matches no base file",
            "path /abs\n": "target-relative",
            "path .claude/../x\n": "target-relative",
            "setting autoCompactWindo\n": "names no key",
            "setting permissions.allow.x\n": "names no key",
            "hook SessionStart startup\n": "matches no hooks.SessionStart group",
            "hook Stop -\n": "has no hooks.Stop",
            "skip AGENTS.md\n": "cannot parse",
            "hook SessionStart\n": "cannot parse",
            "path .claude/settings.json\nsetting autoCompactWindow\n": "no .claude/settings.json is installed",
        }
        for skipbase, message in cases.items():
            with self.subTest(skipbase=skipbase):
                # WHEN installed with the bad entry / THEN exit 2 with the reason, nothing written
                result = self.install(skipbase, check=False)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(message, result.stderr)
                self.assertFalse(self.target.exists())


if __name__ == "__main__":
    unittest.main()
