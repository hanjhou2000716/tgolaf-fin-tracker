import { createClient } from "npm:@supabase/supabase-js@2";

const supabaseUrl = Deno.env.get("SUPABASE_URL") ?? "";
const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const allowedOrigins = (Deno.env.get("CORS_ALLOWLIST") ?? "")
  .split(",")
  .map((value) => value.trim())
  .filter(Boolean);

const jsonHeaders = (request: Request): HeadersInit => {
  const origin = request.headers.get("origin") ?? "";
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
    "Vary": "Origin",
  };
  if (allowedOrigins.includes(origin)) {
    headers["Access-Control-Allow-Origin"] = origin;
    headers["Access-Control-Allow-Headers"] = "authorization, apikey, content-type";
    headers["Access-Control-Allow-Methods"] = "GET, OPTIONS";
  }
  return headers;
};

const response = (request: Request, body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: jsonHeaders(request) });

Deno.serve(async (request) => {
  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: jsonHeaders(request) });
  if (request.method !== "GET") return response(request, { error: "method_not_allowed" }, 405);
  if (!supabaseUrl || !serviceRoleKey) {
    return response(request, { error: "private_api_not_configured" }, 503);
  }

  const authorization = request.headers.get("authorization") ?? "";
  const token = authorization.match(/^Bearer\s+(.+)$/i)?.[1];
  if (!token) return response(request, { error: "unauthorized" }, 401);

  const admin = createClient(supabaseUrl, serviceRoleKey, {
    auth: { autoRefreshToken: false, persistSession: false },
  });
  const { data: userData, error: userError } = await admin.auth.getUser(token);
  if (userError || !userData.user) return response(request, { error: "unauthorized" }, 401);

  const { data: snapshot, error: snapshotError } = await admin
    .from("portfolio_snapshots")
    .select("payload, generated_at")
    .eq("user_id", userData.user.id)
    .maybeSingle();
  if (snapshotError) return response(request, { error: "private_data_unavailable" }, 500);
  if (!snapshot) return response(request, { error: "snapshot_not_found" }, 404);

  return response(request, {
    data: snapshot.payload,
    generatedAt: snapshot.generated_at,
  });
});
