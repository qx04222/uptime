#!/usr/bin/env python3
"""uptime_probe — fleet uptime monitoring (10-min cron, public repo).

This repo is PUBLIC so its GHA minutes are free. Privacy model:
- Probe targets come from the MONITOR_TARGETS secret (JSON, same schema as
  monitor_targets.json), never committed. A local monitor_targets.json
  (gitignored) works as fallback for dev runs.
- Logs print labels only, never URLs — public run logs carry no infra info.
- The rolling "[Uptime] sites down" issue lives in the PRIVATE repo named by
  ISSUE_REPO, written via the fine-grained PAT in GH_TOKEN (issues r/w on that
  repo only). Without the token, issue tracking is skipped and only Lark fires.

A site is UP on any HTTP response < 500 (2xx/3xx/4xx all mean "server answered").
DOWN = connection error, timeout, or 5xx. Always exits 0 — outages are reported
via issue/Lark, not run status, so the public run history reveals nothing.
"""
import json
import os
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TARGETS_FILE = os.path.join(HERE, "monitor_targets.json")
ISSUE_TITLE = "[Uptime] sites down"
REPO = os.environ.get("ISSUE_REPO", "qx04222/railway-audit")
TIMEOUT = 15


def load_targets():
    raw = os.environ.get("MONITOR_TARGETS", "").strip()
    if raw:
        return json.loads(raw).get("uptime_urls", [])
    if os.path.exists(TARGETS_FILE):
        return json.load(open(TARGETS_FILE)).get("uptime_urls", [])
    print("no MONITOR_TARGETS secret and no local monitor_targets.json — nothing to probe")
    return []


def probe(url):
    """Return (ok, detail)."""
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "fleet-uptime/1.0"})
    try:
        r = urllib.request.urlopen(req, timeout=TIMEOUT)
        code = r.status
        return (code < 500, f"HTTP {code}")
    except urllib.error.HTTPError as e:
        return (e.code < 500, f"HTTP {e.code}")
    except Exception as e:  # noqa: BLE001 — any connection/timeout error = down
        return (False, type(e).__name__)


def gh(args, capture=True):
    try:
        p = subprocess.run(["gh"] + args, capture_output=capture, text=True, timeout=30)
        return p.returncode, (p.stdout or "").strip()
    except (subprocess.TimeoutExpired, OSError):
        return 1, ""


def have_issue_token():
    return bool(os.environ.get("GH_TOKEN", "").strip())


def find_open_issue():
    rc, out = gh(["issue", "list", "--repo", REPO, "--state", "open",
                  "--search", f'"{ISSUE_TITLE}" in:title', "--json", "number,title",
                  "--jq", f'[.[] | select(.title=="{ISSUE_TITLE}")][0].number // empty'])
    return out if (rc == 0 and out) else None


def main():
    dry = "--dry-run" in sys.argv
    urls = load_targets()
    down = []
    for u in urls:
        ok, detail = probe(u["url"])
        status = "UP" if ok else "DOWN"
        # label only — this log is public
        print(f"  {status:4} {u['label']:20} ({detail})")
        if not ok:
            down.append((u["label"], u["url"], detail))

    use_issues = have_issue_token() and not dry
    existing = find_open_issue() if use_issues else None

    if not down:
        if existing:
            gh(["issue", "comment", existing, "--repo", REPO,
                "--body", "✅ 全部恢复 — 自动关闭。"])
            gh(["issue", "close", existing, "--repo", REPO, "--reason", "completed"])
            print(f"all recovered → closed issue #{existing}")
        else:
            print("all sites up")
        return

    lines = [f"🔴 {lbl} — {detail}\n  {url}" for lbl, url, detail in down]
    body = "**站点不可达**（自动探活，恢复后自动关闭）：\n\n" + "\n".join(lines)
    if dry:
        print("\n--- would report ---\n" + body)
        return

    issue_url = None
    new_outage = existing is None
    if use_issues:
        if existing:
            gh(["issue", "edit", existing, "--repo", REPO, "--body", body])
        else:
            rc, issue_url = gh(["issue", "create", "--repo", REPO, "--title", ISSUE_TITLE,
                                "--body", body, "--label", "uptime"])
            if rc != 0:
                gh(["label", "create", "uptime", "--repo", REPO, "--color", "B60205"])
                rc, issue_url = gh(["issue", "create", "--repo", REPO, "--title", ISSUE_TITLE,
                                    "--body", body, "--label", "uptime"])
    else:
        print("no GH_TOKEN — skipping issue tracking (Lark only)")

    # Lark only on a NEW outage (not every probe while still down)
    if new_outage:
        try:
            sys.path.insert(0, HERE)
            import lark_notify
            card = lark_notify._build_card(
                "🔴 站点宕机", [f"{lbl} — {detail}" for lbl, _u, detail in down],
                color="red", url=issue_url or None)
            lark_notify._send_card(card, dedup_key="uptime-down", repeat_interval_hours=1)
        except Exception as e:  # noqa: BLE001
            print("lark skipped:", e)
    print(f"{len(down)} down → " + (f"issue {'updated' if existing else 'opened'}" if use_issues else "lark notified"))


if __name__ == "__main__":
    main()
