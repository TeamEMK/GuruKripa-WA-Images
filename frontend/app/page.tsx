"use client";

import { useCallback, useEffect, useState } from "react";

const API = "/api/backend";

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

export default function Dashboard() {
  const {
    data: status,
    loading: statusLoading,
    error: statusError,
    refresh: refreshStatus,
  } = useAutoRefresh<CacheStatus>(`${API}/admin/status`, 4000);

  const { data: matchesData, loading: matchesLoading } = useAutoRefresh<{
    matches: MatchEntry[];
  }>(`${API}/admin/matches?limit=20`, 5000);

  const [refreshing, setRefreshing] = useState(false);
  const [refreshMsg, setRefreshMsg] = useState("");

  async function triggerRefresh() {
    setRefreshing(true);
    setRefreshMsg("");
    try {
      const res = await fetch(`${API}/admin/refresh`, { method: "POST" });
      const body = await res.json();
      setRefreshMsg(
        body.status === "started"
          ? "Cache rebuild started — this may take a few minutes."
          : body.status === "already_running"
          ? "Already rebuilding…"
          : body.status
      );
      refreshStatus();
    } catch {
      setRefreshMsg("Failed to contact backend.");
    } finally {
      setRefreshing(false);
    }
  }

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100 font-sans p-6">
      <header className="mb-8">
        <h1 className="text-2xl font-bold tracking-tight text-white">
          WA Image Matcher
        </h1>
        <p className="text-zinc-400 text-sm mt-1">
          WhatsApp group · CNN image similarity · Google Drive cache
        </p>
      </header>

      {/* Cache status card */}
      <section className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-6">
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
            statusLoading
              ? "…"
              : status?.last_updated
              ? new Date(status.last_updated).toLocaleString()
              : "Never"
          }
        />
      </section>

      {/* Rebuild button */}
      <div className="flex items-center gap-4 mb-8">
        <button
          onClick={triggerRefresh}
          disabled={refreshing || status?.rebuild_running}
          className="px-5 py-2 rounded-lg bg-green-600 text-white font-medium text-sm
            hover:bg-green-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {status?.rebuild_running
            ? "Rebuilding…"
            : refreshing
            ? "Starting…"
            : "Rebuild cache from Drive"}
        </button>
        {refreshMsg && (
          <span className="text-sm text-zinc-400">{refreshMsg}</span>
        )}
      </div>

      {/* Recent matches */}
      <section>
        <h2 className="text-lg font-semibold mb-3 text-white">
          Recent matches
        </h2>

        {matchesLoading ? (
          <p className="text-zinc-500 text-sm">Loading…</p>
        ) : !matchesData?.matches?.length ? (
          <p className="text-zinc-500 text-sm">
            No matches yet. Send an image to your WhatsApp group.
          </p>
        ) : (
          <div className="space-y-3">
            {matchesData.matches.map((entry, i) => (
              <MatchCard key={i} entry={entry} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function StatCard({
  label,
  value,
  sub,
  accent = "zinc",
}: {
  label: string;
  value: string;
  sub?: string;
  accent?: "green" | "red" | "zinc";
}) {
  const dot: Record<string, string> = {
    green: "bg-green-500",
    red: "bg-red-500",
    zinc: "bg-zinc-600",
  };
  return (
    <div className="rounded-xl bg-zinc-900 border border-zinc-800 p-5">
      <div className="flex items-center gap-2 mb-1">
        <span className={`w-2 h-2 rounded-full ${dot[accent]}`} />
        <span className="text-zinc-400 text-xs uppercase tracking-widest">
          {label}
        </span>
      </div>
      <p className="text-2xl font-semibold text-white">{value}</p>
      {sub && <p className="text-xs text-red-400 mt-1">{sub}</p>}
    </div>
  );
}

function MatchCard({ entry }: { entry: MatchEntry }) {
  return (
    <div className="rounded-xl bg-zinc-900 border border-zinc-800 p-4">
      <div className="flex justify-between items-start mb-2">
        <span className="text-xs text-zinc-500 font-mono">
          {new Date(entry.timestamp).toLocaleString()}
        </span>
        <span className="text-xs text-zinc-500 truncate max-w-[200px]">
          {entry.sender}
        </span>
      </div>
      <div className="flex flex-wrap gap-2">
        {entry.matches.map((m, i) => (
          <div
            key={i}
            className="flex items-center gap-1.5 rounded-full bg-zinc-800 px-3 py-1 text-xs"
          >
            <span className="text-zinc-300 truncate max-w-[120px]">{m.name}</span>
            <span
              className={`font-semibold ${
                m.score >= 80
                  ? "text-green-400"
                  : m.score >= 60
                  ? "text-yellow-400"
                  : "text-zinc-400"
              }`}
            >
              {m.score}%
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
