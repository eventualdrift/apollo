# Deployment

**This is a plan, not a record.** Nothing on this page has been run on your machines: the cloud
session Claude works in cannot reach them. The Apollo commands it names are the README's, and
they were tested there against PostgreSQL 16. Everything about your own hardware is untested
until you, or a session running on that hardware, has done it.

The technical gate does not need any of this. Its remaining steps run on the desktop against a
scratch database ([`acceptance.md`](acceptance.md), steps 0 to 8). This page is for what comes
after: the Apollo you will actually live in, for the product trial.

## The machines

As you described them on 2026-11-13:

| Machine | Role |
|---|---|
| Desktop, with the RTX 5070 Ti | The root server. It runs the model. |
| The Apollo VM (authorised) | Runs Apollo and its database. |
| MacBook | Where you work. It connects to the VM. |
| iPhone | Later. See "The phone" below. |

## Two things Claude needs to know

1. **What the desktop runs.** Windows or Linux, and whether the model setup from September is
   still there.
2. **Which machine holds the Apollo VM:** the desktop itself, or your Proxmox host.

   Claude recommends the Proxmox host. The database is everything personal that Apollo keeps.
   In a VM of its own it sits on a machine that does nothing else, on its own encrypted disk.
   On the desktop it would share a machine with everything else you do there. The desktop still
   sees each turn's context, because the model runs on it. It would not hold the stored whole.

## What must be true before anything personal goes in

Each line is a rule Apollo already depends on (spec §K). The deployment has to keep them.

1. **The disk under the database is encrypted** (§K.6): LUKS or the equivalent, on the VM's own
   disk. Apollo does not encrypt columns.
   - A hypervisor's snapshot or backup of the VM contains the database too. Send those only to
     encrypted storage, or leave the VM out of them and rely on `apollo backup`.
2. **PostgreSQL listens on this machine only.** The compose file binds `127.0.0.1`. Keep it so.
3. **Two database roles.** The owner migrates and provisions. Apollo runs as `apollo_app`, and
   the runtime environment has no `APOLLO_ADMIN_DSN` in it (README, "Two database roles").
4. **The two PostgreSQL log settings are in effect**, so that a refused row is never written to
   the server's log (README, "PostgreSQL's own log"). `apollo doctor` checks them.
5. **Secrets live in the environment only:** database passwords, and a model key if the server
   wants one. Never in `apollo.toml`, the repository, a shell history or a chat.
6. **Only Apollo can reach the model server**, and what it sends does not cross the network in
   the clear. Every request carries your identity files, the conversation and any memory that
   matched.
   - If the model is on a different machine from Apollo, keep the model server listening on its
     own loopback address, and reach it through an SSH tunnel from the VM:

     ```sh
     # On the Apollo VM. Its port 8080 becomes the desktop's own port 8080.
     ssh -N -L 127.0.0.1:8080:127.0.0.1:8080 YOU@DESKTOP
     ```

     `apollo.toml` then keeps `base_url = "http://127.0.0.1:8080/v1"`, and the model's port is
     never open to the rest of your network.
   - **Check what the model server writes to its own log.** Some servers log each prompt. A
     prompt holds your memories, and Apollo's forgetting cannot reach another program's log.
7. **Backups are encrypted, tested and expire** ([`backup-restore.md`](backup-restore.md)). The
   backup key is kept off the VM.
8. **`apollo doctor` ends `least privilege: OK`**, with `log settings: OK` above it.

## Using it from the MacBook

```sh
ssh -t YOU@APOLLO-VM apollo chat CONVERSATION_ID
```

The command line runs on the VM, and the MacBook is only its terminal. Nothing of Apollo is
installed on the MacBook, and nothing new listens on your network: SSH is the only way in.
`apollo new` starts a conversation and prints its id.

## The phone

Not built, and it is a decision of its own.

- **For now:** an SSH client on the phone reaches the same command line, on your home network.
  It needs no new code.
- **Properly:** the small web service the spec draws (§A.1), with its access token and
  encrypted transport, and after it a web interface or an app. The spec defers this until a
  second client exists (§P.3, "Device auth, remote access"). The phone is that client, so this
  is the first decision of the next phase.

## Order of work

1. **The technical gate**, on the desktop: [`acceptance.md`](acceptance.md), steps 0 to 8. No VM
   is needed for it.
2. **The VM:** create it, encrypt its disk, install PostgreSQL 16 and Apollo, provision the
   roles, set the two log settings, and run `apollo doctor`.
3. **The model link:** the tunnel above, then `apollo gate1 brain.local` from the VM.
4. **Backups:** take one, and test a restore ([`acceptance.md`](acceptance.md), step 7).
5. **The product trial** (step 9).
