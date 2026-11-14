# Coordinated Backup and Restore

This runbook defines the single-VM/Compose restore evidence required before hosted beta. It does not authorize access to production data. Use disposable resources and synthetic fixtures for drills.

## Operator decisions (required before beta)

| Field | Decision |
|---|---|
| RPO (maximum acceptable data loss) | **DECISION: owner must set and approve** |
| RTO (maximum acceptable recovery time) | **DECISION: owner must set and approve** |
| Backup retention and deletion policy | **DECISION: owner must set and approve** |
| Backup failure alert channel and owner | **DECISION: owner/channel must be assigned** |
| Restore-drill failure alert channel and owner | **DECISION: owner/channel must be assigned** |
| Drill schedule | **DECISION: cadence and next date must be assigned** |

Record measured duration, result, release SHA, image digests, Alembic revision, bundle identifier, and reviewer for every drill. Do not substitute a successful backup notification for restore evidence.

## Inventory and security boundary

Back up one coordinated restore point containing:

- the complete PostgreSQL database, including users, workspace membership, API-key revocation, projects, approved Timeline revisions, task state, and artifact metadata;
- `data/artifacts` (or the configured local/object-store equivalent), including workspace image, audio, video, render manifests, and final MP4 files;
- a manifest containing capture timestamp, release Git SHA, immutable API/worker/web image digests, Alembic revision, secret **fingerprint/version only**, and a SHA-256 checksum for every dump/media file.

Secrets are restored out-of-band from the approved secret manager. Never put raw API keys, provider credentials, `.env`, database passwords, signing keys, or secret-manager exports in the bundle, repository, logs, or CI artifacts. A fingerprint identifies the required secret generation without disclosing it.

## Consistent snapshot procedure

1. Announce the maintenance window and record its start time.
2. Stop ingress and quiesce writes. Stop `worker`, `beat`, and `api`; confirm no writer remains. Redis is not the source of truth.
3. Capture the Alembic revision and immutable release/image identifiers.
4. Run `pg_dump` while writes remain stopped. Treat an empty or failed dump as failure.
5. Snapshot/copy the complete artifact root **before writes resume**. If storage supports atomic snapshots, pin that snapshot to the same quiesced point.
6. Place the dump at `BUNDLE/database.dump` and media under `BUNDLE/media/`, then create the manifest:

   ```bash
   python3 scripts/restore_verify.py create BUNDLE \
     --release-sha "$RELEASE_SHA" \
     --image-digest "api=$API_DIGEST" \
     --image-digest "worker=$WORKER_DIGEST" \
     --alembic-revision "$ALEMBIC_REVISION" \
     --secret-fingerprint "$SECRET_FINGERPRINT" \
     --timestamp "$CAPTURED_AT"
   ```

7. Verify the bundle before writes resume. Transfer it to encrypted retention storage only after verification.
8. Resume the approved release and record backup success/failure. Alert on either DB or media/manifest failure; a DB-only success is not a backup success.

If continuous writes cannot be stopped, an operator must document and approve a storage-specific transaction/snapshot ordering that proves the media snapshot and `pg_dump` represent the same restore point. “Run `pg_dump`, then copy live files” is not sufficient.

## Safe restore

Restore only into a newly provisioned database and an absent/empty artifact target. Never point the procedure at the original environment.

```bash
python3 scripts/restore_verify.py verify BUNDLE \
  --expected-alembic-revision "$ALEMBIC_REVISION" \
  --expected-secret-fingerprint "$SECRET_FINGERPRINT"

python3 scripts/restore_verify.py restore-media BUNDLE \
  --target RESTORED_ARTIFACT_ROOT \
  --expected-alembic-revision "$ALEMBIC_REVISION" \
  --expected-secret-fingerprint "$SECRET_FINGERPRINT"
```

Restore `database.dump` with the matching PostgreSQL tooling, configure out-of-band secrets at the required fingerprint/version, then start the approved release. The verifier exits non-zero before copying media for an incomplete/empty DB dump, missing file, checksum mismatch, Alembic mismatch, secret fingerprint mismatch, unsafe path, or non-empty target.

Application rollback must use the approved release manifest from #858 and pass schema compatibility review. Database downgrade is a separate, explicitly approved data-change procedure; do not run `alembic downgrade` as an automatic application rollback step.

## Post-restore acceptance checks

Run all checks against the disposable restored environment and retain machine-readable results without credentials:

1. Confirm manifest checksums and restored media checksums.
2. Open synthetic workspace A’s project and approved Timeline without the original environment.
3. With workspace B credentials, request A’s project, Timeline/media, run, and artifact: every ownership denial is **404**, never 403.
4. Confirm a key revoked before capture remains revoked and cannot authenticate.
5. Download A’s image/audio/video through protected routes; download the final MP4 through `/api/creator/runs/{run_id}/artifacts/{artifact_id}/download`. Foreign/no credentials must not receive it.
6. Disable paid-provider credentials. Re-render the approved Timeline from restored image/audio/video with FFmpeg and verify the resulting MP4 is non-empty/playable and protected.
7. Reconcile restored `pending`, `queued`, and `running` tasks under the broker recovery policy in #865. Keep dispatch paused until classification completes. Never blindly replay work against paid APIs; require idempotency/settlement evidence and operator approval. Confirm tasks converge without indefinite waiting or duplicate charging.
8. Deliberately run the verifier against a copied/tampered fixture and confirm monitoring detects the restore failure, not only backup completion.

## Disposable drill and CI

`scripts/restore_drill.py` creates synthetic workspaces A/B, a revoked key, an approved Timeline, and image/audio/video fixtures; dumps PostgreSQL, restores it into a fresh container, checks isolation/revocation/checksums, and performs a local FFmpeg MP4 re-render. It uses ephemeral containers and temporary files only.

Child environments keep only `PATH`, `HOME`, standard locale variables, `TMPDIR`
and `VIRTUAL_ENV`; ambient database/broker destinations, provider/cloud credentials,
Celery configuration and telemetry settings are dropped in favour of explicit drill
values. Docker CLI calls alone also retain `DOCKER_HOST`, `DOCKER_CONTEXT`,
`DOCKER_CONFIG`, `DOCKER_CERT_PATH`, `DOCKER_TLS_VERIFY` and `XDG_RUNTIME_DIR` for
rootless/remote daemon selection; these selectors never reach non-Docker children.
`HOME` still selects tool configuration, including Docker configuration/contexts:
this filters variables, not a sandbox. API/reconciler state and fallback paths are
pinned under the temporary drill root. The drill refuses either existing planned
container name before startup; cleanup removes successful starts, and recovers a
failed start only by matching its exact name and per-run `restore-drill.run` nonce
label, removing the returned container IDs rather than an unproven name.

```bash
python3 -m pytest tests/test_restore_verify.py -q
python3 -m pytest tests/test_restore_drill.py tests/test_restore_drill_isolation.py -v
```

The Docker test skips when Docker is unavailable and runs as the separate `restore-drill` CI job. A skip is not restore evidence. The repository addition itself does **not** claim the Docker drill or a production restore was executed. The unit verifier runs without Docker.

## Periodic re-verification and failure handling

- Run the disposable drill on every CI change and on the operator-approved schedule against a recent encrypted backup in an isolated environment.
- Alert separately for backup creation failure, checksum verification failure, restore failure, post-restore authorization failure, re-render failure, and missed scheduled drill.
- Quarantine failed bundles; do not overwrite the last verified restore point. Preserve logs/manifest identifiers without secrets.
- Escalate to the assigned owner, record remediation, and repeat the complete restore. A later backup success does not close a restore failure.
- Review retention, RPO/RTO decisions, owners, broker recovery policy (#865), artifact ownership/retention (#861 and #862), and release rollback manifest (#858) after every incident or material architecture change.

Open beta work tracked in #869 through #875 may refine operations, but this procedure does not depend on unmerged code from those issues.
