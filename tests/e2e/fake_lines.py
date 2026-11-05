"""Stand-in Overpass for tests/e2e/details.cjs: answers every query with the given
ways (tags only), as the online click details expect.  Usage: fake_lines.py <port> two|three"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

port, mode = int(sys.argv[1]), sys.argv[2]
TRACKS = [
    {"type": "way", "id": 146696733, "tags": {"railway": "rail", "name": "京雄城际线", "ref": "3085", "usage": "main",
                                               "maxspeed": "80", "railway:track_ref": "II", "electrified": "contact_line"}},
    {"type": "way", "id": 727471058, "tags": {"railway": "rail", "name": "京雄城际线", "ref": "3085", "usage": "main",
                                               "maxspeed": "80", "railway:track_ref": "I", "electrified": "contact_line"}},
]
OTHER = [{"type": "way", "id": 548287813, "tags": {"railway": "rail", "name": "京沪线", "ref": "0002", "usage": "main",
                                                    "maxspeed": "160"}}]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.reply(b"Rate limit: 2\n")

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.reply(json.dumps({"elements": TRACKS + (OTHER if mode == "three" else [])}).encode())

    def reply(self, data):
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
