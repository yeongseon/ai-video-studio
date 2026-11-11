# Documentation

## Pull-request CI policy

Only documentation-only changes receive a reduced PR matrix. Canonical
case-sensitive `docs/**/*.md` and root-level `*.md` regular files select the
`docs` gate. All other changes—including tests, fixtures, frontend, scripts,
configuration, backend, infrastructure, and unknown paths—run all six gates:
`docs`, `code`, `restore`, `first_short`, `worker`, and `docker`. This is a
deliberate narrowing of the optimization, not a weakening of validation.
Widening reduced classes later requires evidence and Oracle review.

| Document | Description |
|---|---|
| [SYSTEM_TOPOLOGY.md](architecture/SYSTEM_TOPOLOGY.md) | Canonical configured execution, static imports, approval flows, and hosted contract; not live-host verification |
| [FIRST_SHORT_WALKTHROUGH.md](FIRST_SHORT_WALKTHROUGH.md) | Offline demo, Timeline review, render and download |
| [QUICKSTART.md](QUICKSTART.md) | Local bootstrap and proxy authentication |
| [PRODUCT_DIRECTION.md](PRODUCT_DIRECTION.md) | Short-first product scope, target UX, and success criteria |
| [ROADMAP.md](ROADMAP.md) | Planned P0–P3 work and milestone acceptance criteria |
| [SHORT_FIRST_MEDIA_CORE.md](architecture/SHORT_FIRST_MEDIA_CORE.md) | Target generic media core and compatibility boundaries |
| [LONG_FORM_EXTENSION.md](architecture/LONG_FORM_EXTENSION.md) | Future extension checklist only; not active implementation |
| [USAGE.md](USAGE.md) | Detailed usage guide and feature walkthrough |
| [SECURITY.md](SECURITY.md) | Authentication model, trust boundaries, network exposure policy |
| [CUTOVER.md](CUTOVER.md) | Deployment checklist and operational guide |
| [ACA_STAGING_TOPOLOGY.md](ACA_STAGING_TOPOLOGY.md) | Secure target ACA staging topology and configuration contract; not deployment evidence |
| [BACKUP_RESTORE.md](BACKUP_RESTORE.md) | Coordinated database and media backup/restore runbook |
| [LIGHTWEIGHT.md](LIGHTWEIGHT.md) | Run without Celery/Redis |
| [LOCAL_DOCKER.md](LOCAL_DOCKER.md) | Local Docker Compose setup, private by default with an explicit trusted-LAN opt-in |
| [RUN_STORAGE_CONTRACT.md](RUN_STORAGE_CONTRACT.md) | Versioned run storage adapter update contract |
| [QUEUE_DURABILITY.md](QUEUE_DURABILITY.md) | Redis durability, broker replacement recovery, and DLQ retention contract |
