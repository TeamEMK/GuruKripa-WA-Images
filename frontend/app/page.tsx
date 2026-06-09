"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const API = "/api/backend";

// ── Types ──────────────────────────────────────────────────────────────────

type CacheStatus = {
  total_images: number;
  profiled: number;
  last_updated: string | null;
  cache_size_mb: number;
  index_running: boolean;
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
  category: string | null;
  colors: string[];
  description: string | null;
  tags: string[];
  profiled: boolean;
  image_url: string;
};

// ── Hooks ──────────────────────────────────────────────────────────────────

function useAutoRefresh<T>(url: string, intervalMs = 5000) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetch_ = useCallback(async () => {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(20000) });
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
type Banner = { kind: "ok" | "err" | "info"; text: string } | null;

export default function App() {
  const [tab, setTab] = useState<Tab>("dashboard");

  const status = useAutoRefresh<CacheStatus>(`${API}/admin/status`, 3000);
  const matchesData = useAutoRefresh<{ matches: MatchEntry[] }>(`${API}/admin/matches?limit=50`, 5000);
  const catalogData = useAutoRefresh<{ items: CatalogItem[] }>(`${API}/admin/catalog`, 10000);

  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<Banner>(null);
  const [indexLog, setIndexLog] = useState<string[]>([]);

  const isRunning = !!status.data?.index_running;

  // Poll live index log while indexing is in progress
  useEffect(() => {
    if (!isRunning) return;
    const poll = async () => {
      try {
        const res = await fetch(`${API}/admin/index/log`);
        if (res.ok) {
          const data = await res.json();
          setIndexLog(data.log ?? []);
        }
      } catch { /* ignore */ }
    };
    poll();
    const id = setInterval(poll, 2000);
    return () => clearInterval(id);
  }, [isRunning]);

  const conn: "online" | "connecting" | "offline" =
    status.error ? "offline" : status.data ? "online" : "connecting";

  async function runIndex(path: string, label: string) {
    setBusy(true);
    setIndexLog([]);
    setBanner({ kind: "info", text: `${label}…` });
    try {
      const res = await fetch(`/api/admin/${path}`, {
        method: "POST",
        signal: AbortSignal.timeout(30000),
      });
      if (res.status === 401) {
        setBanner({ kind: "err", text: "Unauthorized — check ADMIN_API_KEY." });
        return;
      }
      const body = await res.json();
      if (body.status === "started")
        setBanner({ kind: "ok", text: `${label} started — watch the live log below.` });
      else if (body.status === "already_running")
        setBanner({ kind: "info", text: "Indexing is already running." });
      else if (body.status === "error")
        setBanner({ kind: "err", text: body.reason ?? "Backend error." });
      else setBanner({ kind: "info", text: String(body.status) });
      status.refresh();
    } catch {
      setBanner({ kind: "err", text: "Couldn't reach the backend." });
    } finally {
      setBusy(false);
    }
  }

  const tabs: { id: Tab; label: string; count?: number }[] = [
    { id: "dashboard", label: "Dashboard" },
    { id: "catalog", label: "Catalog", count: catalogData.data?.items?.length },
    { id: "history", label: "History", count: matchesData.data?.matches?.length },
  ];

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-950 via-slate-900 to-indigo-950 text-slate-100">
      {/* Header */}
      <header className="border-b border-white/10 bg-slate-950/70 backdrop-blur-xl sticky top-0 z-20">
        <div className="max-w-7xl mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="grid h-10 w-10 place-items-center rounded-xl bg-gradient-to-br from-indigo-500 to-fuchsia-500 text-lg font-black shadow-lg shadow-indigo-500/30">
              GK
            </div>
            <div>
              <h1 className="text-lg font-bold tracking-tight bg-gradient-to-r from-white to-slate-400 bg-clip-text text-transparent">
                Guru Kripa · Image Matcher
              </h1>
              <p className="text-[11px] text-slate-500">OpenAI Vision · Semantic Search · Google Drive</p>
            </div>
          </div>
          <ConnPill conn={conn} />
        </div>

        <div className="max-w-7xl mx-auto px-6 flex gap-1">
          {tabs.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`relative px-4 py-2.5 text-sm font-medium transition-colors ${
                tab === t.id ? "text-white" : "text-slate-500 hover:text-slate-300"
              }`}
            >
              {t.label}
              {t.count !== undefined && (
                <span className="ml-1.5 text-xs text-slate-500">{t.count}</span>
              )}
              {tab === t.id && (
                <span className="absolute inset-x-2 -bottom-px h-0.5 rounded-full bg-gradient-to-r from-indigo-400 to-fuchsia-400" />
              )}
            </button>
          ))}
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-6 py-8">
        {tab === "dashboard" && (
          <DashboardTab
            status={status.data}
            conn={conn}
            busy={busy}
            banner={banner}
            indexLog={indexLog}
            onIndexAll={() => runIndex("refresh", "Full index")}
            onReTag={() => runIndex("reindex?force=true&limit=10000", "Re-tagging all")}
          />
        )}
        {tab === "catalog" && <CatalogTab items={catalogData.data?.items ?? []} onRefresh={catalogData.refresh} />}
        {tab === "history" && <HistoryTab matches={matchesData.data?.matches ?? []} loading={matchesData.loading} />}
      </main>
    </div>
  );
}

// ── Connection pill ────────────────────────────────────────────────────────

function ConnPill({ conn }: { conn: "online" | "connecting" | "offline" }) {
  const map = {
    online: { dot: "bg-emerald-400", ring: "bg-emerald-400/20", text: "online", color: "text-emerald-300" },
    connecting: { dot: "bg-amber-400", ring: "bg-amber-400/20", text: "connecting", color: "text-amber-300" },
    offline: { dot: "bg-rose-500", ring: "bg-rose-500/20", text: "offline", color: "text-rose-300" },
  }[conn];
  return (
    <div className="flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-3 py-1.5">
      <span className="relative flex h-2 w-2">
        {conn !== "offline" && <span className={`absolute inline-flex h-full w-full animate-ping rounded-full ${map.ring}`} />}
        <span className={`relative inline-flex h-2 w-2 rounded-full ${map.dot}`} />
      </span>
      <span className={`text-xs font-medium ${map.color}`}>{map.text}</span>
    </div>
  );
}

// ── Dashboard Tab ──────────────────────────────────────────────────────────

function DashboardTab({
  status, conn, busy, banner, indexLog, onIndexAll, onReTag,
}: {
  status: CacheStatus | null;
  conn: string;
  busy: boolean;
  banner: Banner;
  indexLog: string[];
  onIndexAll: () => void;
  onReTag: () => void;
}) {
  const total = status?.total_images ?? 0;
  const profiled = status?.profiled ?? 0;
  const pending = Math.max(0, total - profiled);
  const running = !!status?.index_running;
  const pct = total > 0 ? Math.round((profiled / total) * 100) : 0;
  const logRef = useRef<HTMLDivElement>(null);

  // Auto-scroll log to bottom as new lines arrive
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [indexLog]);

  return (
    <div className="space-y-6">
      {/* Stat cards */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <StatCard
          label="Indexed images" icon="🖼️" accent="indigo"
          value={conn === "connecting" ? "…" : String(total)}
          sub={total > 0 ? `${profiled} profiled · ${pending} pending` : "Nothing indexed yet"}
        />
        <StatCard
          label="Cache size" icon="💾" accent="emerald"
          value={conn === "connecting" ? "…" : `${status?.cache_size_mb ?? 0} MB`}
          sub="on the Railway volume"
        />
        <StatCard
          label="Last indexed" icon="🕑" accent="amber"
          value={
            conn === "connecting" ? "…"
              : status?.last_updated ? new Date(status.last_updated).toLocaleString() : "Never"
          }
          sub={conn === "offline" ? "backend unreachable" : ""}
        />
      </div>

      {/* Profiled progress */}
      {total > 0 && (
        <div className="rounded-xl border border-white/10 bg-white/5 p-4">
          <div className="mb-2 flex items-center justify-between text-xs text-slate-400">
            <span>Embedding coverage</span>
            <span className="font-semibold text-slate-200">{pct}%</span>
          </div>
          <div className="h-2 w-full overflow-hidden rounded-full bg-slate-800">
            <div
              className="h-full rounded-full bg-gradient-to-r from-emerald-400 to-teal-400 transition-all duration-500"
              style={{ width: `${pct}%` }}
            />
          </div>
        </div>
      )}

      {/* Actions */}
      <div className="rounded-2xl border border-white/10 bg-gradient-to-br from-white/5 to-transparent p-6 space-y-5">
        <h2 className="text-xs font-semibold uppercase tracking-widest text-slate-400">Index from Google Drive</h2>

        <div className="flex flex-wrap gap-3">
          <button
            onClick={onIndexAll}
            disabled={busy || running}
            className="rounded-lg bg-gradient-to-r from-indigo-500 to-fuchsia-500 px-5 py-2.5 text-sm font-semibold text-white shadow-lg shadow-indigo-500/25 transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {running ? "Indexing…" : busy ? "Starting…" : "⚡ Index all from Drive"}
          </button>
          <button
            onClick={onReTag}
            disabled={busy || running}
            className="rounded-lg border border-amber-400/25 bg-amber-500/10 px-5 py-2.5 text-sm font-semibold text-amber-200 transition hover:bg-amber-500/20 disabled:cursor-not-allowed disabled:opacity-50"
          >
            ♻️ Re-tag all
          </button>
        </div>

        {banner && (
          <div className={`rounded-lg border px-4 py-3 text-sm ${
            banner.kind === "ok" ? "border-emerald-400/30 bg-emerald-500/10 text-emerald-200"
              : banner.kind === "err" ? "border-rose-400/30 bg-rose-500/10 text-rose-200"
              : "border-sky-400/30 bg-sky-500/10 text-sky-200"
          }`}>
            {banner.text}
          </div>
        )}

        {/* Live log panel — visible during indexing or when log has content */}
        {(running || indexLog.length > 0) && (
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <span className="text-xs font-semibold uppercase tracking-widest text-slate-400">Live log</span>
              {running && <span className="h-2 w-2 animate-pulse rounded-full bg-indigo-400" />}
            </div>
            <div
              ref={logRef}
              className="h-56 overflow-y-auto rounded-lg border border-white/10 bg-slate-950/80 p-3 font-mono text-[11px] leading-5 text-slate-300 space-y-0.5"
            >
              {indexLog.length === 0 ? (
                <p className="text-slate-600">Waiting for output…</p>
              ) : (
                indexLog.map((line, i) => (
                  <p key={i} className={
                    line.startsWith("✅") ? "text-emerald-400"
                    : line.startsWith("⚠") ? "text-amber-400"
                    : line.startsWith("Found") || line.startsWith("Scanning") ? "text-sky-400"
                    : "text-slate-300"
                  }>{line}</p>
                ))
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Catalog Tab ────────────────────────────────────────────────────────────

function CatalogTab({ items, onRefresh }: { items: CatalogItem[]; onRefresh: () => void }) {
  const [search, setSearch] = useState("");

  const filtered = items.filter((item) => {
    if (!search.trim()) return true;
    const q = search.toLowerCase();
    return (
      item.stock.toLowerCase().includes(q) ||
      (item.category?.toLowerCase().includes(q) ?? false) ||
      item.folder_path.some((f) => f.toLowerCase().includes(q)) ||
      item.tags.some((t) => t.toLowerCase().includes(q))
    );
  });

  const folders = Array.from(new Set(items.map((i) => i.folder_path[0] ?? "Root"))).sort();

  return (
    <div className="space-y-5">
      <div className="flex flex-col sm:flex-row gap-3 sm:items-center">
        <div className="relative flex-1 max-w-md">
          <span className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500 text-sm">🔍</span>
          <input
            type="text"
            placeholder="Search stock, category, folder, color, tag…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full rounded-lg border border-white/10 bg-slate-900/80 pl-9 pr-4 py-2.5 text-sm text-slate-100 placeholder-slate-600 outline-none focus:border-indigo-400/60"
          />
          {search && (
            <button onClick={() => setSearch("")} className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300">✕</button>
          )}
        </div>
        <span className="text-sm text-slate-500">{filtered.length} of {items.length}</span>
        <button
          onClick={onRefresh}
          className="rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-xs text-slate-400 hover:bg-white/10 transition"
        >
          ↻ Refresh
        </button>
      </div>

      {folders.length > 0 && (
        <div className="flex flex-wrap gap-2">
          <Pill active={!search} onClick={() => setSearch("")} tone="indigo">All</Pill>
          {folders.map((f) => (
            <Pill key={f} active={search === f} onClick={() => setSearch(f)} tone="violet">{f}</Pill>
          ))}
        </div>
      )}

      {filtered.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-white/10 py-20 text-center text-slate-600">
          {items.length === 0 ? "No images indexed yet — run Index all from Drive." : "No matches for your search."}
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5">
          {filtered.map((item) => <ImageCard key={item.id} item={item} />)}
        </div>
      )}
    </div>
  );
}

function Pill({ children, active, onClick, tone }: { children: React.ReactNode; active: boolean; onClick: () => void; tone: "indigo" | "violet" }) {
  const on = tone === "indigo" ? "bg-indigo-500 text-white" : "bg-violet-500 text-white";
  return (
    <button onClick={onClick} className={`rounded-full px-3 py-1 text-xs font-medium transition ${active ? on : "bg-white/5 text-slate-400 hover:bg-white/10"}`}>
      {children}
    </button>
  );
}

function ImageCard({ item }: { item: CatalogItem }) {
  const [imgError, setImgError] = useState(false);
  return (
    <div className="group overflow-hidden rounded-xl border border-white/10 bg-slate-900/60 transition hover:border-indigo-400/40 hover:shadow-lg hover:shadow-indigo-500/10">
      <div className="relative aspect-square overflow-hidden bg-slate-800">
        {!imgError ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={`${API}${item.image_url}`} alt={item.stock} onError={() => setImgError(true)}
            className="h-full w-full object-cover transition duration-300 group-hover:scale-105" />
        ) : (
          <div className="grid h-full w-full place-items-center text-xs text-slate-600">No preview</div>
        )}
        {item.category && (
          <span className="absolute left-2 top-2 rounded-full bg-black/60 px-2 py-0.5 text-[10px] font-medium text-white backdrop-blur">
            {item.category}
          </span>
        )}
      </div>
      <div className="space-y-2 p-3">
        <p className="truncate text-sm font-semibold text-white">{item.stock}</p>
        {item.folder_path.length > 0 && (
          <p className="truncate text-[11px] text-slate-500">{item.folder_path.join(" / ")}</p>
        )}
        {item.tags.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {item.tags.slice(0, 4).map((tag, i) => (
              <span key={i} className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${tagColor(tag)}`}>{tag}</span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function tagColor(tag: string): string {
  const t = tag.toLowerCase();
  if (t.includes("gold")) return "bg-yellow-500/15 text-yellow-300";
  if (t.includes("silver")) return "bg-slate-500/20 text-slate-200";
  if (t.includes("rose")) return "bg-pink-500/15 text-pink-300";
  if (t.includes("red") || t.includes("ruby")) return "bg-red-500/15 text-red-300";
  if (t.includes("blue") || t.includes("sapphire")) return "bg-blue-500/15 text-blue-300";
  if (t.includes("green") || t.includes("emerald")) return "bg-emerald-500/15 text-emerald-300";
  if (t.includes("necklace") || t.includes("chain") || t.includes("haar")) return "bg-purple-500/15 text-purple-300";
  if (t.includes("earring") || t.includes("jhumka")) return "bg-orange-500/15 text-orange-300";
  if (t.includes("ring") || t.includes("bangle")) return "bg-cyan-500/15 text-cyan-300";
  return "bg-white/10 text-slate-300";
}

// ── History Tab ────────────────────────────────────────────────────────────

function HistoryTab({ matches, loading }: { matches: MatchEntry[]; loading: boolean }) {
  if (loading) {
    return (
      <div className="flex items-center justify-center py-20 text-slate-600 text-sm gap-2">
        <span className="h-2 w-2 animate-pulse rounded-full bg-slate-600" />
        Loading history…
      </div>
    );
  }

  if (matches.length === 0) {
    return (
      <div className="rounded-2xl border border-dashed border-white/10 py-20 text-center text-slate-600">
        No searches yet. Send an image or text to your WhatsApp group.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {matches.map((entry, i) => <HistoryCard key={i} entry={entry} />)}
    </div>
  );
}

function HistoryCard({ entry }: { entry: MatchEntry }) {
  const [imgError, setImgError] = useState(false);
  const time = new Date(entry.timestamp);
  const timeStr = time.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const dateStr = time.toLocaleDateString([], { day: "numeric", month: "short" });

  return (
    <div className="rounded-xl border border-white/10 bg-slate-900/60 p-4 flex gap-4">
      {/* Query image thumbnail */}
      <div className="flex-shrink-0">
        {entry.query_url && !imgError ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={entry.query_url}
            alt="query"
            onError={() => setImgError(true)}
            className="h-16 w-16 rounded-lg object-cover border border-white/10"
          />
        ) : (
          <div className="h-16 w-16 rounded-lg bg-slate-800 border border-white/10 grid place-items-center text-slate-600 text-xs">
            {entry.query_url ? "📷" : "💬"}
          </div>
        )}
      </div>

      {/* Content */}
      <div className="flex-1 min-w-0 space-y-2">
        <div className="flex items-center justify-between gap-2">
          <p className="text-xs text-slate-500 truncate">{entry.sender}</p>
          <p className="text-xs text-slate-600 whitespace-nowrap">{dateStr} · {timeStr}</p>
        </div>

        {entry.matches.length === 0 ? (
          <p className="text-xs text-slate-600 italic">No matches returned</p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {entry.matches.map((m, j) => (
              <div key={j} className={`flex items-center gap-1.5 rounded-full px-3 py-1 text-xs border ${
                j === 0 ? "border-indigo-400/30 bg-indigo-500/10" : "border-white/10 bg-white/5"
              }`}>
                <span className="font-semibold text-white">{m.name.replace(/\.[^.]+$/, "")}</span>
                {m.score > 0 && (
                  <span className={`font-medium ${m.score >= 70 ? "text-emerald-400" : m.score >= 50 ? "text-amber-400" : "text-slate-500"}`}>
                    {m.score}%
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Shared ─────────────────────────────────────────────────────────────────

function StatCard({ label, value, sub, icon, accent }: {
  label: string; value: string; sub?: string; icon: string; accent: "indigo" | "emerald" | "amber";
}) {
  const ring = { indigo: "from-indigo-500/20", emerald: "from-emerald-500/20", amber: "from-amber-500/20" }[accent];
  return (
    <div className="relative overflow-hidden rounded-2xl border border-white/10 bg-slate-900/60 p-5">
      <div className={`pointer-events-none absolute -right-8 -top-8 h-24 w-24 rounded-full bg-gradient-to-br ${ring} to-transparent blur-xl`} />
      <div className="mb-2 flex items-center gap-2">
        <span className="text-base">{icon}</span>
        <span className="text-xs uppercase tracking-widest text-slate-400">{label}</span>
      </div>
      <p className="text-2xl font-bold text-white">{value}</p>
      {sub && <p className="mt-1 text-xs text-slate-500">{sub}</p>}
    </div>
  );
}
