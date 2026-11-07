import { useEffect, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";

import { apiJson, apiVoid, API_BASE } from "../api/client";
import ConfirmDialog from "../components/creator/ConfirmDialog";
import type { ProjectSummary } from "../types/api";

interface ProjectListResponse {
  projects: ProjectSummary[];
  total: number;
}

const PAGE_SIZE = 20;

const STATUS_COLORS: Record<string, { bg: string; fg: string }> = {
  draft: { bg: "#e2e8f0", fg: "#475569" },
  active: { bg: "#dbeafe", fg: "#1e40af" },
  completed: { bg: "#dcfce7", fg: "#166534" },
  archived: { bg: "#f3f4f6", fg: "#6b7280" },
  pending: { bg: "#fef3c7", fg: "#92400e" },
  running: { bg: "#dbeafe", fg: "#1d4ed8" },
  failed: { bg: "#fee2e2", fg: "#b91c1c" },
  cancelled: { bg: "#f3f4f6", fg: "#6b7280" },
  paused: { bg: "#ede9fe", fg: "#6d28d9" },
};

const SOURCE_LABELS: Record<string, string> = {
  idea: "Idea",
  markdown: "Markdown",
  url: "URL",
  pasted_json: "JSON",
};

function formatDate(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  } catch {
    return iso;
  }
}

export default function RunsPage() {
  const navigate = useNavigate();

  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Delete state
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);

  const fetchProjects = useCallback(async (pageOffset: number) => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiJson<ProjectListResponse>(`${API_BASE}/projects?limit=${PAGE_SIZE}&offset=${pageOffset}`);
      setProjects(data.projects);
      setTotal(data.total);
    } catch (err) {
      setError(err instanceof Error ? err.message : "An unexpected error occurred");
    } finally {
      setLoading(false);
    }
  }, []);


  const handleDeleteProject = useCallback(async (projectId: number) => {
    setDeletingId(projectId);
    try {
      await apiVoid(`${API_BASE}/projects/${projectId}`, { method: "DELETE" });
      fetchProjects(offset);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed");
    } finally {
      setDeletingId(null);
    }
  }, [fetchProjects, offset]);

  useEffect(() => {
    fetchProjects(offset);
  }, [fetchProjects, offset]);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <div style={{ maxWidth: 960, margin: "0 auto", padding: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 24 }}>
        <h1 style={{ fontSize: 24, fontWeight: 700, margin: 0 }}>Projects</h1>
        <button
          type="button"
          onClick={() => navigate("/create")}
          style={{
            padding: "8px 16px",
            background: "#4285f4",
            color: "#fff",
            border: "none",
            borderRadius: 6,
            fontSize: 13,
            fontWeight: 600,
            cursor: "pointer",
          }}
        >
          + New Project
        </button>
      </div>

      {/* Error state */}
      {error && (
        <div
          role="alert"
          style={{
            padding: "12px 16px",
            marginBottom: 16,
            background: "#fef2f2",
            border: "1px solid #fca5a5",
            borderRadius: 6,
            color: "#b91c1c",
            fontSize: 13,
          }}
        >
          {error}
          <button
            type="button"
            onClick={() => fetchProjects(offset)}
            style={{
              marginLeft: 12,
              padding: "4px 10px",
              background: "#b91c1c",
              color: "#fff",
              border: "none",
              borderRadius: 4,
              fontSize: 12,
              cursor: "pointer",
            }}
          >
            Retry
          </button>
        </div>
      )}

      {/* Loading state */}
      {loading && (
        <div
          role="status"
          aria-label="Loading projects"
          style={{
            textAlign: "center",
            padding: 48,
            color: "#6b7280",
            fontSize: 14,
          }}
        >
          Loading projects…
        </div>
      )}

      {/* Empty state */}
      {!loading && !error && projects.length === 0 && (
        <div
          data-testid="empty-state"
          style={{
            textAlign: "center",
            padding: 48,
            color: "#6b7280",
            background: "#f9fafb",
            borderRadius: 8,
            border: "1px dashed #d1d5db",
          }}
        >
          <p style={{ fontSize: 16, fontWeight: 600, margin: "0 0 8px" }}>
            No projects yet
          </p>
          <p style={{ fontSize: 13, margin: "0 0 16px" }}>
            Create your first short-form video project to get started.
          </p>
          <button
            type="button"
            onClick={() => navigate("/create")}
            style={{
              padding: "8px 16px",
              background: "#4285f4",
              color: "#fff",
              border: "none",
              borderRadius: 6,
              fontSize: 13,
              fontWeight: 600,
              cursor: "pointer",
            }}
          >
            Create Project
          </button>
        </div>
      )}

      {/* Project list */}
      {!loading && projects.length > 0 && (
        <>
          <table
            style={{
              width: "100%",
              borderCollapse: "collapse",
              fontSize: 14,
            }}
          >
            <thead>
              <tr
                style={{
                  borderBottom: "2px solid #e5e7eb",
                  textAlign: "left",
                }}
              >
                <th style={{ padding: "8px 12px", fontWeight: 600, color: "#374151" }}>
                  Title
                </th>
                <th style={{ padding: "8px 12px", fontWeight: 600, color: "#374151" }}>
                  Source
                </th>
                <th style={{ padding: "8px 12px", fontWeight: 600, color: "#374151" }}>
                  Status
                </th>
                <th style={{ padding: "8px 12px", fontWeight: 600, color: "#374151" }}>
                  Created
                </th>
                <th style={{ padding: "8px 12px", fontWeight: 600, color: "#374151", width: 80 }}>
                </th>
              </tr>
            </thead>
            <tbody>
              {projects.map((proj) => {
                const effectiveStatus = proj.latest_run?.status ?? proj.status;
                const statusColor = STATUS_COLORS[effectiveStatus] ?? STATUS_COLORS.draft;
                return (
                  <tr
                    key={proj.id}
                    tabIndex={0}
                    onClick={() => navigate(`/projects/${proj.id}`)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        navigate(`/projects/${proj.id}`);
                      }
                    }}
                    style={{
                      borderBottom: "1px solid #f3f4f6",
                      cursor: "pointer",
                    }}
                    onMouseEnter={(e) => {
                      (e.currentTarget as HTMLTableRowElement).style.background = "#f9fafb";
                    }}
                    onMouseLeave={(e) => {
                      (e.currentTarget as HTMLTableRowElement).style.background = "";
                    }}
                  >
                    <td style={{ padding: "10px 12px", fontWeight: 500 }}>
                      {proj.title || "Untitled"}
                    </td>
                    <td style={{ padding: "10px 12px", color: "#6b7280" }}>
                      {SOURCE_LABELS[proj.source_type] ?? proj.source_type}
                    </td>
                    <td style={{ padding: "10px 12px" }}>
                      <span
                        style={{
                          display: "inline-block",
                          padding: "2px 8px",
                          borderRadius: 9999,
                          fontSize: 12,
                          fontWeight: 500,
                          background: statusColor.bg,
                          color: statusColor.fg,
                        }}
                      >
                        {effectiveStatus}
                      </span>
                      {proj.latest_run?.current_stage && (
                        <div style={{ marginTop: 4, fontSize: 11, color: "#6b7280" }}>
                          {proj.latest_run.current_stage}
                        </div>
                      )}
                    </td>
                    <td style={{ padding: "10px 12px", color: "#6b7280" }}>
                      {formatDate(proj.created_at)}
                    </td>
                    <td style={{ padding: "10px 12px" }}>
                      <button
                        type="button"
                        onClick={(e) => { e.stopPropagation(); setConfirmDeleteId(proj.id); }}
                        style={{
                          padding: "4px 8px",
                          border: "none",
                          background: "transparent",
                          color: "#dc2626",
                          fontSize: 12,
                          cursor: "pointer",
                          fontWeight: 500,
                        }}
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          {/* Pagination */}
          {total > PAGE_SIZE && (
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                marginTop: 16,
                fontSize: 13,
                color: "#6b7280",
              }}
            >
              <span>
                Showing {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
              </span>
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  type="button"
                  disabled={currentPage <= 1}
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                  aria-label="Previous page"
                  style={{
                    padding: "6px 12px",
                    border: "1px solid #d1d5db",
                    borderRadius: 4,
                    background: currentPage <= 1 ? "#f3f4f6" : "#fff",
                    color: currentPage <= 1 ? "#9ca3af" : "#374151",
                    cursor: currentPage <= 1 ? "not-allowed" : "pointer",
                    fontSize: 13,
                  }}
                >
                  Previous
                </button>
                <button
                  type="button"
                  disabled={currentPage >= totalPages}
                  onClick={() => setOffset(offset + PAGE_SIZE)}
                  aria-label="Next page"
                  style={{
                    padding: "6px 12px",
                    border: "1px solid #d1d5db",
                    borderRadius: 4,
                    background: currentPage >= totalPages ? "#f3f4f6" : "#fff",
                    color: currentPage >= totalPages ? "#9ca3af" : "#374151",
                    cursor: currentPage >= totalPages ? "not-allowed" : "pointer",
                    fontSize: 13,
                  }}
                >
                  Next
                </button>
              </div>
            </div>
          )}
        </>
      )}

      {/* Delete confirmation dialog */}
      <ConfirmDialog
        open={confirmDeleteId !== null}
        title="Delete Project?"
        message="This will permanently delete the project and all its data. This action cannot be undone."
        variant="danger"
        confirmLabel="Delete"
        loading={deletingId === confirmDeleteId}
        onConfirm={async () => {
          if (confirmDeleteId !== null) {
            await handleDeleteProject(confirmDeleteId);
          }
          setConfirmDeleteId(null);
        }}
        onCancel={() => setConfirmDeleteId(null)}
      />
    </div>
  );
}
