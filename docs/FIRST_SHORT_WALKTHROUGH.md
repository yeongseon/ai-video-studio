# First Short Walkthrough

This is the walkthrough for creating your **first Short** with the
bundled sample, from a fresh clone through preview, download, and editing. It
links to [QUICKSTART.md](QUICKSTART.md) for setup rather than duplicating it, and
separates automated checks from a first-user time-to-first-Short measurement.

> **The under-10-minute figure is a _target, not a measured result_.** See
> [Time to First Short measurement](#time-to-first-short-measurement) below for
> the honest recorded validation. This document does not claim the KPI is
> achieved.

## Prerequisites (be honest with yourself here)

| Requirement | Needed for | Notes |
|---|---|---|
| Docker + Docker Compose v2 | Everything | The whole stack runs in containers. |
| `OPENAI_API_KEY` | Script / image / TTS on the CPU path | Remote provider; the CPU path cannot generate without it. |
| `GROQ_API_KEY` | Subtitles (STT) on the CPU path | Remote provider for Whisper-class STT. |
| NVIDIA GPU + `--profile gpu` | Local model generation | **Optional.** Local model images are GPU-gated and not shipped in this repo. |

**Generation cannot proceed without either remote provider keys or a working
GPU/local-model setup.** With neither configured, you can still install, start
the stack, open the Studio, and reach setup readiness — but the pipeline stops at
the first generate step. That limitation is by design, not a bug.

## 1. Install and set up

Follow the canonical steps in [QUICKSTART.md](QUICKSTART.md). Do not use an
abbreviated command list as a second bootstrap procedure: Quick Start preserves
the required migration → creator key → Studio proxy → authenticated smoke order
and starts one dedicated Beat scheduler.

The setup uses these entrypoints; follow Quick Start for the required values and
the complete order:

```bash
cp .env.example .env
docker compose up -d postgres redis
docker compose run --rm api alembic upgrade head
docker compose run --rm api python scripts/create_api_key.py \
  --email local@example.com --workspace local --name local-dev
docker compose up -d api worker beat studio-web
```

Then open the Studio at `http://127.0.0.1:5174`.

## 2. Confirm setup readiness

Check that prerequisites are configured before you try to generate. The onboarding
surface reports first-run vs returning state, the duration presets, the human
review gates, and the next action:

- `GET /api/creator/workspaces/{workspace_id}/onboarding`

If a required provider is missing, the onboarding `next_action` guides you to set
the env var and restart, mirroring the first-run Setup Wizard.

The reported duration choices are 15/30/45/60/90 seconds plus Custom. The API's
180-second custom hint and the Studio's approximately-three-minute copy are
guidance only. Hard validation accepts any finite value greater than zero, with
no maximum; a target is intent and does not guarantee achieved render duration.
Only `short` is an accepted content format. See
`packages/creator-domain/tests/test_duration_contract.py::test_finite_long_duration_remains_supported`
and `apps/api/tests/test_creator_onboarding.py::test_onboarding_no_core_duration_ceiling_in_response`.

## 3. Create a sample-backed first Short (optional demo flow)

The demo seeds offline synthetic media into a new workspace-owned project. First
review what it will do — estimated costs, required provider configuration, and the
approvals it will _not_ bypass:

- `GET /api/creator/workspaces/{workspace_id}/demo-short/plan`

Then seed the run. This creates a **new, workspace-owned "Demo Short" project**
with a real sample-backed timeline, plus a run at the `TIMELINE_REVIEW` stage;
it does **not** mutate your existing project and does **not** auto-advance or
auto-approve anything:

- `POST /api/creator/workspaces/{workspace_id}/demo-short/runs`

Inspect the saved Timeline preview before rendering. Approve that exact revision
with `POST /api/creator/runs/{run_id}/approve-timeline-render`, supplying
`{"expected_revision": <saved revision>}`. A changed revision requires a new review.
The result still requires `FINAL` review before publishing. Generated workflows
retain their `SCRIPT`, `VISUAL_PLAN`, `VISUAL_ASSET`, and `FINAL` review gates;
the pre-authored demo does not bypass approvals on those workflows.

## 4. (Optional) Upload your own media

The sample-backed path needs no uploads, but you can incorporate your own images,
video, or audio:

- `POST /api/creator/workspaces/{workspace_id}/assets` (image)
- `POST /api/creator/workspaces/{workspace_id}/assets/audio`
- `POST /api/creator/workspaces/{workspace_id}/assets/videos`

## 5. Generate, review, preview, download, and Edit

Drive the pipeline through its stages (each generate step requires the relevant
provider prerequisite):

- Generate scene images: `POST /api/creator/runs/{run_id}/visual-plan/scenes/{scene_id}/generate-image`
- Generate narration: `POST /api/creator/runs/{run_id}/generate-audio`
- Generate subtitles: `POST /api/creator/runs/{run_id}/generate-subtitles`
- Render the video: `POST /api/creator/runs/{run_id}/render`
- **Preview** the render: `GET /api/creator/runs/{run_id}/preview`
- Preview the editable Timeline: `GET /api/creator/projects/{project_id}/timeline/preview`
- **Download** the finished Short: `GET /api/creator/runs/{run_id}/artifacts/{artifact_id}/download`
- **Edit** the draft: load and save the Timeline via
  `GET /api/creator/projects/{project_id}/timeline` and
  `PUT /api/creator/projects/{project_id}/timeline`, plus scene-level edits in the
  Studio's Scene View.

### Local-only preview and download

Previews and downloads are served on the **loopback interface (`127.0.0.1`) only**.
The stack is never bound to `0.0.0.0` by default, and artifacts are **not exposed
externally automatically**. If you need to share an artifact beyond the local
machine, do so explicitly and deliberately — see [SECURITY.md](SECURITY.md).

### When something fails

Errors across creation and editing are mapped to actionable recovery steps
without leaking secrets or internal paths. A failed run/task exposes a safe
failure summary (category, whether it is retryable, and recovery steps), so you
can tell a transient provider hiccup (retry) from a missing prerequisite (fix and
re-run) from a genuine conflict (refresh and reapply). See
[USAGE.md](USAGE.md) for the review-and-recovery flow.

## Time to First Short measurement

**Definition.** _Start_ = the moment you begin a fresh clone/setup. _End_ = the
first previewed or downloaded Short. The product target is **under 10 minutes,
including setup** — a _target, not a measured result_, and this document makes no
claim that the target is achieved.

**How to measure it yourself.** Note the wall-clock time when you run
`git clone`, then note it again when `GET /api/creator/runs/{run_id}/preview`
first returns your rendered Short (or when the download completes). Record the
environment (CPU remote-provider path or GPU path), the elapsed time, and any
prerequisites or waiting points (image pulls, container health, provider
latency). Timing is environment-dependent and is not asserted in CI.

### Recorded validation runs

| Date | Environment | Start | End | Elapsed | Result | Notes |
|---|---|---|---:|---:|---|---|
| 2026-09-21 | Existing development checkout | File and environment-variable presence check | Check finished | Not measured | **Not a completed first Short** | This was not a fresh installation, browser session, or render. The earlier claim that install/start/open/readiness completed was unsupported and is withdrawn. **KPI not measured.** |

The record above is an environment preflight, not a user-journey timing attempt.
No successful fresh-user time-to-first-Short measurement is recorded here.
Automated PostgreSQL, FFmpeg, API, and browser regression checks are useful
engineering evidence, but they must not be reported as a timed first-user result.
The offline demo does not call paid providers; AI generation still requires
configured remote providers or a working local model setup.

The strongest automated demo evidence is
`tests/integration/test_first_short_runtime.py::test_authenticated_demo_survives_restart_and_renders_downloadable_mp4`:
it exercises real PostgreSQL, Redis, API restart, Celery, FFmpeg, `ffprobe`,
revision approval, final approval, and authenticated download with deterministic
synthetic media. It is not a fresh-clone stopwatch, generated-script acceptance,
long-form test, or paid-provider smoke. Unit/API tests around the same flow use
in-memory fixtures or mocked dispatch/FFmpeg; the separate real-FFmpeg fixture is
`apps/worker-orchestrator/tests/test_demo_sample_render.py::test_approved_seed_renders_both_distinct_synthetic_scenes`.
