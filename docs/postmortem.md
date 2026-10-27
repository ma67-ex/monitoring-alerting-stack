# Postmortem: tool failures during a staged test

This is a staged incident, created to practice the format. All times are from one test run on 2026-10-27.

## Summary

Half of all tool calls were made to fail while the service handled steady traffic. The `HighErrorRate` alert reached the firing state and the dashboard showed the error rate climb. Later the service was stopped on purpose and `ServiceDown` fired.

## Impact

During the test, about half of requests to `/ask` returned status 500. No real users were affected.

## Timeline

| Step | Event |
|---|---|
| 1 | Load generator running at about 5 requests per second. All requests succeed. |
| 2 | Fault injected with `curl 'localhost:8000/chaos?error=0.5'`. |
| 3 | Error rate rises on the dashboard within one scrape cycle. |
| 4 | `HighErrorRate` goes pending, then firing after its one-minute window. |
| 5 | App container stopped with `docker compose stop app`. |
| 6 | `ServiceDown` fires after its 30-second window. |

## Root cause

An injected fault. In a real incident this section would name the actual bug, change, or dependency failure.

## What went well

- Both alerts fired without manual checking.
- The dashboard showed which tools were failing.

## What to improve

- Alerts do not notify anyone yet. Add Alertmanager and a chat or email receiver.
- Latency alert was not exercised in this run. Test it with `curl 'localhost:8000/chaos?slow=2'`.

## Action items

1. Add Alertmanager with one receiver.
2. Run the slow-response test and record the time to alert.
