# Backup and restore

Spec §K.8. The rules, and how each is held:

| Rule | How it is held |
|---|---|
| A retained backup is encrypted at rest. | `apollo backup dump` writes only to a pipe. It refuses a file and a terminal, so the only backup you can keep is one that went through an encryption tool first. |
| A restore has really been performed and verified. | `apollo backup verify` restores a backup into a scratch database, checks it, and drops it. The first one is recorded at the end of this page. |
| Backups are destroyed after the retention window (default 30 days). | `apollo backup prune` removes them by the time in their names. `backup_retention_days` in `apollo.toml` sets the window. |
| Forgotten content can outlive its deletion only inside that window. | See "What a backup does to forgetting" below. |

Apollo does not choose your encryption tool or see your key. The examples use
[`age`](https://age-encryption.org); anything that encrypts a stream will do.

## What you need

- `pg_dump` and `pg_restore` of the same major version as the server (PostgreSQL 16).
- An encryption tool, and a key pair for it.
- The owner connection, `APOLLO_ADMIN_DSN`. Backups are taken and tested as the owner, not as
  `apollo_app`.

Make the key pair once, **on another machine**:

```sh
age-keygen -o apollo-backup.key      # prints the public key: age1...
```

- The Apollo host needs only the public key (`age1...`). It can then make backups it cannot read.
- Keep `apollo-backup.key` somewhere that is not the Apollo host and not the backup directory.
- **If you lose the key, the backups are gone.** Nothing can recover them.

## Taking a backup

Save this as `apollo-backup.sh`. It is the whole procedure:

```sh
#!/usr/bin/env bash
# A backup that either completes or leaves nothing that looks like one.
set -euo pipefail
umask 077                            # the file is yours alone

name="$(apollo backup name age)"
apollo backup dump | age -r "$APOLLO_BACKUP_RECIPIENT" -o "$BACKUP_DIR/$name.partial"
mv "$BACKUP_DIR/$name.partial" "$BACKUP_DIR/$name"
apollo backup prune "$BACKUP_DIR"
```

```sh
export APOLLO_ADMIN_DSN="host=127.0.0.1 port=5432 user=apollo_owner dbname=apollo"
export APOLLO_BACKUP_RECIPIENT="age1..."  BACKUP_DIR=/srv/backups
./apollo-backup.sh
```

- The file is named for when it was taken and what encrypted it, for example
  `apollo-20261113T074856Z.dump.age`. Keep the name: retention reads the time from it.
- **A backup gets that name only once the dump has succeeded.** The encryption tool cannot tell a
  failed dump from a finished one: it encrypts whatever arrived, even nothing, and exits 0. So
  the script writes to `.partial`, stops if the dump fails (`pipefail`), and renames only after.
  A failed run leaves a `.partial` file, which `prune` lists as not a backup and never counts.
- **Old backups are pruned only after a new one exists.** If backups stop working, the ones you
  have are kept.
- `pg_dump` takes one consistent snapshot, so Apollo may keep running. A turn in flight is backed
  up as started, and is marked interrupted the first time Apollo starts on a restored copy.
- Connection details reach `pg_dump` through the environment, never its command line, where a
  password would be visible to every process on the machine.

Do not shorten this to `apollo backup dump | age ... -o "$BACKUP_DIR/$(apollo backup name age)"`.
Under `sh`, and under `cron`, that line exits 0 when the dump fails, and leaves a 200-byte file
with a backup's name.

**What is in it:** every Apollo table, including the audit stream and the record of which
migrations were applied.

**What is not in it:**

- The runtime role and its grants. They belong to the server, not the database. `apollo
  provision` recreates them.
- The two PostgreSQL log settings (see the README, "PostgreSQL's own log"). They are per-database
  settings a superuser makes, and must be made again on a restored database.
- `apollo.toml`, the `identity/` files and your environment variables. The first two are in the
  repository; the last are yours to keep.

## Testing a restore

Do this after your first backup, and again after any upgrade that changes the schema. An untested
backup is a rumour.

```sh
age -d -i apollo-backup.key "$BACKUP_DIR/apollo-20261113T074856Z.dump.age" \
  | apollo backup verify
```

It restores the backup into a new scratch database on the same server, and then checks:

1. **It restores at all:** one transaction, stopping at the first error, with every foreign key
   intact. And it is an Apollo database: a dump taken from some other database is refused.
2. **Its migrations are the ones this version of Apollo knows,** by checksum. Any the backup
   predates are applied, as they would be on a real restore, and named.
3. **Its schema is the schema of a fresh install:** every column, constraint, trigger, function
   and index. The rules that make messages write-once and the audit stream append-only are
   schema, so a restore that lost one would otherwise still look like Apollo.
4. **Its content verifies itself.** Every recorded model call is replayed: the hashes each one
   recorded are recomputed from the restored rows.

It prints row counts per table and the replay counts, ends with `RESTORE VERIFIED` or `RESTORE NOT
VERIFIED`, and exits 1 for the latter. It prints no content. The scratch database is dropped
whatever happens.

What step 4 reaches, and what it does not:

- **It reaches everything that was ever sent to a model:** identity, messages, memories. A row
  that changed since is a mismatch, named by invocation id.
- **It does not reach a row no model call used,** such as a memory that was never retrieved, or
  the audit stream. Nothing recorded a hash of those. For them the evidence is step 1 and the row
  counts.
- A call whose memory you have forgotten replays as `SOURCE_REDACTED`, before and after a
  restore. That is correct, and is not a failure.

The scratch database holds your real data while it exists. It is on the same server as Apollo's
own database, so it is covered by the same disk encryption (§K.6) and by nothing else.

## Restoring for real

Into a new, empty database. The runtime role does not need to exist yet.

```sh
createdb -h 127.0.0.1 -U apollo_owner apollo

export PGHOST=127.0.0.1 PGUSER=apollo_owner          # and PGPASSWORD, or a ~/.pgpass entry
age -d -i apollo-backup.key "$BACKUP_DIR/apollo-20261113T074856Z.dump.age" \
  | pg_restore --no-owner --no-privileges --exit-on-error --single-transaction --dbname=apollo

export APOLLO_ADMIN_DSN="host=127.0.0.1 port=5432 user=apollo_owner dbname=apollo"
apollo migrate                                        # applies anything the backup predates
printf '%s\n' "$APOLLO_APP_PASSWORD" | apollo provision --password-stdin

# As a PostgreSQL superuser: the log settings are not in a backup.
psql -d apollo -c "ALTER DATABASE apollo SET log_error_verbosity = 'terse'"
psql -d apollo -c "ALTER DATABASE apollo SET log_min_error_statement = 'panic'"

unset APOLLO_ADMIN_DSN
export APOLLO_DATABASE_DSN="host=127.0.0.1 port=5432 user=apollo_app dbname=apollo"
apollo doctor                                         # least privilege: OK
apollo turn replay --all                              # the same counts as before the loss
```

`apollo turn replay --all` exits 1 if any call fails to reproduce for a reason the record does not
explain.

## Retention

```sh
apollo backup prune "$BACKUP_DIR"            # --dry-run to see first
```

- It destroys every backup older than `backup_retention_days` (`apollo.toml`, default 30), going
  by the time in the file's name.
- It leaves alone any file it does not recognise as an Apollo backup, and says so. That includes a
  `.partial` file from a failed run, which is yours to delete.
- It exits 1 if it finds a plaintext dump (`apollo-*.dump` with no encryption suffix), and names
  it. It does not remove it: that file is yours to encrypt or destroy.

`apollo-backup.sh` runs it after each successful backup. Schedule the script, with its three
variables set:

```cron
17 3 * * *  cd /srv/apollo && ./apollo-backup.sh
```

`cron` mails you a job's output when it prints any. A failed dump prints one line and exits 1.

If you copy backups elsewhere, the copies are yours to expire too. Apollo can only prune a
directory it is pointed at.

## What a backup does to forgetting

`apollo memory forget` removes a claim from the live database at once. It cannot reach into a
backup taken earlier.

- A backup taken **before** you forgot something still contains it. Restoring that backup brings
  it back.
- A backup taken **after** does not.
- So the honest statement is: what you forget is gone from the live system immediately, and
  ceases to exist in backups within the retention window. A test restores a backup from each
  side of a forget and checks both.

If you restore an old backup, forget again anything you had forgotten since it was taken.

## Record of the first executed restore (O.1/30)

Performed 2026-11-13 by Claude, in the cloud development session, with the commands on this page
as written. It was a scratch deployment on `brain.fake`, not Janu's deployment, which does not
exist yet.

| | |
|---|---|
| Server, `pg_dump`, `pg_restore` | PostgreSQL 16.15 |
| Encryption | `age` 1.1.1, to a key pair made for the test |
| Source | one conversation of four turns, a saved proposal, two memories (one then forgotten), five recorded model calls |

**The backup.** `apollo backup dump | age -r … -o backups/apollo-20261113T074856Z.dump.age`
exited 0. The file was 78,116 bytes, mode `0600`, began `age-encryption.org/v1`, and contained no
`PGDMP` marker and none of the conversation's words. It was the only file written.
`apollo backup dump > file` was refused with "a dump is not written to a file", leaving an empty
file.

**The restore test.** `age -d -i apollo-backup.key … | apollo backup verify` printed:

```
restored into a scratch database, in one transaction
  audit_event                   33 rows
  conversation                   1 rows
  identity_version               1 rows
  memory                         2 rows
  memory_observation             2 rows
  memory_proposal                1 rows
  message                        8 rows
  model_invocation               5 rows
  schema_migration               5 rows
  turn                           4 rows
  turn_retrieval                12 rows
  turn_retrieval_result          6 rows
migrations: all present, checksums match
schema: identical to a fresh install (columns, constraints, triggers, functions, indexes)
replay: 5 recorded model call(s)
       1  bundle SOURCE_REDACTED, render NOT_ATTEMPTED
       4  bundle VERIFIED, render VERIFIED
scratch database dropped
RESTORE VERIFIED
```

The replay counts are the same as `apollo turn replay --all` gave on the source before the
backup. The one `SOURCE_REDACTED` call is the turn that had used the forgotten memory. No scratch
database was left on the server.

**The real restore.** The "Restoring for real" commands were then run into a new database:
`pg_restore` exited 0, `apollo migrate` reported the schema up to date, `apollo provision`
recreated the grants, `apollo doctor` reported `least privilege: OK`, `apollo turn replay --all`
gave the same counts, `apollo show` printed the eight-line conversation, and a further `apollo say`
in it completed as `apollo_app`.

**The dump.** It was encrypted before it was written anywhere, so no plaintext dump existed. The
encrypted file, its key and both databases were destroyed when the test ended.

**Checked again the same day, reviewing this procedure.** Two ways a backup could have looked
good and not been one:

- *The dump fails.* With the database unreachable, the one-line form this page first gave
  (`apollo backup dump | age ... -o "$(apollo backup name age)" && apollo backup prune ...`, as a
  cron line) exited 0 and left a 200-byte file with a backup's name, which `prune` reported as
  kept. `apollo backup verify` refused that file (`pg_restore failed`). The page now gives
  `apollo-backup.sh`: run the same way, it exited 1 and left only a `.partial` file; run with the
  database up, it left one backup, which then ended `RESTORE VERIFIED`.
- *The dump is of the wrong database.* A dump taken with the owner connection pointed at an empty
  database restored cleanly, was migrated into an empty Apollo schema, and was reported
  `RESTORE VERIFIED`. `verify` now refuses an archive that records no applied migration, before
  it migrates anything. A test holds it.

**Not yet done:** the same on Janu's own deployment, with his key and his backup location. That is
his to run once the deployment exists. The automated tests
(`tests/integration/test_backup_restore.py`) perform this restore on every commit.
