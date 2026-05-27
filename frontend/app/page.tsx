"use client";

import { useCallback, useEffect, useState } from "react";

const API = "/api/backend";

// ── Types ──────────────────────────────────────────────────────────────────

type CacheStatus = {
  total_images: number;
  last_updated: string | null;
  cache_size_mb: number;
  rebuild_running: boolean;
};

type MatchEntry = {
  timestamp: string;
  sender: string;
  query_url: string;
  matches: { name: string; score: number }[];
};

type CatalogItem = {
  id: string;
  name: string;
  stock: string;
  folder_path: string[];
  color_tags: string[];
  image_url: string;
};

// ── Hooks ──────────────────────────────────────────────────────────────────

function useAutoRefresh<T>(url: string, intervalMs = 5000) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetch_ = useCallback(async () => {
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData(await res.json());
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  }, [url]);

  useEffect(() => {
    fetch_();
    const id = setInterval(fetch_, intervalMs);
    return () => clearInterval(id);
  }, [fetch_, intervalMs]);

  return { data, loading, error, refresh: fetch_ };
}

// ── Main ───────────────────────────────────────────────────────────────────

type Tab = "dashboard" | "catalog" | "history";

export default function App() {
  const [tab, setTab] = useState<Tab>("dashboard");

  const { data: status, loading: statusLoading, error: statusError, refresh: refreshStatus } =
    useAutoRefresh<CacheStatus>(`${API}/admin/status`, 4000);
  const { data: matchesData } =
    useAutoRefresh<{ matches: MatchEntry[] }>(`${API}/admin/matches?limit=50`, 5000);
  const { data: catalogData, refresh: refreshCatalog } =
    useAutoRefresh<{ items: CatalogItem[] }>(`${API}/admin/catalog`, 10000);

  const [rebuilding, setRebuilding] = useState(false);
  const [msg, setMsg] = useState("");

  async function triggerRebuildAll() {
    setRebuilding(true);
    setMsg("");
    try {
      const res = await fetch(`${API}/admin/rebuild-all`, { method: "POST" });
      const body = await res.json();
      setMsg(
        body.status === "started" ? "Rebuilding cache + color index — this takes a few minutes."
          : body.status === "already_running" ? "Already running…"
          : body.status
      );
      refreshStatus();
      setTimeout(() => refreshCatalog(), 30000);
    } catch { setMsg("Failed to contact backend."); }
    finally { setRebuilding(false); }
  }

  const tabs: { id: Tab; label: string }[] = [
    { id: "dashboard", label: "Dashboard" },
    { id: "catalog", label: `Catalog (${catalogData?.items?.length ?? "…"})` },
    { id: "history", label: `History (${matchesData?.matches?.length ?? "…"})` },
  ];

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100 font-sans">
      {/* Header */}
      <div className="border-b border-zinc-800 bg-zinc-900/60 backdrop-blur sticky top-0 z-10">
        <div className="max-w-7xl mx-auto px-6 py-4 flex items-center justify-between">
          <div>
            <h1 className="text-xl font-bold text-white tracking-tight">WA Image Matcher</h1>
            <p className="text-zinc-500 text-xs mt-0.5">Guru Kripa · CNN similarity · Google Drive</p>
          </div>
          <div className="flex items-center gap-2">
            <span className={`w-2 h-2 rounded-full ${statusError ? "bg-red-500" : "bg-green-500"}`} />
            <span className="text-xs text-zinc-400">{statusError ? "offline" : "online"}</span>
          </div>
        </div>

        {/* Tabs */}
        <div className="max-w-7xl mx-auto px-6 flex gap-1 pb-0">
          {tabs.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`px-4 py-2 text-sm font-medium rounded-t-lg transition-colors border-b-2 ${
                tab === t.id
                  ? "border-green-500 text-green-400 bg-zinc-800/50"
                  : "border-transparent text-zinc-500 hover:text-zinc-300"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>

      <div className="max-w-7xl mx-auto px-6 py-6">
        {tab === "dashboard" && (
          <DashboardTab
            status={status}
            statusLoading={statusLoading}
            statusError={statusError}
            rebuilding={rebuilding}
            msg={msg}
            onRebuildAll={triggerRebuildAll}
          />
        )}
        {tab === "catalog" && (
          <CatalogTab items={catalogData?.items ?? []} />
        )}
        {tab === "history" && (
          <HistoryTab matches={matchesData?.matches ?? []} />
        )}
      </div>
    </div>
  );
}

// ── Dashboard Tab ──────────────────────────────────────────────────────────

function DashboardTab({
  status, statusLoading, statusError,
  rebuilding, msg, onRebuildAll,
}: {
  status: CacheStatus | null;
  statusLoading: boolean;
  statusError: string | null;
  rebuilding: boolean;
  msg: string;
  onRebuildAll: () => void;
}) {
  return (
    <div className="space-y-6">
      {/* Stat cards */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <StatCard
          label="Indexed images"
          value={statusLoading ? "…" : String(status?.total_images ?? 0)}
          sub={statusError ? "backend unreachable" : ""}
          accent={statusError ? "red" : "green"}
        />
        <StatCard
          label="Cache size"
          value={statusLoading ? "…" : `${status?.cache_size_mb ?? 0} MB`}
        />
        <StatCard
          label="Last indexed"
          value={
            statusLoading ? "…"
              : status?.last_updated
              ? new Date(status.last_updated).toLocaleString()
              : "Never"
          }
        />
      </div>

      {/* Actions */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-5 space-y-4">
        <h2 className="text-sm font-semibold text-zinc-300 uppercase tracking-widest">Actions</h2>
        <div className="flex flex-wrap gap-3">
          <button
            onClick={onRebuildAll}
            disabled={rebuilding || !!status?.rebuild_running}
            className="px-5 py-2.5 rounded-lg bg-green-600 text-white font-medium text-sm
              hover:bg-green-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {status?.rebuild_running ? "Rebuilding…" : rebuilding ? "Starting…" : "Rebuild & Index from Drive"}
          </button>
        </div>
        {msg && <p className="text-sm text-zinc-400">{msg}</p>}
        <p className="text-xs text-zinc-600">
          Scans Google Drive, downloads images, extracts CNN embeddings, then builds color + folder index.
        </p>
      </div>
    </div>
  );
}

// ── Catalog Tab ────────────────────────────────────────────────────────────

function CatalogTab({ items }: { items: CatalogItem[] }) {
  const [search, setSearch] = useState("");

  const filtered = items.filter((item) => {
    if (!search.trim()) return true;
    const q = search.toLowerCase();
    return (
      item.stock.toLowerCase().includes(q) ||
      item.folder_path.some((f) => f.toLowerCase().includes(q)) ||
      item.color_tags.some((t) => t.toLowerCase().includes(q))
    );
  });

  // Build folder tree
  const folders = Array.from(
    new Set(items.map((i) => i.folder_path[0] ?? "Root"))
  ).sort();

  return (
    <div className="space-y-5">
      {/* Search + stats */}
      <div className="flex flex-col sm:flex-row gap-3 items-start sm:items-center">
        <div className="relative flex-1 max-w-md">
          <span className="absolute left-3 top-1/2 -translate-y-1/2 text-zinc-500 text-sm">🔍</span>
          <input
            type="text"
            placeholder="Search by stock number, folder, color, type…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full bg-zinc-900 border border-zinc-700 rounded-lg pl-9 pr-4 py-2.5
              text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-green-500 transition-colors"
          />
          {search && (
            <button onClick={() => setSearch("")}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-300">✕</button>
          )}
        </div>
        <span className="text-sm text-zinc-500">
          {filtered.length} of {items.length} images
        </span>
      </div>

      {/* Folder pills */}
      {folders.length > 0 && (
        <div className="flex flex-wrap gap-2">
          <button
            onClick={() => setSearch("")}
            className={`px-3 py-1 rounded-full text-xs font-medium transition-colors ${
              !search ? "bg-green-600 text-white" : "bg-zinc-800 text-zinc-400 hover:bg-zinc-700"
            }`}
          >
            All
          </button>
          {folders.map((f) => (
            <button
              key={f}
              onClick={() => setSearch(f)}
              className={`px-3 py-1 rounded-full text-xs font-medium transition-colors ${
                search === f ? "bg-indigo-600 text-white" : "bg-zinc-800 text-zinc-400 hover:bg-zinc-700"
              }`}
            >
              {f}
            </button>
          ))}
        </div>
      )}

      {/* Image grid */}
      {filtered.length === 0 ? (
        <div className="text-center py-20 text-zinc-600">
          {items.length === 0 ? "No images indexed yet. Run Rebuild cache from Drive." : "No matches for your search."}
        </div>
      ) : (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-4">
          {filtered.map((item) => (
            <ImageCard key={item.id} item={item} />
          ))}
        </div>
      )}
    </div>
  );
}

function ImageCard({ item }: { item: CatalogItem }) {
  const [imgError, setImgError] = useState(false);

  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-xl overflow-hidden hover:border-zinc-600 transition-colors group">
      {/* Thumbnail */}
      <div className="aspect-square bg-zinc-800 overflow-hidden relative">
        {!imgError ? (
          <img
            src={`${API}${item.image_url}`}
            alt={item.stock}
            onError={() => setImgError(true)}
            className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-300"
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center text-zinc-600 text-xs">No preview</div>
        )}
      </div>

      {/* Info */}
      <div className="p-3 space-y-2">
        {/* Stock number */}
        <p className="text-sm font-semibold text-white truncate">{item.stock}</p>

        {/* Folder breadcrumb */}
        {item.folder_path.length > 0 && (
          <div className="flex items-center gap-1 flex-wrap">
            {item.folder_path.map((f, i) => (
              <span key={i} className="flex items-center gap-1">
                {i > 0 && <span className="text-zinc-700 text-xs">/</span>}
                <span className="text-xs text-zinc-500 truncate max-w-[70px]" title={f}>{f}</span>
              </span>
            ))}
          </div>
        )}

        {/* Color/type tags */}
        {item.color_tags.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {item.color_tags.slice(0, 4).map((tag, i) => (
              <span key={i} className={`px-2 py-0.5 rounded-full text-xs font-medium ${tagColor(tag)}`}>
                {tag}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function tagColor(tag: string): string {
  const t = tag.toLowerCase();
  if (t.includes("gold")) return "bg-yellow-900/60 text-yellow-300";
  if (t.includes("silver")) return "bg-zinc-700 text-zinc-200";
  if (t.includes("rose")) return "bg-pink-900/60 text-pink-300";
  if (t.includes("red") || t.includes("ruby")) return "bg-red-900/60 text-red-300";
  if (t.includes("blue") || t.includes("sapphire")) return "bg-blue-900/60 text-blue-300";
  if (t.includes("green") || t.includes("emerald")) return "bg-green-900/60 text-green-300";
  if (t.includes("necklace") || t.includes("chain")) return "bg-purple-900/60 text-purple-300";
  if (t.includes("earring") || t.includes("jhumka")) return "bg-orange-900/60 text-orange-300";
  if (t.includes("ring") || t.includes("bangle")) return "bg-cyan-900/60 text-cyan-300";
  return "bg-zinc-800 text-zinc-400";
}

// ── History Tab ────────────────────────────────────────────────────────────

function HistoryTab({ matches }: { matches: MatchEntry[] }) {
  if (matches.length === 0) {
    return (
      <div className="text-center py-20 text-zinc-600">
        No matches yet. Send an image to your WhatsApp group.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {matches.map((entry, i) => (
        <div key={i} className="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
          <div className="flex justify-between items-start mb-3">
            <div>
              <p className="text-xs text-zinc-500 font-mono">{new Date(entry.timestamp).toLocaleString()}</p>
              <p className="text-xs text-zinc-600 truncate max-w-xs mt-0.5">{entry.sender}</p>
            </div>
            {entry.query_url && (
              <a
                href={entry.query_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs text-indigo-400 hover:text-indigo-300 truncate max-w-[120px]"
              >
                Query image ↗
              </a>
            )}
          </div>
          <div className="flex flex-wrap gap-2">
            {entry.matches.map((m, j) => (
              <div key={j} className="flex items-center gap-1.5 rounded-full bg-zinc-800 px-3 py-1 text-xs">
                <span className="text-zinc-300 truncate max-w-[100px]">{m.name.replace(/\.[^.]+$/, "")}</span>
                <span className={`font-semibold ${
                  m.score >= 80 ? "text-green-400" : m.score >= 60 ? "text-yellow-400" : "text-zinc-400"
                }`}>{m.score}%</span>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Shared components ──────────────────────────────────────────────────────

function StatCard({ label, value, sub, accent = "zinc" }: {
  label: string; value: string; sub?: string; accent?: "green" | "red" | "zinc";
}) {
  const dot: Record<string, string> = { green: "bg-green-500", red: "bg-red-500", zinc: "bg-zinc-600" };
  return (
    <div className="rounded-xl bg-zinc-900 border border-zinc-800 p-5">
      <div className="flex items-center gap-2 mb-1">
        <span className={`w-2 h-2 rounded-full ${dot[accent]}`} />
        <span className="text-zinc-400 text-xs uppercase tracking-widest">{label}</span>
      </div>
      <p className="text-2xl font-semibold text-white">{value}</p>
      {sub && <p className="text-xs text-red-400 mt-1">{sub}</p>}
    </div>
  );
}
