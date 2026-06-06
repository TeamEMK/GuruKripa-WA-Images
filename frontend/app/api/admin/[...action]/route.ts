import { NextRequest, NextResponse } from "next/server";

// Server-side proxy for the protected /admin POST routes. The admin key lives
// ONLY in a server env var here — it is never sent to or stored in the browser.
// The dashboard hits /api/admin/<action> and we attach X-Admin-Key for it.
const BACKEND = process.env.BACKEND_URL || "http://localhost:8000";
const ADMIN_KEY = process.env.ADMIN_API_KEY || "";

export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ action: string[] }> },
) {
  const { action } = await params;
  if (!ADMIN_KEY) {
    return NextResponse.json(
      { status: "error", reason: "ADMIN_API_KEY is not set on the server." },
      { status: 500 },
    );
  }
  const target = `${BACKEND}/admin/${action.join("/")}${req.nextUrl.search}`;
  try {
    const upstream = await fetch(target, {
      method: "POST",
      headers: { "X-Admin-Key": ADMIN_KEY, "Content-Type": "application/json" },
      signal: AbortSignal.timeout(30000),
    });
    const text = await upstream.text();
    return new NextResponse(text, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("content-type") ?? "application/json",
      },
    });
  } catch {
    return NextResponse.json(
      { status: "error", reason: "Couldn't reach the backend." },
      { status: 502 },
    );
  }
}
