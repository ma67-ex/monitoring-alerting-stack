# monitoring-alerting-stack

A small service with the monitoring around it: metrics, a dashboard, alerts, a runbook, and a postmortem. Start it with one command and watch it react when you break it.

The service is a stand-in for an LLM agent. It answers requests by calling one to three fake tools, and it can be told to fail or slow down on demand. That makes it easy to practice spotting and handling incidents.

## What you get

- A Python service that exposes Prometheus metrics: request count by status, latency histogram, tool calls by outcome, and token use.
- Prometheus, scraping the service every 5 seconds.
- Three alert rules: `ServiceDown`, `HighErrorRate` (over 5% for 1 minute), and `HighLatencyP95` (over 1 second for 1 minute).
- A Grafana dashboard with four panels: request rate, error rate, p95 latency, and tool failures by tool.
- A runbook with one section per alert, and a postmortem for a staged incident.

## Run it

You need Docker with Compose.

```
docker compose up -d --build
python3 app/loadgen.py          # steady traffic, 5 requests per second
```

Then open:

- Grafana at http://localhost:3000 (no login, dashboard "Agent service")
- Prometheus at http://localhost:9090/alerts

## Break it on purpose

```
curl 'localhost:8000/chaos?error=0.5'   # half of tool calls fail
curl 'localhost:8000/chaos?slow=2'      # add 2 seconds to every request
curl 'localhost:8000/chaos'             # reset
docker compose stop app                 # take the service down
```

I checked each alert this way. With 50% tool errors and steady traffic, `HighErrorRate` went to firing. With the service stopped, `ServiceDown` fired after about a minute.

## Files

| Path | Purpose |
|---|---|
| `app/service.py` | The demo service and its metrics |
| `app/loadgen.py` | Traffic generator |
| `prometheus/alerts.yml` | Alert rules |
| `grafana/dashboards/agent.json` | Dashboard, loaded automatically |
| `docs/runbook.md` | What to do when each alert fires |
| `docs/postmortem.md` | Write-up of a staged incident |

## Limits

Alerts show up in Prometheus only. Nothing sends them to email or chat, because Alertmanager is not set up. Adding it is the obvious next step.
