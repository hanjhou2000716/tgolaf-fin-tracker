import { createClient } from "npm:@supabase/supabase-js@2";

const supabaseUrl = Deno.env.get("SUPABASE_URL") ?? "";
const secretKeys = (() => {
  try {
    return JSON.parse(Deno.env.get("SUPABASE_SECRET_KEYS") ?? "{}");
  } catch {
    return {};
  }
})();
const serviceRoleKey = Deno.env.get("PORTFOLIO_SERVICE_ROLE_KEY")
  ?? Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")
  ?? secretKeys.default
  ?? "";
const telegramBotToken = Deno.env.get("TELEGRAM_BOT_TOKEN") ?? Deno.env.get("TELEGRAM_TOKEN") ?? "";
const telegramAllowedUserId = Deno.env.get("TELEGRAM_ALLOWED_USER_ID") ?? Deno.env.get("TELEGRAM_CHAT_ID") ?? "";
const portfolioUserId = Deno.env.get("PORTFOLIO_USER_ID") ?? "";
const telegramInitDataMaxAgeSeconds = 300;
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
    headers["Access-Control-Allow-Headers"] = "authorization, apikey, content-type, x-telegram-init-data";
    headers["Access-Control-Allow-Methods"] = "GET, OPTIONS";
  }
  return headers;
};

const response = (request: Request, body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: jsonHeaders(request) });

const hex = (bytes: Uint8Array) => Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");

const hmacSha256 = async (key: Uint8Array, value: string | Uint8Array) => {
  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    key,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const message = typeof value === "string" ? new TextEncoder().encode(value) : value;
  return new Uint8Array(await crypto.subtle.sign("HMAC", cryptoKey, message));
};

const safeEqual = (left: string, right: string) => {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
};

const verifyTelegramInitData = async (initData: string) => {
  if (!telegramBotToken || !telegramAllowedUserId || !portfolioUserId) {
    throw new Error("telegram_auth_not_configured");
  }
  const params = new URLSearchParams(initData);
  const receivedHash = (params.get("hash") ?? "").toLowerCase();
  if (!receivedHash) throw new Error("telegram_hash_missing");
  params.delete("hash");
  const dataCheckString = Array.from(params.entries())
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, value]) => `${key}=${value}`)
    .join("\n");
  const secretKey = await hmacSha256(new TextEncoder().encode("WebAppData"), telegramBotToken);
  const expectedHash = hex(await hmacSha256(secretKey, dataCheckString));
  if (!safeEqual(expectedHash, receivedHash)) throw new Error("telegram_hash_invalid");

  const authDate = Number(params.get("auth_date") ?? 0);
  const now = Math.floor(Date.now() / 1000);
  if (!Number.isInteger(authDate) || authDate <= 0 || now - authDate > telegramInitDataMaxAgeSeconds || authDate - now > 30) {
    throw new Error("telegram_init_data_expired");
  }
  let user: { id?: number | string };
  try {
    user = JSON.parse(params.get("user") ?? "{}");
  } catch {
    throw new Error("telegram_user_invalid");
  }
  if (String(user.id ?? "") !== String(telegramAllowedUserId)) throw new Error("telegram_user_not_allowed");
  return portfolioUserId;
};

Deno.serve(async (request) => {
  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: jsonHeaders(request) });
  if (request.method !== "GET") return response(request, { error: "method_not_allowed" }, 405);
  if (!supabaseUrl || !serviceRoleKey) {
    return response(request, { error: "private_api_not_configured" }, 503);
  }

  const admin = createClient(supabaseUrl, serviceRoleKey, {
    auth: { autoRefreshToken: false, persistSession: false },
  });
  let userId = "";
  const telegramInitData = request.headers.get("X-Telegram-Init-Data") ?? "";
  if (telegramInitData) {
    try {
      userId = await verifyTelegramInitData(telegramInitData);
    } catch {
      return response(request, { error: "unauthorized" }, 401);
    }
  } else {
    const authorization = request.headers.get("authorization") ?? "";
    const token = authorization.match(/^Bearer\s+(.+)$/i)?.[1];
    if (!token) return response(request, { error: "unauthorized" }, 401);
    const { data: userData, error: userError } = await admin.auth.getUser(token);
    if (userError || !userData.user) return response(request, { error: "unauthorized" }, 401);
    userId = userData.user.id;
  }

  const { data: snapshot, error: snapshotError } = await admin
    .from("portfolio_snapshots")
    .select("payload, generated_at")
    .eq("user_id", userId)
    .maybeSingle();
  if (snapshotError) return response(request, { error: "private_data_unavailable" }, 500);
  if (!snapshot) return response(request, { error: "snapshot_not_found" }, 404);

  return response(request, {
    data: snapshot.payload,
    generatedAt: snapshot.generated_at,
  });
});
