# What I learned from Prometheus and Grafana

Both tools are open source. I looked at what each one ships to the browser, using REA (an analysis tool for JavaScript apps), then chose features to build into my own dashboard. I did not copy any code. Grafana is under AGPL, so reading its behavior and writing my own is the safe route.

## What I looked at

- Prometheus 2.55.1: the web UI bundle served by the container (one 2.4 MB JavaScript file).
- Grafana 11.3.0: the compiled frontend build (328 JavaScript files, source maps removed).

Findings come from REA's application graph plus keyword searches in the bundles, so counts and feature lists are indicative. They show what the UI contains, not how it behaves.

## Features found

Prometheus UI: pages for graph, alerts, rules, targets, service discovery, config, flags and TSDB status. The graph page has a table/graph toggle, stacked graphs, local-time switch, exemplars, a metrics explorer, and autocomplete.

Grafana UI: time range picker, refresh interval, thresholds, template variables, annotations, dashboard links, kiosk mode, share and export, and many panel types (time series, stat, gauge, bar gauge, heatmap, pie, histogram, state timeline, logs, table).

## What my dashboard took from that

| Feature | Seen in | Status |
|---|---|---|
| Time range picker (5 min, 15 min, 1 hour) | Grafana | Built |
| Refresh interval and refresh-now | Grafana | Built |
| Stat panels with warn and crit colors | Grafana | Built |
| Threshold lines on charts | Grafana | Built |
| Hover tooltip with crosshair | Grafana | Built |
| Click legend to hide a series | Grafana | Built |
| Kiosk mode (`?kiosk`) | Grafana | Built |
| CSV export per panel | Grafana | Built |
| Light and dark theme | Grafana | Built |
| Explore a metric (pick metric, function, window, group by, table) | Prometheus | Built |
| Alert chips with how long they have been pending or firing | Prometheus alerts page | Built |

## Not built

Template variables, annotations, heatmaps, gauges, stacked graphs, rules and targets pages, autocomplete for queries. Variables and a targets page are the likely next steps.
