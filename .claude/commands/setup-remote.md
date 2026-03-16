# Setup DoltHub Remote

Configure a DoltHub remote for multi-machine beads collaboration in this project.

This is a one-time-per-project operation. After running this command, every machine with `dolt login` configured can pull and push beads data via DoltHub.

## 1. Confirm intent

Ask the user:

> This will configure a DoltHub remote for your project's beads database and push the current beads data to it. Proceed?

If the user says no, stop here.

## 2. Get the DoltHub remote URL

Ask the user for their DoltHub remote in the format `<owner>/<database>` (e.g., `nguyenv119/my-project`).

Store this as `REMOTE_URL`.

## 3. Check that dolt is installed

```bash
which dolt
```

If this fails (dolt not found), stop and tell the user:

> `dolt` is not installed. Install it from https://docs.dolthub.com/introduction/installation and re-run `/setup-remote`.

## 4. Check that dolt login has been run

```bash
ls ~/.dolt/creds/
```

If the directory is empty or does not exist, stop and tell the user:

> You have not authenticated with DoltHub. Run `dolt login` and follow the prompts, then re-run `/setup-remote`.

## 5. Configure the remote

Add the DoltHub remote as `origin`:

```bash
(cd .beads/dolt && dolt remote add origin $REMOTE_URL)
```

If this fails with "remote already exists", tell the user and ask if they want to remove and re-add it:

```bash
(cd .beads/dolt && dolt remote remove origin)
(cd .beads/dolt && dolt remote add origin $REMOTE_URL)
```

## 6. Push current beads data to the remote

```bash
(cd .beads/dolt && dolt push -u origin main)
```

If this fails, show the error to the user and stop. Common causes:
- Auth: re-run `dolt login`
- Wrong URL: verify the remote path in DoltHub

## 7. Verify and confirm

```bash
(cd .beads/dolt && dolt remote -v)
```

Report success to the user:

> Remote configured successfully. `dolt remote -v` shows:
> ```
> <output of dolt remote -v>
> ```
> Your beads database is now synced to DoltHub at `$REMOTE_URL`. Other machines can pull it once they run `dolt login` and the sync hooks are in place.
