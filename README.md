---
title: Monitoring Alerting Stack
sdk: docker
app_port: 7860
---

# monitoring-alerting-stack

A small monitoring system written from scratch in Python, with no third-party packages. It has three parts: a demo service that exposes metrics, a collector that scrapes and stores them and fires alerts, and a web dashboard.

The demo service stands in for an LLM agent. It answers requests by calling one to three fake tools, and it can be told to fail or slow down on demand. That makes it easy to practice spotting and handling incidents.

## The three services

| Service | Port | What it does |
|---|---|---|
| `app` | 8000 | Demo agent. Exposes `/metrics` in the Prometheus text format. Its own tiny metrics library is in `app/metrics.py`. |
| `collector` | 9090 | Scrapes targets every 5 seconds, keeps the series in memory, answers range queries, and evaluates alert rules. |
| `dashboard` | 3000 | Web page with stat tiles, charts, live alert status and a metric explorer. Panels are defined in `dashboard/dashboard.json`. |

Metrics are request count by status, request latency histogram, tool calls by outcome, and token use.

## Run it

You need Docker with Compose.

```
docker compose up -d --build
```

Then open http://localhost:3000. To get traffic, send requests to the service:

```
python3 app/loadgen.py
```

Run the unit tests (no Docker needed):

```
python3 -m unittest discover -s tests
```

## Dashboard features

- A status band at the top: "Nominal", "Pending" or "Firing", filled solid red or amber when something is wrong, with the alert names and how long each has been active.
- One flat row per metric, stacked on a shared time axis: current value, a meter against the limit, the chart, and the alert tag for that metric.
- Hover any chart and every row shows its value at that moment, with a synced crosshair.
- Click a legend entry to hide a series. Download any row as CSV.
- Time range (5m, 15m, 1h) and refresh interval (off, 5 s, 15 s, 1 min), kept in the URL.
- Keyboard: `1` `2` `3` range, `r` refresh, `t` theme, `k` kiosk.
- Dark by default, with a light theme, and kiosk mode with `?kiosk` for a wall display.
- An alert rules list and an ad hoc query form: pick any collected metric, choose `rate`, `last` or `p95`, set a window and grouping.

The feature list came from studying Prometheus and Grafana. See `docs/reverse-engineering-notes.md`.

## Alerts

Rules live in `collector/alerts.json`. Each rule is a query, a comparison, and a hold time. An alert goes from inactive to pending when the condition first holds, then to firing once it has held for the full `for` period.

| Alert | Fires when |
|---|---|
| `ServiceDown` | The service has not answered scrapes for 30 seconds. |
| `HighErrorRate` | More than 5% of requests fail, for 1 minute. |
| `HighLatencyP95` | The 95th percentile latency is above 1 second, for 1 minute. |

## Break it on purpose

```
curl 'localhost:8000/chaos?error=0.5'   # half of tool calls fail
curl 'localhost:8000/chaos?slow=2'      # add 2 seconds to every request
curl 'localhost:8000/chaos'             # reset
docker compose stop app                 # take the service down
```

I ran each of these with steady traffic and watched the matching alert reach firing: `HighErrorRate` and `HighLatencyP95` after about a minute, and `ServiceDown` after the service had been stopped for 30 seconds.

## Query format

Dashboards and alerts share one small JSON query format. For example, the error ratio:

```json
{"fn": "ratio",
 "num": {"fn": "rate", "metric": "agent_requests_total", "match": {"status": "500"}, "window": 60},
 "den": {"fn": "rate", "metric": "agent_requests_total", "window": 60}}
```

Supported functions are `rate`, `last`, `ratio`, and `quantile`. Counter resets are handled in `rate`.

## Limits

- Data is kept in memory for one hour and lost on restart.
- Alerts show on the dashboard only. Nothing sends them to email or chat yet.
- Only one kind of target is supported: an HTTP endpoint in the Prometheus text format.
