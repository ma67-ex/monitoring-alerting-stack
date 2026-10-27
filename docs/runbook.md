# Runbook

One section per alert. Each one says what the alert means, how to check it, and how to fix it.

## ServiceDown

**Meaning:** Prometheus could not scrape the app for 30 seconds.

**Check:**
1. Run `docker compose ps`. Is the `app` container running?
2. Run `docker compose logs app --tail 50` for a crash message.

**Fix:** Run `docker compose up -d app`. If it keeps crashing, read the log and fix the error before restarting again.

## HighErrorRate

**Meaning:** More than 5% of requests returned status 500 over the last minute. In this service that means a tool call failed.

**Check:**
1. Open the dashboard at http://localhost:3000 and look at "Tool failures by tool". One tool failing points to a problem with that tool. All tools failing points to something shared.
2. Run `docker compose logs app --tail 50`.

**Fix:** Fix or disable the failing tool. In the demo, reset the injected fault with `curl 'localhost:8000/chaos'`.

## HighLatencyP95

**Meaning:** The slowest 5% of requests took over 1 second for a minute.

**Check:**
1. Look at "p95 latency" and "Request rate". Did traffic jump, or did latency rise at steady traffic?
2. Rising latency at steady traffic usually means a slow dependency.

**Fix:** Find the slow tool or dependency and fix or time it out. In the demo, reset with `curl 'localhost:8000/chaos'`.
