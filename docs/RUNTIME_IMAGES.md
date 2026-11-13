# Pinned runtime images

The local and CI stack runs PostgreSQL and Redis from exact-patch images pinned by digest.
`docker-compose.yml` is the only place a version is chosen. Every other reference (the CI
service containers and `docker run` lines, the restore and release-lifecycle scripts, test
fixtures) must be byte-identical to it, and
`apps/api/tests/test_runtime_image_pins.py` fails if one drifts or if a moving tag such as
`postgres:16-alpine`, `redis:alpine` or `latest` comes back.

Python, Nginx, FFmpeg and Node pins are separate work: #1002, #1003 and #996. Proving that an
existing data volume survives a pin change is #1021.

## Pins at the time of this change (2026-11-13)

| Service | Version | Base image | Multi-arch index digest | Supported until |
|---|---|---|---|---|
| PostgreSQL 16 | 16.16 | Alpine (base version not re-verified) | `sha256:0adbf25a9e96...` | 2028-11-09 |
| Redis OSS 7.4 (Extended) | 7.4.12 | Alpine (3.22 reported in #1022; not re-verified here) | `sha256:7f7d30b7fdd0...` | 2029-12-01 |

The `image:` lines of `docker-compose.yml` are authoritative; this table is a dated snapshot and
no test reads it. The digest is the multi-arch **index** digest, not a platform image digest.
Docker selects the image by that digest and treats the tag as a label, so the tag and the digest must always be
changed together.

Verified for this update on **2026-11-13 (UTC)** using Python `urllib.request.urlopen` (HTTPS GET)
against Docker Hub's public tags API:

- [`library/postgres/tags/16.16-alpine`](https://hub.docker.com/v2/repositories/library/postgres/tags/16.16-alpine):
  top-level `digest` is
  `sha256:0adbf25a9e96d38823bb813f3ac0ed41fadb665c88f1df7fe6dbfb060cbdc106`;
  `last_updated` is `2026-11-12T22:13:55.084706Z`. The `images` list contains seven
  Linux platforms: `386`, `amd64`, `arm/v6`, `arm/v7`, `arm64/v8`, `ppc64le`, `s390x`.
  Entries with `os=unknown` / `architecture=unknown` are not counted as runnable platforms;
  this PostgreSQL index does **not** list `riscv64`.
- [`library/redis/tags/7.4.12-alpine`](https://hub.docker.com/v2/repositories/library/redis/tags/7.4.12-alpine):
  top-level `digest` remains
  `sha256:7f7d30b7fdd09467bac58198a1f166dd69789d348d4fe859cccb20e0cf287fbe`.
  Its eight Linux platforms are the seven above plus `riscv64`. Paginated GETs of
  `https://hub.docker.com/v2/repositories/library/redis/tags?page_size=100&name=7.4.`
  (following every `next` URL) and numeric sorting of exact `7.4.<patch>-alpine` names
  confirmed **7.4.12 is still the newest**. Redis is unchanged.

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
   repo=postgres; tag=16.16-alpine
   token="$(curl -fsS "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/$repo:pull" \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"
   curl -fsSI -H "Authorization: Bearer $token" \
     -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json' \
     "https://registry-1.docker.io/v2/library/$repo/manifests/$tag" \
     | tr -d '\r' | awk -F': ' 'tolower($1)=="docker-content-digest"{print $2}'
   ```

3. Change the reference in `docker-compose.yml`, then every other place the test reports
   (it prints `file:line` for each drifting reference), then the snapshot table at the top of
   this page. Raise `MIN_POSTGRES_PATCH` / `MIN_REDIS_PATCH` in the guard when a security
   release ships; never lower a floor. The offline floor catches a consistently stale pin,
   while the drift check catches consumers missed during an update. Update matcher examples
   without changing the malformed-reference behavior they test.
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
and the release notes of 16.7 to 16.16 each say that no dump and restore is required. **Never
change the major** (16 to 17) by editing the tag on an existing volume; that needs `pg_upgrade`
or a dump and restore.

On 2026-11-13 (UTC), the migration sections of **every 16.7–16.15 release** were re-read,
plus the changelog entries naming these actions (not a fresh full-changelog/security audit of
all nine releases). They name these follow-up actions: reindexing BRIN bloom (16.9) and
`numeric_minmax_multi_ops` indexes (16.10), recreating self-referential foreign keys on partitioned
tables (16.9), reindexing `contrib/btree_gist` float indexes that may contain NaN and
`contrib/ltree` btree indexes with very long paths (16.15), allowing non-default
logical decoding output plugins, affected `pgcrypto` PGP data, and a `psql` change that stops
reading in-line data after a failed `COPY ... FROM STDIN`. A scan of `apps/api/migrations`, the
application sources and the scripts found none of those features at baseline `ffeb6c3` (no
extensions, partitioned tables, BRIN, GiST, GIN or ltree indexes, logical replication or
`COPY ... FROM STDIN`; the `psql` calls pass plain SQL with `-c`). Re-check this against the release
notes whenever the schema or the pin changes.

The earlier #1022 review reported an Alpine 3.21 to 3.24 base change for its 16.6 to 16.15
update. This 16.16 review did **not** inspect the image's Alpine version, libc or ICU versions,
or test collation ordering; do not infer collation compatibility from the `alpine` tag.
#1021 tracks PostgreSQL's `amcheck` over the indexes of a seeded volume after an in-place
upgrade. That can reveal ordering or integrity problems in those indexes, but
`amcheck` can show corruption and never its absence, so it does not certify an arbitrary existing
volume. The backup taken before the first start remains the safeguard.

### 16.15 -> 16.16 (reviewed 2026-11-13)

Read [16.16 E.1.1 and the changelog](https://www.postgresql.org/docs/release/16.16/)
directly through Python urllib GET and an HTML-to-text pass. Released **2026-11-12**,
this fixes numerous security issues, including **CVE-2026-6460**. E.1.1 says no
dump/restore is required for 16.x, but explicitly asks operators to examine the `REINDEX`
entries and rebuild affected indexes. For hosts still on the pre-#1022 16.6 pin, apply
the 16.7–16.15 review above too. Releases before 16.7 were not re-read here.

| Release-note action | Applicability and operator action |
|---|---|
| Btree de-duplication on unbounded `bpchar` | The fix disables de-duplication for **new entries only**. Reindex existing btree indexes on unbounded `bpchar` after updating. Length-constrained `CHAR(n)` is not at risk according to the notes; inspect actual catalog typmods rather than assuming a source spelling implies an unbounded type. |
| GIN VACUUM and posting-tree deletion | Multiple index scans within one VACUUM could miss dead tuples; a race with leaf splits had the same effect. Deleting a posting-tree page after an incomplete split could disconnect siblings. `REINDEX` is recommended if corruption is suspected. |
| TOAST indexes after `REINDEX CONCURRENTLY` | Stale mutable `pg_index` fields could miss index updates and produce “missing chunk number” errors. The notes say the TOAST heap is intact for **this bug** and a `REINDEX` suffices; other causes of that error are not ruled out. |
| `contrib/pg_trgm` GiST (`gist_trgm_ops`) | The fix does not repair existing indexes; rebuild these after updating. None is declared by this application. |
| Composite-type defenses | Keep the new checks enabled. `composite_type_mismatch` can disable them **at a security risk**. Full protection requires rebuilding C-language extensions (some need source changes). The repository uses the stock Alpine image with no custom C extensions installed/configured by this stack; locally installed extensions must be inventoried and rebuilt by their operator. |

**Schema conclusion (read-only source review at `ffeb6c3`; migrations unchanged here):**
no btree index on unbounded `bpchar`, and no GIN, GiST (including trigram) or BRIN index
is declared. This is not a live-host catalog audit. Evidence:

- Reviewed column types, index calls and raw SQL across all 49 migration files under
  `apps/api/migrations/versions/` (001–052, with numbering gaps), including
  `postgresql_using`, `postgresql_where`, `sa.CHAR`, JSONB, tsvector, trigram and array
  spellings. Character fields use `sa.String(length=...)` / `sa.String(n)` (VARCHAR),
  not `CHAR`/`bpchar`; unbounded fields use `sa.Text()`. For example 004 defines
  `scene_id` as `String(length=100)` and its raw partial unique index on `(run_id, scene_id)`;
  005 defines scalar/default-btree indexes, including descending timestamp keys.
- Partial indexes in 022 and 043 use `postgresql_where` but no alternative access method;
  raw indexes in 026 and 029 likewise omit `USING` (default btree). A partial predicate
  does not make an index GIN. Primary/unique keys also use default btree.
- 034 `creator_media_assets.metadata` and 052 `creator_project_settings.settings` are
  JSONB, **without** a JSONB index; 034's indexes cover scalar workspace/project/type
  and timestamp/id columns, while 052 has only its project primary key. 033's Timeline
  `segments_json` is Text, not a GIN-indexed JSONB column. Application storage uses SQL,
  not a second ORM schema: `packages/creator-service/creator_service/db.py`,
  `postgres_media_asset_storage.py` and `postgres_timeline_storage.py` agree with these types.
  Repository SQL/source searches found no hidden `CREATE INDEX ... USING gin`,
  `postgresql_using`, `CHAR`, tsvector/trigram/array index or extension installation.
- **TOAST exposure does apply:** project source Text in 001, run metadata Text in 002,
  artifact Text in 005, Timeline Text in 033, media JSONB in 034 and settings JSONB in 052
  are TOAST-able. No application `REINDEX` operation was found, but external maintenance
  history is unknown. Do not equate absence of GIN/bpchar with absence of TOAST risk.

For a real host, take a verified pre-upgrade backup, stop PostgreSQL and recreate just
its container from the new pin while retaining the **same data directory** (same major 16;
no dump/restore required by the release notes). Do not remove the volume. This procedure
is guidance, not an existing-volume test; #1021 remains open.

Run these **read-only** catalog queries in each application database as an operator
before deciding whether its locally added indexes need work. The first finds unbounded
bpchar btree **index keys** (also expression results); constrained `CHAR(n)` keys and
INCLUDE-only columns are excluded. Review definitions for domains/casts/custom operator
classes as well; the queries do not prove an index is healthy.

```sql
SELECT DISTINCT i.indexrelid::regclass AS index_name,
       i.indrelid::regclass AS table_name, pg_get_indexdef(i.indexrelid) AS definition
FROM pg_index AS i
JOIN pg_class AS c ON c.oid = i.indexrelid
JOIN pg_am AS am ON am.oid = c.relam
JOIN pg_attribute AS a ON a.attrelid = i.indexrelid
WHERE am.amname = 'btree' AND a.attnum BETWEEN 1 AND i.indnkeyatts
  AND a.atttypid = 'pg_catalog.bpchar'::regtype AND a.atttypmod = -1
ORDER BY 1;

SELECT i.indexrelid::regclass AS index_name, i.indrelid::regclass AS table_name,
       pg_get_indexdef(i.indexrelid) AS definition
FROM pg_index AS i
JOIN pg_class AS c ON c.oid = i.indexrelid
JOIN pg_am AS am ON am.oid = c.relam
WHERE am.amname = 'gin'
ORDER BY 1;
```

After installing 16.16, an authorized operator can rebuild each affected bpchar or
suspect GIN index (replace the quoted names with the catalog result):

```sql
REINDEX INDEX CONCURRENTLY "public"."affected_index";
```

For suspected TOAST-index corruption from the reported bug, identify the affected
table/index first, then use `REINDEX INDEX CONCURRENTLY "pg_toast"."pg_toast_<oid>_index";`
or `REINDEX TABLE CONCURRENTLY "public"."affected_table";` (the latter includes its TOAST
index). These are **mutating maintenance commands**, not part of the read-only inspection;
run outside a transaction block with sufficient privileges/disk space and a backup.
For `gist_trgm_ops` rebuilds, note that exclusion-constraint indexes cannot be rebuilt
concurrently: schedule a blocking `REINDEX INDEX` maintenance window where necessary.
See [REINDEX](https://www.postgresql.org/docs/16/sql-reindex.html). No catalog query or
reindex command was executed against a database in this review.

## Redis

Redis 7.4.10 to 7.4.12 are security releases: stream `RESTORE`, RDB loading, ACL key permission,
TLS and blocked-client fixes, and a cluster-bus fix in 7.4.12 that does not apply to this
single-instance, non-cluster stack. The stack runs with `--appendonly yes`. Moving from the 7.4
line to another is a deliberate change with its own review, not a tag edit.

## Rollback

- **Configuration (historical #1022)**: reverting #1022 restores the previous configuration exactly as it was,
  including its floating tags and the mismatches #1022 removed, and it removes the guard.
  Historically, a specific earlier image could be set in `docker-compose.yml` and every
  other place the guard reports. The security floor below now deliberately rejects these
  old patches; these references are recovery history, not a routine update procedure. The previous
  PostgreSQL reference was
  `postgres:16.6-alpine@sha256:1d04b9ba1d4996401f2552b51beda8187f175c0645c091e4781134fc9c9a3eef`.
  Redis was the floating `redis:7.4-alpine`, which already resolved to the pinned 7.4.12 image on
  2026-11-12; the previous exact patch is
  `redis:7.4.11-alpine@sha256:d9436322172a5b482c187bf1f363d3c4fea086fcca6b9c8a94de8f0f39ed3193`.
- **16.16 update**: its immediate previous PostgreSQL pin is
  `postgres:16.15-alpine@sha256:aa6cb33cf4c19c765cefcf48d96176258800870a9a1e930ad126200f9d825139`.
  The new security floor deliberately refuses that downgrade, even if all consumers agree.
  An emergency rollback is an explicit reviewed exception/revert, not a reason to lower the
  floor. Retain the drift guard and Redis pin; reverting this update does not remove #1022.
- **Data**: PostgreSQL states that a data directory is compatible with earlier and later minor
  releases of the same major version, so in principle an older minor can open it. Do not rely on
  that here: some fixes ask for a reindex or data cleanup that a downgrade does not undo, the base
  operating system compatibility is unverified, and no test here opened a volume with the older
  image. A minor rollback is safe only to an image that actually opens that data directory;
  the release-note no-dump/restore statement is not proof of this Docker/volume pair. The
  rollback point for data is the backup taken before the first start on the new patch. The same
  applies to the Redis append-only and snapshot files.

## Verification and limits

The 16.16 digest was pulled with `timeout 600 docker pull <pin>` and the single disposable
`docker run --rm --name rv1041-version <pin> postgres --version` printed
`postgres (PostgreSQL) 16.16`. It did not start a server or attach a volume. The tag/index
and platform verification is recorded above. The Compose 2.24.4, 2.38.2 and 5.0.2 config
checks belong to #1022's previous pins and were not repeated for this update.

CI starts the pinned images from a fresh volume, or restarts the same image: the `docker` job
(Compose start, Alembic upgrade, Redis restart, force-recreate with the volume retained and
fresh-volume replacement), `restore-drill`, `first-short-runtime`, `worker-topology-runtime`
(including a stop and start of its Redis container) and the `test` / `test-worker` service containers. Not covered
here, and tracked in #1021: a volume written by the previous patch opened by the pinned one,
agreement between each tag and the version the running server reports (only the 16.16 binary
version was checked here, not a running server), index integrity after the
Alpine change, and refusal of a major bump on an existing volume. The arm64 manifests are in the
pinned indexes but were not run.

The drift guard reads every tracked YAML, Python, shell, TOML, Makefile and Dockerfile file as text
for a reference written with any tag, qualified by a registry or namespace, inside a
`${NAME:-default}` default, or untagged or digest-only on an `image:` key or a `FROM` line, and it
parses the tracked YAML and reads every `image` value (and the workflow job `container` shorthand)
whatever its quoting, flow style, anchor or Compose tag. A `docker run` or `docker create` command, with or without global options (`--context x`, `-Hunix:///x`, `--config "a dir"`, `-c=ctx`, ...),
is read without parsing it: from the command word to the end of the statement (continued lines joined, and the decoded `run` values of workflow
steps, so folded scalars too) every whole word that names either image is a reference, except the
value of an option that carries a name such as `--name redis`. Quoting, command substitution and
`bash -c` therefore cannot hide an image, and an untagged `docker run ... redis` is caught in every
file that the guard scans; the price is that a word handed to the container's own command, such as
`python -c 'import redis'`, is reported as well and has to be rewritten, and a config or context
named `run` or `create`, or a boolean global option directly before `compose`, is read as the
subcommand. Each reference is compared
byte for byte with the Compose pin, so even `docker.io/library/postgres:...` is rejected when it
differs from the canonical spelling. As a secondary tripwire, each known consumer must keep at
least the number of references it has today; that count is per file, so it can be offset by an
unrelated addition and is not the primary defense. The guard does not read prose documentation,
such as this page. It cannot see a reference assembled at run time (`$IMAGE`) or an image passed as
an element of a Python argument list, and it cannot tell whether a tag and its digest belong
together: a tag edited without its digest, consistently everywhere, passes. The runtime check in
#1021 asserts the version each running server reports, and the lookup in "Updating a pin"
re-derives a digest from its tag.
