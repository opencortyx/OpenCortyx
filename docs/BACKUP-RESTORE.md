# Backup and restore

OpenCortyx keeps its state in three stores, each in its own Docker volume: PostgreSQL (the control
plane: users, settings, catalogue, runs), NebulaGraph (the graph) and Qdrant (the search surfaces).
A backup archives all of them together, so a restore brings back a consistent whole.

```bash
bin/opencortyx backup                 # writes backups/opencortyx_<timestamp>/
bin/opencortyx backup /path/to/dir
bin/opencortyx restore backups/opencortyx_<timestamp>
```

On Windows, use `bin\opencortyx.ps1` with the same arguments.

`backup` stops the stack for the copy (a database's files are only consistent when nothing is
writing to them), archives each volume, writes a `manifest.json`, and starts the stack again. A
backup directory holds:

```
manifest.json
postgres_data.tar.gz
nebula_meta_data.tar.gz
nebula_storage_data.tar.gz
qdrant_data.tar.gz
data_store.tar.gz
```

`restore` asks for confirmation, stops the stack, replaces every store with the backup (a backup from before `data_store` existed restores without it) and
starts everything again. Set `OPENCORTYX_YES=1` to skip the prompt in scripts.

If a service fails to come back after `backup` (a container can fail to start under memory
pressure), start the stack again with `docker compose start`; the backup itself is already written.

Recommendations:

- Schedule `bin/opencortyx backup` daily (cron or Task Scheduler) and keep about 30 days.
- Test a restore on a separate machine or a copy of the stack before you need it.
- Backups contain all your data. Store them encrypted and never commit them.
