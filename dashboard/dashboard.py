"""Dashboard server: serves the page and dashboard definition, proxies /api/* to the collector."""
import os, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COLLECTOR = os.environ.get("COLLECTOR_URL", "http://collector:9090")
HERE = os.path.dirname(__file__)
FILES = {"/": ("index.html", "text/html"), "/dashboard.json": ("dashboard.json", "application/json")}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive; every reply sends Content-Length

    def reply(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in FILES:
            name, ctype = FILES[path]
            return self.reply(200, open(os.path.join(HERE, name), "rb").read(), ctype)
        if path.startswith("/api/"):
            try:
                with urllib.request.urlopen(COLLECTOR + self.path, timeout=5) as r:
                    return self.reply(r.status, r.read(), "application/json")
            except urllib.error.HTTPError as e:
                return self.reply(e.code, e.read(), "application/json")
            except OSError:
                return self.reply(502, b'{"error":"collector unreachable"}', "application/json")
        self.reply(404, b"not found", "text/plain")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", 3000))), Handler).serve_forever()
