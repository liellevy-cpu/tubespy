#!/usr/bin/env python3
"""Local dev server: serves public/ AND the /api endpoints against a local store.

For local preview only (no auth token, no collection worker — add-brand just
queues). Production uses brand_service.py behind the here.now proxy.

    python devserve.py --port 8899
"""
import argparse
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from add_brand import Store, Invalid, Limited

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"


def make_handler(store):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(PUBLIC), **k)

        def log_message(self, *a):
            pass

        def _json(self, code, value):
            data = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            size = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(size)) if size else {}

        def do_GET(self):
            if self.path.startswith("/api/brands"):
                return self._json(200, {"brands": store.list()})
            if self.path.startswith("/api/item-states"):
                return self._json(200, {"states": store.item_states()})
            return super().do_GET()

        def do_POST(self):
            try:
                if self.path.startswith("/api/add-brand"):
                    row, created = store.submit(self._body(), "local-dev")
                    return self._json(201 if created else 200, {"brand": row, "created": created})
                if self.path.startswith("/api/item-state"):
                    body = self._body()
                    return self._json(200, store.set_item_state(body.get("video_id"), body.get("state")))
            except Limited as exc:
                return self._json(429, {"error": str(exc)})
            except (Invalid, ValueError):
                return self._json(400, {"error": "Invalid request."})
            return self._json(404, {"error": "Not found"})

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--db", type=Path, default=ROOT / "build/dev-brands.sqlite")
    args = parser.parse_args()
    store = Store(args.db)
    store.seed_used(["GmrgKvRelpk"])  # seed the Used tab
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store))
    print(f"Dev library running at http://127.0.0.1:{args.port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
