import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import TimelinePreview from "../components/creator/TimelinePreview";

afterEach(() => { vi.restoreAllMocks(); });

beforeEach(() => {
  const times = new WeakMap<HTMLMediaElement, number>();
  vi.spyOn(HTMLMediaElement.prototype, "currentTime", "get").mockImplementation(function (this: HTMLMediaElement) {
    return times.get(this) ?? 0;
  });
  vi.spyOn(HTMLMediaElement.prototype, "currentTime", "set").mockImplementation(function (this: HTMLMediaElement, value) {
    times.set(this, value);
  });
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
});

function respond(kind: "image" | "video") {
  vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify({
    timeline_revision: 17,
    segments: [{ kind, source: "workspaces/7/assets/sample", media_url: "/api/creator/projects/72/assets/9/content",
      timeline_start_seconds: 0, duration_seconds: 5, fit_mode: "contain" }],
    output_spec: { width: 1080, height: 1920, fps: 30 },
    narration_path: null, music_path: null, subtitle_path: null,
  })));
}

it.each(["image", "video"] as const)("uses the served %s URL and signals its revision only after media loads", async (kind) => {
  respond(kind);
  const onReady = vi.fn();
  render(<TimelinePreview projectId={72} onReady={onReady} />);
  const media = await screen.findByTestId("preview-media");
  expect(media).toHaveAttribute("src", "/api/creator/projects/72/assets/9/content");
  expect(onReady).not.toHaveBeenCalled();
  if (kind === "image") fireEvent.load(media);
  else {
    if (!(media instanceof HTMLVideoElement)) throw new Error("Expected video");
    decoded(media);
  }
  expect(onReady).toHaveBeenCalledWith(17);
});

function mixed(firstVideo = false) {
  const image = { kind: "image", source: "/image.png", timeline_start_seconds: 0, duration_seconds: 4 };
  const video = { kind: "video", source: "/video.mp4", timeline_start_seconds: firstVideo ? 0 : 4,
    duration_seconds: 4, trim_start_seconds: 2, trim_end_seconds: 6 };
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
    timeline_revision: 17, segments: [firstVideo ? video : image, ...(firstVideo ? [] : [video]),
      { ...video, timeline_start_seconds: firstVideo ? 4 : 8, trim_start_seconds: 1, trim_end_seconds: 5 },
      { ...image, timeline_start_seconds: firstVideo ? 8 : 12, duration_seconds: 2 }],
    output_spec: { width: 1080, height: 1920, fps: 30 },
    narration_path: null, music_path: null, subtitle_path: null,
  })));
}

function videoElement() {
  const media = screen.getByTestId("preview-media");
  if (!(media instanceof HTMLVideoElement)) throw new Error("Expected video placement");
  return media;
}

function decoded(video: HTMLVideoElement) {
  Object.defineProperty(video, "readyState", { configurable: true, value: 2 });
  fireEvent.loadedMetadata(video);
  fireEvent.loadedData(video);
  fireEvent.seeked(video);
}

function seek(time: number) {
  fireEvent.change(screen.getByTestId("preview-seek"), { target: { value: String(time) } });
}

it("seeks a trimmed video to source-local time and starts/stops its media clock", async () => {
  // Given a mixed timeline whose video starts two seconds into its source.
  mixed();
  render(<TimelinePreview projectId={72} />);
  await screen.findByTestId("preview-media");
  // When seeking into that placement and toggling transport.
  seek(5);
  const video = videoElement();
  decoded(video);
  // Then source-local time, not timeline time or zero, drives the media.
  expect(video.currentTime).toBe(3);
  fireEvent.click(screen.getByTestId("preview-play-toggle"));
  await waitFor(() => expect(video.play).toHaveBeenCalled());
  vi.mocked(video.pause).mockClear();
  fireEvent.click(screen.getByTestId("preview-play-toggle"));
  expect(video.pause).toHaveBeenCalled();
});

it("withholds first-video readiness until the trimmed target frame is decoded", async () => {
  mixed(true);
  const onReady = vi.fn();
  render(<TimelinePreview projectId={72} onReady={onReady} />);
  await screen.findByTestId("preview-media");
  const video = videoElement();
  fireEvent.loadedData(video);
  expect(onReady).not.toHaveBeenCalled();
  Object.defineProperty(video, "readyState", { configurable: true, value: 1 });
  fireEvent.loadedMetadata(video);
  expect(video.currentTime).toBe(2);
  expect(onReady).not.toHaveBeenCalled();
  decoded(video);
  expect(onReady).toHaveBeenCalledWith(17);
});

it("pauses outgoing same-source placements and realigns backward seeks", async () => {
  mixed(true);
  render(<TimelinePreview projectId={72} />);
  await screen.findByTestId("preview-media");
  const first = videoElement();
  decoded(first);
  seek(5);
  const second = videoElement();
  decoded(second);
  expect(second).not.toBe(first);
  expect(first.pause).toHaveBeenCalled();
  expect(second.currentTime).toBe(2);
  seek(1);
  decoded(videoElement());
  expect(videoElement().currentTime).toBe(3);
});

it("reports a rejected play request as paused instead of advancing silently", async () => {
  mixed(true);
  vi.mocked(HTMLMediaElement.prototype.play).mockRejectedValue(new DOMException("Blocked", "NotAllowedError"));
  render(<TimelinePreview projectId={72} />);
  await screen.findByTestId("preview-media");
  decoded(videoElement());
  fireEvent.click(screen.getByTestId("preview-play-toggle"));
  expect(await screen.findByRole("status")).toHaveTextContent(/paused.*play/i);
  expect(screen.getByTestId("preview-play-toggle")).toHaveAttribute("data-playing", "false");
});

it("pauses on visibility loss and unmount", async () => {
  mixed(true);
  const view = render(<TimelinePreview projectId={72} />);
  await screen.findByTestId("preview-media");
  const video = videoElement();
  decoded(video);
  fireEvent.click(screen.getByTestId("preview-play-toggle"));
  Object.defineProperty(document, "hidden", { configurable: true, value: true });
  fireEvent(document, new Event("visibilitychange"));
  expect(screen.getByTestId("preview-play-toggle")).toHaveAttribute("data-playing", "false");
  Object.defineProperty(document, "hidden", { configurable: true, value: false });
  vi.mocked(video.pause).mockClear();
  view.unmount();
  expect(video.pause).toHaveBeenCalled();
});

it("holds the logical clock while buffering and resumes after the target is displayable", async () => {
  mixed(true);
  render(<TimelinePreview projectId={72} />);
  await screen.findByTestId("preview-media");
  const video = videoElement();
  decoded(video);
  let frame: FrameRequestCallback = () => {};
  vi.spyOn(globalThis, "requestAnimationFrame").mockImplementation((callback) => { frame = callback; return 1; });
  fireEvent.click(screen.getByTestId("preview-play-toggle"));
  await act(async () => {});
  fireEvent.waiting(video);
  act(() => { frame(1000); frame(2000); });
  expect(screen.getByTestId("preview-seek")).toHaveValue("0");
  expect(video.pause).toHaveBeenCalled();
  fireEvent.canPlay(video);
  await act(async () => {});
  act(() => { frame(3000); frame(3100); });
  expect(Number(screen.getByTestId("preview-seek").getAttribute("value"))).toBeGreaterThan(0);
});

it("advances image-video-image boundaries and replays from the end", async () => {
  mixed();
  render(<TimelinePreview projectId={72} />);
  await screen.findByTestId("preview-media");
  let frame: FrameRequestCallback = () => {};
  vi.spyOn(globalThis, "requestAnimationFrame").mockImplementation((callback) => { frame = callback; return 1; });
  seek(3.9);
  fireEvent.click(screen.getByTestId("preview-play-toggle"));
  act(() => { frame(0); frame(100); });
  const video = videoElement();
  decoded(video);
  await act(async () => {});
  expect(video.currentTime).toBe(2);
  expect(video.play).toHaveBeenCalled();
  fireEvent.ended(video);
  expect(screen.getByTestId("preview-play-toggle")).toHaveAttribute("data-playing", "false");
  seek(12);
  expect(screen.getByTestId("preview-media").tagName).toBe("IMG");
  expect(video.pause).toHaveBeenCalled();
  seek(14);
  fireEvent.click(screen.getByTestId("preview-play-toggle"));
  expect(screen.getByTestId("preview-seek")).toHaveValue("0");
});

it("revokes readiness if a later placement fails", async () => {
  mixed();
  const onUnavailable = vi.fn();
  const onReady = vi.fn();
  render(<TimelinePreview projectId={72} onReady={onReady} onUnavailable={onUnavailable} />);
  fireEvent.load(await screen.findByTestId("preview-media"));
  expect(onReady).toHaveBeenCalledWith(17);
  onUnavailable.mockClear();
  seek(5);
  fireEvent.error(videoElement());
  expect(onUnavailable).toHaveBeenCalledOnce();
  expect(await screen.findByRole("alert")).toHaveTextContent(/reload/i);
});

it("discloses the unpreviewed audio, captions, transitions and framing differences", async () => {
  respond("image");
  render(<TimelinePreview projectId={72} />);
  await screen.findByTestId("preview-media");
  expect(screen.getByTestId("preview-fidelity")).toHaveTextContent(/audio.*captions.*transitions/i);
  expect(screen.getByTestId("preview-fidelity")).toHaveTextContent(/framing/i);
});

it("keeps gap boundaries half-open during playback", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
    segments: [{ kind: "image", source: "/a.png", timeline_start_seconds: 0, duration_seconds: 1 },
      { kind: "image", source: "/b.png", timeline_start_seconds: 2, duration_seconds: 1 }],
    output_spec: { width: 1080, height: 1920 },
  })));
  render(<TimelinePreview projectId={72} />);
  await screen.findByTestId("preview-media");
  let frame: FrameRequestCallback = () => {};
  vi.spyOn(globalThis, "requestAnimationFrame").mockImplementation((callback) => { frame = callback; return 1; });
  seek(1);
  expect(screen.getByTestId("preview-empty")).toBeInTheDocument();
  fireEvent.click(screen.getByTestId("preview-play-toggle"));
  act(() => { frame(0); frame(2000); });
  expect(screen.getByTestId("preview-media")).toHaveAttribute("src", "/b.png");
  expect(screen.getByTestId("preview-seek")).toHaveValue("2");
});

it.each(["image", "video"] as const)("shows a recoverable error when the %s cannot load", async (kind) => {
  respond(kind);
  const onReady = vi.fn();
  render(<TimelinePreview projectId={72} onReady={onReady} />);
  fireEvent.error(await screen.findByTestId("preview-media"));
  expect(await screen.findByRole("alert")).toHaveTextContent(/reload/i);
  expect(screen.getByTestId("timeline-preview")).toHaveAttribute("data-status", "error");
  expect(onReady).not.toHaveBeenCalled();
});

it("bounds the portrait preview to the viewport while retaining its aspect ratio", async () => {
  respond("image");
  render(<TimelinePreview projectId={72} />);
  const stage = await screen.findByTestId("preview-stage");
  expect(stage).toHaveStyle({ "max-height": "60vh", "max-width": "100%", "aspect-ratio": "1080 / 1920" });
});

it("does not mark a replacement preview ready merely because the old image loaded", async () => {
  respond("image");
  const onReady = vi.fn();
  const { rerender } = render(<TimelinePreview projectId={72} onReady={onReady} />);
  fireEvent.load(await screen.findByTestId("preview-media"));
  onReady.mockClear();
  rerender(<TimelinePreview projectId={73} onReady={onReady} />);
  await waitFor(() => expect(screen.getByTestId("timeline-preview")).toHaveAttribute("data-status", "ready"));
  expect(onReady).not.toHaveBeenCalled();
});
