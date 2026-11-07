import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import TimelinePreview from "../components/creator/TimelinePreview";

let decodedFrameCallbacks = new WeakMap<HTMLVideoElement, VideoFrameRequestCallback[]>();

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

beforeEach(() => {
  decodedFrameCallbacks = new WeakMap();
  Object.defineProperty(HTMLVideoElement.prototype, "requestVideoFrameCallback", {
    configurable: true,
    value: vi.fn(function (this: HTMLVideoElement, callback: VideoFrameRequestCallback) {
      const callbacks = decodedFrameCallbacks.get(this) ?? [];
      callbacks.push(callback);
      decodedFrameCallbacks.set(this, callbacks);
      return callbacks.length;
    }),
  });
  Object.defineProperty(HTMLVideoElement.prototype, "cancelVideoFrameCallback", {
    configurable: true, value: vi.fn(),
  });
  const times = new WeakMap<HTMLMediaElement, number>();
  vi.spyOn(HTMLMediaElement.prototype, "currentTime", "get").mockImplementation(function (this: HTMLMediaElement) {
    return times.get(this) ?? 0;
  });
  vi.spyOn(HTMLMediaElement.prototype, "currentTime", "set").mockImplementation(function (
    this: HTMLMediaElement, value,
  ) { times.set(this, value); });
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
});

function renderTrimmedVideo(onReady: (revision: number | undefined) => void) {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
    timeline_revision: 17,
    segments: [{ kind: "video", source: "/video.mp4", timeline_start_seconds: 0,
      duration_seconds: 4, trim_start_seconds: 2, trim_end_seconds: 6 }],
    output_spec: { width: 1080, height: 1920, fps: 30 },
  })));
  render(<TimelinePreview projectId={72} onReady={onReady} />);
}

async function settledVideo() {
  const media = await screen.findByTestId("preview-media");
  if (!(media instanceof HTMLVideoElement)) throw new Error("Expected video placement");
  Object.defineProperty(media, "readyState", { configurable: true, value: 2 });
  fireEvent.loadedMetadata(media);
  fireEvent.loadedData(media);
  fireEvent.seeked(media);
  return media;
}

function presentDecodedFrame(video: HTMLVideoElement, mediaTime: number) {
  const callback = decodedFrameCallbacks.get(video)?.shift();
  if (!callback) throw new Error("Expected a pending decoded-frame callback");
  act(() => callback(0, { mediaTime } as VideoFrameCallbackMetadata));
}

it.each(["wrong source", "internal seek", "browser seeking", "drifted completion"])(
  "withholds readiness for %s until the trimmed target frame is decoded", async (condition) => {
    // Given a first trimmed video in one of the unsettled media states.
    const onReady = vi.fn();
    renderTrimmedVideo(onReady);
    const media = await screen.findByTestId("preview-media");
    if (!(media instanceof HTMLVideoElement)) throw new Error("Expected video placement");
    Object.defineProperty(media, "readyState", { configurable: true, value: 2 });
    if (condition === "wrong source") fireEvent.loadedData(media);
    fireEvent.loadedMetadata(media);
    if (condition === "browser seeking") {
      Object.defineProperty(media, "seeking", { configurable: true, value: true });
      fireEvent.seeked(media);
    }
    if (condition === "drifted completion") {
      media.currentTime = 0;
      fireEvent.seeked(media);
    }
    fireEvent.loadedData(media);

    // When the seek settles and its target frame is decoded.
    expect(onReady).not.toHaveBeenCalled();
    Object.defineProperty(media, "seeking", { configurable: true, value: false });
    fireEvent.seeked(media);
    if (condition === "drifted completion") presentDecodedFrame(media, 2);
    presentDecodedFrame(media, 2);

    // Then readiness is released for the matching revision.
    expect(onReady).toHaveBeenCalledWith(17);
  },
);

it("arms decoded-frame observation before waiting for seek completion", async () => {
  // Given the first trimmed video has loaded metadata but has not emitted seeked.
  renderTrimmedVideo(vi.fn());
  const media = await screen.findByTestId("preview-media");
  if (!(media instanceof HTMLVideoElement)) throw new Error("Expected video placement");
  Object.defineProperty(media, "readyState", { configurable: true, value: 1 });

  // When metadata starts the trim-target seek.
  fireEvent.loadedMetadata(media);

  // Then frame observation is already armed so the first presented target frame cannot be missed.
  expect(media.requestVideoFrameCallback).toHaveBeenCalledOnce();
});

it("withholds readiness until a callback observes the decoded trim target", async () => {
  // Given the first placement has settled at a trimmed source target.
  const onReady = vi.fn();
  renderTrimmedVideo(onReady);
  const video = await settledVideo();

  // When no callback, then a stale decoded frame, has been observed.
  expect(onReady).not.toHaveBeenCalled();
  presentDecodedFrame(video, 0);

  // Then approval remains blocked until a callback observes the target frame.
  expect(onReady).not.toHaveBeenCalled();
  presentDecodedFrame(video, 2);
  expect(onReady).toHaveBeenCalledWith(17);
});

it("keeps observing when the target frame arrives before browser seeking settles", async () => {
  // Given the first target frame is decoded while Chromium still reports an active seek.
  const onReady = vi.fn();
  renderTrimmedVideo(onReady);
  const video = await settledVideo();
  Object.defineProperty(video, "seeking", { configurable: true, value: true });

  // When that callback arrives before the browser settles, then no readiness is published yet.
  presentDecodedFrame(video, 2);
  expect(onReady).not.toHaveBeenCalled();

  // When Chromium later presents the target without emitting another media event.
  Object.defineProperty(video, "seeking", { configurable: true, value: false });
  presentDecodedFrame(video, 2);

  // Then the continued frame observation releases readiness safely.
  expect(onReady).toHaveBeenCalledWith(17);
});

it("ignores a decoded-frame callback from an obsolete seek generation", async () => {
  // Given readiness is waiting on the first seek generation.
  const onReady = vi.fn();
  renderTrimmedVideo(onReady);
  const video = await settledVideo();

  // When a timeline seek supersedes that generation before its callback runs.
  fireEvent.change(screen.getByTestId("preview-seek"), { target: { value: "1" } });
  presentDecodedFrame(video, 2);

  // Then only the decoded frame requested for the new target can release readiness.
  expect(onReady).not.toHaveBeenCalled();
  fireEvent.seeked(video);
  presentDecodedFrame(video, 3);
  expect(onReady).toHaveBeenCalledWith(17);
});

it("uses a bounded readiness fallback when decoded-frame callbacks are unavailable", async () => {
  // Given a browser without requestVideoFrameCallback.
  vi.useFakeTimers();
  Object.defineProperty(HTMLVideoElement.prototype, "requestVideoFrameCallback", {
    configurable: true, value: undefined,
  });
  const onReady = vi.fn();
  renderTrimmedVideo(onReady);
  await act(async () => {});

  // When media settlement starts the bounded fallback.
  const video = screen.getByTestId("preview-media");
  Object.defineProperty(video, "readyState", { configurable: true, value: 2 });
  fireEvent.loadedMetadata(video);
  fireEvent.loadedData(video);
  fireEvent.seeked(video);
  expect(onReady).not.toHaveBeenCalled();
  act(() => vi.advanceTimersByTime(100));

  // Then readiness is released after the fallback confirms the settled target.
  expect(onReady).toHaveBeenCalledWith(17);
});
