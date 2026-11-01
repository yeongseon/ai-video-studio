# Final render output integrity

Every accepted `FFmpegService.render` output (including approved saved Timeline
renders) and `render_plan` output is checked before a unique temporary MP4 is
atomically renamed. Nonzero encoder/concat exits always fail. Validation raises
`MediaIntegrityError`, a `RuntimeError`, preserving existing worker error/retry
mapping and usage settlement; no upload or artifact creation occurs on rejection.
Approval, ownership, revision and cancellation fences remain upstream and unchanged.

## Rules and tolerances

- Exactly one video stream; required audio derives from `RenderInput.audio_path`
  or the plan's narration/music layers. A plan renderer that does not implement
  a requested audio layer fails closed rather than silently dropping it.
- Positive finite format and stream durations, positive dimensions, valid finite
  rational FPS, and positive file size must parse from bounded ffprobe JSON.
- Dimensions equal the profile; codec families map `libx264` to `h264`, `libx265`
  to `hevc`, and `libmp3lame` to `mp3` (AAC is `aac`).
- Nominal FPS differs by at most 0.01 FPS. Average-rate drift times video duration
  is at most segment count plus one frame, absorbing container endpoint rounding
  without allowing a proportional loss of samples on longer shorts.
- Format, video and required audio durations must each match expected plan duration
  within **max(0.100 seconds, 2 / profile FPS)**, plus 1 microsecond for decimal
  representation. This aggregate ceiling never grows with segment count.
  Independently rounded plans that exceed it fail closed rather than licensing
  truncation; exact-CFR tiny segments concatenate without cumulative loss. Legacy durations
  derive from narration; Timeline narration is padded to the approved plan.
  `-shortest` is not permission to truncate the plan. Explicit segment trim and
  final `-t` bound zoompan expansion and muxer buffering; short narration fails.
- File size is at least 1 KiB and equals ffprobe's size. There is no bitrate floor:
  static small media is legitimate and the legacy 10 KiB assumption was unsound.
- Decode the final second (or entire clip when shorter) with one decoder thread,
  `-xerror`, a two-second output cap, and framehash output. Any nonzero exit or
  error-level stderr fails. Actual video and required audio decoded samples must
  exist; an error-free empty decode is not assurance. Framehash is a null-like sink
  that proves samples without retaining raw frames. This is bounded tail assurance,
  **not** a claim that every middle frame has been decoded.

Both validation subprocesses have 15-second timeouts and isolated sessions;
stdout is limited to 1 MiB and stderr to 64 KiB, drained in 8 KiB reads. Overflow
rejects output. Every exit terminates the group (SIGTERM, 200 ms grace, SIGKILL)
and waits for the direct child. KeyboardInterrupt and soft-limit exceptions
propagate unchanged after cleanup, not as ordinary media failures.
timeout/unavailable binaries reject output. ffprobe selects only required fields,
not full packet/frame scans. Temporary output and segment directories are cleaned
in `finally`, including validation/cancellation exceptions; prior final files are
not accepted as new renders. No dependency, API, task-name or quota-policy change.

## History and measurements

Commit `645bd3e` (2026-06-06) changed rendering to two-pass concat and retained
the recovery introduced by `ec2de1d` (2026-06-05). The predecessor claimed ENOMEM
during filter teardown, but its tests wrote zeros and mocked positive duration.
Neither commit has an associated PR returned by GitHub's commits/pulls endpoint;
neither provides a deterministic real benign nonzero fixture. No benign case was
reproduced, so recovery is removed, not allowlisted.

Local FFmpeg 4.4.2, real 540×960/30 FPS H.264/AAC lavfi fixtures, three validations:
30 seconds: **200.24 / 206.36 / 200.59 ms**; 90 seconds:
**203.20 / 206.79 / 203.33 ms**. This excludes fixture encoding and includes both
subprocesses, metadata parsing and tail decode. Future #884 chunk callers can
reuse `MediaExpectation` and `validate_media` with their own expected chunk timing;
chunking is not implemented here.

Only coded dimensions are currently validated. SAR and rotation/display-matrix
presentation metadata are not checked; callers must not interpret this as full
displayed-geometry validation. Pipeline outputs are square-pixel/unrotated, but
arbitrary provider media would require additional presentation rules.

Integrity failure strings contain fixed rule identifiers only. Numeric expected
and observed durations are separate exception attributes; legacy and Timeline
workers classify this typed error as terminal ProviderError before string-based
rate-limit heuristics. Quota settlement still uses the existing terminal/unknown
provider-outcome path.

The shipped worker-image topology is verified by CI's `worker-topology-runtime`
job (Dockerfile runtime target, pinned Celery, solo and scaled prefork scenarios).
Local process topology tests run with the prepared Python 3.12 venv; image topology
is delegated to CI, without using another local slot's ports or containers.
