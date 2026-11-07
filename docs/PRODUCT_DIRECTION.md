# Product Direction: AI Video Studio

> AI creates the first cut. You stay in control.

## Status and principle

This document records the product direction and target experience. It is not a
feature inventory or a statement that the roadmap has shipped. The current
stage-based generation foundation is described in the [repository README](../README.md).

**Short-first product, long-form-ready architecture.**

AI Video Studio is an open-source, short-form-first AI video creation tool that
turns ideas and media assets into editable short-form videos. We are evolving
from an AI Shorts generator into a studio, not a general-purpose nonlinear
editor (NLE). See [BRANDING.md](BRANDING.md) for the product name and the pending
repository rename, tracked in
[#886](https://github.com/yeongseon/short-form-studio/issues/886).

## Current product scope

The target audience makes YouTube Shorts, Instagram Reels, TikTok videos, product
shorts, story shorts, knowledge and educational shorts, promotional shorts, and
social clips.

- Shipped duration choices are **15, 30, 45, 60, 90 seconds, and Custom**.
- The UI guides Custom users toward approximately **3 minutes or less**, but hard
  validation only requires a finite value greater than zero. It has no maximum.
- Target duration is intent, not guaranteed achieved render duration. Longer
  values remain valid at the domain/settings boundary but do not establish tested
  long-form rendering performance or product support.
- Typed settings accept vertical 9:16 (`short_vertical`), square 1:1
  (`short_square`), and landscape 16:9 (`short_landscape`). They are persisted;
  square and landscape are real-FFmpeg tested across selectable encodings, but
  settings are not yet propagated through approval to current project
  renders; the compatibility render remains vertical. A landscape social video
  is not, by geometry alone, long-form work.
- Selectable encoding profiles are `preview`, `standard`, and `high_quality`.
  `legacy_default` is a read-only result for projects without stored settings and
  is not equivalent to `standard`.

These statements are backed by `packages/creator-domain/creator_domain/models/project_settings.py`,
`packages/creator-domain/creator_domain/models/output_spec.py`,
`apps/studio-web/src/__tests__/IdeaFormDuration.test.tsx`, and
`packages/creator-service/tests/test_output_preset_ffmpeg.py`. End-to-end
long-form support remains tracked by [#884](https://github.com/yeongseon/short-form-studio/issues/884),
[#885](https://github.com/yeongseon/short-form-studio/issues/885), and
[#905](https://github.com/yeongseon/short-form-studio/issues/905).

Focus on **real assets, scene editing, a simple timeline, AI editing, and easy OSS setup**.

### Not in the current scope

Do not add long-form project types, 10-minute editors, chapter editors, long-form
recipes or UI, 20-minute timelines, chunk rendering, chapter-level rendering,
long-form publishing workflows, or long-form-specific UX. Do not build a full
NLE, CapCut clone, Premiere clone, or After Effects clone.

`Sequence`, chapters, and chunk rendering remain [future design notes](architecture/LONG_FORM_EXTENSION.md),
not current issues or milestone dependencies.

## Target creation experience

```text
New Short -> Describe idea -> Select duration -> Select style
          -> Optionally upload assets -> Generate -> Preview
                                                   |-> Download
                                                   `-> Edit
```

Users start with an **AI draft**, never an empty timeline. The first creation
screen shows the idea, duration, style, optional assets, and Generate action, not
the editor. The result offers playback, Download, and Edit.

For example, a user uploads three product photos, a product video, and a logo.
AI supplies the script, scene plan, narration, captions, music, and missing visuals.
The draft combines actual product assets with generated assets rather than
generating an image for every scene by default.

Asset selection priority is uploaded assets, existing project assets, external
or stock assets when available, then AI generation for gaps. This is not a
commitment to implement a stock provider now.

## Target editing experience

Only Edit opens the studio: Media library, Preview, Scene/Inspector, a simple
Timeline, and AI Assistant. Scene Cards are the primary editing interface;
the timeline is a supporting tool. Scene editing, manual timeline editing, and
AI requests are parallel ways to edit the same state, not three mandatory steps.

A Scene Card shows its script, visual asset, duration, and transition. Users can:

- Regenerate an image or replace the asset.
- Edit script or change voice.
- Adjust duration or change transition.

The simple timeline supports move, trim, split, delete, replace, resize, and
reorder. Initial tracks are Visual, Text, Caption, Narration (Voice in the UI),
and Music. Advanced compositing and many stacked video tracks are out of scope.

AI requests stay focused on short edits, for example:

- "Make this 30 seconds" or "Speed up the opening."
- "Replace the second image" or "Remove the final CTA."
- "Make captions larger" or "Lower the background music."
- "Switch to vertical video."

AI proposes a validated diff before changing the timeline. Users **Apply** or
**Cancel**; both manual and accepted AI changes support Undo and Redo. The
[architecture](architecture/SHORT_FIRST_MEDIA_CORE.md) describes the shared command boundary.

## Preserve the generation foundation

Keep the existing pipeline stages, human review, task tracking, usage and cost
accounting, providers, retries, idempotency, storage, and security. Do not replace
the working backend simply to simplify the product presentation.

Present friendly progress such as Script, Visuals, Voice, Captions, and Rendering
instead of raw internal stage identifiers. This presentation change must not
silently bypass required review approvals. The fully guided creation experience
must accommodate those approval gates.

## Recipes, styles, and templates

Start with general, story, knowledge, product, and promotional short recipes.
Educational shorts can use the knowledge recipe initially. News, real estate,
restaurants, and travel are later recipe extensions, not core domain branches.

A recipe defines **what to make**; a CreativeProfile defines **how it feels**.
A reusable template combines a ContentRecipe, OutputSpec, CreativeProfile, and
Timeline skeleton. Platform names belong in product labels, not core classes.

## Success criteria

Measure usefulness rather than feature count. These are targets, not measured results:

| Metric | What it tells us |
|---|---|
| Time to First Short | Target: a new user creates the first Short in under 10 minutes, including setup |
| Edited draft usage | Users refine and use AI drafts instead of discarding them |
| Uploaded Asset Usage Rate | Users can incorporate their own images, video, and audio |
| Second Video Creation | Users return to create another video |
| Manual Edit vs AI Edit | Relative use of Scene, Timeline, and AI editing |

Instrumentation definitions and privacy treatment must be designed before adding
telemetry; these metrics do not authorize collecting raw media, prompts, or keys.

See the [roadmap](ROADMAP.md) for implementation priorities and acceptance criteria.
