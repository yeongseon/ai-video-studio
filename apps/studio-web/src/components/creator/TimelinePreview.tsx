/**
 * TimelinePreview — browser preview of a compiled Timeline RenderPlan.
 *
 * Uses the compiled visual placements, not final-render parity. Provides playback,
 * seek, and aspect handling with deterministic timing: the displayed segment is
 * a pure function of currentTime via segmentAtTime(), so timing does not depend
 * on wall-clock in the mapping (rAF only advances currentTime during playback).
 *
 * Three states via data-status: loading, error, ready.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { TimelineVideo } from "./TimelineVideo";

import {
  fetchTimelinePreview,
  type RenderPlan,
  type RenderSegment,
} from "../../api/timelinePreview";

export interface TimelinePreviewProps {
  projectId: number;
  output?: string;
  encoding?: string;
  onReady?: (revision: number | undefined) => void;
  onUnavailable?: () => void;
}

/** Total timeline duration = furthest segment end. */
export function totalDuration(plan: RenderPlan): number {
  return plan.segments.reduce(
    (max, s) => Math.max(max, s.timeline_start_seconds + s.duration_seconds),
    0,
  );
}

/**
 * Pure mapping from a playhead time to the active segment. A segment owns the
 * half-open window [start, start + duration); a boundary belongs to the next
 * segment, and times past the end return null. Segments are ordered and
 * non-overlapping (enforced by the domain), so the first match wins.
 */
export function segmentAtTime(plan: RenderPlan, t: number): RenderSegment | null {
  for (const seg of plan.segments) {
    const start = seg.timeline_start_seconds;
    const end = start + seg.duration_seconds;
    if (t >= start && t < end) {
      return seg;
    }
  }
  return null;
}

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; plan: RenderPlan };

export default function TimelinePreview({
  projectId,
  output,
  encoding,
  onReady,
  onUnavailable,
}: TimelinePreviewProps) {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [currentTime, setCurrentTime] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const held = useRef(false);
  const onHold = useCallback((value: boolean) => { held.current = value; }, []);
  const onBlocked = useCallback((message: string) => { setPlaying(false); setNotice(message); }, []);
  const rafRef = useRef<number | null>(null);
  const lastTsRef = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    setPlaying(false);
    onUnavailable?.();
    fetchTimelinePreview(projectId, { output, encoding })
      .then((plan) => {
        if (!cancelled) {
          setState({ status: "ready", plan });
          setCurrentTime(0);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : "Failed to load preview";
          setState({ status: "error", message });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, output, encoding, onUnavailable]);

  const total = state.status === "ready" ? totalDuration(state.plan) : 0;

  useEffect(() => {
    const hide = () => { if (document.hidden) setPlaying(false); };
    document.addEventListener("visibilitychange", hide);
    return () => document.removeEventListener("visibilitychange", hide);
  }, []);

  useEffect(() => {
    if (!playing || state.status !== "ready") {
      return;
    }
    const step = (ts: number) => {
      if (held.current) {
        lastTsRef.current = null;
        rafRef.current = requestAnimationFrame(step);
        return;
      }
      if (lastTsRef.current !== null) {
        const delta = (ts - lastTsRef.current) / 1000;
        setCurrentTime((prev) => {
          const active = segmentAtTime(state.plan, prev);
          const boundary = active ? active.timeline_start_seconds + active.duration_seconds
            : state.plan.segments.find((segment) => segment.timeline_start_seconds > prev)?.timeline_start_seconds ?? total;
          const next = Math.min(prev + delta, boundary);
          if (next >= total) {
            setPlaying(false);
            return total;
          }
          return next;
        });
      }
      lastTsRef.current = ts;
      rafRef.current = requestAnimationFrame(step);
    };
    rafRef.current = requestAnimationFrame(step);
    return () => {
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
      }
      lastTsRef.current = null;
    };
  }, [playing, state, total]);

  if (state.status === "loading") {
    return (
      <div data-testid="timeline-preview" data-status="loading">
        Loading preview…
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div data-testid="timeline-preview" data-status="error" role="alert">
        {state.message}
      </div>
    );
  }

  const { plan } = state;
  const active = segmentAtTime(plan, currentTime);
  const placement = active ? plan.segments.indexOf(active) : -1;
  const { width, height } = plan.output_spec;
  const mediaLoaded = () => {
    if (active === plan.segments[0]) onReady?.(plan.timeline_revision);
  };
  const mediaFailed = () => {
    onUnavailable?.();
    setPlaying(false);
    setState({ status: "error", message: "Preview media could not load. Reload the project to retry; if it still fails, check asset access before approving." });
  };

  return (
    <div data-testid="timeline-preview" data-status="ready">
      <div
        data-testid="preview-stage"
        data-aspect={`${width}x${height}`}
        style={{
          aspectRatio: `${width} / ${height}`,
          width: `min(100%, ${60 * width / height}vh)`,
          maxHeight: "60vh",
          maxWidth: "100%",
          background: "#000",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          overflow: "hidden",
        }}
      >
        {active === null ? (
          <div data-testid="preview-empty">No content at this time</div>
        ) : active.kind === "image" ? (
          <img
            key={placement}
            data-testid="preview-media"
            src={active.media_url ?? active.source}
            onLoad={mediaLoaded}
            onError={mediaFailed}
            alt=""
            style={{
              width: "100%",
              height: "100%",
              objectFit: active.fit_mode === "contain" ? "contain" : "cover",
            }}
          />
        ) : (
          <TimelineVideo
            key={placement}
            segment={active}
            time={currentTime}
            playing={playing}
            onHold={onHold}
            onReady={mediaLoaded}
            onError={mediaFailed}
            onBlocked={onBlocked}
            onEnded={() => setPlaying(false)}
          />
        )}
      </div>

      <div>
        <button
          type="button"
          data-testid="preview-play-toggle"
          data-playing={playing ? "true" : "false"}
          onClick={() => {
            setNotice(null);
            if (currentTime >= total) setCurrentTime(0);
            setPlaying((p) => !p);
          }}
          style={{ minWidth: 64, minHeight: 44 }}
        >
          {playing ? "Pause" : "Play"}
        </button>
        <input
          type="range"
          role="slider"
          data-testid="preview-seek"
          aria-label="Timeline position"
          min={0}
          max={total}
          step={0.1}
          value={currentTime}
          onChange={(e) => {
            setPlaying(false);
            setCurrentTime(Number(e.target.value));
          }}
        />
        <span data-testid="preview-time">
          {currentTime.toFixed(1)} / {total.toFixed(1)}
        </span>
      </div>
      {notice && <p role="status">{notice}</p>}
      <p data-testid="preview-fidelity">Preview shows trimmed visual frames only. Audio, captions and transitions are not previewed; final-render framing may differ.</p>

      {(plan.narration_path !== null || plan.music_path !== null) && (
        <audio
          data-testid="preview-audio-layer"
          src={plan.narration_path ?? plan.music_path ?? undefined}
          muted
          hidden
        />
      )}
      {plan.subtitle_path !== null && (
        <div data-testid="preview-caption-indicator">Captions</div>
      )}
    </div>
  );
}
