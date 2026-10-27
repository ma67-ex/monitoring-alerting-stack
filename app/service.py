"""Demo service: pretends to be an agent that answers requests with tool calls.

Endpoints:
  /ask            answer a request (calls 1-3 fake tools)
  /metrics        Prometheus metrics
  /chaos?error=0.3&slow=1.5   inject failures: error rate (0-1) and extra latency (seconds)
"""
import random, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import metrics
from metrics import Counter, Histogram

REQUESTS = Counter("agent_requests_total", "Requests handled", ["status"])
LATENCY = Histogram("agent_request_seconds", "Request latency in seconds")
TOOLS = Counter("agent_tool_calls_total", "Tool calls", ["tool", "outcome"])
TOKENS = Counter("agent_tokens_total", "Tokens used")

chaos = {"error": 0.0, "slow": 0.0}


def handle_ask():
    time.sleep(random.uniform(0.05, 0.3) + chaos["slow"])
    for tool in random.sample(["search", "calculator", "chart"], random.randint(1, 3)):
        ok = random.random() >= chaos["error"]
        TOOLS.inc(tool=tool, outcome="ok" if ok else "error")
        if not ok:
            return 500
    TOKENS.inc(random.randint(200, 1500))
    return 200


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/metrics":
            return self.reply(200, metrics.render())
        if url.path == "/chaos":
            q = parse_qs(url.query)
            chaos["error"] = float(q.get("error", [0])[0])
            chaos["slow"] = float(q.get("slow", [0])[0])
            return self.reply(200, f"chaos set: {chaos}\n".encode())
        if url.path == "/ask":
            start = time.perf_counter()
            code = handle_ask()
            LATENCY.observe(time.perf_counter() - start)
            REQUESTS.inc(status=code)
            return self.reply(code, b"ok\n" if code == 200 else b"tool failed\n")
        self.reply(404, b"not found\n")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
