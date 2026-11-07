import { describe, expect, it } from "vitest";

import type { ProjectDetail, ProjectSummary } from "../types/api";

const explicitSettingsProject = {
  id: 1,
  title: "Square short",
  source_type: "idea",
  status: "active",
  settings: {
    schema_version: 1,
    content_format: "short",
    output_preset: "short_square",
    encoding_profile: "high_quality",
    target_duration_seconds: 45,
  },
  settings_source: "explicit",
} satisfies ProjectDetail;

const legacyDefaultProject = {
  id: 2,
  title: "Legacy short",
  source_type: "markdown",
  status: "draft",
  created_at: "2026-11-07T00:00:00Z",
  updated_at: "2026-11-07T00:00:00Z",
  settings: {
    schema_version: 1,
    content_format: "short",
    output_preset: "short_vertical",
    encoding_profile: "legacy_default",
    target_duration_seconds: null,
  },
  settings_source: "legacy_default",
} satisfies ProjectSummary;

describe("project response API types", () => {
  it("represents an explicit-settings response fixture", () => {
    expect(explicitSettingsProject.settings_source).toBe("explicit");
    expect(explicitSettingsProject.settings.output_preset).toBe("short_square");
    expect(explicitSettingsProject.settings.target_duration_seconds).toBe(45);
  });

  it("represents a legacy-default response fixture", () => {
    expect(legacyDefaultProject.settings_source).toBe("legacy_default");
    expect(legacyDefaultProject.settings.encoding_profile).toBe("legacy_default");
    expect(legacyDefaultProject.settings.target_duration_seconds).toBeNull();
  });
});
