import React, { useState, useEffect, useRef } from "react";
import { 
  Download, 
  RotateCcw, 
  Music, 
  CheckCircle2, 
  AlertCircle, 
  Loader2, 
  XCircle,
  FileArchive
} from "lucide-react";

const API_BASE = "http://localhost:8000";

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

  // Clean up SSE connection on unmount
  useEffect(() => {
    return () => {
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
      }
    };
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
  };

  const failedCount = Object.values(tracksStatus).filter((t) => t.status === "failed").length;
  const doneCount = Object.values(tracksStatus).filter((t) => t.status === "done").length;
  const failedTracks = Array.from(selectedIds)
    .map((vid) => ({ vid, ...(playlist?.tracks.find((t) => t.id === vid) || {}) }))
    .filter((t) => tracksStatus[t.vid]?.status === "failed")
    .map((t) => ({ vid: t.vid, title: t.title, error: tracksStatus[t.vid].error }));

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col items-center p-4 sm:p-8 font-sans">
      <header className="w-full max-w-4xl flex items-center justify-between pb-6 mb-8 border-b border-slate-800">
        <div className="flex items-center gap-3">
          <div className="p-2 bg-indigo-600 rounded-lg text-white">
            <Music className="w-6 h-6" />
          </div>
          <div>
            <h1 className="text-xl font-bold tracking-tight">Playlist to MP3</h1>
            <p className="text-xs text-slate-400">Fast batch audio extraction</p>
          </div>
        </div>
        {step !== "input" && (
          <button
            onClick={handleReset}
            className="flex items-center gap-1.5 text-xs text-slate-400 hover:text-white bg-slate-900 border border-slate-700 px-3 py-1.5 rounded-md transition"
          >
            <RotateCcw className="w-3.5 h-3.5" /> Start Over
          </button>
        )}
      </header>

      <main className="w-full max-w-4xl">
        {/* Error Alert */}
        {error && (
          <div className="mb-6 p-4 bg-red-950/60 border border-red-800/80 rounded-xl flex items-center gap-3 text-red-200">
            <AlertCircle className="w-5 h-5 flex-shrink-0" />
            <p className="text-sm">{error}</p>
          </div>
        )}

        {/* STEP 1: Input URL */}
        {step === "input" && (
          <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 sm:p-10 text-center shadow-xl">
            <h2 className="text-2xl font-semibold mb-2">Fetch YouTube Playlist</h2>
            <p className="text-slate-400 text-sm mb-6">
              Paste a public playlist URL to select and extract MP3 tracks.
            </p>
            <form onSubmit={handleFetchPlaylist} className="flex flex-col sm:flex-row gap-3 max-w-xl mx-auto">
              <input
                type="text"
                placeholder="https://www.youtube.com/playlist?list=..."
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                className="flex-1 px-4 py-3 bg-slate-950 border border-slate-700 rounded-xl text-sm focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
                required
              />
              <button
                type="submit"
                disabled={loading}
                className="px-6 py-3 bg-indigo-600 hover:bg-indigo-500 font-medium rounded-xl text-sm transition flex items-center justify-center gap-2 disabled:opacity-50"
              >
                {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : "Fetch"}
              </button>
            </form>
          </div>
        )}

        {/* STEP 2: Preview & Select Tracks */}
        {step === "preview" && playlist && (
          <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl flex flex-col gap-6">
            <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4 pb-4 border-b border-slate-800">
              <div>
                <h2 className="text-xl font-bold">{playlist.playlist_title}</h2>
                <p className="text-xs text-slate-400 mt-1">
                  Showing {playlist.returned_tracks} of {playlist.total_tracks_in_playlist} items
                  {playlist.truncated && " (Capped at 50 max for MVP)"}
                </p>
              </div>
              <div className="flex items-center gap-3">
                <button
                  onClick={toggleSelectAll}
                  className="text-xs font-medium px-3 py-2 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded-lg transition"
                >
                  {selectedIds.size === playlist.tracks.length ? "Deselect All" : "Select All"}
                </button>
                <button
                  onClick={handleStartDownload}
                  disabled={selectedIds.size === 0 || loading}
                  className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 font-medium rounded-lg text-sm transition flex items-center gap-2 disabled:opacity-50"
                >
                  {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : `Download (${selectedIds.size})`}
                </button>
              </div>
            </div>

            {/* Track Selection Table */}
            <div className="divide-y divide-slate-800/60 max-h-[550px] overflow-y-auto pr-2">
              {playlist.tracks.map((t) => {
                const isSelected = selectedIds.has(t.id);
                return (
                  <div
                    key={t.id}
                    onClick={() => toggleSelect(t.id)}
                    className={`flex items-center gap-4 py-3 px-2 rounded-lg cursor-pointer transition select-none ${
                      isSelected ? "hover:bg-slate-800/40" : "opacity-40 hover:opacity-75"
                    }`}
                  >
                    <input
                      type="checkbox"
                      checked={isSelected}
                      onChange={() => {}}
                      className="rounded border-slate-700 text-indigo-600 focus:ring-0 w-4 h-4 pointer-events-none"
                    />
                    <img
                      src={t.thumbnail || "https://placehold.co/80x45/020617/white?text=Audio"}
                      alt=""
                      className="w-16 h-10 object-cover rounded bg-slate-950 flex-shrink-0"
                    />
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium truncate">{t.title}</p>
                      <p className="text-xs text-slate-500">{formatDuration(t.duration)}</p>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* STEP 3: Active Processing & SSE Streaming */}
        {step === "processing" && (
          <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl flex flex-col gap-6">
            <div className="flex justify-between items-center pb-4 border-b border-slate-800">
              <div>
                <h2 className="text-lg font-bold">Processing Audio Tracks</h2>
                <p className="text-xs text-slate-400 capitalize">Job Status: {jobStatus}</p>
              </div>
              {jobStatus !== "cancelled" && (
                <button
                  onClick={handleCancelJob}
                  className="text-xs text-red-400 hover:text-red-300 border border-red-900 bg-red-950/40 px-3 py-1.5 rounded-md transition flex items-center gap-1.5"
                >
                  <XCircle className="w-3.5 h-3.5" /> Cancel Job
                </button>
              )}
            </div>

            {/* Per-Track Progress Rows */}
            <div className="space-y-3 max-h-[500px] overflow-y-auto pr-1">
              {Array.from(selectedIds).map((vid) => {
                const track = tracksStatus[vid] || { status: "pending", progress: 0 };
                const originalMeta = playlist?.tracks.find((t) => t.id === vid);

                return (
                  <div key={vid} className="p-3 bg-slate-950 rounded-xl border border-slate-800/80">
                    <div className="flex justify-between items-center text-xs mb-1.5">
                      <span className="font-medium text-slate-300 truncate max-w-md">
                        {originalMeta?.title || vid}
                      </span>
                      <span className="capitalize text-slate-400 text-[11px] font-mono">
                        {track.status} {track.status === "downloading" && `(${track.progress}%)`}
                      </span>
                    </div>

                    {/* Progress Bar Container */}
                    <div className="w-full bg-slate-900 h-1.5 rounded-full overflow-hidden">
                      <div
                        className={`h-full transition-all duration-300 ${
                          track.status === "done"
                            ? "bg-emerald-500 w-full"
                            : track.status === "failed" || track.status === "cancelled"
                            ? "bg-red-500 w-full"
                            : track.status === "converting"
                            ? "bg-amber-500 w-full animate-pulse"
                            : "bg-indigo-500"
                        }`}
                        style={{
                          width:
                            track.status === "downloading"
                              ? `${track.progress}%`
                              : undefined,
                        }}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* STEP 4: Completed View */}
        {step === "completed" && (
          <div className="bg-slate-900 border border-slate-800 rounded-2xl p-8 text-center shadow-xl flex flex-col items-center gap-5">
            <div className="w-12 h-12 bg-emerald-950 text-emerald-400 border border-emerald-800/50 rounded-full flex items-center justify-center">
              <CheckCircle2 className="w-6 h-6" />
            </div>
            <div>
              <h2 className="text-xl font-bold">Conversion Complete!</h2>
              <p className="text-slate-400 text-sm mt-1">
                {doneCount} of {selectedIds.size} tracks converted successfully.
              </p>
            </div>

            {/* Report: failed tracks */}
            {failedTracks.length > 0 && (
              <div className="w-full text-left bg-slate-950 border border-red-900/50 rounded-xl p-4 flex flex-col gap-3">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-semibold text-red-300">
                    {failedTracks.length} Failed ({failedCount} total errors)
                  </h3>
                  <div className="flex gap-3 text-xs">
                    <span className="text-emerald-400">{doneCount} converted</span>
                    <span className="text-red-400">{failedCount} failed</span>
                  </div>
                </div>
                <div className="divide-y divide-red-900/30 max-h-40 overflow-y-auto">
                  {failedTracks.map(({ vid, title, error }) => (
                    <div key={vid} className="py-2 flex flex-col gap-0.5">
                      <span className="text-xs font-medium text-red-200 truncate">
                        {title || vid}
                      </span>
                      <span className="text-[11px] text-red-400/80 break-words">
                        {error || "Unknown error"}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <a
              href={`${API_BASE}/api/jobs/${jobId}/download`}
              download
              className="px-6 py-3 bg-emerald-600 hover:bg-emerald-500 text-white font-medium rounded-xl text-sm transition flex items-center gap-2 shadow-lg shadow-emerald-900/30"
            >
              <FileArchive className="w-4 h-4" /> Download Available MP3s (.ZIP)
            </a>
          </div>
        )}
      </main>
    </div>
  );
}
