# Short-first Product, Generic Media Core

## Status

This is the **target architecture** for the [short-first roadmap](../ROADMAP.md).
Names, fields, and paths below describe planned contracts, not existing APIs or
packages. The [README](../../README.md) describes the current pipeline foundation.
The [system topology](SYSTEM_TOPOLOGY.md) maps configured execution and current
approval gates to source files. The target sketches here are not deployed topology.

> Short Form is a product constraint, not a core domain constraint.

The product layer owns short-oriented UI, recipes, and presets. The core owns
generic media, timing, output, and rendering. Generic means reusable boundaries,
not implementing speculative long-form infrastructure now.

## Preserve the foundation

Keep `creator-domain`, `creator-service`, and `creator-provider`, together with
Project, PipelineRun, Workspace, Provider Registry, task tracking, usage, cost,
artifact storage, Celery, Redis, PostgreSQL, human review, retry/idempotency,
security, and tests. Introduce compatible slices rather than replacing this core.

Keep existing internal stages from IDEA_READY through script/visual review,
audio, subtitles, rendering, FINAL_REVIEW, and PUBLISHED. Friendly progress labels
are a UI projection, not a replacement state machine or an approval bypass.

## Target flow

**TARGET / PLANNED integration**, not an automatic conversion of generated runs
into approved Timelines. See the separate current entry flows in the topology.

<!-- diagram: planned -->
```mermaid
flowchart LR
    project["Project: production workspace"]
    recipe["ContentRecipe: content structure"]
    draft["AI Draft: script, assets, audio"]
    scenes["Scenes: editable groups"]
    segments["MediaSegments: timed asset placements"]
    timeline["Timeline: authoritative editable state"]
    edit["Scene, manual or AI editing"]
    preview["Browser Preview: inspect the edit"]
    compiler["Timeline Compiler: resolve media and timing"]
    plan["RenderPlan: renderer input"]
    ffmpeg["FFmpeg: encode short video"]
    project --> recipe
    recipe --> draft
    draft --> scenes
    scenes --> segments
    segments --> timeline
    edit --> timeline
    timeline --> preview
    timeline --> compiler
    compiler --> plan
    plan --> ffmpeg
```

For now, **one Project has one Timeline**. No Sequence domain is required.
Scene remains the user-facing grouping, but one Scene can contain multiple
MediaSegments rather than being equated with a single image.

## MediaAsset and asset selection

MediaAsset generalizes generated visuals to uploaded images/video/audio, logos,
graphics, music, and brand assets. Proposed fields:

| Field | Meaning |
|---|---|
| `id`, `workspace_id` | Stable identity and ownership boundary |
| `project_id`, `run_id` | Optional project/run association; must agree with workspace ownership |
| `media_type` | IMAGE, VIDEO, AUDIO, LOGO, or GRAPHIC |
| `origin` | GENERATED, UPLOADED, EXTERNAL_URL, STOCK, or IMPORTED |
| `storage_key`, `mime_type` | Optional storage reference and detected media type |
| `width`, `height` | Optional dimensions where applicable |
| `duration_seconds` | Optional duration for time-based media |
| `source_url` | Optional provenance, not unrestricted permission to fetch a URL |
| `metadata` | Per-asset structured metadata, including detected byte size as appropriate |

STOCK is extensible vocabulary, not a requirement to build stock integration now.
Choose uploaded assets first, existing project assets second, external/stock
assets when available third, and generate missing assets last.

Planned `POST /api/creator/assets` accepts image, video, and audio uploads. The
library supports finding and reusing authorized assets. Probe actual MIME type,
size, width, height, and duration as applicable; client-supplied metadata is not
trusted. The precise upload/list contracts and storage migration need tests when
implemented. Preserve VisualAsset compatibility and existing generated artifacts.

## OutputSpec and EncodingProfile

Separate output geometry from encoding settings and product duration choices:

| Product preset | Dimensions | Aspect ratio |
|---|---|---|
| `short_vertical` | 1080 × 1920 | 9:16 |
| `short_square` | 1080 × 1080 | 1:1 |
| `short_landscape` | 1920 × 1080 | 16:9 |

OutputSpec is generic; product presets configure it. Labels such as Shorts,
Reels, and TikTok live in the UI, not platform-specific core classes.
EncodingProfile contains codec/quality settings selected through `preview`,
`standard`, or `high_quality`, independently of output aspect ratio.

Recipe/product settings own requested target duration. Timeline timings determine
the actual rendered duration. Neither OutputSpec nor RenderPlan encodes a fixed
60-second or three-minute domain ceiling. Operational resource limits remain
necessary and are distinct from a short-only domain invariant.

## ContentRecipe, CreativeProfile, and templates

ContentRecipe answers **what to make**: target-duration range, content structure,
visual strategy, pacing and segment-duration range, narration, BGM, and subtitle
behavior. RecipeRegistry resolves explicit recipe IDs. Core logic must not branch
on niche names such as `ssul`, `viral`, `facts`, `product`, or `education`.

Initial recipe layout proposed by this design (the files now exist; this does not
make every target contract above implemented):

```text
recipes/shorts/
  general.yaml
  story.yaml
  knowledge.yaml
  product.yaml
  promotional.yaml
```

For example, `shorts_knowledge` targets 30–60 seconds, uses hook/explanation/
key-point/conclusion structure, fast pacing with 2–6 second segments, narration,
BGM, and emphasized subtitles. Optional `content_format` may describe `short`,
but the schema must not be permanently restricted to that single value. Do not
create a long-form recipe or directory now.

CreativeProfile answers **how it feels**. Initial profiles are `default`,
`cinematic`, `minimal`, `energetic`, `story`, and `product`. Remove any implicit
`shorts_default -> ssul_v2` fallback as part of migration; resolve defaults
explicitly and keep legacy runs reproducible through an explicit compatibility
mapping. A niche recipe or style must not become a hidden core fallback.

A template combines ContentRecipe + OutputSpec + CreativeProfile + Timeline
skeleton. Templates must resolve valid registry IDs rather than create a second
copy of generation or rendering logic.

## MediaSegment, Timeline, and RenderPlan

Proposed MediaSegment data consists of `id`, `scene_id`, `asset_id`,
`timeline_start_seconds`, `duration_seconds`, optional `trim_start_seconds` and
`trim_end_seconds`, `fit_mode` (initially cover), and optional transition.
This supports multiple assets within one Scene and source trims for video/audio.
Text, captions, and other track content must not require a fake image asset.

Timeline owns ordered tracks and timing. Initial track roles are Visual, Text,
Caption, Narration, and Music. Persist one authoritative Timeline state and
revision per project. Browser Preview reads that state; final render compiles
the same revision rather than independently rebuilding an edit from pipeline data.

The compiler validates asset references, timing, source bounds, track roles, and
supported transitions, and resolves a renderer-facing RenderPlan. RenderSegment
contains the resolved media kind/reference and the timings, trims, fit, and
transition needed for rendering. It is not just an image path.

RenderPlan contains:

- `segments`: ordered RenderSegments.
- `output_spec`: generic output geometry.
- `encoding_profile`: independently selected encoding settings.
- Optional `narration_path`, `subtitle_path`, and `music_path` for compiled layers.

These optional paths are compiler outputs derived from Timeline state, not a
second editable source of truth. Resolution must occur through authorized
storage references, not arbitrary client-provided filesystem paths.

FFmpeg consumes RenderPlan and handles image/video mixtures rather than only an
image slideshow. It needs precise timing but does not need to classify a plan
as a three-minute Short or a 20-minute video. This does **not** promise current
long-form rendering support. Keep existing Shorts render APIs via a compatibility
adapter while migrating `image_paths` callers to segment-based plans.

## EditorCommand and shared state

Both UI and AI use typed EditorCommands, for example `trim_segment` with
`segment_id` and `duration_seconds`, or `replace_asset` with `segment_id` and
`asset_id`. Manual controls must not bypass the shared command boundary.

**TARGET / PLANNED editing flow.** Accepting an edit is not render approval.

<!-- diagram: planned -->
```mermaid
flowchart LR
    manual["Manual action: user edit"]
    command["Validated EditorCommand: typed change"]
    request["AI request: suggested edit"]
    proposal["Structured proposal: revision-bound change"]
    validation["Validation: check proposal"]
    diff["Diff: review changes"]
    cancel["Cancel: no edit"]
    apply["Apply: accept edit"]
    history["Timeline revision and history: saved state"]
    manual --> command
    command --> history
    request --> proposal
    proposal --> validation
    validation --> diff
    diff --> cancel
    diff --> apply
    apply --> history
```

Undo and Redo cover manual and accepted AI edits. Autosave persists the accepted
state; optimistic UI state reconciles with server validation and revision checks.
An AI proposal is tied to its base revision and must be revalidated if that state
changes before Apply. AI does not mutate Timeline directly or overwrite concurrent
edits silently. A canceled proposal creates no accepted edit/history entry.

## Compatibility and security requirements

Existing VisualAsset data, render callers, and explicit review gates remain valid
during migration. New registry defaults cannot silently reinterpret stored runs.
Implement adapters/migrations and regression tests before retiring legacy contracts.

Future asset, Timeline, command, and render routes must use the repository's
workspace/project/run access dependencies. Validate every referenced asset against
the authorized workspace and applicable project scope, including replacement and
render compilation. Unauthorized object lookups return **404**, not 403.

Upload/probe work must validate actual file content, bound bytes and probe
resources, use safe storage keys, and avoid trusting client paths. External URL
origins do not bypass SSRF protections. Artifact access retains the loopback-only
default and explicit-consent external exposure policy. See [Security](../SECURITY.md)
and [AGENTS.md](../../AGENTS.md).

Security-critical and production-hardening implementation requires failing boundary
tests first, the full API suite, and Oracle 100/100 before merge as specified in
AGENTS.md. Tests should cover access control, IDOR, anti-enumeration, and auth
bypass along with media/command-specific boundaries. This design is not evidence
that those future implementations have passed those gates.

## Package boundaries and deferred work

Keep domain contracts in the existing domain boundary, orchestration/persistence
in services, and provider integrations behind providers. Separate `creator-media`
and `creator-render` packages are possible extractions when real dependencies
justify them, not packages to create solely for this diagram.

Only five long-form extension boundaries are needed now: generic assets, Timeline
without a short-only duration cap, generic OutputSpec, RenderPlan independent of
product length categories, and extensible recipes. Sequence, chapters, chunk
rendering, and assembly remain in the [future checklist](LONG_FORM_EXTENSION.md).
