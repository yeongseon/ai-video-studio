/**
 * SF-78 error-category labels (P0-4).
 *
 * Maps the backend ErrorCategory taxonomy to short, user-readable labels so the
 * UI can render the structured `error` envelope (ApiError) and failed-task
 * `failure` summaries consistently. Kept separate from the legacy task-level
 * ERROR_LABELS (provider_timeout, rate_limit, …) which remain for older runs.
 */
export const ERROR_CATEGORY_LABELS: Record<string, string> = {
  NOT_FOUND: "Not found",
  VALIDATION: "Invalid request",
  PROVIDER_AUTH: "Provider not configured",
  CONFLICT: "Conflict",
  VERSION_CONFLICT: "Out of date",
  QUOTA: "Rate limited",
  UNAVAILABLE: "Temporarily unavailable",
  DATA_INTEGRITY: "Data error",
  INTERNAL: "Something went wrong",
  ARTIFACT_STORAGE_LOW: "Output storage is low",
  SCRATCH_STORAGE_LOW: "Temporary storage is low",
};

export function categoryLabel(category: string | undefined): string {
  if (category && ERROR_CATEGORY_LABELS[category]) {
    return ERROR_CATEGORY_LABELS[category];
  }
  return "Something went wrong";
}
