"""Stand-in Geofabrik for tests/e2e/construction.cjs: an index with "china" and six
provinces (tests/test_extracts.py), each serving the same small extract with a
railway line under construction.  Needs osmium.  Usage: fake_geofabrik.py <port>"""
import hashlib
import json
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "src")]
from test_extracts import index_json, write_pbf  # noqa: E402

port = int(sys.argv[1])
base = f"http://127.0.0.1:{port}"
with tempfile.TemporaryDirectory() as folder:
    pbf = Path(folder) / "served.osm.pbf"
    write_pbf(pbf)
    DATA = pbf.read_bytes()
INDEX = json.dumps(index_json(base)).encode()
MD5 = f"{hashlib.md5(DATA).hexdigest()}  x.osm.pbf\n".encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, body: bytes, send_body: bool = True):
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Length", str(len(body or b"")))
        self.end_headers()
        if send_body and body:
            self.wfile.write(body)

    def _body(self):
        if self.path == "/index-v1.json":
            return INDEX
        if self.path.endswith(".osm.pbf.md5"):
            return MD5
        if self.path.endswith(".osm.pbf"):
            return DATA
        return None

    def do_GET(self):
        if self.path.endswith(".osm.pbf"):
            print("download", self.path, flush=True)
        self._reply(self._body())

    def do_HEAD(self):
        self._reply(self._body(), send_body=False)


ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
