export const API_BASE = "/api/creator";

export type ProjectSettings = {
  readonly content_format: "short";
  readonly output_preset: "short_vertical" | "short_square" | "short_landscape";
  readonly encoding_profile: "preview" | "standard" | "high_quality" | "legacy_default";
  readonly target_duration_seconds: number | null;
  readonly schema_version: 1;
};

export type ProjectResponse = {
  readonly settings: ProjectSettings;
  readonly settings_source: "explicit" | "legacy_default";
};

export type ProjectDetail = ProjectResponse & {
  id: number;
  title: string | null;
  source_type: string;
  status: string;
  idea_brief?: string | null;
  markdown_source?: string | null;
  url_source?: string | null;
  latest_run?: {
    run_id: number;
    current_stage: string | null;
    status: string | null;
  } | null;
};

export type ProjectSummary = ProjectResponse & {
  readonly id: number;
  readonly title: string | null;
  readonly source_type: "idea" | "markdown" | "url" | "pasted_json";
  readonly status: "draft" | "active" | "completed" | "archived" | "deleting";
  readonly created_at: string;
  readonly updated_at: string;
  readonly latest_run?: {
    readonly run_id: number;
    readonly current_stage: string | null;
    readonly status: string | null;
  } | null;
};

export interface ModelDefaults {
  script_model?: string;
  image_model?: string;
  tts_model?: string;
  subtitle_model?: string;
  render_profile?: string;
}

export interface RunDetail {
  id: number;
  project_id: number;
  current_stage: string;
  status: string;
  restart_from: string | null;
  model_defaults: ModelDefaults | null;
  error_message?: string | null;
  finished_at?: string | null;
  metadata?: Record<string, unknown> | null;
}

export interface SceneData {
  scene_id: string;
  section_id: string;
  scene_index: number;
  section_type: string;
  original_text: string;
  prompt: string;
  prompt_edited: boolean;
  prompt_source: "auto_generated" | "user_edited" | "model_suggested";
  style_tags: string[];
  mood: string | null;
  composition: string | null;
  generation_status: "pending" | "generating" | "completed" | "failed";
  latest_asset_id: number | null;
}

export type VisualPlanScene = SceneData;

export interface VisualScene {
  scene_id: string;
  prompt: string;
  prompt_edited: boolean;
  prompt_source: "auto_generated" | "user_edited" | "model_suggested";
  style_tags: string[];
  mood: string | null;
  composition: string | null;
  generation_status: "pending" | "generating" | "completed" | "failed";
}

export interface ApiKeyStatus {
  provider: string;
  label: string;
  configured: boolean;
}

export const FINAL_REVIEW_STAGES = new Set(["FINAL_REVIEW"]);

export const RUN_POLL_STAGES = new Set([
  "SCRIPT_GENERATING",
  "VISUAL_PLAN_GENERATING",
  "VISUAL_ASSET_GENERATING",
  "AUDIO_GENERATING",
  "SUBTITLE_GENERATING",
  "RENDER_GENERATING",
]);

export const STAGE_BACK_LABELS: Record<string, string> = {
  SCRIPT_REVIEW: "\u2190 Back to Idea",
  VISUAL_PLAN_SETUP: "\u2190 Back to Script Review",
  VISUAL_PLAN_REVIEW: "\u2190 Back to Visual Plan Setup",
  VISUAL_ASSET_REVIEW: "\u2190 Back to Visual Plan",
  FINAL_REVIEW: "\u2190 Back to Visual Assets",
};

export const STAGE_ORDER: string[] = [
  "IDEA_READY",
  "SCRIPT_GENERATING",
  "SCRIPT_REVIEW",
  "VISUAL_PLAN_SETUP",
  "VISUAL_PLAN_GENERATING",
  "VISUAL_PLAN_REVIEW",
  "VISUAL_ASSET_GENERATING",
  "VISUAL_ASSET_REVIEW",
  "AUDIO_GENERATING",
  "SUBTITLE_GENERATING",
  "TIMELINE_REVIEW",
  "RENDER_GENERATING",
  "FINAL_REVIEW",
  "PUBLISHED",
];
