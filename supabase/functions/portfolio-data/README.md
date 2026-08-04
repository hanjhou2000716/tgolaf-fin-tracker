# portfolio-data Edge Function

Deploy after applying the migration:

```bash
supabase functions deploy portfolio-data --no-verify-jwt
supabase secrets set SUPABASE_SERVICE_ROLE_KEY=... CORS_ALLOWLIST=https://hanjhou2000716.github.io
```

The function performs its own JWT verification with `auth.getUser(token)` so
the response contract is explicit: missing or expired tokens return `401`.
The service role key is only a server-side Supabase secret. It must never be
placed in `public-site/private/index.html` or any other browser asset.

Acceptance checks:

```bash
curl -i https://YOUR_PROJECT.supabase.co/functions/v1/portfolio-data
# HTTP/2 401

curl -i -H "Authorization: Bearer USER_ACCESS_TOKEN" \
  https://YOUR_PROJECT.supabase.co/functions/v1/portfolio-data
# HTTP/2 200 for the owning user only
```
