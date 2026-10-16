# Updating and uninstalling

## Update

```bash
opencortyx backup          # optional but recommended
opencortyx update
```

`update` moves an installation that follows releases to the newest release tag (or pulls `main` for
a branch checkout), then starts the stack. Database migrations run automatically in the `setup`
service before the API starts. Check the result with `opencortyx doctor`.

Read the [changelog](../../CHANGELOG.md) before upgrading across several versions.

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
