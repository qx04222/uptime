"""
Lark bot DM notifier — sends cards to one specific user via the Lark IM API.

Reuses the mailpulse engine's Lark app (LARK_APP_ID + LARK_APP_SECRET) to
avoid creating a second app. The recipient is identified by LARK_RECEIVE_ID
(open_id format, e.g. ou_xxxxxxxxxxxxxxxxxxxx).

If any of the three env vars is missing, all notify_*() calls become silent
no-ops — code path works without Lark integration.

Lark API references:
- https://open.larksuite.com/document/server-docs/im-v1/message/create
- https://open.larksuite.com/document/server-docs/authentication-management/access-token/tenant_access_token_internal
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import time
import urllib.error
import urllib.request

LARK_BASE = "https://open.larksuite.com"
_token_cache: dict = {"token": "", "expires_at": 0}

# ── notification state (dedup) ────────────────────────────────────────────────
# Persisted in state/notify-state.json, committed back to repo by the audit workflow.
# Schema: { "<fingerprint>": { "last_sent_at": "<iso>", "count": <int>, "title": "<title>" } }
REPO_ROOT = pathlib.Path(__file__).resolve().parent
NOTIFY_STATE_FILE = REPO_ROOT / "state" / "notify-state.json"
DEFAULT_REPEAT_INTERVAL_HOURS = 4


def _fingerprint(*parts: str) -> str:
    """Stable fingerprint for dedup. e.g. _fingerprint('audit', 'arcview', 'P0')."""
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _load_notify_state() -> dict:
    if not NOTIFY_STATE_FILE.exists():
        return {}
    try:
        return json.loads(NOTIFY_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_notify_state(state: dict) -> None:
    NOTIFY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Prune entries last sent > 30d ago — they're not relevant to dedup anymore
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)
    pruned = {}
    for fp, entry in state.items():
        try:
            last = datetime.datetime.fromisoformat(entry.get("last_sent_at", ""))
            if last >= cutoff:
                pruned[fp] = entry
        except (ValueError, TypeError):
            continue
    NOTIFY_STATE_FILE.write_text(json.dumps(pruned, indent=2, sort_keys=True))


def _should_send(fingerprint: str, repeat_interval_hours: float) -> tuple[bool, str]:
    """Returns (allow, reason). False if last send within repeat_interval."""
    state = _load_notify_state()
    entry = state.get(fingerprint)
    if not entry:
        return True, "first-time"
    try:
        last = datetime.datetime.fromisoformat(entry["last_sent_at"])
    except (ValueError, KeyError):
        return True, "unparseable-state"
    age = datetime.datetime.now(datetime.timezone.utc) - last
    age_hours = age.total_seconds() / 3600
    if age_hours >= repeat_interval_hours:
        return True, f"last sent {age_hours:.1f}h ago"
    return False, f"suppressed (last sent {age_hours:.1f}h ago, repeat_interval={repeat_interval_hours}h)"


def _record_send(fingerprint: str, title: str) -> None:
    state = _load_notify_state()
    entry = state.get(fingerprint, {"count": 0})
    entry["last_sent_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    entry["count"] = entry.get("count", 0) + 1
    entry["title"] = title[:120]
    state[fingerprint] = entry
    _save_notify_state(state)


def _env(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    return v or None


def _have_credentials() -> bool:
    return all(_env(k) for k in ("LARK_APP_ID", "LARK_APP_SECRET", "LARK_RECEIVE_ID"))


def _get_token() -> str | None:
    now = time.time()
    if _token_cache["token"] and _token_cache["expires_at"] > now + 300:
        return _token_cache["token"]
    app_id = _env("LARK_APP_ID")
    app_secret = _env("LARK_APP_SECRET")
    if not (app_id and app_secret):
        return None
    # Defensive: strip any stray whitespace that may have leaked in via secret setup
    app_id = app_id.strip()
    app_secret = app_secret.strip()
    print(f"  lark: requesting token (app_id len={len(app_id)} secret len={len(app_secret)})", flush=True)
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(
        f"{LARK_BASE}/open-apis/auth/v3/tenant_access_token/internal",
        data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        print(f"  lark: token fetch failed: {e}", flush=True)
        return None
    if data.get("code") != 0:
        print(f"  lark: token err code={data.get('code')} msg={data.get('msg')}", flush=True)
        return None
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expires_at"] = now + data.get("expire", 7200)
    return _token_cache["token"]


def _card_to_markdown(card: dict) -> str:
    """Lark card → WeCom markdown: bold title, the lark_md body, URL buttons as links.
    Payload buttons (Lark card callbacks) have no WeCom equivalent and are dropped."""
    title = card.get("header", {}).get("title", {}).get("content", "")
    parts = [f"**{title}**"] if title else []
    for el in card.get("elements", []):
        if el.get("tag") == "div":
            parts.append(el.get("text", {}).get("content", ""))
        for action in el.get("actions", []) if el.get("tag") == "action" else []:
            if action.get("url"):
                parts.append(f"[{action.get('text', {}).get('content', 'Open')}]({action['url']})")
    text = "\n".join(p for p in parts if p)
    return text.encode()[:4000].decode(errors="ignore")   # WeCom markdown cap is 4096 bytes


def _send_wecom(card: dict) -> tuple[bool, str]:
    """Post to the WeCom ops group webhook (WECOM_OPS_WEBHOOK). Success = errcode 0."""
    url = _env("WECOM_OPS_WEBHOOK")
    if not url:
        return False, "WECOM_OPS_WEBHOOK not set"
    body = json.dumps({"msgtype": "markdown", "markdown": {"content": _card_to_markdown(card)}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as e:
        return False, f"network: {e}"
    if data.get("errcode") != 0:
        return False, f"wecom errcode={data.get('errcode')} msg={data.get('errmsg')}"
    return True, "sent"


def _send_card(card: dict, *, dedup_key: str | None = None, repeat_interval_hours: float = DEFAULT_REPEAT_INTERVAL_HOURS) -> tuple[bool, str]:
    """Send an alert card to the WeCom ops group and (while it still exists) Lark DM.
       Either channel landing counts as sent. If dedup_key is provided, suppress
       repeat sends of the same fingerprint within repeat_interval_hours."""
    # Dedup check before any network call
    if dedup_key:
        ok, reason = _should_send(dedup_key, repeat_interval_hours)
        if not ok:
            print(f"  lark: {reason}", flush=True)
            return True, reason  # treat as "successful" — intentional suppression
    wecom_ok, wecom_msg = _send_wecom(card)
    lark_ok, lark_msg = _send_lark(card)
    print(f"  notify: wecom={wecom_msg} | lark={lark_msg}", flush=True)
    if (wecom_ok or lark_ok) and dedup_key:
        title = card.get("header", {}).get("title", {}).get("content", "?")
        _record_send(dedup_key, title)
    return wecom_ok or lark_ok, f"wecom: {wecom_msg}; lark: {lark_msg}"


def _send_lark(card: dict) -> tuple[bool, str]:
    if not _have_credentials():
        return False, "LARK_APP_ID/SECRET/RECEIVE_ID not all set — skipping"
    token = _get_token()
    if not token:
        return False, "no token"
    receive_id = (_env("LARK_RECEIVE_ID") or "").strip()
    print(f"  lark: sending msg (receive_id len={len(receive_id)})", flush=True)
    body = json.dumps({
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(card),
    }).encode()
    req = urllib.request.Request(
        f"{LARK_BASE}/open-apis/im/v1/messages?receive_id_type=open_id",
        data=body, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        # Read response body for diagnostic
        try:
            err_body = e.read().decode()[:300]
        except Exception:
            err_body = ""
        return False, f"HTTP {e.code}: {err_body}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, f"network: {e}"
    if data.get("code") != 0:
        return False, f"lark err code={data.get('code')} msg={data.get('msg')}"
    msg_id = data.get("data", {}).get("message_id", "")
    return True, f"sent msg_id={msg_id}"


def _build_card(title: str, lines: list[str], color: str = "blue", url: str | None = None) -> dict:
    elements: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}]
    if url:
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "Open in GitHub"},
                "url": url,
                "type": "primary",
            }],
        })
    return {
        "header": {"title": {"tag": "plain_text", "content": title}, "template": color},
        "elements": elements,
    }


# ── public API used by audit.py / build-failure workflow / digest ────────────

def notify_audit_findings(p0: int, p1: int, p2: int, issue_url: str | None = None) -> None:
    if not (p0 or p1 or p2):
        return
    color = "red" if p0 else ("orange" if p1 else "yellow")
    parts = []
    if p0: parts.append(f"**P0×{p0}**")
    if p1: parts.append(f"P1×{p1}")
    if p2: parts.append(f"P2×{p2}")
    lines = [
        f"Severity: {' · '.join(parts)}",
        "",
        "_Open the GitHub issue for details and the full audit report._",
    ]
    # Dedup: same severity combination within 4h → suppress
    dedup = _fingerprint("audit_findings", f"P0={p0}", f"P1={p1}", f"P2={p2}")
    ok, msg = _send_card(_build_card("Railway Audit — findings", lines, color, url=issue_url), dedup_key=dedup)
    print(f"  lark audit: {msg}", flush=True)


def notify_recovery(action_count: int, actions: list[dict], issue_url: str | None = None) -> None:
    if not action_count:
        return
    lines = [f"**{action_count} action(s) taken**", ""]
    for a in actions[:5]:
        lines.append(f"- `{a.get('project','?')}`: action=`{a.get('action','?')}`")
        for reason in a.get("reasons", [])[:3]:
            lines.append(f"    - {reason}")
    if len(actions) > 5:
        lines.append(f"_…and {len(actions)-5} more_")
    # Recovery is high-signal — dedup very short window (30min) so repeated
    # triggers in same incident don't spam, but next incident does notify
    projects = ",".join(sorted({a.get("project", "?") for a in actions}))
    actions_kinds = ",".join(sorted({a.get("action", "?") for a in actions}))
    color = "red" if "human_needed" in actions_kinds else "violet"
    dedup = _fingerprint("recovery", projects, actions_kinds)
    ok, msg = _send_card(_build_card("Auto-Recovery executed", lines, color, url=issue_url), dedup_key=dedup, repeat_interval_hours=0.5)
    print(f"  lark recovery: {msg}", flush=True)


def notify_runtime_fix(actions: list[dict], issue_url: str | None = None) -> None:
    """Notify when Tier-2 dispatches a runtime fix agent. Only fires for actual
       dispatches (not cooldown/untracked skips). Deduped per (repo, path) on a
       24h window so we don't re-ping the same endpoint every audit run."""
    dispatched = [a for a in actions if a.get("action") == "dispatched"]
    if not dispatched:
        return
    lines = [f"**{len(dispatched)} runtime fix agent(s) dispatched**", ""]
    for a in dispatched[:5]:
        lines.append(f"- `{a.get('repo','?')}` → `{a.get('path','?')}`")
        if a.get("summary"):
            lines.append(f"    - {a['summary'][:120]}")
    if len(dispatched) > 5:
        lines.append(f"_…and {len(dispatched)-5} more_")
    repos = ",".join(sorted({a.get("repo", "?") for a in dispatched}))
    paths = ",".join(sorted({a.get("path", "?") for a in dispatched}))
    dedup = _fingerprint("runtime_fix", repos, paths)
    ok, msg = _send_card(_build_card("Runtime auto-fix dispatched", lines, "violet", url=issue_url),
                         dedup_key=dedup, repeat_interval_hours=24)
    print(f"  lark runtime-fix: {msg}", flush=True)


def notify_build_failure(workflow: str, repo: str, branch: str, run_url: str, actor: str, issue_url: str) -> None:
    lines = [
        f"**Repo**: `{repo}`",
        f"**Workflow**: `{workflow}`",
        f"**Branch**: `{branch}`",
        f"**Triggered by**: @{actor}",
        f"[Issue]({issue_url}) · [Run]({run_url})",
    ]
    ok, msg = _send_card(_build_card("Build Failure", lines, "red", url=issue_url))
    print(f"  lark build-failure: {msg}", flush=True)


def notify_dependabot_digest(by_repo: dict) -> None:
    total = sum(len(prs) for prs in by_repo.values())
    if not total:
        _send_card(_build_card("Dependabot weekly digest", ["✓ No open Dependabot PRs across tracked repos."], "green"))
        return
    lines = [f"**{total} open PR(s)** across {len(by_repo)} repo(s)", ""]
    for repo, prs in sorted(by_repo.items()):
        if not prs:
            continue
        lines.append(f"**{repo}** ({len(prs)})")
        for pr in prs[:5]:
            t = pr.get("title", "?")
            u = pr.get("url", "")
            ut = pr.get("update_type", "?")
            lines.append(f"- [{t}]({u}) · `{ut}`")
        if len(prs) > 5:
            lines.append(f"_…and {len(prs)-5} more_")
        lines.append("")
    lines.append("_Comment `@dependabot squash and merge` on patch PRs to merge; review minor/major manually._")
    ok, msg = _send_card(_build_card("Dependabot weekly digest", lines, "turquoise"))
    print(f"  lark digest: {msg}", flush=True)
