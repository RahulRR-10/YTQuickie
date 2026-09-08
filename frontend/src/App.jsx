import React, { useState, useEffect, useRef } from "react";

const API_BASE = import.meta.env.VITE_API_BASE ?? "";

const WINDOW_TITLE = "YTQuickie v1.0 [Audio Ripper]";

// --- Retro UI building blocks -------------------------------------------------

function Led({ label, active, color = "#00ff66", blink }) {
  const lit = active && !blink;
  return (
    <span className="flex items-center gap-1.5 font-mono text-[11px] uppercase tracking-wider text-zinc-400 select-none">
      <span
        className={`led ${lit ? "animate-led" : ""}`}
        style={{
          background: active ? color : "#3f3f46",
          boxShadow: active ? `0 0 6px ${color}` : "none",
        }}
      />
      {label}
    </span>
  );
}

function RetroButton({ children, onClick, disabled, className = "", tone = "default", type = "button" }) {
  const toneCls =
    tone === "primary" ? "text-led-green"
    : tone === "danger" ? "text-led-red"
    : tone === "amber" ? "text-led-amber"
    : "text-zinc-200";
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`bevel-up chisel-bg font-mono text-[12px] font-bold uppercase tracking-wider select-none px-3 py-1.5 active:translate-y-[1px] active:border-t-black active:border-l-black active:border-b-zinc-500 active:border-r-zinc-500 disabled:opacity-40 disabled:cursor-not-allowed ${toneCls} ${className}`}
    >
      {children}
    </button>
  );
}

function StatusTag({ status }) {
  const map = {
    pending: ["QUEUED", "text-zinc-400"],
    downloading: ["RIP", "text-led-cyan"],
    converting: ["ENC", "text-led-amber"],
    done: ["OK", "text-led-green"],
    failed: ["ERR", "text-led-red"],
    cancelled: ["ABRT", "text-led-red"],
  };
  const [label, color] = map[status] || [String(status || "?").toUpperCase(), "text-zinc-300"];
  return <span className={`text-[10px] font-bold ${color}`}>[{label}]</span>;
}

function SegBar({ pct = 0, status = "pending" }) {
  const SEGMENTS = 24;
  const filled = Math.max(0, Math.min(SEGMENTS, Math.round(((pct || 0) / 100) * SEGMENTS)));
  const tone =
    status === "done" ? "bg-led-green"
    : status === "failed" || status === "cancelled" ? "bg-led-red"
    : status === "converting" ? "bg-led-amber animate-led"
    : status === "downloading" ? "bg-led-green"
    : "bg-zinc-700";
  return (
    <div className="bevel-up bg-black p-1.5 flex gap-[3px]">
      {Array.from({ length: SEGMENTS }).map((_, i) => (
        <span
          key={i}
          className={`h-4 flex-1 ${i < filled ? tone : "bg-zinc-900"}`}
        />
      ))}
    </div>
  );
}

function TitleBar({ onMinimize, onClose, showReset, isDesktop }) {
  return (
    <div
      className="pywebview-drag-region flex items-center justify-between h-10 pl-2 pr-1 select-none bg-gradient-to-b from-[#0b2f54] to-[#071b33] border-b-2 border-black"
      style={{ userSelect: "none", WebkitUserSelect: "none" }}
    >
      <div className="flex items-center gap-2 min-w-0">
        <span className="bevel-in w-6 h-6 flex items-center justify-center bg-black text-led-green text-[13px] leading-none">
          ♫
        </span>
        <span className="font-bitmap text-[14px] font-bold tracking-wide text-white truncate">
          {WINDOW_TITLE}
        </span>
      </div>
      <div className="flex items-center gap-1">
        <button
          onClick={onMinimize}
          className="w-5 h-5 bevel-up chisel-bg text-[10px] font-bold text-zinc-300 leading-none active:translate-y-[1px]"
          title="Minimize"
        >
          _
        </button>
        <button
          onClick={onClose}
          disabled={!showReset && !isDesktop}
          className="w-5 h-5 bevel-up chisel-bg text-[10px] font-bold text-led-red leading-none active:translate-y-[1px] disabled:opacity-30"
          title="Close"
        >
          X
        </button>
      </div>
    </div>
  );
}

function MenuStrip({ onSettings }) {
  return (
    <div className="flex gap-5 px-3 py-1.5 border-b-2 border-black bg-[#101014] font-mono text-[12px] text-zinc-300 select-none">
      <span
        onClick={onSettings}
        className="px-1 hover:bg-led-green hover:text-black cursor-pointer"
      >
        Settings
      </span>
    </div>
  );
}

// --- Main App -----------------------------------------------------------------

export default function App() {
  // Navigation & Step State
  // Steps: 'input' | 'preview' | 'processing' | 'completed'
  const [step, setStep] = useState("input");

  // Playlist Metadata
  const [url, setUrl] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [playlist, setPlaylist] = useState(null);
  const [selectedIds, setSelectedIds] = useState(new Set());

  // Job & Progress State
  const [jobId, setJobId] = useState(null);
  const [jobStatus, setJobStatus] = useState(null);
  const [tracksStatus, setTracksStatus] = useState({});
  const eventSourceRef = useRef(null);

  // Settings State
  const [downloadDir, setDownloadDir] = useState("");
  const [settingsSaved, setSettingsSaved] = useState(false);
  const settingsReturnRef = useRef("input");
  const [archivePath, setArchivePath] = useState(null);

  // Clean up SSE connection on unmount
  useEffect(() => {
    return () => {
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
      }
    };
  }, []);

  // Load persisted settings on mount
  useEffect(() => {
    fetch(`${API_BASE}/api/settings`)
      .then((r) => r.json())
      .then((d) => setDownloadDir(d.download_dir || ""))
      .catch(() => {});
  }, []);

  // Format track duration (seconds -> mm:ss)
  const formatDuration = (sec) => {
    if (!sec) return "--:--";
    const minutes = Math.floor(sec / 60);
    const remainingSecs = Math.floor(sec % 60);
    return `${minutes}:${remainingSecs < 10 ? "0" : ""}${remainingSecs}`;
  };

  // 1. Fetch Playlist Metadata
  const handleFetchPlaylist = async (e) => {
    e.preventDefault();
    if (!url.trim()) return;

    setLoading(true);
    setError(null);

    try {
      const res = await fetch(`${API_BASE}/api/playlist/fetch`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });

      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to fetch playlist");

      setPlaylist(data);
      setSelectedIds(new Set(data.tracks.map((t) => t.id)));
      setStep("preview");
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  // Toggle single selection
  const toggleSelect = (id) => {
    const next = new Set(selectedIds);
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
    }
    setSelectedIds(next);
  };

  // Toggle Select All
  const toggleSelectAll = () => {
    if (selectedIds.size === playlist.tracks.length) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(playlist.tracks.map((t) => t.id)));
    }
  };

  // 2. Start Download Job
  const handleStartDownload = async () => {
    if (selectedIds.size === 0) return;

    setLoading(true);
    setError(null);

    const titleLookup = {};
    playlist.tracks.forEach((t) => {
      if (selectedIds.has(t.id)) {
        titleLookup[t.id] = t.title;
      }
    });

    try {
      const res = await fetch(`${API_BASE}/api/jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          video_ids: Array.from(selectedIds),
          titles: titleLookup,
        }),
      });

      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to create job");

      setJobId(data.job_id);
      setStep("processing");
      connectSSE(data.job_id);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  // 3. Connect to SSE Stream
  const connectSSE = (id) => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
    }

    const sse = new EventSource(`${API_BASE}/api/jobs/${id}/stream`);
    eventSourceRef.current = sse;

    // Snapshot event
    sse.addEventListener("state", (e) => {
      const initial = JSON.parse(e.data);
      setJobStatus(initial.status);
      setTracksStatus(initial.tracks || {});
    });

    // Per-track / overall updates
    sse.addEventListener("update", (e) => {
      const payload = JSON.parse(e.data);

      if (payload.type === "job_status") {
        setJobStatus(payload.status);
        if (payload.status === "completed") {
          setStep("completed");
          sse.close();
        } else if (payload.status === "cancelled") {
          sse.close();
        }
      } else if (payload.type === "track_update") {
        setTracksStatus((prev) => ({
          ...prev,
          [payload.track.video_id]: payload.track,
        }));
      }
    });

    sse.onerror = () => {
      // Reconnection or close logic handled natively by browser EventSource
    };
  };

  // 4. Cancel Job
  const handleCancelJob = async () => {
    if (!jobId) return;
    try {
      await fetch(`${API_BASE}/api/jobs/${jobId}`, { method: "DELETE" });
      if (eventSourceRef.current) eventSourceRef.current.close();
      setJobStatus("cancelled");
    } catch (err) {
      console.error("Cancel failed:", err);
    }
  };

  // 5. Reset App
  const handleReset = () => {
    if (eventSourceRef.current) eventSourceRef.current.close();
    setStep("input");
    setUrl("");
    setPlaylist(null);
    setSelectedIds(new Set());
    setJobId(null);
    setJobStatus(null);
    setTracksStatus({});
    setError(null);
    setArchivePath(null);
    setSettingsSaved(false);
  };

  // 5b. Go back to the previous step
  const goBack = async () => {
    if (step === "processing") {
      await handleCancelJob();
    } else if (eventSourceRef.current) {
      eventSourceRef.current.close();
    }
    setError(null);
    if (step === "completed") {
      setStep("preview");
    } else if (step === "preview") {
      setStep("input");
    }
  };

  // 6. Window controls (desktop only; browser falls back to reset)
  const isDesktop = !!window.pywebview?.api;
  const handleMinimize = () => {
    window.pywebview?.api?.minimize?.();
  };
  const handleClose = () => {
    if (window.pywebview?.api?.close) {
      window.pywebview.api.close();
    } else {
      handleReset();
    }
  };

  // 6b. Settings view
  const openSettings = () => {
    if (eventSourceRef.current) eventSourceRef.current.close();
    settingsReturnRef.current = step;
    setError(null);
    setSettingsSaved(false);
    setStep("settings");
  };

  const closeSettings = () => {
    setError(null);
    setStep(settingsReturnRef.current);
  };

  const handleSaveSettings = async () => {
    setLoading(true);
    setError(null);
    setSettingsSaved(false);
    try {
      const res = await fetch(`${API_BASE}/api/settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ download_dir: downloadDir }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to save settings");
      setDownloadDir(data.download_dir);
      setSettingsSaved(true);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleBrowseFolder = async () => {
    try {
      const raw = await window.pywebview.api.select_download_folder();
      if (raw) setDownloadDir(raw);
    } catch (err) {
      setError(err.message || "Failed to select folder");
    }
  };

  const failedCount = Object.values(tracksStatus).filter((t) => t.status === "failed").length;
  const doneCount = Object.values(tracksStatus).filter((t) => t.status === "done").length;
  const failedTracks = Array.from(selectedIds)
    .map((vid) => ({ vid, ...(playlist?.tracks.find((t) => t.id === vid) || {}) }))
    .filter((t) => tracksStatus[t.vid]?.status === "failed")
    .map((t) => ({ vid: t.vid, title: t.title, error: tracksStatus[t.vid].error }));

  const statusLine =
    step === "input" ? "AWAITING URL"
    : step === "settings" ? "SETTINGS"
    : step === "preview" ? `SELECTED ${selectedIds.size}/${playlist ? playlist.returned_tracks : 0}`
    : step === "processing" ? `JOB ${jobStatus || "-"}`
    : "ARCHIVE READY";

  // 7. Download archive (auto-save in desktop, browser download as fallback)
  const handleDownloadArchive = async () => {
    try {
      if (window.pywebview?.api) {
        const raw = await window.pywebview.api.download_zip(jobId);
        const res = typeof raw === "string" ? JSON.parse(raw) : raw;
        if (res?.ok) {
          setArchivePath(res.path);
          setError(null);
        } else {
          setError(res?.error || "Download failed");
        }
      } else {
        const a = document.createElement("a");
        a.href = `${API_BASE}/api/jobs/${jobId}/download`;
        a.download = "";
        document.body.appendChild(a);
        a.click();
        a.remove();
      }
    } catch (err) {
      setError(err.message || "Download failed");
    }
  };

  return (
    <div className="crt h-screen w-screen text-zinc-200 flex flex-col font-mono relative overflow-hidden">
      <div className="scanlines" />

      <div className="flex-1 min-h-0 w-full bevel-up chisel-bg flex flex-col">
        <TitleBar
          onMinimize={handleMinimize}
          onClose={handleClose}
          showReset={step !== "input"}
          isDesktop={isDesktop}
        />
        <MenuStrip onSettings={openSettings} />

        <div className="flex-1 min-h-0 overflow-y-auto flex flex-col gap-4 p-3 sm:p-4">
          {step !== "input" && step !== "settings" && (
            <div className="flex items-center justify-between gap-2">
              <RetroButton onClick={goBack} disabled={loading}>← BACK</RetroButton>
            </div>
          )}

          {/* Error Alert */}
          {error && (
            <div className="bevel-in bg-black p-3 flex items-start gap-2 text-led-red">
              <span className="text-[12px] font-bold">&gt;&gt; ERROR:</span>
              <p className="text-[12px] leading-relaxed break-words">{error}</p>
            </div>
          )}

          {/* STEP 1: Input URL */}
          {step === "input" && (
            <div className="flex flex-col gap-3">
              <div className="bevel-in bg-black px-4 py-3 flex items-center justify-between gap-2">
                <span className="text-[14px] font-bold text-led-green uppercase tracking-wider">
                  &gt;&gt; Tape-deck / URL Receiver
                </span>
                <span className="text-[11px] text-zinc-500">SRC: YOUTUBE</span>
              </div>

              <div className="bevel-in bg-black px-3 py-2">
                <div className="text-[11px] text-zinc-500 uppercase">&gt; Paste YouTube URL below</div>
                <form onSubmit={handleFetchPlaylist} className="flex flex-col gap-4 pt-2">
                  <div className="bevel-up bg-black px-3 py-2 flex items-center gap-2">
                    <span className="text-[12px] text-led-cyan">URL&gt;</span>
                    <input
                      type="text"
                      placeholder="YouTube playlist or video URL"
                      value={url}
                      onChange={(e) => setUrl(e.target.value)}
                      className="flex-1 bg-transparent font-mono text-[14px] text-led-green placeholder-zinc-600 focus:outline-none caret-led-green"
                      required
                    />
                  </div>

                  <div className="flex items-center justify-between flex-wrap gap-3">
                    <div className="flex items-center gap-5">
                      <Led label="READY" active={!loading && !error} />
                      <Led label="BUSY" active={loading} color="#ffb000" blink={loading} />
                      <Led label="ERR" active={!!error} color="#ff3355" blink={!!error} />
                    </div>
                    <RetroButton type="submit" tone="primary" disabled={loading}>
                      {loading ? "COMMS... BUSY" : "FETCH TRACKS"}
                    </RetroButton>
                  </div>
                </form>
              </div>

              <div className="bevel-in bg-black px-4 py-3 flex items-center gap-5">
                <Led label="PWR" active color="#00ff66" />
                <Led label="LINK" active color="#00e5ff" />
                <span className="text-[11px] text-zinc-600 uppercase tracking-wider">
                  Max 50 tracks per rip · 192kbps encode
                </span>
              </div>
            </div>
          )}

          {/* STEP 2: Preview & Select Tracks */}
          {step === "preview" && playlist && (
            <div className="flex flex-col gap-3">
              <div className="bevel-in bg-black px-4 py-3 flex flex-col gap-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[16px] font-bold text-led-green uppercase truncate">
                    {playlist.playlist_title}
                  </span>
                  <span className="text-[12px] text-zinc-500 whitespace-nowrap">
                    {playlist.returned_tracks}/{playlist.total_tracks_in_playlist} ITEMS
                    {playlist.truncated ? " (CAP 50)" : ""}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-[11px] text-zinc-500 uppercase">&gt; Tracks ready for selection</span>
                  <Led label={`${selectedIds.size} SEL`} active={selectedIds.size > 0} color="#00e5ff" />
                </div>
              </div>

              <div className="bevel-in bg-black">
                <div className="flex items-center gap-2 px-3 py-2 border-b-2 border-black bg-[#141418] font-mono text-[12px] font-bold tracking-wider text-led-amber select-none">
                  <span className="w-8">[x]</span>
                  <span className="flex-1">TRACK TITLE</span>
                  <span className="w-16 text-right">LEN</span>
                  <span className="w-24 text-right">BITRATE</span>
                </div>
                <div className="max-h-[480px] overflow-y-auto">
                  {playlist.tracks.map((t, i) => {
                    const isSelected = selectedIds.has(t.id);
                    return (
                      <div
                        key={t.id}
                        onClick={() => toggleSelect(t.id)}
                        className={`flex items-center gap-2 px-3 py-1.5 font-mono text-[13px] cursor-pointer select-none border-b border-zinc-900 ${
                          i % 2 ? "bg-zinc-900/70" : "bg-black"
                        } ${
                          isSelected ? "text-led-green" : "text-zinc-500 hover:text-zinc-200"
                        }`}
                      >
                        <span className="w-8">{isSelected ? "[+]" : "[ ]"}</span>
                        <span className="flex-1 truncate">{t.title}</span>
                        <span className="w-16 text-right text-zinc-400">
                          {formatDuration(t.duration)}
                        </span>
                        <span className="w-24 text-right text-led-cyan">192k</span>
                      </div>
                    );
                  })}
                </div>
              </div>

              <div className="flex items-center gap-3 flex-wrap">
                <RetroButton onClick={toggleSelectAll}>
                  {selectedIds.size === playlist.tracks.length ? "CLEAR" : "SELECT ALL"}
                </RetroButton>
                <RetroButton tone="primary" onClick={handleStartDownload} disabled={selectedIds.size === 0 || loading}>
                  {loading ? "CONVERTING..." : `CONVERT MP3s (${selectedIds.size})`}
                </RetroButton>
              </div>
            </div>
          )}

          {/* STEP 3: Active Processing & SSE Streaming */}
          {step === "processing" && (
            <div className="flex flex-col gap-3">
              <div className="bevel-in bg-black px-4 py-3 flex items-center justify-between gap-2">
                <span className="text-[14px] font-bold text-led-green uppercase">
                  &gt;&gt; Tape deck active — rip sequence running
                </span>
                <Led label="REC" active color="#ff3355" blink />
              </div>

              <div className="bevel-in bg-black p-2.5 flex flex-col gap-2.5 max-h-[560px] overflow-y-auto">
                <div className="text-[12px] text-zinc-500">
                  &gt; JOB STATUS: <span className="text-led-amber uppercase">{jobStatus}</span>
                </div>
                {Array.from(selectedIds).map((vid) => {
                  const track = tracksStatus[vid] || { status: "pending", progress: 0 };
                  const originalMeta = playlist?.tracks.find((t) => t.id === vid);

                  return (
                    <div key={vid} className="border border-zinc-900 p-2.5 flex flex-col gap-1.5 bg-zinc-950/50">
                      <div className="flex items-center justify-between gap-2 font-mono text-[12px]">
                        <span className="truncate text-zinc-200">{originalMeta?.title || vid}</span>
                        <span className="flex items-center gap-2 whitespace-nowrap">
                          <StatusTag status={track.status} />
                          {track.status === "downloading" && (
                            <span className="text-led-cyan">({track.progress}%)</span>
                          )}
                          {track.status === "converting" && (
                            <span className="text-led-amber">44.1kHz</span>
                          )}
                        </span>
                      </div>
                      <SegBar
                        pct={
                          track.status === "downloading" ? track.progress
                          : track.status === "done" ? 100
                          : track.status === "converting" ? 100
                          : 0
                        }
                        status={track.status}
                      />
                    </div>
                  );
                })}
              </div>

              {jobStatus !== "cancelled" && (
                <RetroButton tone="danger" onClick={handleCancelJob}>
                  [ABORT OPERATION]
                </RetroButton>
              )}
            </div>
          )}

          {/* STEP 4: Completed View */}
          {step === "completed" && (
            <div className="flex flex-col items-center gap-4 text-center">
              <div className="bevel-in bg-black w-full px-3 py-6 flex flex-col items-center gap-1.5">
                <span className="font-bitmap text-[24px] font-bold tracking-widest text-led-green">
                  RIPPING COMPLETE
                </span>
                <span className="text-[12px] text-zinc-400 uppercase tracking-wider">
                  All tracks cached — archive ready
                </span>
              </div>

              <div className="bevel-in bg-black w-full px-4 py-3 font-mono text-[13px] text-zinc-300 flex flex-wrap justify-center gap-x-6 gap-y-1.5 uppercase">
                <span>Tracks Encoded: <span className="text-led-green font-bold">{doneCount}</span></span>
                <span>Skipped: <span className="text-led-red font-bold">{failedCount}</span></span>
                <span>Format: <span className="text-led-cyan font-bold">MPEG-1 L3</span></span>
              </div>

              {failedTracks.length > 0 && (
                <div className="w-full bevel-in bg-black p-2.5 flex flex-col gap-1.5 text-left max-h-48 overflow-y-auto">
                  <div className="text-[12px] font-bold text-led-red">
                    &gt; {failedTracks.length} TRACK(S) FAILED:
                  </div>
                  {failedTracks.map((t) => (
                    <div key={t.vid} className="flex flex-col font-mono text-[12px] border-t border-zinc-900 pt-1.5">
                      <span className="text-zinc-300 truncate">{t.title || t.vid}</span>
                      <span className="text-led-red">&gt; {t.error || "UNKNOWN ERROR"}</span>
                    </div>
                  ))}
                </div>
              )}

              {archivePath && (
                <div className="w-full bevel-in bg-black px-3 py-2 flex flex-col gap-1 text-left">
                  <span className="text-[12px] font-bold text-led-green">&gt;&gt; SAVED TO:</span>
                  <span className="text-[12px] text-zinc-300 break-all">{archivePath}</span>
                </div>
              )}

              <button
                onClick={handleDownloadArchive}
                className="bevel-up chisel-bg px-8 py-4 font-mono font-bold text-[15px] text-led-green uppercase tracking-widest text-center"
              >
                ↓ DOWNLOAD ARCHIVE (.ZIP)
              </button>
            </div>
          )}

          {/* STEP 5: Settings */}
          {step === "settings" && (
            <div className="flex flex-col gap-3">
              <div className="bevel-in bg-black px-4 py-3 flex items-center justify-between gap-2">
                <span className="text-[14px] font-bold text-led-green uppercase tracking-wider">
                  &gt;&gt; Settings / Configuration
                </span>
                <span className="text-[11px] text-zinc-500">SYS: CONFIG</span>
              </div>

              <div className="bevel-in bg-black px-3 py-2">
                <div className="text-[11px] text-zinc-500 uppercase">&gt; Download location</div>
                <div className="flex flex-col gap-3 pt-2">
                  <div className="bevel-up bg-black px-3 py-2 flex items-center gap-2">
                    <span className="text-[12px] text-led-cyan">DIR&gt;</span>
                    <input
                      type="text"
                      value={downloadDir}
                      onChange={(e) => setDownloadDir(e.target.value)}
                      placeholder="Default: Downloads folder"
                      className="flex-1 bg-transparent font-mono text-[13px] text-led-green placeholder-zinc-600 focus:outline-none caret-led-green"
                    />
                  </div>

                  {isDesktop && (
                    <RetroButton onClick={handleBrowseFolder} disabled={loading}>
                      [BROWSE FOLDER]
                    </RetroButton>
                  )}

                  {settingsSaved && (
                    <div className="bevel-in bg-black p-2 text-led-green text-[12px]">
                      &gt;&gt; SETTINGS SAVED
                    </div>
                  )}

                  <div className="flex items-center gap-3 flex-wrap">
                    <RetroButton tone="primary" onClick={handleSaveSettings} disabled={loading}>
                      {loading ? "SAVING..." : "SAVE SETTINGS"}
                    </RetroButton>
                    <RetroButton onClick={closeSettings} disabled={loading}>
                      ← BACK
                    </RetroButton>
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Status Bar */}
        <div className="flex items-center justify-between gap-2 px-2 h-8 border-t-2 border-black bg-[#0d0d11] font-mono text-[11px] text-zinc-500 uppercase select-none">
          <span className="flex items-center gap-2 min-w-0">
            <span
              className="led"
              style={{
                background: step === "failed" || error ? "#ff3355" : "#00ff66",
                boxShadow: step === "failed" || error ? "0 0 6px #ff3355" : "0 0 6px #00ff66",
              }}
            />
            <span className="truncate">&gt;&gt; {statusLine}</span>
          </span>
          <span className="hidden sm:inline whitespace-nowrap">YTQ v1.0 · 192KBPS</span>
        </div>
      </div>
    </div>
  );
}