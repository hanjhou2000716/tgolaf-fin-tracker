# portfolio-data Edge Function

Deploy after applying the migration:

```bash
supabase functions deploy portfolio-data --no-verify-jwt
supabase secrets set SUPABASE_SERVICE_ROLE_KEY=... SUPABASE_USER_ID=... TELEGRAM_BOT_TOKEN=... TELEGRAM_ALLOWED_USER_ID=... CORS_ALLOWLIST=https://hanjhou2000716.github.io
```

The function accepts either a Supabase access token or Telegram WebApp
`initData`. Telegram data is verified with the bot token, checked for a fresh
`auth_date`, and restricted to `TELEGRAM_ALLOWED_USER_ID`; it is then mapped to
the configured `SUPABASE_USER_ID`. Missing, expired, or tampered credentials
return `401`.
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

The private GitHub Pages route is `/private/`. It is intended to be opened by
the Telegram bot's WebApp button, so the user does not need to enter an email
or password. Opening `/private/` directly in a normal browser shows an
instruction to open it from Telegram and does not reveal any data.
