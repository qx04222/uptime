# uptime

Generic HTTP uptime probe on a 10-minute GitHub Actions cron.

Targets are supplied via the `MONITOR_TARGETS` repo secret (JSON:
`{"uptime_urls": [{"label": "...", "url": "..."}]}`); logs print labels only.
Outage state is tracked as a rolling issue in a separate private repo
(`UPTIME_ISSUES_PAT`, fine-grained, issues r/w only) with optional Lark DM
alerts (`LARK_APP_ID` / `LARK_APP_SECRET` / `LARK_RECEIVE_ID`).

A site counts as UP on any HTTP response < 500; DOWN on connection error,
timeout, or 5xx. The probe always exits 0 — outcomes are reported via
issue/Lark, not run status.
