"""Watch the public Growth and Skynet health contracts for stale data."""

import datetime
import os
import sys

from service_contracts import (
    FUTURE_TIMESTAMP_TOLERANCE,
    GROWTH_BUTTON_TEXT,
    GROWTH_STALE_AFTER_HOURS,
    TAIPEI,
    parse_contract_timestamp,
)
ENDPOINTS = {
    "Growth Dashboard": "https://hanjhou2000716.github.io/tgolaf-fin-tracker/status.json",
    "Skynet Monitoring": "https://hanjhou2000716.github.io/skynet-monitoring/status.json",
}
GROWTH_URL = "https://hanjhou2000716.github.io/tgolaf-fin-tracker/"
DEFAULT_STALE_AFTER_HOURS = 18
SKYNET_SCHEMA_VERSION = 2


def default_stale_after_hours(name):
    """Return a source-specific fallback without weakening Skynet checks."""
    return GROWTH_STALE_AFTER_HOURS if "growth" in str(name).lower() else DEFAULT_STALE_AFTER_HOURS


def parse_generated_at(value):
    return parse_contract_timestamp(value).astimezone(TAIPEI)


def evaluate_status(name, payload, now):
    """Return human-readable health issues; an empty list means healthy."""
    if "skynet" in str(name).lower() and payload.get("schemaVersion") == SKYNET_SCHEMA_VERSION:
        return evaluate_skynet_v2(name, payload, now)

    issues = []
    if now.tzinfo is None:
        now = now.replace(tzinfo=TAIPEI)
    else:
        now = now.astimezone(TAIPEI)
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
        if generated_at - now > FUTURE_TIMESTAMP_TOLERANCE:
            issues.append(f"{name} generatedAt is in the future")
        elif age_hours > stale_hours:
            issues.append(f"{name} stale for {age_hours:.1f}h (limit {stale_hours:.0f}h)")
    except (TypeError, ValueError) as error:
        issues.append(f"{name} invalid freshness contract: {error}")

    legacy_freshness = payload.get("freshness")
    legacy_freshness = legacy_freshness if isinstance(legacy_freshness, dict) else {}
    sources = payload.get("sources", legacy_freshness.get("sources", {}))
    sources = sources if isinstance(sources, dict) else {}
    for source, state in sources.items():
        if state != "ok":
            issues.append(f"{name} source {source} is {state}")
    return issues


def _as_taipei(value):
    parsed = parse_generated_at(value)
    return parsed


def evaluate_skynet_v2(name, payload, now):
    """Evaluate service liveness and each market's session freshness separately."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=TAIPEI)
    else:
        now = now.astimezone(TAIPEI)

    issues = []
    service = payload.get("service") if isinstance(payload.get("service"), dict) else {}
    markets = payload.get("markets") if isinstance(payload.get("markets"), dict) else {}
    calendar = payload.get("calendar") if isinstance(payload.get("calendar"), dict) else {}

    if service.get("status") not in (None, "ok") and not markets:
        issues.append(f"{name} SERVICE_DEGRADED: service status={service.get('status')}")

    generated_value = service.get("generatedAt") or payload.get("generatedAt")
    try:
        generated_at = _as_taipei(generated_value)
        age_hours = (now - generated_at).total_seconds() / 3600
        if generated_at - now > FUTURE_TIMESTAMP_TOLERANCE:
            issues.append(f"{name} TIME_CONTRACT_INVALID: generatedAt is in the future")
        elif age_hours > DEFAULT_STALE_AFTER_HOURS:
            issues.append(f"{name} SERVICE_STALE: service stale for {age_hours:.1f}h (limit 18h)")
    except (TypeError, ValueError) as error:
        issues.append(f"{name} TIME_CONTRACT_INVALID: {error}")

    # Daily update windows run every calendar day, independently of market holidays.
    expected_window = None
    if now.hour >= 17:
        expected_window = (now.date().isoformat(), "afternoon")
    elif now.hour >= 8:
        expected_window = (now.date().isoformat(), "morning")
    if expected_window:
        actual_window_date = service.get("windowDate")
        actual_window = service.get("window")
        # The fallback time determines when we report a missed window, not
        # whether an earlier successful primary run counts for that window.
        if (actual_window_date != expected_window[0] or actual_window != expected_window[1]
                or service.get("status") != "ok"):
            issues.append(
                f"{name} UPDATE_WINDOW_MISSED: expected {expected_window[0]} {expected_window[1]} update"
            )
    if service.get("status") not in (None, "ok") and markets:
        detailed_markets = any(
            isinstance(item, dict) and item.get("status") not in ("fresh", "market_closed")
            for item in markets.values()
        )
        if not detailed_markets:
            issues.append(f"{name} SERVICE_DEGRADED: service status={service.get('status')}")

    market_calendar_details = any(
        isinstance(item, dict) and item.get("status") == "calendar_unverified"
        for item in markets.values()
    )
    if calendar.get("status") != "verified" and not market_calendar_details:
        issues.append(f"{name} CALENDAR_UNVERIFIED: calendar status={calendar.get('status', 'missing')}")

    for market_key, market_label in (("taiwan", "Taiwan"), ("us", "US")):
        market = markets.get(market_key)
        if not isinstance(market, dict):
            issues.append(f"{name} {market_label} SOURCE_UNAVAILABLE: market contract missing")
            continue
        status = market.get("status")
        latest = market.get("latestSessionDate")
        expected = market.get("expectedSessionDate")
        reason = market.get("reasonCode")

        if status in ("fresh", "market_closed"):
            if not latest or not expected or latest != expected:
                issues.append(
                    f"{name} {market_label} MARKET_DATA_STALE: latest session {latest or 'missing'}, expected {expected or 'unknown'}"
                )
                continue
            due_value = market.get("nextDueAt")
            if due_value:
                try:
                    due_at = _as_taipei(due_value)
                    if due_at - now > FUTURE_TIMESTAMP_TOLERANCE and due_at.date() < now.date():
                        issues.append(f"{name} {market_label} TIME_CONTRACT_INVALID: nextDueAt is inconsistent")
                    elif now > due_at:
                        issues.append(f"{name} {market_label} MARKET_DATA_STALE: next expected session deadline passed")
                except (TypeError, ValueError) as error:
                    issues.append(f"{name} {market_label} TIME_CONTRACT_INVALID: invalid nextDueAt ({error})")
        elif status == "calendar_unverified":
            issues.append(f"{name} {market_label} CALENDAR_UNVERIFIED: {reason or 'calendar cannot confirm session'}")
        elif status == "unavailable":
            issues.append(f"{name} {market_label} SOURCE_UNAVAILABLE: {reason or 'market source unavailable'}")
        else:
            code = "MARKET_DATA_STALE" if status == "stale" else "SOURCE_UNAVAILABLE"
            detail = reason or ("market status=" + str(status or "missing"))
            issues.append(f"{name} {market_label} {code}: {detail}")

    source_states = payload.get("sources")
    if not isinstance(source_states, dict):
        freshness = payload.get("freshness")
        freshness = freshness if isinstance(freshness, dict) else {}
        source_states = freshness.get("sources", {})
    if not isinstance(source_states, dict):
        source_states = {}
    for source, state in source_states.items():
        if state != "ok":
            relevant_market = "taiwan" if source.lower() in ("taiex", "006208") else "us"
            current = markets.get(relevant_market, {})
            if current.get("status") not in ("unavailable", "stale", "calendar_unverified"):
                issues.append(f"{name} SOURCE_UNAVAILABLE: source {source} is {state}")

    if payload.get("status") != "ok" and not issues:
        issues.append(f"{name} status={payload.get('status', 'missing')}")

    # Do not repeat a generic degraded status when an actionable cause is present.
    return list(dict.fromkeys(issues))


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


def main(dry_run=None):
    if dry_run is None:
        dry_run = os.getenv("HEALTH_CHECK_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    now = datetime.datetime.now(TAIPEI)
    issues = []
    for name, url in ENDPOINTS.items():
        issues.extend(fetch_status(name, url, now))
    if not issues:
        print("Health watchdog: both systems are current and healthy")
        return 0
    print("Health watchdog found issues:\n" + "\n".join(issues))
    if dry_run:
        print("Dry run: Telegram notification suppressed")
    else:
        send_alert(issues)
    return 1


if __name__ == "__main__":
    sys.exit(main())
