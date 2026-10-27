"""Send steady traffic to the service: python3 app/loadgen.py [url] [requests_per_second]"""
import sys, time, urllib.request, urllib.error

url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/ask"
rps = float(sys.argv[2]) if len(sys.argv) > 2 else 5
while True:
    try:
        urllib.request.urlopen(url, timeout=10).read()
    except (urllib.error.URLError, OSError):
        pass
    time.sleep(1 / rps)
