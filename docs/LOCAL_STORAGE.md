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
APP_UID="${APP_UID:-1000}"
APP_GID="${APP_GID:-1000}"
install -d -m 0700 "$HOME/.local/state/short-form-studio-storage"
STATE_DIR="$(mktemp -d "$HOME/.local/state/short-form-studio-storage/$(date -u +%Y%m%dT%H%M%SZ).XXXXXX")"
printf '%s\n' "$STATE_DIR"
MANIFEST="$STATE_DIR/artifacts-before-migration.sha256"
ENV_FILE="$STATE_DIR/storage-receipt.json"
export SRC DEST MANIFEST ENV_FILE
export APP_UID APP_GID
export STATE_DIR
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

`APP_UID`/`APP_GID` default to the image IDs, `1000:1000`. Override them only to
match an explicitly configured application identity (tests use the invoking user's
IDs); these migration variables do not change the container user.
Run Copy as the configured identity with access to every source entry, or as an
administrator able to perform its ownership correction. Run Verify, Switch,
Inspect and Rollback as the project `.env` owner, not a different admin account.
That user must read/traverse the entire `0750` destination (for example via its
application group), the source and the private state directory.

Both `apps/api/Dockerfile` and `apps/worker-orchestrator/Dockerfile` explicitly
create group `appuser` with GID **1000** and user `appuser` with UID **1000**, then
use `USER appuser`. For ordinary rootful Docker without user-namespace remapping,
the destination and all copied media must be writable/traversable by `1000:1000`.
Have the host administrator provision only the new dedicated tree, for example
`install -d -m 0750 -o 1000 -g 1000 "$DEST"`. Parent directories must allow traversal.
Do not recursively change the old tree or use world-writable permissions.
For rootless Docker/userns-remap, first determine the mapped host IDs with the
administrator; literal host `1000:1000` is not sufficient in that topology.
Copy as UID/GID `1000:1000`, or have the administrator run the mandatory ownership
correction in Copy on the new dedicated tree only. Switch gates every entry's
ownership. This procedure targets rootful Docker without userns-remap; do not use
these literal-ID gates for other topologies without a separately reviewed procedure.

Docker creates a **missing** bind-mount source as a root-owned directory, with no
error, on whatever filesystem is there. If `/data` is not mounted when the containers
start, that is the root SSD, and the `1000:1000` containers cannot write to it. Confirm
the mount and the ownership of `$DEST` immediately before every container creation,
and never let Docker create it implicitly.

`ARTIFACTS_HOST_DIR` changes only the host bind source. API and Worker still see
`/app/data/artifacts`; keep `ARTIFACT_ROOT=./data/artifacts` (image working directory
`/app`) or `/app/data/artifacts`, never the host HDD path. Keep `STORAGE_BACKEND=local`.
Host paths must be absolute or start with `./` (resolved against the base Compose
file's directory); a bare `media/hdd` is treated as a named volume, not a bind path.
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
links outside the tree. **Paste or source the initial variable block into the
operator's Bash shell** so its exports persist. Keep its private state directory
outside the checkout and both media trees; it contains private artifact filenames.
The default is persistent and private (`0700`), not under volatile `/tmp`.
Do not delete it during acceptance. Save the echoed path securely. In a new shell,
do not rerun setup and create a fresh empty directory: re-export `STATE_DIR` to the
saved absolute directory, `MANIFEST="$STATE_DIR/artifacts-before-migration.sha256"`
and `ENV_FILE="$STATE_DIR/storage-receipt.json"`, plus the original `SRC`, `DEST`,
`APP_UID` and `APP_GID`. Ensure Copy's administrator and the `.env` owner can access
the same private state directory without making it public.
`ENV_FILE` is a storage-only rollback receipt, never the project `.env` or its alias.
`SRC` and `DEST` must be absolute paths without single quotes or
newlines, and not ending in a backslash, and the blocks refuse anything else. Spaces, `$` and `#` are fine: the override
line is written with the path single-quoted, which Compose reads literally, so it never
re-interpolates or truncates the path. Save each Copy/Verify/Switch/Inspect/Rollback
block as its own script and run it from the project directory
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
if [ "$(id -u):$(id -g)" != "$APP_UID:$APP_GID" ]; then
  chown -R "$APP_UID:$APP_GID" -- "$DEST"
fi
chmod 0750 -- "$DEST"
```

The trailing slashes copy the contents, preserving relative paths, hidden files
and empty directories. The copy must exit successfully; investigate any errors.
Run as the configured `APP_UID:APP_GID` with access to every source entry, or as the administrator who
can perform the mandatory `chown`. Archive mode copies source modes, including the
root directory's mode; the final `chmod` restores the dedicated root to `0750`.
Check traversal and write permissions throughout the tree before restarting writers.
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
find "$DEST" \( ! -uid "$APP_UID" -o ! -gid "$APP_GID" \) -print > "$inventory/wrong-owner"
test ! -s "$inventory/wrong-owner" || { echo "DEST must be owned by the configured APP_UID:APP_GID throughout" >&2; exit 1; }
python3 - <<'PY'
import hashlib, json, os, pathlib, re, stat, tempfile

dotenv = pathlib.Path.cwd() / ".env"
receipt = pathlib.Path(os.environ["ENV_FILE"])
if any(name in os.environ for name in ("ARTIFACTS_HOST_DIR", "COMPOSE_ENV_FILES", "COMPOSE_DISABLE_ENV_FILE")):
    raise SystemExit("Remove Compose process overrides before Switch")
if receipt.resolve() == dotenv.resolve():
    raise SystemExit("ENV_FILE must not alias project .env")
with os.fdopen(os.open(dotenv, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
        raise SystemExit(".env must be a regular, singly linked file owned by the operator")
    original = stream.read()
if re.search(rb"(?m)^[ \t]*(?:export[ \t]+)?ARTIFACTS_HOST_DIR[ \t]*(?:=|:|\r?$)", original):
    raise SystemExit(".env already defines ARTIFACTS_HOST_DIR; reconcile it before migration")
separator = b"" if not original or original.endswith(b"\n") else b"\n"
line = f"ARTIFACTS_HOST_DIR='{os.environ['DEST']}'\n".encode()
record = {"size": len(original), "sha256": hashlib.sha256(original).hexdigest(),
          "addition": (separator + line).decode(), "separator": separator.decode(), "line": line.decode()}
fd, temporary = tempfile.mkstemp(prefix=".env-storage-", dir=dotenv.parent)
try:
    with os.fdopen(fd, "wb") as stream:
        stream.write(original + separator + line)
        stream.flush()
        os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
        os.fchown(stream.fileno(), info.st_uid, info.st_gid)
        os.fsync(stream.fileno())
    receipt_fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(receipt_fd, "w", encoding="utf-8") as stream:
        json.dump(record, stream)
        stream.flush()
        os.fsync(stream.fileno())
    latest = dotenv.lstat()
    if (latest.st_dev, latest.st_ino, latest.st_mtime_ns, latest.st_size) != (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size):
        raise SystemExit(".env changed during Switch; reconcile manually")
    os.replace(temporary, dotenv)
    directory_fd = os.open(dotenv.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
```

This atomically appends exactly one storage line to the project `.env`, preserving
all existing bytes, ownership and mode, without printing or backing up secrets.
The private receipt records only the addition and the original size/hash; do not
share it, since the hash could help guess low-entropy secrets. A crash
before the rename may leave a receipt but no switch: inspect before retrying; never
overwrite a receipt blindly. Do not edit `.env` concurrently with these blocks.
After an interrupted Switch or Rollback, look for leftover `.env-storage-*` files
in the checkout and securely shred those plaintext secret copies before any build
or sharing. Git ignores them, but that is not secure erasure; on copy-on-write,
snapshot or SSD filesystems follow the host's secure-erasure policy as well.
Ensure no exported `ARTIFACTS_HOST_DIR`, `COMPOSE_ENV_FILES`, `COMPOSE_DISABLE_ENV_FILE`
or explicit `--env-file` bypasses this project configuration. Remove conflicting
shell/service-manager overrides before acceptance and future maintenance.
Plain `docker compose`, `make docker-up`, `make docker-local-up`/`docker-local-build`
resolve the persisted value without extra env-file flags.
`scripts/cutover_runtime_acceptance.sh` is a disposable CI acceptance harness that
overwrites `.env`; **never run it on a migrated or production checkout**.
Do not replace `.env` from
an example after migration: doing so loses this setting as well as credentials.

### Inspect

Print only effective bind sources and memory limits of every artifact consumer
(select your usual overlays via `COMPOSE_FILE`, or add their `-f` flags; Compose
prints a literal `$` as `$$`, which
the snippet undoes). **Never print or share the whole
`docker compose config` output:** it contains the secrets from `.env` on every Compose
version, even with `--no-env-resolution`, because interpolated fields carry them too.
The snippet filters standard output only. Compose can still write diagnostics to
standard error, for example a warning that names an unset variable referenced inside a
secret value, so do not share that either.

```bash
set -euo pipefail
docker compose config --format json |
  python3 -c '
import json, sys
services = json.load(sys.stdin)["services"]
for name, service in services.items():
    sources = [v["source"].replace("$$", "$") for v in service.get("volumes", []) if v.get("target") == "/app/data/artifacts"]
    if sources:
        memory = service.get("deploy", {}).get("resources", {}).get("limits", {}).get("memory")
        print(name, sources, f"{int(memory) // 2**20} MiB" if memory is not None else "unset")
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
python3 - <<'PY'
import hashlib, json, os, pathlib, re, stat, tempfile

dotenv = pathlib.Path.cwd() / ".env"
receipt = pathlib.Path(os.environ["ENV_FILE"])
if receipt.resolve() == dotenv.resolve():
    raise SystemExit("ENV_FILE must not alias project .env")
with os.fdopen(os.open(dotenv, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
        raise SystemExit(".env must be a regular, singly linked file owned by the operator")
    current = stream.read()
record = json.loads(receipt.read_text(encoding="utf-8"))
size, addition = record["size"], record["addition"].encode()
separator, line = record["separator"].encode(), record["line"].encode()
if separator not in (b"", b"\n") or addition != separator + line or not re.fullmatch(rb"ARTIFACTS_HOST_DIR='/[^'\r\n]*'\n", line) or line.endswith(b"\\'\n"):
    raise SystemExit("Receipt must describe exactly one literal ARTIFACTS_HOST_DIR line")
prefix, suffix = current[:size], current[size:]
if hashlib.sha256(prefix).hexdigest() != record["sha256"] or not suffix.startswith(addition):
    raise SystemExit(".env changed since Switch; reconcile manually without discarding secrets")
rest = suffix[len(addition):]
keep = separator if rest else b""
restored = prefix + keep + rest
fd, temporary = tempfile.mkstemp(prefix=".env-storage-", dir=dotenv.parent)
try:
    with os.fdopen(fd, "wb") as stream:
        stream.write(restored)
        stream.flush()
        os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
        os.fchown(stream.fileno(), info.st_uid, info.st_gid)
        os.fsync(stream.fileno())
    latest = dotenv.lstat()
    if (latest.st_dev, latest.st_ino, latest.st_mtime_ns, latest.st_size) != (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size):
        raise SystemExit(".env changed during Rollback; reconcile manually")
    os.replace(temporary, dotenv)
    directory_fd = os.open(dotenv.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
```

Rollback removes exactly the storage line recorded by Switch, keeping the added
separator if later settings follow; otherwise it restores the original bytes.
It refuses a changed original prefix and retains
later appended settings. The default `./data/artifacts` must be the original source
for this procedure; an already customized storage setting requires a separate plan.
If the receipt is lost, keep writers stopped and use the `.env` owner's private
editor to remove exactly the one `ARTIFACTS_HOST_DIR='...'` line added by Switch
(including only that line's trailing newline), preserving all credentials, other
settings and any separator before later settings. Do not regenerate `.env` from
an example or guess which of multiple storage lines to remove; reconcile manually
if the migrated line cannot be identified. Re-verify the old source against the
saved manifest; if it too is lost, recover trusted integrity evidence before use.
Inspect plain Compose config again, then have the operator recreate affected
containers and repeat authorized existing-asset checks before allowing writes.
No reverse-copy or deletion is needed during this pre-write acceptance window.
If writes already resumed on the HDD, stop all writers again and reconcile new
files and database references with the operator before rollback; the old copy
cannot contain new uploads. Never silently discard those assets. Keep both trees
and the manifest until explicit operator approval; this guide includes no deletion step.
