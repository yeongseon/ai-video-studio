# Local Storage

## Scope and host checks

Use an explicit HDD directory for growing durable input/output media when the
checkout is on a space-constrained root SSD. This is not a capacity guarantee:
the target inventory (Ubuntu 22.04, 6 cores, 32 GiB RAM; SSD about 217 GiB/73%
used, HDD `/data` about 916 GiB/80% used) is only a snapshot. Re-measure on the
actual host and allow room for both copies, future media and filesystem overhead.
An HDD can be slower; measure render throughput before committing to it.

This procedure was **not run on a real host**. Automated evidence is limited to
Compose config inspection and a temporary fixture copy, corruption detection and
rollback. No live assets, containers, mounts or host permissions were changed.
Free-space admission guardrails, bounded render scratch and metrics are a later
slice; changing the artifact mount does not bound `/tmp` or container overlay use.
Existing retention and workspace budgets are unchanged. This is not a backup.

Choose an absolute destination on the intended mounted filesystem, not a path
copied blindly from this example. Start in the repository root, using Bash:

```bash
SRC="$(pwd -P)/data/artifacts"
DEST="/data/ai-video-studio/artifacts"
MANIFEST="$(pwd -P)/artifacts-before-migration.sha256"
ENV_FILE="$(pwd -P)/.env.storage"
export SRC DEST MANIFEST ENV_FILE
df -h "$SRC" /data
findmnt --target /data --output TARGET,SOURCE,FSTYPE,OPTIONS
stat -c '%n uid=%u gid=%g mode=%a' "$SRC" /data
```

Confirm `/data` really is the HDD mount, has sufficient free space and is writable;
if absent, abort rather than accidentally creating a directory on the SSD below
an unmounted mountpoint. Arrange persistent host mounting before container startup.
After provisioning the destination, repeat:

```bash
df -h "$SRC" "$DEST"
findmnt --target "$DEST" --output TARGET,SOURCE,FSTYPE,OPTIONS
stat -c '%n uid=%u gid=%g mode=%a' "$SRC" "$DEST"
```

## Ownership and configuration

Both `apps/api/Dockerfile` and `apps/worker-orchestrator/Dockerfile` explicitly
create group `appuser` with GID **1000** and user `appuser` with UID **1000**, then
use `USER appuser`. For ordinary rootful Docker without user-namespace remapping,
the destination and all copied media must be writable/traversable by `1000:1000`.
Have the host administrator provision only the new dedicated tree, for example
`install -d -m 0750 -o 1000 -g 1000 "$DEST"`. Parent directories must allow traversal.
Do not recursively change the old tree or use world-writable permissions.
For rootless Docker/userns-remap, first determine the mapped host IDs with the
administrator; literal host `1000:1000` is not sufficient in that topology.
After copying, the administrator may assign `chown -R 1000:1000 "$DEST"` to the
new dedicated tree only, if needed. Recheck permissions before restarting writers.

Docker creates a **missing** bind-mount source as a root-owned directory, with no
error, on whatever filesystem is there. If `/data` is not mounted when the containers
start, that is the root SSD, and the `1000:1000` containers cannot write to it. Confirm
the mount and the ownership of `$DEST` immediately before every container creation,
and never let Docker create it implicitly.

`ARTIFACTS_HOST_DIR` changes only the host bind source. API and Worker still see
`/app/data/artifacts`; keep `ARTIFACT_ROOT=./data/artifacts` (image working directory
`/app`) or `/app/data/artifacts`, never the host HDD path. Keep `STORAGE_BACKEND=local`.
Relative host paths are resolved against the base Compose file's directory.
Default: `ARTIFACTS_HOST_DIR=./data/artifacts`. Dev, E2E and local-server overlays
inherit this bind; the scaled workers use the same host variable. The E2E runner's
separate `artifact_data` volume and Worker `dlq_data` volume are not relocated.

Base API and Worker memory limits are independently controlled in MiB:
`API_MAX_MEMORY_MB` defaults to 1024 and `WORKER_MAX_MEMORY_MB` defaults to 4096.
For each, precedence is nonempty service-specific variable, then nonempty legacy
`MAX_MEMORY_MB`, then that service's default. Compose passes the effective value
as each container's `MAX_MEMORY_MB`, matching its cgroup limit. Choose positive
integer values and leave RAM for the OS, database and other services. Applications
and hosted ACA still consume `MAX_MEMORY_MB` directly; no application contract
changes. Scaled stage-worker memory policy is unchanged by this slice.

## Non-destructive migration

**Stop all writers first**, through the operator's approved maintenance workflow:
block uploads/new jobs, drain or cancel active work safely, stop API, every Worker
(including scaled workers), Beat/retention tasks and any external process writing
these files. Keep them stopped through verification and the read-only smoke check.
Do not touch unrelated containers or delete/recreate database/DLQ volumes.
Coordinate database metadata backup as described in [Backup and Restore](BACKUP_RESTORE.md).

Use **copy, never move**: no `mv`, no rsync `--remove-source-files`, no `--delete`.
Use a new empty destination, disjoint from the source; do not merge unrelated data.
The commands below assume a nonempty regular-file media tree (empty directories
are supported). Symlinks and special files require separate review, not following
links outside the tree. Set the four absolute variables above; `MANIFEST` must be
outside both trees. `SRC` and `DEST` must be absolute paths without single quotes or
newlines, and not ending in a backslash, and the blocks refuse anything else. Spaces, `$` and `#` are fine: the override
file is written with the path single-quoted, which Compose reads literally, so it never
re-interpolates or truncates the path. Save each block as its own script and run it
with `bash -euo pipefail script.sh`; never paste one into a login shell, where its
`exit` and `set -e` would end your session. **Verify before switching.**
Keep the manifest and the old directory untouched until the operator explicitly approves deletion.

### Copy

```bash
set -euo pipefail
case "$SRC" in /*) ;; *) echo "SRC must be an absolute path" >&2; exit 1 ;; esac
case "$DEST" in /*) ;; *) echo "DEST must be an absolute path" >&2; exit 1 ;; esac
case "$SRC" in *"'"* | *$'\n'* | *\\) echo "SRC must not contain quotes or newlines or end with a backslash" >&2; exit 1 ;; esac
case "$DEST" in *"'"* | *$'\n'* | *\\) echo "DEST must not contain quotes or newlines or end with a backslash" >&2; exit 1 ;; esac
test -d "$SRC"
test -d "$DEST"
test ! -L "$SRC" || { echo "SRC must not be a symlink" >&2; exit 1; }
test ! -L "$DEST" || { echo "DEST must not be a symlink" >&2; exit 1; }
test "$(realpath "$SRC")" != "$(realpath "$DEST")"
case "$(realpath "$DEST")/" in "$(realpath "$SRC")/"*) exit 1 ;; esac
case "$(realpath "$SRC")/" in "$(realpath "$DEST")/"*) exit 1 ;; esac
test -z "$(find "$DEST" -mindepth 1 -print -quit)"
test -z "$(find "$SRC" ! -type d ! -type f -print -quit)"
test ! -e "$MANIFEST"
(cd "$SRC" && find . -type f -exec sha256sum -- {} +) > "$MANIFEST"
test -s "$MANIFEST"
rsync --archive --no-owner --no-group -- "$SRC/" "$DEST/"
```

The trailing slashes copy the contents, preserving relative paths, hidden files
and empty directories. The copy must exit successfully; investigate any errors.
Run as an account able to read every source file and write the new tree; ownership
is intentionally not copied. Provision/check destination ownership as above.
If the block fails, do not switch; investigate using a new reviewed empty
destination and a new manifest path rather than deleting the old directory.

### Verify

```bash
set -euo pipefail
(cd "$SRC" && sha256sum --check --strict "$MANIFEST")
(cd "$DEST" && sha256sum --check --strict "$MANIFEST")
inventory="$(mktemp -d)"
trap 'rm -rf "$inventory"' EXIT
(cd "$SRC" && find . -type f -print0 | LC_ALL=C sort -z) > "$inventory/source"
(cd "$DEST" && find . -type f -print0 | LC_ALL=C sort -z) > "$inventory/destination"
cmp "$inventory/source" "$inventory/destination"
```

Both checks must exit zero with every file `OK`, and the final `cmp` must print
nothing. The manifest proves the content of the files it lists. The inventory
comparison proves only that both trees hold the same set of regular-file names: it
catches a file added to the source after the manifest was built (for example by a
writer that was not really stopped) and an extra file in the destination. It does
not verify file modes, modification times, hardlink relationships, sparse allocation
or empty directories, and it cannot show that a file present on both sides under the
same new name has the same content. A failure to read either tree, such as a
permission error, stops the block. The source must still be quiescent; a checksum is
not a snapshot of concurrent writes, and none of this is safe against someone
swapping a directory while the commands run. Do not ignore a nonzero exit or
regenerate the manifest from the destination to hide a mismatch.

### Switch

```bash
set -euo pipefail
case "$SRC" in /*) ;; *) echo "SRC must be an absolute path" >&2; exit 1 ;; esac
case "$DEST" in /*) ;; *) echo "DEST must be an absolute path" >&2; exit 1 ;; esac
case "$SRC" in *"'"* | *$'\n'* | *\\) echo "SRC must not contain quotes or newlines or end with a backslash" >&2; exit 1 ;; esac
case "$DEST" in *"'"* | *$'\n'* | *\\) echo "DEST must not contain quotes or newlines or end with a backslash" >&2; exit 1 ;; esac
(cd "$SRC" && sha256sum --check --strict "$MANIFEST")
(cd "$DEST" && sha256sum --check --strict "$MANIFEST")
inventory="$(mktemp -d)"
trap 'rm -rf "$inventory"' EXIT
(cd "$SRC" && find . -type f -print0 | LC_ALL=C sort -z) > "$inventory/source"
(cd "$DEST" && find . -type f -print0 | LC_ALL=C sort -z) > "$inventory/destination"
cmp "$inventory/source" "$inventory/destination"
printf "ARTIFACTS_HOST_DIR='%s'\n" "$DEST" > "$ENV_FILE"
```

This writes only a dedicated storage override file, not your secrets-bearing
`.env`. Ensure neither a shell-exported `ARTIFACTS_HOST_DIR` nor a later env-file
overrides it. Pass both files, in this order, to every later Compose command,
including the approved recreation and any maintenance operation: a plain
`docker compose up` without `--env-file .env --env-file "$ENV_FILE"` falls back to
`./data/artifacts` and would recreate containers on the old tree.

### Inspect

Print only the effective bind sources and memory limits of the API and Worker (add the
overlay files you normally pass with `-f`; Compose prints a literal `$` as `$$`, which
the snippet undoes). **Never print or share the whole
`docker compose config` output:** it contains the secrets from `.env` on every Compose
version, even with `--no-env-resolution`, because interpolated fields carry them too.
The snippet filters standard output only. Compose can still write diagnostics to
standard error, for example a warning that names an unset variable referenced inside a
secret value, so do not share that either.

```bash
set -euo pipefail
docker compose --env-file .env --env-file "$ENV_FILE" -f docker-compose.yml config --format json |
  python3 -c '
import json, sys
services = json.load(sys.stdin)["services"]
for name in ("api", "worker"):
    service = services[name]
    sources = [v["source"].replace("$$", "$") for v in service.get("volumes", []) if v.get("target") == "/app/data/artifacts"]
    print(name, sources, int(service["deploy"]["resources"]["limits"]["memory"]) // 2**20, "MiB")
'
```

Review the bind sources and memory values this prints. Only then have the operator
recreate the affected API/Worker containers via the approved workflow, initially
keeping writes blocked. Merely restarting an existing container does not change its
bind. Do not change database rows, `storage_key` values, or relative artifact paths:
the unchanged `storage_key` identity resolves to the copied bytes under the same
container root.

With an existing authorized workspace session, preview/download known existing
inputs and a published MP4 through the usual API/Studio routes; compare the bytes
with the manifest. Check that unauthorized/cross-workspace requests still return
404, not raw filesystem access. This live-host check is required but **not verified**
by the temporary-fixture tests. Resume writers only after operator acceptance.

### Rollback

```bash
set -euo pipefail
case "$SRC" in *"'"* | *$'\n'* | *\\) echo "SRC must not contain quotes or newlines or end with a backslash" >&2; exit 1 ;; esac
case "$SRC" in /*) ;; *) echo "SRC must be an absolute path" >&2; exit 1 ;; esac
(cd "$SRC" && sha256sum --check --strict "$MANIFEST")
printf "ARTIFACTS_HOST_DIR='%s'\n" "$SRC" > "$ENV_FILE"
```

Rollback = point the variable back to the original directory, inspect config
again with the same env-file order, then have the operator recreate affected
containers and repeat authorized existing-asset checks before allowing writes.
No reverse-copy or deletion is needed during this pre-write acceptance window.
If writes already resumed on the HDD, stop all writers again and reconcile new
files and database references with the operator before rollback; the old copy
cannot contain new uploads. Never silently discard those assets. Keep both trees
and the manifest until explicit operator approval; this guide includes no deletion step.
