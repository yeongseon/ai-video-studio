# Production Deployment Guide

> **Default hold:** this workflow remains inert unless the repository variable
> `DEPLOYMENT_ENABLED` is exactly `true`. This guide does not authorize changing
> that variable, publishing images, migrating a database, or deploying services.

The [system modes and hosted contract](architecture/SYSTEM_TOPOLOGY.md#modes-and-hosted-contract)
diagram is a configuration/contract template, not a discovered live cluster.
Hosts, private networks, databases, brokers, singleton scheduling, storage and
authenticated TLS ingress remain operator-owned resources.

The [secure ACA staging contract](ACA_STAGING_TOPOLOGY.md) specifies the target
private Azure Container Apps topology and environment matrix for #924. It is a
design input only: it does not prove that Bicep, Azure resources, images,
migrations, probes, smoke tests, or rollback exist or have passed.

## Validated release manifest

Every release and rollback uses one immutable JSON manifest. It binds the
reviewed source SHA, approved API/worker/Studio image digests, Alembic head,
schema contract, and queue contract. A date tag or `latest` is only an operator
label; deployment and migration consume manifest digests.

Push delivery publishes only `candidate-<full SHA>` images first. Trivy scans
those exact digests. Only after all scans succeed does `promote-release` attach
the approved date tag and `latest`. The `release-preflight` job validates real
HTTPS deploy/smoke targets, contracts, digest syntax, and registry existence
before either migration job can start. Missing variables, placeholder hosts,
arbitrary tags, missing images, or incompatible contracts stop the run there.

### Required environment configuration

Configure protected `staging`, `production`, and `production-db-downgrade`
GitHub environments. Environment variables must provide real HTTPS values for:

- `STAGING_API_URL`, `STAGING_STUDIO_URL`, `STAGING_DEPLOY_TARGET`
- `PRODUCTION_API_URL`, `PRODUCTION_STUDIO_URL`, `PRODUCTION_DEPLOY_TARGET`
- `SCHEMA_CONTRACT_VERSION`, `QUEUE_CONTRACT_VERSION`
- `SCHEMA_COMPATIBLE_DB_REVISIONS` as a comma-separated allowlist for the
  current schema contract

Environment secrets supply `DATABASE_URL`, `DEPLOY_TOKEN`, and
`SMOKE_API_KEY`. The deploy target must accept the manifest-bound API, worker,
and Studio digest payload and return `api_digest`, `worker_digest`, and
`studio_digest` observed by the hosting platform. The workflow does not trust
service environment variables as digest attestation. API, every active worker,
and Studio expose their independently embedded source SHA; worker identities
expire unless each instance refreshes its own record. The API image requires
`API_IMAGE_DIGEST` for its diagnostic identity endpoint, but deployment
certification uses the platform response. Authenticated smoke uses the real
demo-short creation route and its owned media-content endpoint.

The API image listens on `0.0.0.0:8000` inside its container so the platform
proxy can reach it. The runtime must not publish that port on an unrestricted
host interface: bind host publication to `127.0.0.1` or attach it only to a
private container network behind the authenticated TLS proxy.

### Release procedure and stop points

1. Keep `DEPLOYMENT_ENABLED` absent/false while configuring and reviewing all
   targets. No build, promotion, migration, or deploy job may run in this state.
2. For an authorized release window only, enable the hold and merge the reviewed
   SHA. `wait-for-ci` certifies that exact SHA. A CI failure stops before build.
3. `build-candidate` publishes unapproved candidate tags. A scan failure stops
   before release tags or `latest` are promoted; investigate or rebuild rather
   than retagging the candidate manually.
4. `create-manifest` obtains Alembic head from the candidate API image and emits
   `release-manifest-<tag>`. Preflight failure stops before any DB change.
5. Migrations execute `alembic` inside the selected API image by digest. The
   current revision, image heads, and manifest revision must all match or the
   command exits non-zero and deployment stops.
6. Staging verification must match all three running digests/source SHA, the DB
   revision, and authenticated create/download. Production cannot proceed on a
   failure. Production repeats the same checks after deployment.
7. Disable `DEPLOYMENT_ENABLED` after the approved window. Preserve artifacts
   and logs before rerunning a failed stage; start a new run rather than editing
   the manifest.

### Application rollback and database downgrade

Use `workflow_dispatch`, choose `release_action=rollback`, and enter the run ID
of a successful prior CD run that retained its `release-manifest-*` artifact.
Preflight verifies the run's workflow identity and successful conclusion,
downloads its manifest, proves the images still exist, and checks compatibility
with the current schema/queue contract. The app-only rollback skips both
migration jobs, deploys all three recorded digests together, then verifies the
live DB revision against `SCHEMA_COMPATIBLE_DB_REVISIONS` rather than requiring
the older manifest's exact revision. It never runs migrations from the current
main checkout.

Keep the additive database schema by default. A database downgrade is a
separate emergency operation: choose `release_action=db_downgrade`, provide the
target release run ID and a `migration_runner_run_id` whose manifest revision
exactly matches the current newer database, then type `DOWNGRADE` in
`approve_db_downgrade`. The workflow validates both successful-run artifacts,
repository identities, immutable tags, digests, contracts, and the live DB's
exact match to the migration-runner manifest. It runs Alembic from that newer
runner image, then verifies exact equality with the target manifest revision.
Approval occurs in the separately protected `production-db-downgrade`
environment; this operation does not deploy applications. Require a tested
backup/restore plan and drained traffic before approving it.

### Evidence and recovery

GitHub Actions retains `release-manifest-*`, `selected-release-*`,
`staging-release-evidence-*`, and `production-release-evidence-*` artifacts for
90 days. Job logs contain CI checks, scan results, immutable image references,
Alembic comparisons, deploy-target responses, and authenticated smoke commands.
Registry package versions provide candidate and promoted-tag provenance.

Recovery is roll-forward first. Before migration, correct configuration or
candidate findings and rerun. After a successful migration but failed app
deploy, retain the compatible schema and dispatch the previous manifest as an
app-only rollback. After verification failure, freeze promotion, preserve the
evidence artifact, compare each running digest, and redeploy the same manifest.
Use database downgrade only through its separate approval path.

CI builds API images from release N and N+1, installs N into disposable
PostgreSQL, seeds an authenticated project and media asset, upgrades to N+1,
then rolls the application back to N without a DB downgrade and verifies both
resources remain accessible. Real registry promotion and protected-environment
deployments still require hosted infrastructure and explicit operator approval.

This document outlines the production deployment workflow for AI Video Studio, with a focus on database migration safety and rollback strategies.

## Overview

The authoritative delivery sequence is the [release procedure](#release-procedure-and-stop-points):
default hold, exact-source CI, candidate builds and scans, immutable manifest and
HTTPS preflight, then protected migrations and staging verification before
production deployment and verification. A push alone does not authorize delivery.

## Prerequisites

### Required Secrets

Configure these in GitHub repository settings (`Settings > Secrets and variables > Actions`):

| Secret | Description | Example |
|--------|-------------|---------|
| `DATABASE_URL` | Production PostgreSQL connection string | `postgresql://user:pass@prod-db.example.com:5432/dbname` |
| _(deployment-specific)_ | Cloud provider credentials, SSH keys, etc. | Varies by infrastructure |

### Environment Protection Rules

The `production` environment should be configured with the following protections:

1. **Required Reviewers**: At least 1 reviewer from the production deployment team
2. **Deployment Branches**: Only `main` branch can deploy
3. **Wait Timer**: Optional 5-minute wait timer for additional safety
4. **Environment Secrets**: All production credentials stored as environment secrets

Configure in GitHub: `Settings > Environments > production > Protection rules`

### Infrastructure Requirements

Production environment must have:

- PostgreSQL 14+ database (accessible from GitHub Actions runners)
- Target deployment infrastructure (Kubernetes cluster, VMs, cloud platform, etc.)
- Network connectivity from GitHub Actions to production systems
- Monitoring and alerting configured for health checks

## Migration Strategy

### Philosophy: Backward-Compatible Migrations

All database migrations MUST be backward-compatible to support zero-downtime deployments:

**Allowed (safe) migrations:**
- Add new tables
- Add new columns with default values or NULL allowed
- Add new indexes (use `CONCURRENTLY` in PostgreSQL)
- Create new constraints that existing data already satisfies
- Add new ENUM values (at end of list in PostgreSQL)

**Requires Multi-Step Deployment:**
- Remove columns (1: make nullable, 2: stop using in code, 3: drop column)
- Remove tables (1: stop using in code, 2: drop table)
- Rename columns (1: add new column, 2: dual-write, 3: migrate data, 4: drop old column)
- Change column types (1: add new column, 2: migrate data, 3: drop old column)
- Add NOT NULL constraints (1: add nullable, 2: migrate data, 3: add constraint)

### Migration Workflow

#### 1. Creating a New Migration

```bash
# Always start from latest
cd apps/api
alembic upgrade head

# Create migration with descriptive name
alembic revision --autogenerate -m "add_video_thumbnail_url_column"

# Review generated migration file in migrations/versions/
# Verify upgrade() and downgrade() are correct
```

#### 2. Testing Migration Locally

```bash
# Test upgrade
alembic upgrade head

# Test downgrade (ensure rollback works)
alembic downgrade -1

# Re-upgrade to test idempotency
alembic upgrade head
```

#### 3. CI Validation

The CI workflow automatically validates migrations:

- **Single head check**: Ensures no branched migration history
- **Migration syntax check**: Verifies migration files are valid Python

No manual action needed - CI will fail if migrations are malformed.

#### 4. Production Deployment

On a push to `main`, CD remains inert unless `DEPLOYMENT_ENABLED=true` was
explicitly authorized for that release window. Exact-SHA CI, candidate scans,
manifest/HTTPS preflight, protected migration approval and staging verification
must succeed before production proceeds. See the release procedure above for
the complete stop points; this section is not an unconditional push/migrate path.

**Legacy diagnostic example, not a CD override:** use the selected immutable
API image and the protected release procedure for actual production migrations.

```bash
# SSH to production server or use cloud CLI
cd /path/to/app
source venv/bin/activate
export DATABASE_URL="postgresql://..."
cd apps/api
alembic upgrade head
```

### Migration Verification

After migrations run, the CD workflow verifies:

```bash
# Show current revision
alembic current

# Show latest head(s)
alembic heads

# Verify no pending migrations
alembic history | grep "(head)" | wc -l  # Should be 1
```

## Rollback Strategy

### Application Rollback

If deployment fails or issues arise:

1. **Immediate**: Use the [manifest-bound application rollback](#application-rollback-and-database-downgrade), not an arbitrary image tag
2. **Database**: Database stays at migrated state (see below)

### Database Rollback

**WARNING**: Database rollbacks are risky and should be avoided by following backward-compatible migration practices.

#### When to Rollback Migrations

Only rollback if:
- Migration introduced a critical bug affecting data integrity
- Migration blocked application functionality
- Rollback can be done safely without data loss

#### How to Rollback

The following illustrates Alembic mechanics only. Production downgrade requires
the separately protected, manifest-bound emergency procedure above; do not run
this checkout-based example as an alternative approval path.

```bash
# 1. Stop application traffic to database (read-only mode or full shutdown)
# 2. Identify target revision
cd apps/api
alembic history

# 3. Downgrade to previous revision
alembic downgrade -1  # Go back one revision
# OR
alembic downgrade <revision_id>  # Go to specific revision

# 4. Verify
alembic current

# 5. Restart application on older version
```

#### After Rollback

If you rolled back a migration:

1. Fix the migration locally
2. Test thoroughly (upgrade + downgrade + upgrade)
3. Create new PR with corrected migration
4. Deploy new migration after approval

**DO NOT** reuse the same revision ID - Alembic tracks executed revisions in `alembic_version` table.

## Deployment Commands

The CD workflow calls configured HTTPS deployment adapters with immutable
digests. Infrastructure-specific adapters remain operator-owned; the workflow
does not add or require Kubernetes.

### Kubernetes

The examples below illustrate operator adapter mechanics, not the release
contract. Actual adapters must apply all three selected immutable image digests,
attest observed platform identities and preserve the protected verification gates.
They are not evidence that a Kubernetes cluster or any named host exists.

```bash
# Update deployment images
kubectl set image deployment/api api=your-registry/api:${GITHUB_SHA}
kubectl set image deployment/worker worker=your-registry/worker:${GITHUB_SHA}

# Wait for rollout
kubectl rollout status deployment/api
kubectl rollout status deployment/worker

# Health check
kubectl exec deploy/api -- curl -sf http://localhost:8000/healthz
```

### Docker Compose (Single Server)

```bash
# SSH to server
ssh production-server << 'EOF'
cd /opt/short-form-studio
git pull origin main
docker compose pull
docker compose up -d --remove-orphans
docker compose ps
EOF

# Health check
curl -sf https://api.example.com/healthz
```

### Cloud Platforms

- **AWS ECS**: Update task definitions, create new service deployment
- **GCP Cloud Run**: Deploy new revision with `gcloud run deploy`
- **Azure Container Instances**: Update container group with new image
- **Heroku**: `git push heroku main` (migrations run automatically via release phase)

## Health Checks

After deployment, verify all services are healthy:

### API Health Check

```bash
curl -sf https://api.example.com/healthz
# Expected: {"status": "healthy"}
```

### Worker Health Check

```bash
# Check Celery worker logs
docker logs short-form-studio-worker-1
# OR
kubectl logs -l app=worker --tail=50

# Check Flower dashboard (if enabled)
open https://flower.example.com
```

### Frontend Health Check

```bash
curl -sf https://app.example.com/
# Expected: 200 OK with HTML content
```

### Database Connection Check

```bash
# From API container
docker exec short-form-studio-api-1 python -c "
from sqlalchemy import create_engine
import os
engine = create_engine(os.environ['DATABASE_URL'])
with engine.connect() as conn:
    result = conn.execute('SELECT 1')
    print('Database connected:', result.fetchone())
"
```

## Monitoring and Alerts

### Recommended Monitoring

- **Migration duration**: Alert if migrations take longer than expected
- **Deployment success rate**: Track failed deployments
- **Health check failures**: Alert immediately on 5xx errors
- **Database connections**: Monitor connection pool saturation
- **Worker queue depth**: Alert on backlog growth

### Logging

Key logs to monitor during deployment:

- **Alembic migration logs**: Verify each migration step
- **Application startup logs**: Check for configuration errors
- **Database query logs**: Identify slow queries after schema changes

## Troubleshooting

### Migration Fails with "Target database is not up to date"

**Cause**: Production database is behind expected revision.

**Solution**:
```bash
# Check current revision
alembic current

# Check expected revision
alembic heads

# Run pending migrations manually
alembic upgrade head
```

### Migration Fails with "Can't locate revision identified by '<revision>'"

**Cause**: Migration file missing or revision ID mismatch.

**Solution**:
1. Verify all migration files are committed and pushed
2. Check `alembic_version` table in database
3. If corrupted, manually fix revision ID in database

### Multiple Heads Detected

**Cause**: Conflicting migrations created on separate branches.

**Solution**:
```bash
# Show all heads
alembic heads

# Merge heads into single history
alembic merge -m "merge_migration_branches" <rev1> <rev2>

# Test merged migration
alembic upgrade head
```

### Deployment Succeeds but Application Crashes

**Cause**: Migration and code are incompatible.

**Solution**:
1. Check application logs for errors
2. Verify migration was backward-compatible
3. If not, rollback application (NOT database) to previous version
4. Fix code to handle new schema
5. Redeploy

## Security Considerations

### Database Credentials

- Store `DATABASE_URL` as encrypted GitHub secret
- Use read-write database user for migrations (not superuser)
- Rotate credentials regularly
- Audit migration logs for suspicious activity

### Migration Review

All migrations MUST be reviewed by at least one other engineer before merge:

- **Review checklist**:
  - [ ] Migration is backward-compatible
  - [ ] Downgrade function is correct and tested
  - [ ] No hardcoded credentials or sensitive data
  - [ ] Indexes use `CONCURRENTLY` for large tables
  - [ ] Data migrations are idempotent
  - [ ] Performance impact is acceptable

### Deployment Approval

Production deployments require manual approval from designated reviewers:

- Platform/SRE team for infrastructure changes
- Security team for auth/permission changes
- Product lead for schema changes affecting user data

## Emergency Procedures

### Roll Forward (Preferred)

If a deployment introduces a bug:

1. Create hotfix branch from `main`
2. Fix the issue
3. Deploy hotfix through normal CD pipeline (with expedited review)

### Roll Back (Last Resort)

If roll forward is not possible:

1. Revert application deployment to previous version
2. **DO NOT** rollback migrations unless absolutely necessary
3. Monitor for data consistency issues
4. Plan roll forward fix immediately

### Database Corruption

If migration corrupts data:

1. **STOP ALL APPLICATION TRAFFIC IMMEDIATELY**
2. Assess damage scope (query affected tables)
3. Restore from backup if necessary
4. Replay transaction logs if available
5. Notify users of potential data loss
6. Post-mortem required

## Post-Deployment

After successful deployment:

1. Monitor error rates and performance metrics for 1 hour
2. Verify background jobs are processing normally
3. Check Sentry/error tracking for new exceptions
4. Update deployment log with timestamp and version
5. Notify team in Slack/Discord

## References

- [Alembic Documentation](https://alembic.sqlalchemy.org/)
- [PostgreSQL Concurrent Indexes](https://www.postgresql.org/docs/current/sql-createindex.html#SQL-CREATEINDEX-CONCURRENTLY)
- [GitHub Actions Environment Protection Rules](https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment#environment-protection-rules)
- [Usage Guide](USAGE.md)
- [Production Cutover Checklist](CUTOVER.md)
