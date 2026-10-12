Installing Gentle Shell does not strictly require Git (all packages are installed via npm), but many features like code review, worktrees, and CodeGraph depend on it. On minimal systems like Debian, Git might not be installed, and users only discover this later when they encounter generic warnings.

We need to add a non-blocking Git check to the installer that warns users on the review screen before they consent to the installation, providing the system-appropriate command to install Git.

Expected Behavior:
1. Add an optional `git` probe that checks for Git availability and returns `{ available: boolean }`.
   - IMPORTANT: The probe must NEVER execute the `git` binary itself. On macOS, running `/usr/bin/git` when the Command Line Tools are missing will pop up an interactive installation dialog.
   - Instead, check if `git` exists on the PATH. On macOS, `/usr/bin/git` is always present as a stub. To know if Git is actually installed on macOS without triggering the dialog, you can check if `xcode-select -p` executes successfully (which never opens a dialog), or if another Git binary is found first on the PATH.
2. Update `collectInventory` to keep `inventory.git` only when the probe successfully answers. It should not fail or change the installation footprint if the probe throws.
3. In the installer server logic (e.g. `guidance` / plan views), provide a `gitNotice` for both standard and bundled plans when Git is missing. The notice must include the OS-specific install command:
   - Linux: `sudo apt-get install -y git`
   - macOS: `xcode-select --install`
   - Windows: `winget install --id Git.Git -e`
4. In the install wizard UI, display a "Git not found" notice on the review screen if the `gitNotice` is present in the plan. The warning should show the install command, but the plan must stay installable (it must never be a blocker).