import { describe, it, expect } from "vitest";
import { ERROR_CATEGORY_LABELS, categoryLabel } from "../api/errorLabels";

const ALL_CATEGORIES = [
  "NOT_FOUND",
  "VALIDATION",
  "PROVIDER_AUTH",
  "CONFLICT",
  "VERSION_CONFLICT",
  "QUOTA",
  "UNAVAILABLE",
  "DATA_INTEGRITY",
  "INTERNAL",
  "ARTIFACT_STORAGE_LOW",
  "SCRATCH_STORAGE_LOW",
];

describe("SF-78 error category labels", () => {
  it("has a non-empty label for every ErrorCategory", () => {
    for (const category of ALL_CATEGORIES) {
      expect(ERROR_CATEGORY_LABELS[category]).toBeTruthy();
    }
  });

  it("categoryLabel returns the mapped label", () => {
    expect(categoryLabel("UNAVAILABLE")).toBe(ERROR_CATEGORY_LABELS.UNAVAILABLE);
  });

  it("categoryLabel falls back to a safe label for unknown categories", () => {
    expect(categoryLabel("SOMETHING_NEW")).toBeTruthy();
    expect(categoryLabel(undefined)).toBeTruthy();
  });
});
