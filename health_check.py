"""Watch the public Growth and Skynet health contracts for stale data."""

import datetime
import os
import sys

from service_contracts import GROWTH_BUTTON_TEXT, GROWTH_STALE_AFTER_HOURS

TAIPEI = datetime.timezone(datetime.timedelta(hours=8), name="Asia/Taipei")
ENDPOINTS = {
    "Growth Dashboard": "https://hanjhou2000716.github.io/tgolaf-fin-tracker/status.json",
    "Skynet Monitoring": "https://hanjhou2000716.github.io/skynet-monitoring/status.json",
}
GROWTH_URL = "https://hanjhou2000716.github.io/tgolaf-fin-tracker/"
DEFAULT_STALE_AFTER_HOURS = 18


def default_stale_after_hours(name):
    """Return a source-specific fallback without weakening Skynet checks."""
    return GROWTH_STALE_AFTER_HOURS if "growth" in str(name).lower() else DEFAULT_STALE_AFTER_HOURS


def parse_generated_at(value):
    if not value:
        raise ValueError("generatedAt is missing")
    parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=TAIPEI) if parsed.tzinfo is None else parsed.astimezone(TAIPEI)


def evaluate_status(name, payload, now):
    """Return human-readable health issues; an empty list means healthy."""
    issues = []
    if payload.get("status") != "ok":
        issues.append(f"{name} status={payload.get('status', 'missing')}")

    try:
        generated_at = parse_generated_at(payload.get("generatedAt"))
        freshness = payload.get("freshness")
        freshness = freshness if isinstance(freshness, dict) else {}
        declared_stale_hours = freshness.get("staleAfterHours", payload.get("staleAfterHours"))
        if declared_stale_hours in (None, ""):
            declared_stale_hours = default_stale_after_hours(name)
        stale_hours = float(declared_stale_hours)
        if stale_hours <= 0:
            raise ValueError("staleAfterHours must be positive")
        age_hours = (now - generated_at).total_seconds() / 3600
        if age_hours > stale_hours:
            issues.append(f"{name} stale for {age_hours:.1f}h (limit {stale_hours:.0f}h)")
    except (TypeError, ValueError) as error:
        issues.append(f"{name} invalid freshness contract: {error}")

    sources = payload.get("sources", payload.get("freshness", {}).get("sources", {}))
    for source, state in sources.items():
        if state != "ok":
            issues.append(f"{name} source {source} is {state}")
    return issues


def fetch_status(name, url, now):
    import requests

    try:
        response = requests.get(url, timeout=15, headers={"Cache-Control": "no-cache"})
        response.raise_for_status()
        return evaluate_status(name, response.json(), now)
    except requests.RequestException as error:
        return [f"{name} endpoint unavailable: {error}"]
    except ValueError as error:
        return [f"{name} returned invalid JSON: {error}"]


def send_alert(issues):
    import requests

    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Telegram credentials are not configured for the health watchdog")
    message = "⚠️ 資產系統資料健康告警\n\n" + "\n".join(f"• {issue}" for issue in issues)
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": message,
            "reply_markup": {"inline_keyboard": [[{"text": GROWTH_BUTTON_TEXT, "web_app": {"url": GROWTH_URL}}]]},
        },
        timeout=15,
    )
    response.raise_for_status()


def main():
    now = datetime.datetime.now(TAIPEI)
    issues = []
    for name, url in ENDPOINTS.items():
        issues.extend(fetch_status(name, url, now))
    if not issues:
        print("Health watchdog: both systems are current and healthy")
        return 0
    print("Health watchdog found issues:\n" + "\n".join(issues))
    send_alert(issues)
    return 1


if __name__ == "__main__":
    sys.exit(main())
