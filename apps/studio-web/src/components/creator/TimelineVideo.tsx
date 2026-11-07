import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { RenderSegment } from "../../api/timelinePreview";

// At 30 fps, resync after two source frames of drift. Never derive timeline time from media.
export const MEDIA_DRIFT_SECONDS = 2 / 30;

export function sourceTime(segment: RenderSegment, time: number): number {
  const start = segment.trim_start_seconds ?? 0;
  return Math.min(segment.trim_end_seconds ?? start + segment.duration_seconds,
    Math.max(start, start + time - segment.timeline_start_seconds));
}

type Props = {
  readonly segment: RenderSegment;
  readonly time: number;
  readonly playing: boolean;
  readonly onHold: (held: boolean) => void;
  readonly onReady: () => void;
  readonly onError: () => void;
  readonly onBlocked: (message: string) => void;
  readonly onEnded: () => void;
};

export function TimelineVideo(props: Props) {
  const { playing, onHold, onBlocked } = props;
  const videoRef = useRef<HTMLVideoElement>(null);
  const latest = useRef(props);
  latest.current = props;
  const seeking = useRef(false);
  const notified = useRef(false);
  const seekGeneration = useRef(0);
  const decodedFrameRequest = useRef<number | null>(null);
  const decodedTargetGeneration = useRef<number | null>(null);
  const fallbackTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [displayable, setDisplayable] = useState(false);

  const cancelReadinessGate = useCallback(() => {
    const video = videoRef.current;
    if (video && decodedFrameRequest.current !== null && "cancelVideoFrameCallback" in video) {
      video.cancelVideoFrameCallback(decodedFrameRequest.current);
    }
    decodedFrameRequest.current = null;
    if (fallbackTimer.current !== null) clearTimeout(fallbackTimer.current);
    fallbackTimer.current = null;
  }, []);

  const markReady = useCallback((video: HTMLVideoElement, target: number, generation: number) => {
    if (videoRef.current !== video || seekGeneration.current !== generation || seeking.current || video.seeking ||
      video.readyState < 2 || Math.abs(video.currentTime - target) > MEDIA_DRIFT_SECONDS) return false;
    setDisplayable(true);
    if (!notified.current) {
      notified.current = true;
      latest.current.onReady();
    }
    if (!latest.current.playing) latest.current.onHold(false);
    return true;
  }, []);

  const observeDecodedTarget = useCallback((video: HTMLVideoElement, target: number, generation: number) => {
    if (decodedFrameRequest.current !== null || typeof video.requestVideoFrameCallback !== "function") return;
    decodedFrameRequest.current = video.requestVideoFrameCallback((_, metadata) => {
      decodedFrameRequest.current = null;
      if (seekGeneration.current !== generation || videoRef.current !== video) return;
      if (Math.abs(metadata.mediaTime - target) > MEDIA_DRIFT_SECONDS) {
        observeDecodedTarget(video, target, generation);
        return;
      }
      decodedTargetGeneration.current = generation;
      markReady(video, target, generation);
    });
  }, [markReady]);

  const synchronize = useCallback(() => {
    const video = videoRef.current;
    if (!video || video.readyState < 1) return;
    const target = sourceTime(latest.current.segment, latest.current.time);
    if (Math.abs(video.currentTime - target) > 0.000001) {
      seekGeneration.current += 1;
      cancelReadinessGate();
      decodedTargetGeneration.current = null;
      seeking.current = true;
      setDisplayable(false);
      latest.current.onHold(true);
      video.pause();
      observeDecodedTarget(video, target, seekGeneration.current);
      video.currentTime = target;
    }
  }, [cancelReadinessGate, observeDecodedTarget]);

  const settled = () => {
    const video = videoRef.current;
    if (!video || seeking.current || video.seeking || video.readyState < 2) return;
    const target = sourceTime(latest.current.segment, latest.current.time);
    if (Math.abs(video.currentTime - target) > MEDIA_DRIFT_SECONDS) {
      synchronize();
      return;
    }
    const generation = seekGeneration.current;
    if (decodedTargetGeneration.current === generation) {
      markReady(video, target, generation);
      return;
    }
    if (typeof video.requestVideoFrameCallback === "function") {
      if (decodedFrameRequest.current !== null) {
        if ("cancelVideoFrameCallback" in video) video.cancelVideoFrameCallback(decodedFrameRequest.current);
        decodedFrameRequest.current = null;
      }
      observeDecodedTarget(video, target, generation);
      return;
    }
    if (fallbackTimer.current !== null) return;
    fallbackTimer.current = setTimeout(() => {
      fallbackTimer.current = null;
      markReady(video, target, generation);
    }, 100);
  };

  useLayoutEffect(() => {
    const video = videoRef.current;
    onHold(true);
    return () => { cancelReadinessGate(); video?.pause(); onHold(false); };
  }, [onHold, cancelReadinessGate]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const target = sourceTime(props.segment, props.time);
    if (!props.playing || Math.abs(video.currentTime - target) > MEDIA_DRIFT_SECONDS) synchronize();
  }, [props.segment, props.time, props.playing, synchronize]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    let cancelled = false;
    if (playing && displayable) {
      onHold(true);
      video.play().then(() => {
        if (!cancelled) onHold(false);
      }).catch((error: unknown) => {
        if (cancelled) return;
        video.pause();
        onHold(false);
        onBlocked(error instanceof Error
          ? `Preview paused: ${error.message}. Press Play to retry.`
          : "Preview paused. Press Play to retry.");
      });
    } else video.pause();
    return () => { cancelled = true; video.pause(); };
  }, [playing, displayable, onHold, onBlocked]);

  return <video
    ref={videoRef}
    data-testid="preview-media"
    src={props.segment.media_url ?? props.segment.source}
    onLoadedMetadata={() => { synchronize(); settled(); }}
    onLoadedData={settled}
    onSeeked={() => { seeking.current = false; settled(); }}
    onCanPlay={settled}
    onWaiting={() => {
      decodedTargetGeneration.current = null;
      props.onHold(true);
      setDisplayable(false);
      videoRef.current?.pause();
    }}
    onEnded={props.onEnded}
    onError={props.onError}
    playsInline muted preload="auto"
    style={{ width: "100%", height: "100%", objectFit: props.segment.fit_mode === "contain" ? "contain" : "cover" }}
  />;
}
