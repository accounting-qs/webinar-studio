"use client";

import { useState, useCallback, useRef, useEffect, type DragEvent, type ChangeEvent } from "react";
import {
  startImport, fetchUploads, fetchUploadStatus, fetchUploadHeaders,
  deleteUpload, pauseImport, resumeImport, cancelImport,
  requestSignedUploadUrl, uploadToStorage, confirmUpload,
  fetchCustomFields,
  type ApiUpload, type UploadFileResponse, type UploadStatusResponse,
} from "@/lib/api";

/* ─── Constants ────────────────────────────────────────────────────────── */

const SYSTEM_FIELDS = [
  { value: "skip", label: "— Skip —", group: "action" },
  { value: "contact_id", label: "Contact ID", group: "identity" },
  { value: "first_name", label: "First Name", group: "identity" },
  { value: "last_name", label: "Last Name", group: "identity" },
  { value: "email", label: "Email", group: "identity" },
  { value: "company_website", label: "Company Website", group: "identity" },
  { value: "bucket", label: "Bucket", group: "enrichment" },
  { value: "classification", label: "Classification", group: "enrichment" },
  { value: "confidence", label: "Confidence", group: "enrichment" },
  { value: "reasoning", label: "Reasoning", group: "enrichment" },
  { value: "cost", label: "Cost", group: "enrichment" },
  { value: "status", label: "Status", group: "enrichment" },
  { value: "enrichment_classification", label: "Enrichment Classification", group: "enrichment" },
  { value: "primary_identity", label: "Primary Identity", group: "enrichment" },
  { value: "sub_identity", label: "Sub-Identity", group: "enrichment" },
  { value: "sector", label: "Sector", group: "enrichment" },
  { value: "lead_list_name", label: "Lead List Name", group: "source" },
  { value: "segment_name", label: "Segment Name", group: "source" },
  { value: "created_date", label: "Created Date", group: "source" },
  { value: "industry", label: "Industry", group: "source" },
  { value: "employee_range", label: "Employee Range", group: "source" },
  { value: "country", label: "Country", group: "source" },
  { value: "database_provider", label: "Database Provider", group: "source" },
  { value: "scraper", label: "Scraper", group: "source" },
];

const AUTO_MAP: Record<string, string> = {
  contact_id: "contact_id",
  first_name: "first_name",
  last_name: "last_name",
  email: "email",
  company_website: "company_website",
  bucket: "bucket",
  classification: "classification",
  confidence: "confidence",
  reasoning: "reasoning",
  cost: "cost",
  status: "status",
  proxy_used: "skip",
  lead_list_name: "lead_list_name",
  "list build - segment name": "segment_name",
  "list build - created date": "created_date",
  "list build - industry": "industry",
  "list build - employee range": "employee_range",
  "list build - country": "country",
  "list build - database provider": "database_provider",
  scraper: "scraper",
  enrichment_classification: "enrichment_classification",
  primary_identity: "primary_identity",
  // Legacy header name kept so older CSVs still auto-map to the renamed column.
  characteristic: "sub_identity",
  sub_identity: "sub_identity",
  sector: "sector",
};

function autoMapHeader(header: string): string {
  const h = header.toLowerCase().trim();
  return AUTO_MAP[h] ?? "skip";
}

/* ─── Types ────────────────────────────────────────────────────────────── */

type Step = "idle" | "uploading" | "mapping" | "importing";

/* ─── Component ────────────────────────────────────────────────────────── */

export function UploadPage() {
  const [step, setStep] = useState<Step>("idle");
  const [dragOver, setDragOver] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  // Upload state
  const [uploadResponse, setUploadResponse] = useState<UploadFileResponse | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadProgress, setUploadProgress] = useState(0);

  // Mapping state
  const [mappings, setMappings] = useState<Record<string, string>>({});
  const [customFields, setCustomFields] = useState<string[]>([]);
  const [newFieldName, setNewFieldName] = useState("");
  const [showNewField, setShowNewField] = useState(false);

  // Upload mode
  const [uploadMode, setUploadMode] = useState<"bucket" | "custom_list">("bucket");
  const [customListName, setCustomListName] = useState("");

  // Duplicate handling
  const [duplicateMode, setDuplicateMode] = useState<"ignore" | "overwrite">("ignore");

  // Import history
  const [uploadHistory, setUploadHistory] = useState<ApiUpload[]>([]);

  // Detail modal
  const [selectedUpload, setSelectedUpload] = useState<ApiUpload | null>(null);

  // Delete confirmation
  const [deleteTarget, setDeleteTarget] = useState<ApiUpload | null>(null);
  const [deleteLoading, setDeleteLoading] = useState(false);
  const [detailStatus, setDetailStatus] = useState<UploadStatusResponse | null>(null);
  const [controlLoading, setControlLoading] = useState(false);

  /* ── Load upload history ─────────────────────────────────────────── */
  const loadHistory = useCallback(() => {
    fetchUploads()
      .then(({ uploads }) => setUploadHistory(uploads))
      .catch((err) => console.error("Failed to load uploads:", err));
  }, []);

  useEffect(() => {
    loadHistory();
    // Load existing custom fields from DB
    fetchCustomFields().then(({ fields }) => {
      setCustomFields(fields.map((f) => f.field_name));
    }).catch(() => {});
  }, [loadHistory]);

  // Poll only active imports by their individual status endpoint
  useEffect(() => {
    const activeUploads = uploadHistory.filter(
      (u) => u.status === "processing" || u.status === "paused"
    );
    if (activeUploads.length === 0) return;

    const interval = setInterval(async () => {
      for (const u of activeUploads) {
        try {
          const status = await fetchUploadStatus(u.id);
          setUploadHistory((prev) =>
            prev.map((h) => h.id === u.id ? { ...h, ...status, status: status.status, progress: status.progress, processed_rows: status.processed_rows, inserted_count: status.inserted_count, skipped_count: status.skipped_count, overwritten_count: status.overwritten_count, error_message: status.error_message } : h)
          );
        } catch { /* ignore poll errors */ }
      }
    }, 3000);
    return () => clearInterval(interval);
  }, [uploadHistory]);

  /* ── File handling ───────────────────────────────────────────────── */

  const handleFile = useCallback(async (file: File) => {
    if (!file.name.endsWith(".csv")) return;
    if (file.size > 500 * 1024 * 1024) {
      setUploadError("File exceeds 500 MB limit");
      return;
    }

    setStep("uploading");
    setUploadProgress(0);
    setUploadError(null);

    try {
      // Step 1: Get signed URL from backend
      const { upload_id, signed_url } = await requestSignedUploadUrl(file.name, file.size);

      // Step 2: Upload directly to Supabase Storage with real progress
      await uploadToStorage(signed_url, file, (pct) => setUploadProgress(pct));

      // Step 3: Confirm upload — backend reads headers/preview from Storage
      const response = await confirmUpload(upload_id, file.size);
      setUploadProgress(100);
      setUploadResponse(response);

      // Auto-map headers
      const autoMappings: Record<string, string> = {};
      response.headers.forEach((header) => {
        autoMappings[header] = autoMapHeader(header);
      });
      setMappings(autoMappings);

      // Brief pause to show 100%, then transition
      setTimeout(() => setStep("mapping"), 500);
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Upload failed");
      setStep("idle");
    }
  }, []);

  const onDrop = useCallback(
    (e: DragEvent) => {
      e.preventDefault();
      setDragOver(false);
      const file = e.dataTransfer.files[0];
      if (file) handleFile(file);
    },
    [handleFile]
  );

  const onFileSelect = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (file) handleFile(file);
    },
    [handleFile]
  );

  /* ── Import ────────────────────────────────────────────────────────── */

  const handleImport = useCallback(async () => {
    if (!uploadResponse) return;

    try {
      await startImport(uploadResponse.id, mappings, duplicateMode, uploadMode, uploadMode === "custom_list" ? customListName : undefined);
      setStep("idle");
      setUploadResponse(null);
      setMappings({});
      setUploadMode("bucket");
      setCustomListName("");
      loadHistory();
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Failed to start import");
    }
  }, [uploadResponse, mappings, duplicateMode, uploadMode, customListName, loadHistory]);

  /* ── History item click handler ────────────────────────────────────── */

  const handleHistoryClick = useCallback(async (u: ApiUpload) => {
    if (u.status === "uploading") {
      // CSV uploaded but never mapped → go to mapping step
      try {
        const headersRes = await fetchUploadHeaders(u.id);
        setUploadResponse(headersRes);
        const autoMappings: Record<string, string> = {};
        headersRes.headers.forEach((header) => {
          autoMappings[header] = autoMapHeader(header);
        });
        setMappings(autoMappings);
        setStep("mapping");
      } catch (err) {
        setUploadError(err instanceof Error ? err.message : "Failed to load headers — CSV may have expired");
      }
    } else {
      // Processing/complete/failed → show stats modal
      setSelectedUpload(u);
    }
  }, []);

  /* ── Detail modal polling ─────────────────────────────────────────── */

  useEffect(() => {
    if (!selectedUpload) {
      setDetailStatus(null);
      return;
    }

    const load = () => {
      fetchUploadStatus(selectedUpload.id)
        .then(setDetailStatus)
        .catch(console.error);
    };
    load();

    if (selectedUpload.status === "processing" || selectedUpload.status === "pending" || selectedUpload.status === "paused") {
      const interval = setInterval(load, 2000);
      return () => clearInterval(interval);
    }
  }, [selectedUpload]);

  // Update selectedUpload when history refreshes
  useEffect(() => {
    if (selectedUpload) {
      const updated = uploadHistory.find((u) => u.id === selectedUpload.id);
      if (updated) setSelectedUpload(updated);
    }
  }, [uploadHistory]);

  /* ── Delete upload ───────────────────────────────────────────────── */

  const handleDelete = useCallback(async (u: ApiUpload) => {
    setDeleteLoading(true);
    try {
      await deleteUpload(u.id);
      setDeleteTarget(null);
      setSelectedUpload(null);
      loadHistory();
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to delete");
    } finally {
      setDeleteLoading(false);
    }
  }, [loadHistory]);

  /* ── Import control handlers ──────────────────────────────────────── */

  const handlePause = useCallback(async (uploadId: string) => {
    setControlLoading(true);
    try {
      await pauseImport(uploadId);
      loadHistory();
      if (selectedUpload?.id === uploadId) {
        setSelectedUpload((prev) => prev ? { ...prev, status: "paused" } : null);
      }
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to pause");
    } finally {
      setControlLoading(false);
    }
  }, [loadHistory, selectedUpload]);

  const handleResume = useCallback(async (uploadId: string) => {
    setControlLoading(true);
    try {
      await resumeImport(uploadId);
      loadHistory();
      if (selectedUpload?.id === uploadId) {
        setSelectedUpload((prev) => prev ? { ...prev, status: "processing" } : null);
      }
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to resume");
    } finally {
      setControlLoading(false);
    }
  }, [loadHistory, selectedUpload]);

  const handleCancel = useCallback(async (uploadId: string) => {
    if (!confirm("Cancel this import? Already-imported rows will remain in the database.")) return;
    setControlLoading(true);
    try {
      await cancelImport(uploadId);
      loadHistory();
      if (selectedUpload?.id === uploadId) {
        setSelectedUpload((prev) => prev ? { ...prev, status: "cancelled" } : null);
      }
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to cancel");
    } finally {
      setControlLoading(false);
    }
  }, [loadHistory, selectedUpload]);

  /* ── Custom field ────────────────────────────────────────────────── */

  const addCustomField = useCallback(() => {
    const name = newFieldName.trim();
    if (!name || customFields.includes(name)) return;
    setCustomFields((prev) => [...prev, name]);
    setNewFieldName("");
    setShowNewField(false);
  }, [newFieldName, customFields]);

  /* ── Render helpers ──────────────────────────────────────────────── */

  const formatNumber = (n: number) => n.toLocaleString();
  const formatBytes = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const getStatusColor = (status: string) => {
    switch (status) {
      case "complete": return "#10b981";
      case "processing": return "#3b82f6";
      case "pending": case "uploading": return "#f59e0b";
      case "paused": return "#f59e0b";
      case "cancelled": return "#6b7280";
      case "failed": return "#ef4444";
      default: return "#6b7280";
    }
  };

  const getStatusIcon = (status: string) => {
    switch (status) {
      case "complete": return "✓";
      case "processing": return "⏳";
      case "pending": case "uploading": return "⏳";
      case "paused": return "⏸";
      case "cancelled": return "⊘";
      case "failed": return "✗";
      default: return "?";
    }
  };

  /* ─── RENDER ─────────────────────────────────────────────────────── */

  return (
    <div style={{ maxWidth: 960, margin: "0 auto", padding: "24px 16px" }}>
      <h2 style={{ fontWeight: 700, fontSize: 24, marginBottom: 4, color: "var(--foreground)" }}>
        List Upload
      </h2>
      <p style={{ color: "var(--muted-foreground)", fontSize: 14, marginBottom: 24 }}>
        Upload CSV contact lists — files are stored in Supabase and imported in the background.
      </p>

      {/* ── Uploading step ──────────────────────────────────────────── */}
      {step === "uploading" && (
        <div style={{
          background: "var(--card-bg)", border: "1px solid var(--border-subtle)",
          borderRadius: 12, padding: 32, textAlign: "center", marginBottom: 24,
        }}>
          <div style={{ fontSize: 48, marginBottom: 16 }}>☁️</div>
          <h3 style={{ fontSize: 18, fontWeight: 600, marginBottom: 8, color: "var(--foreground)" }}>
            Uploading to Supabase Storage...
          </h3>
          <p style={{ color: "var(--muted-foreground)", fontSize: 14, marginBottom: 16 }}>
            {uploadResponse?.file_name || "Processing..."}
          </p>
          <div style={{
            background: "var(--border-subtle)", borderRadius: 8, height: 12,
            overflow: "hidden", maxWidth: 400, margin: "0 auto",
          }}>
            <div style={{
              height: "100%", borderRadius: 8,
              background: "linear-gradient(90deg, #3b82f6, #8b5cf6)",
              width: `${uploadProgress}%`,
              transition: "width 200ms ease",
            }} />
          </div>
          <p style={{ color: "var(--muted-foreground)", fontSize: 13, marginTop: 8 }}>{uploadProgress}%</p>
        </div>
      )}

      {/* ── Drop zone (idle) ────────────────────────────────────────── */}
      {(step === "idle" || step === "importing") && (
        <div
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
          onClick={() => fileRef.current?.click()}
          style={{
            border: `2px dashed ${dragOver ? "#3b82f6" : "var(--border-subtle)"}`,
            borderRadius: 12, padding: "32px 24px", textAlign: "center",
            cursor: "pointer", marginBottom: 24,
            background: dragOver ? "rgba(59,130,246,0.05)" : "var(--card-bg)",
            transition: "all 200ms ease",
          }}
        >
          <div style={{ fontSize: 36, marginBottom: 8 }}>📄</div>
          <p style={{ fontWeight: 600, fontSize: 15, color: "var(--foreground)" }}>
            Drop your CSV here or click to browse
          </p>
          <p style={{ color: "var(--muted-foreground)", fontSize: 13, marginTop: 4 }}>
            Supports files up to 500 MB • CSV format only
          </p>
          <input ref={fileRef} type="file" accept=".csv" hidden onChange={onFileSelect} />
        </div>
      )}

      {uploadError && (
        <div style={{
          background: "rgba(239,68,68,0.1)", border: "1px solid rgba(239,68,68,0.3)",
          borderRadius: 8, padding: "12px 16px", marginBottom: 16, color: "#ef4444", fontSize: 14,
        }}>
          ⚠️ {uploadError}
          <button
            onClick={() => setUploadError(null)}
            style={{ float: "right", background: "none", border: "none", color: "#ef4444", cursor: "pointer" }}
          >✕</button>
        </div>
      )}

      {/* ── Mapping step ────────────────────────────────────────────── */}
      {step === "mapping" && uploadResponse && (
        <div style={{
          background: "var(--card-bg)", border: "1px solid var(--border-subtle)",
          borderRadius: 16, overflow: "hidden", marginBottom: 32,
          boxShadow: "0 4px 20px rgba(0,0,0,0.05)",
        }}>
          {/* Header */}
          <div style={{
            padding: "24px 32px", borderBottom: "1px solid var(--border-subtle)",
            display: "flex", justifyContent: "space-between", alignItems: "center",
            background: "var(--background)",
          }}>
            <div>
              <h3 style={{ fontWeight: 700, fontSize: 18, color: "var(--foreground)", marginBottom: 4 }}>
                Map Fields — {uploadResponse.file_name}
              </h3>
              <div style={{ color: "var(--muted-foreground)", fontSize: 13, display: "flex", alignItems: "center", gap: 8 }}>
                <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                  <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%", background: "#3b82f6" }} />
                  {formatNumber(uploadResponse.total_rows)} contacts
                </span>
                {uploadResponse.file_size ? (
                  <>
                    <span>•</span>
                    <span>{formatBytes(uploadResponse.file_size)}</span>
                  </>
                ) : null}
              </div>
            </div>
            <button
              onClick={() => { setStep("idle"); setUploadResponse(null); }}
              style={{
                background: "var(--card-bg)", border: "1px solid var(--border-subtle)",
                borderRadius: 8, padding: "8px 16px", color: "var(--foreground)",
                cursor: "pointer", fontSize: 13, fontWeight: 500,
                transition: "background 150ms",
              }}
              onMouseEnter={(e) => e.currentTarget.style.background = "var(--border-subtle)"}
              onMouseLeave={(e) => e.currentTarget.style.background = "var(--card-bg)"}
            >Cancel</button>
          </div>

          <div style={{ padding: "0 32px" }}>
            {/* Upload mode toggle */}
            <div style={{ display: "flex", alignItems: "center", gap: 16, margin: "20px 0 0" }}>
              <span style={{ fontSize: 12, fontWeight: 600, color: "var(--muted-foreground)", textTransform: "uppercase", letterSpacing: "0.5px" }}>Upload Mode:</span>
              <div style={{ display: "flex", gap: 4, background: "var(--background)", borderRadius: 8, padding: 3, border: "1px solid var(--border-subtle)" }}>
                <button
                  onClick={() => setUploadMode("bucket")}
                  style={{
                    padding: "6px 16px", borderRadius: 6, fontSize: 12, fontWeight: 600, cursor: "pointer", border: "none",
                    background: uploadMode === "bucket" ? "#7c3aed" : "transparent",
                    color: uploadMode === "bucket" ? "white" : "var(--muted-foreground)",
                    transition: "all 150ms",
                  }}
                >Bucket Upload</button>
                <button
                  onClick={() => setUploadMode("custom_list")}
                  style={{
                    padding: "6px 16px", borderRadius: 6, fontSize: 12, fontWeight: 600, cursor: "pointer", border: "none",
                    background: uploadMode === "custom_list" ? "#7c3aed" : "transparent",
                    color: uploadMode === "custom_list" ? "white" : "var(--muted-foreground)",
                    transition: "all 150ms",
                  }}
                >Custom List</button>
              </div>
              {uploadMode === "custom_list" && (
                <div style={{ display: "flex", alignItems: "center", gap: 8, flex: 1 }}>
                  <label style={{ fontSize: 12, fontWeight: 600, color: "var(--muted-foreground)", whiteSpace: "nowrap" }}>List Name:</label>
                  <input
                    type="text"
                    value={customListName}
                    onChange={(e) => setCustomListName(e.target.value)}
                    placeholder={uploadResponse?.file_name?.replace(/\.csv$/i, "") || "Enter list name"}
                    style={{
                      flex: 1, padding: "6px 12px", borderRadius: 8, border: "1px solid var(--border-subtle)",
                      background: "var(--card-bg)", color: "var(--foreground)", fontSize: 13,
                      outline: "none",
                    }}
                    onFocus={() => { if (!customListName && uploadResponse) setCustomListName(uploadResponse.file_name.replace(/\.csv$/i, "")); }}
                  />
                </div>
              )}
            </div>

            {/* Mapping table */}
            <div style={{ margin: "24px 0", border: "1px solid var(--border-subtle)", borderRadius: 12, overflow: "hidden" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                <thead style={{ background: "var(--background)" }}>
                  <tr style={{ borderBottom: "1px solid var(--border-subtle)" }}>
                    <th style={{ textAlign: "left", padding: "12px 16px", color: "var(--muted-foreground)", fontWeight: 600, width: "30%" }}>CSV Header</th>
                    <th style={{ textAlign: "left", padding: "12px 16px", color: "var(--muted-foreground)", fontWeight: 600, width: "30%" }}>Preview</th>
                    <th style={{ textAlign: "left", padding: "12px 16px", color: "var(--muted-foreground)", fontWeight: 600, width: "40%" }}>Map To</th>
                  </tr>
                </thead>
                <tbody>
                  {uploadResponse.headers.map((header, idx) => {
                    const isMapped = mappings[header] && mappings[header] !== "skip";
                    return (
                    <tr key={header} style={{
                      borderBottom: "1px solid var(--border-subtle)",
                      background: isMapped ? "rgba(59,130,246,0.02)" : "transparent",
                    }}>
                      <td style={{ padding: "12px 16px", fontWeight: 600, color: "var(--foreground)" }}>{header}</td>
                      <td style={{ padding: "12px 16px", color: "var(--muted-foreground)", maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {uploadResponse.preview_rows[0]?.[idx] || "—"}
                      </td>
                      <td style={{ padding: "12px 16px" }}>
                        <select
                          value={mappings[header] || "skip"}
                          onChange={(e) => setMappings((prev) => ({ ...prev, [header]: e.target.value }))}
                          style={{
                            background: "var(--card-bg)", color: "var(--foreground)",
                            border: `1px solid ${isMapped ? "#3b82f6" : "var(--border-subtle)"}`,
                            borderRadius: 8, padding: "8px 12px", fontSize: 13, width: "100%",
                            cursor: "pointer", transition: "border-color 200ms",
                            outline: "none", boxShadow: isMapped ? "0 0 0 1px #3b82f6" : "none",
                          }}
                        >
                          {SYSTEM_FIELDS.filter((f) => uploadMode !== "custom_list" || f.value !== "bucket").map((f) => (
                            <option key={f.value} value={f.value}>{f.label}</option>
                          ))}
                          {customFields.map((f) => (
                            <option key={f} value={`custom:${f}`}>🏷️ {f}</option>
                          ))}
                        </select>
                      </td>
                    </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 24, marginBottom: 24 }}>
              {/* Custom field add */}
              <div style={{
                padding: "20px", background: "var(--background)", borderRadius: 12,
                border: "1px solid var(--border-subtle)",
              }}>
                <h4 style={{ fontSize: 14, fontWeight: 600, color: "var(--foreground)", marginBottom: 12 }}>Custom Fields</h4>
                {showNewField ? (
                  <div style={{ display: "flex", gap: 8 }}>
                    <input
                      value={newFieldName}
                      onChange={(e) => setNewFieldName(e.target.value)}
                      placeholder="Field name..."
                      style={{
                        background: "var(--card-bg)", color: "var(--foreground)",
                        border: "1px solid var(--border-subtle)", borderRadius: 6,
                        padding: "8px 12px", fontSize: 13, flex: 1,
                      }}
                      onKeyDown={(e) => e.key === "Enter" && addCustomField()}
                    />
                    <button
                      onClick={addCustomField}
                      style={{
                        background: "#3b82f6", color: "#fff", border: "none",
                        borderRadius: 6, padding: "8px 16px", cursor: "pointer", fontSize: 13, fontWeight: 500,
                      }}
                    >Add</button>
                    <button
                      onClick={() => setShowNewField(false)}
                      style={{
                        background: "var(--card-bg)", border: "1px solid var(--border-subtle)",
                        borderRadius: 6, padding: "8px 16px", cursor: "pointer", fontSize: 13, color: "var(--foreground)",
                      }}
                    >Cancel</button>
                  </div>
                ) : (
                  <button
                    onClick={() => setShowNewField(true)}
                    style={{
                      background: "var(--card-bg)", border: "1px dashed var(--border-subtle)",
                      borderRadius: 8, padding: "10px 16px", cursor: "pointer", fontSize: 13,
                      color: "var(--foreground)", width: "100%", fontWeight: 500,
                      transition: "border-color 150ms",
                    }}
                    onMouseEnter={(e) => e.currentTarget.style.borderColor = "#3b82f6"}
                    onMouseLeave={(e) => e.currentTarget.style.borderColor = "var(--border-subtle)"}
                  >+ Add Custom Field</button>
                )}
              </div>

              {/* Duplicate mode */}
              <div style={{
                padding: "20px", background: "rgba(59,130,246,0.04)", borderRadius: 12,
                border: "1px solid rgba(59,130,246,0.2)",
              }}>
                <h4 style={{ fontSize: 14, fontWeight: 600, color: "var(--foreground)", marginBottom: 12 }}>Conflict Resolution</h4>
                <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                  <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", fontSize: 13, color: "var(--foreground)" }}>
                    <input
                      type="radio" name="dup" checked={duplicateMode === "ignore"}
                      onChange={() => setDuplicateMode("ignore")}
                      style={{ cursor: "pointer" }}
                    />
                    <div>
                      <div style={{ fontWeight: 500 }}>Skip (Recommended)</div>
                      <div style={{ color: "var(--muted-foreground)", fontSize: 12 }}>Keep existing DB records when emails match.</div>
                    </div>
                  </label>
                  <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", fontSize: 13, color: "var(--foreground)" }}>
                    <input
                      type="radio" name="dup" checked={duplicateMode === "overwrite"}
                      onChange={() => setDuplicateMode("overwrite")}
                      style={{ cursor: "pointer" }}
                    />
                    <div>
                      <div style={{ fontWeight: 500 }}>Overwrite</div>
                      <div style={{ color: "var(--muted-foreground)", fontSize: 12 }}>Update existing DB records with this CSV's data.</div>
                    </div>
                  </label>
                </div>
              </div>
            </div>
          </div>

          <div style={{ padding: "24px 32px", borderTop: "1px solid var(--border-subtle)", background: "var(--background)" }}>
            <button
              onClick={handleImport}
              disabled={uploadMode === "custom_list" && !customListName.trim()}
              style={{
                width: "100%", padding: "14px 0", fontSize: 15, fontWeight: 600,
                background: (uploadMode === "custom_list" && !customListName.trim())
                  ? "linear-gradient(135deg, #6b7280, #9ca3af)"
                  : "linear-gradient(135deg, #3b82f6, #8b5cf6)",
                color: "#fff", border: "none", borderRadius: 10,
                cursor: (uploadMode === "custom_list" && !customListName.trim()) ? "not-allowed" : "pointer",
                boxShadow: "0 4px 14px rgba(59,130,246,0.3)", transition: "transform 100ms",
                opacity: (uploadMode === "custom_list" && !customListName.trim()) ? 0.6 : 1,
              }}
              onMouseDown={(e) => e.currentTarget.style.transform = "scale(0.99)"}
              onMouseUp={(e) => e.currentTarget.style.transform = "scale(1)"}
              onMouseLeave={(e) => e.currentTarget.style.transform = "scale(1)"}
            >
              🚀 Start {uploadMode === "custom_list" ? "Custom List" : "Bucket"} Import ({formatNumber(uploadResponse.total_rows)} rows)
            </button>
          </div>
        </div>
      )}

      {/* ── Import History ──────────────────────────────────────────── */}
      {uploadHistory.length > 0 && (
        <div style={{ marginBottom: 40 }}>
          <h3 style={{ fontWeight: 700, fontSize: 18, marginBottom: 16, color: "var(--foreground)", display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ fontSize: 20 }}>📋</span> Import History
          </h3>
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {uploadHistory.map((u) => {
              const isActive = u.status === "processing" || u.status === "pending" || u.status === "uploading" || u.status === "paused";
              return (
              <div
                key={u.id}
                onClick={() => handleHistoryClick(u)}
                style={{
                  background: isActive ? "var(--background)" : "var(--card-bg)",
                  border: `1px solid ${isActive ? "rgba(59,130,246,0.3)" : "var(--border-subtle)"}`,
                  boxShadow: isActive ? "0 4px 12px rgba(59,130,246,0.05)" : "none",
                  borderRadius: 12, padding: "16px 20px", cursor: "pointer",
                  transition: "all 200ms ease",
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.borderColor = isActive ? "rgba(59,130,246,0.6)" : "#3b82f6";
                  e.currentTarget.style.boxShadow = "0 4px 12px rgba(0,0,0,0.05)";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.borderColor = isActive ? "rgba(59,130,246,0.3)" : "var(--border-subtle)";
                  e.currentTarget.style.boxShadow = isActive ? "0 4px 12px rgba(59,130,246,0.05)" : "none";
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
                    <span style={{
                      display: "inline-flex", alignItems: "center", justifyContent: "center",
                      width: 36, height: 36, borderRadius: "50%",
                      background: `${getStatusColor(u.status)}15`,
                      color: getStatusColor(u.status),
                      fontSize: 16, fontWeight: 700,
                    }}>
                      {getStatusIcon(u.status)}
                    </span>
                    <div>
                      <div style={{ fontWeight: 600, fontSize: 15, color: "var(--foreground)", marginBottom: 2 }}>
                        {u.file_name}
                      </div>
                      <div style={{ fontSize: 13, color: "var(--muted-foreground)" }}>
                        {u.created_at ? new Date(u.created_at).toLocaleDateString(undefined, {
                          month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"
                        }) : ""}
                        {u.status === "complete" && ` • ${formatNumber(u.total_contacts)} contacts imported`}
                        {u.status === "uploading" && ` • Awaiting Field Mapping`}
                      </div>
                    </div>
                  </div>
                  <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                    <div style={{ textAlign: "right" }}>
                      {isActive && u.status !== "uploading" && (
                        <span style={{ fontSize: 14, color: u.status === "paused" ? "#f59e0b" : "#3b82f6", fontWeight: 600 }}>
                          {u.status === "paused" ? "Paused" : `${u.progress}%`}
                        </span>
                      )}
                      {u.status === "uploading" && (
                        <div style={{
                          padding: "6px 12px", background: "#f59e0b15", color: "#f59e0b",
                          borderRadius: 8, fontSize: 12, fontWeight: 600,
                        }}>
                          Map Fields →
                        </div>
                      )}
                      {u.status === "complete" && (
                        <span style={{ fontSize: 14, color: "#10b981", fontWeight: 600 }}>
                          {formatNumber(u.total_contacts)}
                        </span>
                      )}
                      {u.status === "cancelled" && (
                        <span style={{ fontSize: 13, color: "#6b7280", fontWeight: 500 }}>Cancelled</span>
                      )}
                      {u.status === "failed" && (
                        <span style={{ fontSize: 13, color: "#ef4444", fontWeight: 500 }}>Failed</span>
                      )}
                    </div>
                    {/* Delete button — hidden for processing imports */}
                    {u.status !== "processing" && u.status !== "pending" && u.status !== "paused" && (
                      <button
                        onClick={(e) => { e.stopPropagation(); setDeleteTarget(u); }}
                        title="Delete this import"
                        style={{
                          background: "transparent", border: "1px solid var(--border-subtle)",
                          borderRadius: 8, width: 32, height: 32, cursor: "pointer",
                          display: "inline-flex", alignItems: "center", justifyContent: "center",
                          color: "var(--muted-foreground)", fontSize: 14,
                          transition: "all 150ms",
                        }}
                        onMouseEnter={(e) => {
                          e.currentTarget.style.borderColor = "#ef4444";
                          e.currentTarget.style.color = "#ef4444";
                          e.currentTarget.style.background = "rgba(239,68,68,0.06)";
                        }}
                        onMouseLeave={(e) => {
                          e.currentTarget.style.borderColor = "var(--border-subtle)";
                          e.currentTarget.style.color = "var(--muted-foreground)";
                          e.currentTarget.style.background = "transparent";
                        }}
                      >🗑</button>
                    )}
                  </div>
                </div>

                {/* Progress bar and controls for active imports */}
                {isActive && u.status !== "uploading" && (
                  <div style={{ marginTop: 14 }}>
                    <div style={{
                      background: "var(--background)", borderRadius: 6, height: 6,
                      overflow: "hidden", border: "1px solid var(--border-subtle)"
                    }}>
                      <div style={{
                        height: "100%", borderRadius: 6,
                        background: u.status === "paused"
                          ? "linear-gradient(90deg, #f59e0b, #d97706)"
                          : "linear-gradient(90deg, #3b82f6, #8b5cf6)",
                        width: `${u.progress}%`,
                        transition: "width 500ms ease",
                      }} />
                    </div>
                    {/* Inline import controls */}
                    <div style={{ display: "flex", gap: 8, marginTop: 10, justifyContent: "flex-end" }}>
                      {u.status === "processing" && (
                        <button
                          onClick={(e) => { e.stopPropagation(); handlePause(u.id); }}
                          disabled={controlLoading}
                          style={{
                            background: "rgba(245,158,11,0.08)", border: "1px solid rgba(245,158,11,0.3)",
                            borderRadius: 6, padding: "5px 12px", fontSize: 12, fontWeight: 600,
                            color: "#f59e0b", cursor: "pointer", transition: "all 150ms",
                          }}
                        >⏸ Pause</button>
                      )}
                      {u.status === "paused" && (
                        <button
                          onClick={(e) => { e.stopPropagation(); handleResume(u.id); }}
                          disabled={controlLoading}
                          style={{
                            background: "rgba(59,130,246,0.08)", border: "1px solid rgba(59,130,246,0.3)",
                            borderRadius: 6, padding: "5px 12px", fontSize: 12, fontWeight: 600,
                            color: "#3b82f6", cursor: "pointer", transition: "all 150ms",
                          }}
                        >▶ Resume</button>
                      )}
                      {(u.status === "processing" || u.status === "paused") && (
                        <button
                          onClick={(e) => { e.stopPropagation(); handleCancel(u.id); }}
                          disabled={controlLoading}
                          style={{
                            background: "rgba(239,68,68,0.08)", border: "1px solid rgba(239,68,68,0.3)",
                            borderRadius: 6, padding: "5px 12px", fontSize: 12, fontWeight: 600,
                            color: "#ef4444", cursor: "pointer", transition: "all 150ms",
                          }}
                        >✕ Cancel</button>
                      )}
                    </div>
                  </div>
                )}
              </div>
              );
            })}
          </div>
        </div>
      )}

      {/* ── Detail Modal ───────────────────────────────────────────── */}
      {selectedUpload && (
        <div
          style={{
            position: "fixed", top: 0, left: 0, right: 0, bottom: 0,
            background: "rgba(0,0,0,0.6)", backdropFilter: "blur(4px)",
            display: "flex", alignItems: "center", justifyContent: "center",
            zIndex: 1000, padding: 16,
          }}
          onClick={() => setSelectedUpload(null)}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              background: "var(--background)", borderRadius: 16,
              padding: 0, width: "100%", maxWidth: 540,
              border: "1px solid var(--border-subtle)",
              boxShadow: "0 24px 80px rgba(0,0,0,0.35)",
              overflow: "hidden",
            }}
          >
            {/* Header */}
            <div style={{
              padding: "20px 24px", borderBottom: "1px solid var(--border-subtle)",
              display: "flex", justifyContent: "space-between", alignItems: "flex-start",
            }}>
              <div style={{ flex: 1, minWidth: 0, paddingRight: 12 }}>
                <h3 style={{
                  fontSize: 16, fontWeight: 700, color: "var(--foreground)",
                  marginBottom: 6, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                }}>
                  {selectedUpload.file_name}
                </h3>
                <div style={{
                  display: "inline-flex", alignItems: "center", gap: 6,
                  padding: "3px 10px", borderRadius: 12,
                  background: `${getStatusColor(selectedUpload.status)}15`,
                  color: getStatusColor(selectedUpload.status),
                  fontSize: 12, fontWeight: 600, textTransform: "capitalize",
                }}>
                  {getStatusIcon(selectedUpload.status)} {selectedUpload.status}
                </div>
              </div>
              <button
                onClick={() => setSelectedUpload(null)}
                style={{
                  background: "var(--border-subtle)", border: "none",
                  width: 28, height: 28, borderRadius: 8,
                  display: "flex", alignItems: "center", justifyContent: "center",
                  color: "var(--muted-foreground)", cursor: "pointer", fontSize: 14,
                  flexShrink: 0,
                }}
              >✕</button>
            </div>

            {/* Body */}
            <div style={{ padding: "20px 24px" }}>
              {/* Progress bar + controls */}
              {(selectedUpload.status === "processing" || selectedUpload.status === "pending" || selectedUpload.status === "paused") && (
                <div style={{ marginBottom: 20 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
                    <span style={{ fontSize: 13, color: "var(--muted-foreground)" }}>
                      {selectedUpload.status === "paused" ? "Paused" : "Importing..."}
                    </span>
                    <span style={{ fontSize: 13, fontWeight: 600, color: selectedUpload.status === "paused" ? "#f59e0b" : "#3b82f6" }}>
                      {detailStatus?.progress ?? selectedUpload.progress}%
                    </span>
                  </div>
                  <div style={{ background: "var(--border-subtle)", borderRadius: 6, height: 8, overflow: "hidden" }}>
                    <div style={{
                      height: "100%", borderRadius: 6,
                      background: selectedUpload.status === "paused"
                        ? "linear-gradient(90deg, #f59e0b, #d97706)"
                        : "linear-gradient(90deg, #3b82f6, #8b5cf6)",
                      width: `${detailStatus?.progress ?? selectedUpload.progress}%`,
                      transition: "width 500ms ease",
                    }} />
                  </div>
                  {/* Control buttons */}
                  <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
                    {selectedUpload.status === "processing" && (
                      <button
                        onClick={() => handlePause(selectedUpload.id)}
                        disabled={controlLoading}
                        style={{
                          flex: 1, padding: "8px 0", fontSize: 13, fontWeight: 600,
                          background: "rgba(245,158,11,0.08)", color: "#f59e0b",
                          border: "1px solid rgba(245,158,11,0.3)", borderRadius: 8,
                          cursor: controlLoading ? "wait" : "pointer", transition: "all 150ms",
                        }}
                      >⏸ Pause Import</button>
                    )}
                    {selectedUpload.status === "paused" && (
                      <button
                        onClick={() => handleResume(selectedUpload.id)}
                        disabled={controlLoading}
                        style={{
                          flex: 1, padding: "8px 0", fontSize: 13, fontWeight: 600,
                          background: "rgba(59,130,246,0.08)", color: "#3b82f6",
                          border: "1px solid rgba(59,130,246,0.3)", borderRadius: 8,
                          cursor: controlLoading ? "wait" : "pointer", transition: "all 150ms",
                        }}
                      >▶ Resume Import</button>
                    )}
                    {(selectedUpload.status === "processing" || selectedUpload.status === "paused") && (
                      <button
                        onClick={() => handleCancel(selectedUpload.id)}
                        disabled={controlLoading}
                        style={{
                          flex: 1, padding: "8px 0", fontSize: 13, fontWeight: 600,
                          background: "rgba(239,68,68,0.08)", color: "#ef4444",
                          border: "1px solid rgba(239,68,68,0.3)", borderRadius: 8,
                          cursor: controlLoading ? "wait" : "pointer", transition: "all 150ms",
                        }}
                      >✕ Cancel Import</button>
                    )}
                  </div>
                </div>
              )}

              {/* Stats grid */}
              {(() => {
                const s = detailStatus || selectedUpload;
                const totalRows = ("total_contacts" in s ? s.total_contacts : 0) || (detailStatus?.total_rows ?? 0);
                const stats = [
                  { label: "Total Rows", value: formatNumber(totalRows), color: "var(--foreground)", icon: "📊" },
                  { label: "Processed", value: formatNumber(s.processed_rows), color: "#3b82f6", icon: "⚙️" },
                  { label: "Inserted", value: formatNumber(s.inserted_count), color: "#10b981", icon: "✅" },
                  { label: "Skipped", value: formatNumber(s.skipped_count), color: "#f59e0b", icon: "⏭️" },
                  { label: "Overwritten", value: formatNumber(s.overwritten_count), color: "#8b5cf6", icon: "🔄" },
                ];
                return (
                  <div style={{
                    display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 10,
                    marginBottom: 20,
                  }}>
                    {stats.map((stat, idx) => (
                      <div
                        key={stat.label}
                        style={{
                          background: "var(--card-bg)", borderRadius: 10,
                          padding: "12px 14px", border: "1px solid var(--border-subtle)",
                          ...(idx === 0 ? { gridColumn: "1 / -1" } : {}),
                        }}
                      >
                        <div style={{ fontSize: 11, color: "var(--muted-foreground)", marginBottom: 4, display: "flex", alignItems: "center", gap: 4 }}>
                          <span>{stat.icon}</span> {stat.label}
                        </div>
                        <div style={{ fontSize: idx === 0 ? 22 : 18, fontWeight: 700, color: stat.color }}>
                          {stat.value}
                        </div>
                      </div>
                    ))}
                  </div>
                );
              })()}

              {/* Error message */}
              {(selectedUpload.error_message || detailStatus?.error_message) && (
                <div style={{
                  background: "rgba(239,68,68,0.08)", border: "1px solid rgba(239,68,68,0.2)",
                  borderRadius: 10, padding: "12px 16px", fontSize: 13, color: "#ef4444",
                  marginBottom: 16, lineHeight: 1.5,
                }}>
                  ⚠️ {detailStatus?.error_message || selectedUpload.error_message}
                </div>
              )}

              {/* Bucket summary */}
              {(detailStatus?.bucket_summary || selectedUpload.bucket_summary) && (
                <div>
                  <h4 style={{ fontSize: 13, fontWeight: 600, color: "var(--foreground)", marginBottom: 8 }}>
                    Bucket Summary ({(detailStatus?.bucket_summary || selectedUpload.bucket_summary || []).length})
                  </h4>
                  <div style={{
                    maxHeight: 220, overflowY: "auto", borderRadius: 10,
                    border: "1px solid var(--border-subtle)",
                  }}>
                    {(detailStatus?.bucket_summary || selectedUpload.bucket_summary || []).map((b, i) => (
                      <div
                        key={b.name}
                        style={{
                          display: "flex", justifyContent: "space-between", alignItems: "center",
                          padding: "8px 14px",
                          borderBottom: i < (detailStatus?.bucket_summary || selectedUpload.bucket_summary || []).length - 1
                            ? "1px solid var(--border-subtle)" : "none",
                          fontSize: 13,
                        }}
                      >
                        <span style={{ color: "var(--foreground)" }}>{b.name}</span>
                        <span style={{ fontWeight: 600, color: "#3b82f6", fontVariantNumeric: "tabular-nums" }}>
                          {formatNumber(b.count)}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>

            {/* Footer */}
            <div style={{ padding: "16px 24px", borderTop: "1px solid var(--border-subtle)" }}>
              <button
                onClick={() => setSelectedUpload(null)}
                style={{
                  width: "100%", padding: "10px 0",
                  background: "var(--card-bg)", color: "var(--foreground)",
                  border: "1px solid var(--border-subtle)", borderRadius: 8,
                  cursor: "pointer", fontSize: 14, fontWeight: 500,
                  transition: "background 150ms",
                }}
                onMouseEnter={(e) => e.currentTarget.style.background = "var(--border-subtle)"}
                onMouseLeave={(e) => e.currentTarget.style.background = "var(--card-bg)"}
              >Close</button>
            </div>
          </div>
        </div>
      )}
      {/* ── Delete Confirmation Modal ────────────────────────────────── */}
      {deleteTarget && (
        <div
          onClick={() => !deleteLoading && setDeleteTarget(null)}
          style={{
            position: "fixed", inset: 0, zIndex: 9999,
            background: "rgba(0,0,0,0.5)", backdropFilter: "blur(4px)",
            display: "flex", alignItems: "center", justifyContent: "center",
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              background: "var(--background)", borderRadius: 16,
              border: "1px solid var(--border-subtle)",
              width: "100%", maxWidth: 440, overflow: "hidden",
              boxShadow: "0 20px 60px rgba(0,0,0,0.2)",
            }}
          >
            {/* Header */}
            <div style={{ padding: "24px 24px 0" }}>
              <div style={{
                display: "inline-flex", alignItems: "center", justifyContent: "center",
                width: 48, height: 48, borderRadius: "50%",
                background: "rgba(239,68,68,0.1)", marginBottom: 16,
              }}>
                <span style={{ fontSize: 24 }}>⚠️</span>
              </div>
              <h3 style={{ fontWeight: 700, fontSize: 18, color: "var(--foreground)", marginBottom: 8 }}>
                Delete Import
              </h3>
              <p style={{ fontSize: 14, color: "var(--muted-foreground)", lineHeight: 1.6, marginBottom: 4 }}>
                {deleteTarget.status === "uploading" ? (
                  <>This will remove the uploaded file <strong style={{ color: "var(--foreground)" }}>{deleteTarget.file_name}</strong> and its record. The CSV has not been imported yet.</>
                ) : (
                  <>This will permanently delete <strong style={{ color: "var(--foreground)" }}>{deleteTarget.file_name}</strong> and remove <strong style={{ color: "#ef4444" }}>{formatNumber(deleteTarget.total_contacts)} contacts</strong> that were imported with this list.</>
                )}
              </p>
              <p style={{ fontSize: 13, color: "#ef4444", fontWeight: 500, marginTop: 8 }}>
                This action cannot be undone.
              </p>
            </div>

            {/* Actions */}
            <div style={{ padding: "24px", display: "flex", gap: 12 }}>
              <button
                onClick={() => setDeleteTarget(null)}
                disabled={deleteLoading}
                style={{
                  flex: 1, padding: "12px 0", fontSize: 14, fontWeight: 500,
                  background: "var(--card-bg)", color: "var(--foreground)",
                  border: "1px solid var(--border-subtle)", borderRadius: 10,
                  cursor: "pointer", transition: "background 150ms",
                }}
                onMouseEnter={(e) => e.currentTarget.style.background = "var(--border-subtle)"}
                onMouseLeave={(e) => e.currentTarget.style.background = "var(--card-bg)"}
              >Cancel</button>
              <button
                onClick={() => handleDelete(deleteTarget)}
                disabled={deleteLoading}
                style={{
                  flex: 1, padding: "12px 0", fontSize: 14, fontWeight: 600,
                  background: deleteLoading ? "#b91c1c" : "#ef4444",
                  color: "#fff", border: "none", borderRadius: 10,
                  cursor: deleteLoading ? "wait" : "pointer",
                  transition: "background 150ms",
                  opacity: deleteLoading ? 0.7 : 1,
                }}
                onMouseEnter={(e) => !deleteLoading && (e.currentTarget.style.background = "#dc2626")}
                onMouseLeave={(e) => !deleteLoading && (e.currentTarget.style.background = "#ef4444")}
              >{deleteLoading ? "Deleting..." : deleteTarget.status === "uploading" ? "Delete File" : `Delete & Remove ${formatNumber(deleteTarget.total_contacts)} Contacts`}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
