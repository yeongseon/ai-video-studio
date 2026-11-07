import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import ReviewPage from "../pages/ReviewPage";
import ProjectHeader from "../pages/project/ProjectHeader";
import type { ProjectDetail } from "../types/api";

afterEach(() => { vi.restoreAllMocks(); });

function setup(options: { stage?: string; publishStatus?: number; timeline?: boolean } = {}) {
  let stage = options.stage ?? "FINAL_REVIEW";
  const spy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.endsWith("/approve-final")) {
      if (options.publishStatus === 409) return new Response(JSON.stringify({ detail: { error: "Final review changed" } }), { status: 409 });
      stage = "PUBLISHED";
      return new Response(JSON.stringify({ current_stage: stage }));
    }
    if (url.endsWith("/preview")) return new Response(JSON.stringify({
      run_id: 41, current_stage: stage,
      video: { id: 89, path: "/tmp/opencode/short-form-refactor/data/browser-artifacts/41/render/output.mp4", render_profile: "shorts_default", created_at: "2026-09-22T00:00:00Z" },
      audio: null, subtitle: null,
    }));
    if (url.endsWith("/runs/41")) return new Response(JSON.stringify({ id: 41, project_id: 7, current_stage: stage, status: "paused", metadata: options.timeline === false ? {} : { render_source: "timeline" } }));
    if (url.endsWith("/storyboard")) return new Response(JSON.stringify({ paragraphs: [] }));
    void init;
    return new Response(JSON.stringify({ scenes: [], script: null }));
  });
  render(<MemoryRouter initialEntries={["/review/41"]}><Routes>
    <Route path="/review/:runId" element={<ReviewPage />} />
  </Routes></MemoryRouter>);
  return spy;
}

it.each([true, false])("uses the owned artifact endpoint for video and download (timeline=%s)", async (timeline) => {
  setup({ timeline });
  const section = await screen.findByTestId("review-video-section");
  expect(section.querySelector("video")).toHaveAttribute("src", "/api/creator/runs/41/artifacts/89/download");
  expect(within(section).getByRole("link", { name: /Download video/i })).toHaveAttribute("href", "/api/creator/runs/41/artifacts/89/download");
});

it("shows only the timeline workflow and does not fetch legacy generation outputs", async () => {
  const spy = setup();
  await screen.findByTestId("review-video-section");
  expect(screen.queryByTestId("review-storyboard-section")).not.toBeInTheDocument();
  expect(screen.queryByTestId("review-script-section")).not.toBeInTheDocument();
  const steps = screen.getByRole("list", { name: "Timeline workflow" });
  expect(within(steps).getAllByRole("listitem")).toHaveLength(4);
  expect(within(steps).getByText("Final review")).toHaveAttribute("aria-current", "step");
  expect(spy.mock.calls.some(([url]) => /\/(script|storyboard|visual-plan|visual-assets)$/.test(String(url)))).toBe(false);
});

it("publishes once only after explicit final approval", async () => {
  const spy = setup();
  const publish = await screen.findByRole("button", { name: "Approve final & publish" });
  expect(spy.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  act(() => { fireEvent.click(publish); fireEvent.click(publish); });
  await waitFor(() => expect(screen.queryByRole("button", { name: "Approve final & publish" })).not.toBeInTheDocument());
  expect(spy.mock.calls.filter(([url]) => String(url).endsWith("/approve-final"))).toHaveLength(1);
  expect(spy.mock.calls.find(([url]) => String(url).endsWith("/approve-final"))?.[1]).toMatchObject({ method: "POST" });
  expect(screen.getByRole("status")).toHaveTextContent("Published");
});

it("keeps failed publish visible and retryable", async () => {
  setup({ publishStatus: 409 });
  fireEvent.click(await screen.findByRole("button", { name: "Approve final & publish" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Final review changed");
  expect(screen.getByRole("button", { name: "Approve final & publish" })).toBeEnabled();
});

it("does not offer publishing before final review", async () => {
  setup({ stage: "RENDER_GENERATING" });
  await screen.findByTestId("review-video-section");
  expect(screen.queryByRole("button", { name: "Approve final & publish" })).not.toBeInTheDocument();
});

it.each(["SCRIPT_REVIEW", "VISUAL_PLAN_REVIEW", "VISUAL_ASSET_REVIEW", "TIMELINE_REVIEW", "FINAL_REVIEW"])(
  "hides generic resume at paused %s while retaining failure recovery", (stage) => {
    const props = {
      project: {
        id: 7,
        title: "Sample",
        source_type: "idea",
        status: "active",
        settings: {
          schema_version: 1,
          content_format: "short",
          output_preset: "short_vertical",
          encoding_profile: "legacy_default",
          target_duration_seconds: null,
        },
        settings_source: "legacy_default",
      } satisfies ProjectDetail,
      run: { id: 41, project_id: 7, current_stage: stage, status: "paused", restart_from: null, model_defaults: null },
      titleDraft: "Sample", setTitleDraft: vi.fn(), savingTitle: false, onTitleSave: vi.fn(),
      stopping: false, resuming: false, deleting: false, confirmAction: null, setConfirmAction: vi.fn(),
      onStop: vi.fn(), onResume: vi.fn(), onDelete: vi.fn(), onNavigateBack: vi.fn(), currentStage: stage,
    };
    const { rerender } = render(<ProjectHeader {...props} />);
    expect(screen.queryByRole("button", { name: /Resume/ })).not.toBeInTheDocument();
    rerender(<ProjectHeader {...props} run={{ ...props.run, status: "failed" }} />);
    expect(screen.getByRole("button", { name: /Resume/ })).toBeEnabled();
  },
);
