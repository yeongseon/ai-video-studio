import { useEffect, useLayoutEffect, useRef, useState } from "react";
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
  const [displayable, setDisplayable] = useState(false);

  const synchronize = () => {
    const video = videoRef.current;
    if (!video || video.readyState < 1) return;
    const target = sourceTime(latest.current.segment, latest.current.time);
    if (Math.abs(video.currentTime - target) > 0.000001) {
      seeking.current = true;
      setDisplayable(false);
      latest.current.onHold(true);
      video.pause();
      video.currentTime = target;
    }
  };

  const settled = () => {
    const video = videoRef.current;
    if (!video || seeking.current || video.seeking || video.readyState < 2) return;
    const target = sourceTime(latest.current.segment, latest.current.time);
    if (Math.abs(video.currentTime - target) > MEDIA_DRIFT_SECONDS) {
      synchronize();
      return;
    }
    setDisplayable(true);
    latest.current.onReady();
    if (!latest.current.playing) latest.current.onHold(false);
  };

  useLayoutEffect(() => {
    const video = videoRef.current;
    onHold(true);
    return () => { video?.pause(); onHold(false); };
  }, [onHold]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const target = sourceTime(props.segment, props.time);
    if (!props.playing || Math.abs(video.currentTime - target) > MEDIA_DRIFT_SECONDS) synchronize();
  }, [props.segment, props.time, props.playing]);

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
