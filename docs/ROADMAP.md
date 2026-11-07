# Short-first Roadmap

This is planned work for the [product direction](PRODUCT_DIRECTION.md), not a
shipped-feature checklist. All entries are initially unchecked; confirm existing
implementation and tests when taking up each item. Numbers are **local planning
IDs**, not GitHub issue numbers.

Published backlog: [tracking issue #623](https://github.com/yeongseon/short-form-studio/issues/623)
links all 79 implementation issues (#624–#702) and their dependencies. Work is
managed by the tracking issue plus `priority:` labels, not GitHub milestones.
Every implementation issue requires TDD and Red/Green evidence.

Long-form implementation is excluded. Its [extension checklist](architecture/LONG_FORM_EXTENSION.md)
is neither an active backlog nor a prerequisite for this work.

This historical checklist is not the current support matrix. In particular,
`OutputSpec`, the three named short output geometries, `EncodingProfile`, and typed
project-settings persistence now exist. Project-selected settings still do not
flow through approval/worker rendering; that delivery boundary is documented in
[`adr/008-typed-project-settings.md`](adr/008-typed-project-settings.md). Long-form
rendering and acceptance remain open in [#884](https://github.com/yeongseon/short-form-studio/issues/884),
[#885](https://github.com/yeongseon/short-form-studio/issues/885), and
[#905](https://github.com/yeongseon/short-form-studio/issues/905).

## P0 — Short Media Foundation

- [ ] 1. Introduce MediaAsset.
- [ ] 2. Add MediaType and MediaOrigin.
- [ ] 3. Add image upload.
- [ ] 4. Add video upload.
- [ ] 5. Add audio upload.
- [ ] 6. Add media metadata probing.
- [ ] 7. Add Asset Library API.
- [ ] 8. Preserve VisualAsset compatibility.

## P0 — Output and Recipe

- [ ] 9. Split RenderProfile into OutputSpec and EncodingProfile.
- [ ] 10. Add short_vertical OutputSpec.
- [ ] 11. Add short_square OutputSpec.
- [ ] 12. Add short_landscape OutputSpec.
- [ ] 13. Introduce ContentRecipe.
- [ ] 14. Add RecipeRegistry.
- [ ] 15. Move shorts-specific niche logic into recipes.
- [ ] 16. Introduce CreativeProfile.
- [ ] 17. Remove the implicit ssul_v2 fallback.

## P0 — Generic Short Renderer

- [ ] 18. Introduce MediaSegment.
- [ ] 19. Introduce RenderSegment.
- [ ] 20. Introduce RenderPlan.
- [ ] 21. Refactor the image_paths renderer to segments.
- [ ] 22. Support image segments.
- [ ] 23. Support video segments.
- [ ] 24. Support mixed image/video rendering.
- [ ] 25. Preserve the existing Shorts render API.

## P1 — Simple Timeline

- [ ] 26. Introduce the Timeline domain.
- [ ] 27. Introduce the simple Track domain.
- [ ] 28. Add Timeline persistence.
- [ ] 29. Add the Timeline-to-RenderPlan compiler.
- [ ] 30. Add Browser Preview.
- [ ] 31. Add Timeline Ruler.
- [ ] 32. Add Playhead.
- [ ] 33. Add Visual Track.
- [ ] 34. Add Caption Track.
- [ ] 35. Add Voice Track.
- [ ] 36. Add Music Track.

The initial track model also accommodates Text; it does not imply advanced
overlay compositing or additional video layers.

## P1 — Short Editor

- [ ] 37. Add Scene View.
- [ ] 38. Add Media Library.
- [ ] 39. Add drag and drop.
- [ ] 40. Add trim.
- [ ] 41. Add split.
- [ ] 42. Add delete.
- [ ] 43. Add replace.
- [ ] 44. Add reorder.
- [ ] 45. Add duration editing.
- [ ] 46. Add transition editing.
- [ ] 47. Add output aspect-ratio switching.

Move/resize interactions map to segment timing and duration commands. Scene
editing remains the default; timeline editing is a supporting surface.

## P1 — Editor State

- [ ] 48. Introduce EditorCommand.
- [ ] 49. Add Undo.
- [ ] 50. Add Redo.
- [ ] 51. Add Autosave.
- [ ] 52. Add optimistic local editor state.

Build the shared command/history boundary alongside the first mutable editor
controls, not after all controls have separate state mutation implementations.

## P2 — AI Editing

- [ ] 53. Add AI Command Bar.
- [ ] 54. Add a Timeline summary for the LLM.
- [ ] 55. Add structured EditorCommand generation.
- [ ] 56. Add AI command validation through the shared command boundary.
- [ ] 57. Add AI Diff Preview.
- [ ] 58. Add the AI shorten command.
- [ ] 59. Add the AI pacing command.
- [ ] 60. Add the AI replace-asset command.
- [ ] 61. Add the AI subtitle-style command.
- [ ] 62. Add the AI audio-volume command.

Manual commands already require validation in P1; item 56 extends that boundary
to untrusted AI proposals. AI never bypasses validation, explicit Apply, or history.

## P2 — Short Quality

- [ ] 63. Add reusable Short Templates.
- [ ] 64. Add scene-level regenerate.
- [ ] 65. Add scene-level asset search.
- [ ] 66. Add subtitle style presets.
- [ ] 67. Add voice presets.
- [ ] 68. Add BGM presets.
- [ ] 69. Add transition presets.
- [ ] 70. Add reusable CreativeProfiles.

## P3 — OSS Usability

- [ ] 71. Simplify Docker Quick Start.
- [ ] 72. Add Setup Wizard.
- [ ] 73. Add Provider Configuration UI.
- [ ] 74. Add API Key Configuration UI.
- [ ] 75. Add Sample Project.
- [ ] 76. Add a one-click demo Short.
- [ ] 77. Improve onboarding.
- [ ] 78. Improve error messages.
- [ ] 79. Add a first-video walkthrough.

## Dependency order

1. MediaAsset, upload, metadata, and library.
2. OutputSpec, EncodingProfile, ContentRecipe, and CreativeProfile.
3. MediaSegment, RenderPlan, and mixed image/video rendering.
4. Timeline persistence/compiler and Browser Preview.
5. Scene editor and simple timeline, with EditorCommand, Undo/Redo, and autosave.
6. AI Command Bar, validated proposals, diff, and editing.
7. Templates, quality presets, and OSS onboarding polish.

This is a dependency sequence, not permission for a single big-bang migration.
Implement and verify compatible vertical slices. Preserve existing asset data
and render callers while introducing the generic core.

## Delivery phases and acceptance criteria

These are delivery phases tracked via [issue #623](https://github.com/yeongseon/short-form-studio/issues/623)
and `priority:` labels, not GitHub milestones.

### 1. Better Short Generator

A user can create a Short MP4 from an idea and their own uploaded assets, using
both real and generated images/video, script, narration, captions, and BGM.
Demonstrate mixed image/video rendering with actual uploaded media; an AI-only
image slideshow is not sufficient. Existing generation/render clients still work.

### 2. Short-form editing studio

Scene View, Media Library, Preview, and a simple Timeline let a user trim,
replace, reorder, change transitions, and render the edited draft. Browser
preview and final render consume the same saved Timeline revision. Editing
includes shared commands and Undo/Redo rather than independent UI-only state.

### 3. AI-first Short Editor

AI Command Bar proposes structured edits with a diff. Apply produces an undoable
change; Cancel leaves the Timeline unchanged. Manual editing and accepted AI
editing share state, validation, and history.

### 4. OSS v1.0

Easy installation and setup, uploaded/generated media, Scene Editor, simple
Timeline, AI editing, stable rendering, documentation, and templates are usable
together. Validate the **under-10-minute first Short target** with a first-user
walkthrough; do not treat the target as an already measured result.

## Verification gates

**All implementation is TDD-based**, not only security work: write behavior tests
first, observe the intended failure (Red), implement the minimum fix (Green), then
refactor with tests passing. Record Red/Green commands and results in the PR.
Before behavior-preserving refactors, add characterization tests and capture the
passing baseline. Documentation-only work uses content and link validation.

Each implementation slice needs tests for its domain contracts and compatibility.
Preview/render parity, mixed-media output, command history, and persisted edits
need integration coverage as their features land.

Security-critical and production-hardening work follows [AGENTS.md](../AGENTS.md):
write failing boundary tests first, implement minimally, run the full API suite,
and obtain Oracle 100/100 before merge. Preserve workspace authorization and 404
anti-enumeration. This documentation change does not certify those future features.
