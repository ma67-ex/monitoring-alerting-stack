#!/bin/bash
# Start the demo service, collector, dashboard and a light traffic generator; exit if any dies.
trap 'kill 0' TERM INT
python app/service.py &
python collector/collector.py &
python dashboard/dashboard.py &
python app/loadgen.py http://localhost:8000/ask 3 &
wait -n
