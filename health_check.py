"""Watch the public Growth and Skynet health contracts for stale data."""

import datetime
import io
import json
import os
import re
import sys
import zipfile

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
STATE_SCHEMA_VERSION = 1
STATE_ARTIFACT_NAME = "health-watchdog-state"
ALERT_REPEAT_HOURS = 24
GITHUB_API = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")


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


def _taipei_time(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=TAIPEI)
    return value.astimezone(TAIPEI)


def incident_key(issue):
    """Create a stable, non-financial incident identity from a watchdog issue."""
    text = str(issue)
    system = next((name for name in ENDPOINTS if text.startswith(name + " ")), "Unknown system")
    detail = text[len(system):].strip()
    market = "service"
    match = re.match(r"^(Taiwan|US)\s+([A-Z][A-Z0-9_]*):", detail)
    if match:
        market = "taiwan" if match.group(1) == "Taiwan" else "us"
        category = match.group(2)
    else:
        match = re.match(r"^([A-Z][A-Z0-9_]*):", detail)
        if match:
            category = match.group(1)
        elif detail.startswith("stale for "):
            category = "SERVICE_STALE"
        elif detail.startswith("status="):
            category = "STATUS_DEGRADED"
        elif detail.startswith("source "):
            category = "SOURCE_UNAVAILABLE"
            source_match = re.match(r"source\s+([^\s]+)", detail)
            market = source_match.group(1).lower() if source_match else "service"
        elif detail.startswith("endpoint unavailable"):
            category = "ENDPOINT_UNAVAILABLE"
        elif detail.startswith("returned invalid JSON"):
            category = "INVALID_RESPONSE"
        elif detail.startswith("invalid freshness contract"):
            category = "INVALID_CONTRACT"
        else:
            category = "HEALTH_CONTRACT"
    # The update-window miss and stale service describe the same service outage.
    if category in ("SERVICE_STALE", "UPDATE_WINDOW_MISSED"):
        category = "SERVICE_AVAILABILITY"
    return "|".join((system, category, market))


def normalize_incident_state(value):
    """Validate persisted state; invalid/missing state fails open to an empty map."""
    if not isinstance(value, dict) or value.get("schemaVersion") != STATE_SCHEMA_VERSION:
        return {"schemaVersion": STATE_SCHEMA_VERSION, "active": {}}
    active = value.get("active")
    if not isinstance(active, dict):
        return {"schemaVersion": STATE_SCHEMA_VERSION, "active": {}}
    clean = {}
    for key, record in active.items():
        if not isinstance(key, str) or not isinstance(record, dict):
            continue
        try:
            first_seen = parse_generated_at(record.get("firstSeenAt")).isoformat()
            last_alert = parse_generated_at(record.get("lastAlertAt")).isoformat()
        except (TypeError, ValueError):
            continue
        clean[key] = {"firstSeenAt": first_seen, "lastAlertAt": last_alert}
    return {"schemaVersion": STATE_SCHEMA_VERSION, "active": clean}


def alert_plan(issues, state, now, repeat_hours=ALERT_REPEAT_HOURS):
    """Select first/24-hour reminder messages and drop incidents that recovered."""
    now = _taipei_time(now)
    previous = normalize_incident_state(state)["active"]
    current = {}
    due = {}
    for issue in issues:
        key = incident_key(issue)
        current.setdefault(key, str(issue))
    next_state = {}
    for key, issue in current.items():
        prior = previous.get(key)
        if prior:
            try:
                first_seen = parse_generated_at(prior["firstSeenAt"])
                last_alert = parse_generated_at(prior["lastAlertAt"])
                age = (now - last_alert).total_seconds() / 3600
                should_send = age < 0 or age >= repeat_hours
            except (KeyError, TypeError, ValueError):
                first_seen, should_send = now, True
        else:
            first_seen, should_send = now, True
        next_state[key] = {
            "firstSeenAt": first_seen.isoformat(),
            # Updated only after Telegram confirms a successful response.
            "lastAlertAt": prior.get("lastAlertAt") if prior and not should_send else now.isoformat(),
        }
        if should_send:
            due[key] = issue
    return list(due.values()), {"schemaVersion": STATE_SCHEMA_VERSION, "active": next_state}


def _github_headers(token):
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def load_incident_state(state_path=None, environ=None):
    """Load the latest prior run's state artifact. Any read failure notifies immediately."""
    environ = os.environ if environ is None else environ
    state_path = state_path or environ.get("HEALTH_WATCHDOG_STATE_FILE")
    token = environ.get("GITHUB_TOKEN")
    repository = environ.get("GITHUB_REPOSITORY")
    current_run = environ.get("GITHUB_RUN_ID")
    if token and repository and current_run:
        import requests

        headers = _github_headers(token)
        workflow_url = f"{GITHUB_API}/repos/{repository}/actions/workflows/health-watchdog.yml/runs"
        try:
            response = requests.get(
                workflow_url,
                headers=headers,
                params={"branch": "main", "status": "completed", "per_page": 50},
                timeout=15,
            )
            response.raise_for_status()
            runs = response.json().get("workflow_runs", [])
            for run in runs:
                if str(run.get("id")) == str(current_run):
                    continue
                artifacts_response = requests.get(
                    f"{GITHUB_API}/repos/{repository}/actions/runs/{run['id']}/artifacts",
                    headers=headers,
                    timeout=15,
                )
                artifacts_response.raise_for_status()
                artifacts = artifacts_response.json().get("artifacts", [])
                artifact = next((item for item in artifacts
                                 if item.get("name") == STATE_ARTIFACT_NAME and not item.get("expired")), None)
                if not artifact:
                    continue
                archive = requests.get(artifact["archive_download_url"], headers=headers, timeout=30)
                archive.raise_for_status()
                with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
                    member = next((name for name in bundle.namelist()
                                   if name.rsplit("/", 1)[-1] == "health-watchdog-state.json"), None)
                    if member is None:
                        raise ValueError("incident state artifact is missing its JSON file")
                    return normalize_incident_state(json.loads(bundle.read(member).decode("utf-8")))
        except Exception as error:  # Fail open: uncertainty must not suppress a warning.
            print(f"Health watchdog state unavailable; alerting without dedup: {type(error).__name__}")
            return {"schemaVersion": STATE_SCHEMA_VERSION, "active": {}}
    if state_path and os.path.isfile(state_path):
        try:
            with open(state_path, encoding="utf-8") as file:
                return normalize_incident_state(json.load(file))
        except (OSError, ValueError):
            print("Health watchdog state invalid; alerting without dedup")
    return {"schemaVersion": STATE_SCHEMA_VERSION, "active": {}}


def save_incident_state(state, state_path=None):
    state_path = state_path or os.getenv("HEALTH_WATCHDOG_STATE_FILE")
    if not state_path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(state_path)), exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as file:
        json.dump(normalize_incident_state(state), file, ensure_ascii=False, sort_keys=True)


def main(dry_run=None, now=None):
    if dry_run is None:
        dry_run = os.getenv("HEALTH_CHECK_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    now = _taipei_time(now or datetime.datetime.now(TAIPEI))
    issues = []
    for name, url in ENDPOINTS.items():
        issues.extend(fetch_status(name, url, now))
    if not issues:
        if not dry_run:
            save_incident_state({"schemaVersion": STATE_SCHEMA_VERSION, "active": {}})
        print("Health watchdog: both systems are current and healthy")
        return 0
    print("Health watchdog found issues:\n" + "\n".join(issues))
    if dry_run:
        print("Dry run: Telegram notification suppressed")
        return 1

    state = load_incident_state()
    due_issues, next_state = alert_plan(issues, state, now)
    # Preserve the old state if Telegram fails; a missing/old artifact will retry next run.
    save_incident_state(state)
    if due_issues:
        print(f"Telegram alert due for {len(due_issues)} new or recurring incident(s)")
        send_alert(due_issues)
        _, updated = alert_plan(due_issues, state, now)
        sent_keys = {incident_key(issue) for issue in due_issues}
        for key in sent_keys:
            if key in updated["active"]:
                next_state["active"][key] = updated["active"][key]
    else:
        print("Telegram reminder suppressed; active incidents were already reported within 24 hours")
    save_incident_state(next_state)
    return 1


if __name__ == "__main__":
    sys.exit(main())
