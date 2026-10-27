"""Dashboard server: serves the page and dashboard definition, proxies /api/* to the collector."""
import hashlib, os, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COLLECTOR = os.environ.get("COLLECTOR_URL", "http://collector:9090")
HERE = os.path.dirname(__file__)
# Static files are read once at startup; the ETag lets browsers revalidate with a 304.
FILES = {}
for path, name, ctype in [("/", "index.html", "text/html"), ("/dashboard.json", "dashboard.json", "application/json")]:
    body = open(os.path.join(HERE, name), "rb").read()
    FILES[path] = (body, ctype, '"%s"' % hashlib.sha256(body).hexdigest()[:16])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive; every reply sends Content-Length

    def reply(self, code, body, ctype, etag=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if etag:
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in FILES:
            body, ctype, etag = FILES[path]
            if self.headers.get("If-None-Match") == etag:
                return self.reply(304, b"", ctype, etag)
            return self.reply(200, body, ctype, etag)
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
