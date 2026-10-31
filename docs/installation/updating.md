# Updating and uninstalling

## Update

```bash
opencortyx backup          # optional but recommended
opencortyx update
```

`update` moves an installation that follows releases to the newest release tag (or pulls `main` for
a branch checkout), then starts the stack with the new version's own command. Database migrations
run automatically in the `setup` service before the API starts. Check the result with
`opencortyx doctor`.

Read the [changelog](../../CHANGELOG.md) before upgrading across several versions.

### From 0.1.0-beta.2

The `update` command of 0.1.0-beta.2 switches to the new version but then starts it with its own,
older code, which still tries to start the GPU model server that later versions removed. Upgrade
from 0.1.0-beta.2 by hand instead; your data is kept.

macOS and Linux:

```bash
cd ~/opencortyx
git fetch --tags
git checkout v0.2.0-beta.1
bin/opencortyx up
```

Windows (PowerShell):

```powershell
cd $HOME\opencortyx
git fetch --tags
git checkout v0.2.0-beta.1
.\bin\opencortyx.ps1 up
```

If `git checkout` reports local changes to a file under `bin/`, that copy was edited by hand
(usually to work around an install problem fixed since); `git checkout -- bin` discards the edits.
Then open **Models** and add a generation model: the bundled one is gone (see the
[changelog](../../CHANGELOG.md)).

Use the newest release tag (the [releases page](https://github.com/opencortyx/OpenCortyx/releases)
lists them). From 0.1.0-beta.3 on, `opencortyx update` works; from 0.1.0-beta.4 on, it also starts
the new version with that version's own command.

## Stop and start

```bash
opencortyx down            # stops containers; data is kept
opencortyx up
```

## Uninstall

1. Delete the data, containers and volumes (this cannot be undone; back up first):

   ```bash
   opencortyx backup
   opencortyx reset
   ```

2. Remove the images:

   ```bash
   docker image ls "*opencortyx*" -q | xargs docker image rm
   ```

3. Delete the installation directory (`~/opencortyx`, or `%USERPROFILE%\opencortyx` on Windows).
4. Remove the CLI: delete `~/.local/bin/opencortyx`, or on Windows remove `...\opencortyx\bin` from your
   user PATH.
