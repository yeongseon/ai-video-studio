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
- Nominal FPS differs by at most 0.01 FPS. Cheap `nb_frames` metadata (fallback:
  rounded stream duration × average FPS) must match rounded plan duration × FPS
  within max(two frames, ceiling(timing tolerance × FPS)). Independently, samples
  must match nominal FPS × stream duration within two endpoint frames and average
  FPS × stream duration within one frame. No allowance grows with segment count;
  sparse timestamp-preserving media cannot conceal missing samples at the endpoint.
- Format, video and required audio durations must each match expected plan duration
  within **max(0.100 seconds, 2 / profile FPS)**, plus 1 microsecond for decimal
  representation and 1 nanosecond of machine-precision comparison slack. This aggregate ceiling never grows with segment count.
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
and waits for the direct child. Exit observation uses Linux/POSIX `waitid(WNOWAIT)`
without reaping, anchoring PGID to the unreaped leader through the final signal.
SIGKILL and the bounded one-second reap run in nested finally blocks even when
the grace wait is interrupted. No Popen context-manager unbounded wait is used.
KeyboardInterrupt and soft-limit exceptions
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

Oracle r1 remediation measurements (FFmpeg 4.4.2): 30 exact-CFR image segments
totaling 5 seconds with AAC produced 150 frames at 30 FPS and 300 at 60 FPS;
video duration 5.000011, format 5.001000, AAC duration 4.969002 seconds. Worst
observed rounding was 31 ms, below the 100 ms floor. Segment count does not
license larger aggregate error; non-frame-aligned plans exceeding the ceiling
fail closed. Boundary pairs cover 15/24/25/30/60 FPS and 1/5/30 segments.
After byte-bounded draining, measured validation on the loaded local host:
30 seconds **633.93 / 443.40 / 376.34 ms**; 90 seconds
**412.59 / 546.01 / 445.81 ms**. This exceeds the earlier ~200 ms target but
remains subsecond and independent of short duration; no full-media scan added.

## Oracle round-2 cumulative frame correction

Each segment receives `round(cumulative_end × FPS) - round(cumulative_start × FPS)`
frames, using accurately summed cumulative boundaries. FFmpeg output `-frames:v`
enforces that budget after CFR conversion/zoompan; concat duration directives are
budget/FPS, not independently rounded source milliseconds. Timeline video segments
receive the same budget. Segments occupying zero output frames fail explicitly.
Tiny intermediates are explicitly muxed MP4 (internal `.ts` filenames remain for
compatibility), avoiding ambiguous tiny-MPEG-TS probing. Video endpoints receive
at most one cloned endpoint frame before the exact output cap. No tolerance widened.

The real matrix covers 225 image/mixed/Ken-Burns cases at 15/24/25/30/60 FPS,
durations 0.1/0.2/1/one-sixth/2.4 seconds, counts 1/5/30. Total frames equal rounded
aggregate plan duration × FPS; alternating 30 × 0.1 seconds at 60 FPS yields
180 frames, not 195. Sparse 120/150-frame fixtures reject at every segment count.
Idle validation (4.4.2, same 540×960 H264/AAC fixtures): 30 seconds
**210.42 / 235.80 / 235.94 ms**; 90 seconds **213.43 / 223.26 / 207.12 ms**.

The shipped worker-image topology is verified by CI's `worker-topology-runtime`
job (Dockerfile runtime target, pinned Celery, solo and scaled prefork scenarios).
Local process topology tests run with the prepared Python 3.12 venv; image topology
is delegated to CI, without using another local slot's ports or containers.
