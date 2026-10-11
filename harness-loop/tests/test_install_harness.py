"""Tests for harness-loop/install_harness.py, run against real temp git repos."""
import hashlib
import json
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
        version is reproducible on any machine or sandbox.
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
        """With no add-on, addon and addon_sha256 are null, not missing or empty."""
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
        """A typo in --addon must fail loudly rather than install a bare core harness."""
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
        """A ref that does not resolve must fail with a clear exit code, not a traceback."""
        # GIVEN a ref that does not exist
        target = self.tmp / "t"
        # WHEN installing it
        result = self.install("no-such-ref", target, check=False)
        # THEN it exits 2 and wrote nothing
        self.assertEqual(result.returncode, 2)
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
