# Pinned runtime images

The local and CI stack runs PostgreSQL and Redis from exact-patch images pinned by digest.
`docker-compose.yml` is the only place a version is chosen. Every other reference (the CI
service containers and `docker run` lines, the restore and release-lifecycle scripts, test
fixtures) must be byte-identical to it, and
`apps/api/tests/test_runtime_image_pins.py` fails if one drifts or if a moving tag such as
`postgres:16-alpine` or `latest` comes back.

Python, Nginx, FFmpeg and Node pins are separate work: #1002, #1003 and #996. Proving that an
existing data volume survives a pin change is #1021.

## Pins at the time of this change (2026-11-12)

| Service | Version | Base image | Multi-arch index digest | Supported until |
|---|---|---|---|---|
| PostgreSQL 16 | 16.15 | Alpine 3.24 | `sha256:aa6cb33cf4c1...` | 2028-11-09 |
| Redis OSS 7.4 (Extended) | 7.4.12 | Alpine 3.22 | `sha256:7f7d30b7fdd0...` | 2029-12-01 |

The `image:` lines of `docker-compose.yml` are authoritative; this table is a dated snapshot and
no test reads it. The digest is the multi-arch **index** digest (amd64, arm64 and six more
platforms), so it resolves to the right platform image on every supported machine. Docker selects
the image by that digest and treats the tag as a label, so the tag and the digest must always be
changed together.

Support dates were read on 2026-11-12 from the
[PostgreSQL versioning policy](https://www.postgresql.org/support/versioning/) (final release of
16: November 9, 2028) and the
[Redis Open Source version management](https://redis.io/docs/latest/operate/oss_and_stack/install/version-mgmt/)
page (7.4 Extended, EOL December 1, 2029). PostgreSQL publishes minor releases every quarter (the
second Thursday of February, May, August and November), so review the pin at each of them.

## Updating a pin

1. Pick the newest patch of the same major line (never a new major here). Read the release
   notes of **every** release since the current pin: PostgreSQL
   `https://www.postgresql.org/docs/release/16.<minor>/` (the "Migration to Version" paragraph
   lists actions such as reindexing) and the Redis releases at
   `https://github.com/redis/redis/releases`.
2. Resolve the digest from the registry rather than from memory or a mirror, and run the same
   lookup again on the tag of an existing pin to confirm that the tag still resolves to its digest:

   ```bash
   repo=postgres; tag=16.15-alpine
   token="$(curl -fsS "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/$repo:pull" \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"
   curl -fsSI -H "Authorization: Bearer $token" \
     -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json' \
     "https://registry-1.docker.io/v2/library/$repo/manifests/$tag" \
     | tr -d '\r' | awk -F': ' 'tolower($1)=="docker-content-digest"{print $2}'
   ```

3. Change the reference in `docker-compose.yml`, then every other place the test reports
   (it prints `file:line` for each drifting reference), then the snapshot table at the top of
   this page.
4. Open a pull request. CI exercises the new images for real: the `restore-drill` job takes a
   backup and restores it into a fresh container from the pinned PostgreSQL image, the `docker`
   job starts both through Compose and restarts Redis, and the `first-short-runtime` and
   `worker-topology-runtime` jobs run the pinned PostgreSQL and Redis.

Changing a real host is a separate, operator-approved step. Before the first start on a new
patch, take the backup described in [BACKUP_RESTORE.md](BACKUP_RESTORE.md) and keep it until
the new patch has run cleanly.

## PostgreSQL minor upgrades and existing volumes

A minor upgrade replaces the binaries and keeps the data directory: PostgreSQL states that
minor releases never change the internal storage format and that the executables are simply
replaced while the server is down ([upgrading](https://www.postgresql.org/docs/16/upgrading.html)),
and the release notes of 16.7 to 16.15 each say that no dump and restore is required. **Never
change the major** (16 to 17) by editing the tag on an existing volume; that needs `pg_upgrade`
or a dump and restore.

Those release notes name these follow-up actions: reindexing BRIN bloom and
`numeric_minmax_multi_ops` indexes, recreating self-referential foreign keys on partitioned
tables, reindexing `contrib/btree_gist` and `contrib/ltree` indexes, allowing non-default
logical decoding output plugins, affected `pgcrypto` PGP data, and a `psql` change that stops
reading in-line data after a failed `COPY ... FROM STDIN`. A scan of `apps/api/migrations`, the
application sources and the scripts found none of those features at the time of this change (no
extensions, partitioned tables, BRIN, GiST, GIN or ltree indexes, logical replication or
`COPY ... FROM STDIN`; the `psql` calls pass plain SQL with `-c`). Re-check this against the release
notes whenever the schema or the pin changes.

The base operating system moves from Alpine 3.21 to 3.24 (musl libc in both). A libc change under
an existing volume is the usual way text indexes go stale, and this change did not verify how the
two musl releases order text; the `amcheck` pass of #1021 is the guard for it.

## Redis

Redis 7.4.10 to 7.4.12 are security releases: stream `RESTORE`, RDB loading, ACL key permission,
TLS and blocked-client fixes, and a cluster-bus fix in 7.4.12 that does not apply to this
single-instance, non-cluster stack. The stack runs with `--appendonly yes`. Moving from the 7.4
line to another is a deliberate change with its own review, not a tag edit.

## Rollback

- **Configuration**: `git revert` the commit that changed the pin; the test keeps all
  references consistent again. The previous PostgreSQL reference was
  `postgres:16.6-alpine` (index `sha256:1d04b9ba1d4996401f2552b51beda8187f175c0645c091e4781134fc9c9a3eef`).
  Redis was the floating `redis:7.4-alpine`, which already resolved to the pinned 7.4.12 image on
  2026-11-12; the previous exact patch is
  `redis:7.4.11-alpine@sha256:d9436322172a5b482c187bf1f363d3c4fea086fcca6b9c8a94de8f0f39ed3193`.
- **Data**: PostgreSQL documents minor *upgrades*, not downgrades. Once a newer binary has
  opened a data directory, do not assume an older image can. The rollback point for data is the
  backup taken before the first start on the new patch. The same applies to the Redis append-only
  and snapshot files.

## Verification and limits

No container was started to produce this change. The digests were read from the Docker registry
API: each pinned tag resolves to its digest and each index lists eight platforms. `docker compose
config` resolves both pins under Compose 2.24.4, 2.38.2 and 5.0.2.

CI starts the pinned images from a fresh volume, or restarts the same image: the `docker` job
(Compose start, Alembic upgrade, Redis restart, force-recreate with the volume retained and
fresh-volume replacement), `restore-drill`, `first-short-runtime`, `worker-topology-runtime`
(including a stop and start of its Redis container) and the `test` service container. Not covered
here, and tracked in #1021: a volume written by the previous patch opened by the pinned one,
agreement between each tag and the version the running server reports, index integrity after the
Alpine change, and refusal of a major bump on an existing volume. The arm64 manifests are in the
pinned indexes but were not run.

The drift guard is textual. It covers tracked YAML, Python, shell, TOML, Makefile and Dockerfile
files. It does not read prose documentation, such as this page, and it cannot see a reference that
is assembled at run time or written without a tag.
