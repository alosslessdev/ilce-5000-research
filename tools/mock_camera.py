#!/usr/bin/env python3
"""Mock of the a5000's Smart Remote Control HTTP API, for testing the client
without the camera. Emulates the JSON-RPC methods, the liveview framing and
still capture/download.

    ./mock_camera.py --port 8090        # then: ./sony_wifi.py --url http://127.0.0.1:8090 info
"""

import argparse
import json
import struct
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAYLOAD_MAGIC = 0x24356879
IMAGES = {}
STATE = {"live": False, "pending": None, "half": False}


def make_jpeg(width=640, height=360, label="0"):
    path = f"{tempfile.gettempdir()}/mock_{label}.jpg"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=1",
                    "-frames:v", "1", "-q:v", "6", path], check=True)
    with open(path, "rb") as f:
        return f.read()


def frame(jpeg, seq):
    header = b"\xff\x01" + struct.pack("!HI", seq & 0xFFFF, int(time.time() * 1000) & 0xFFFFFFFF)
    size = len(jpeg)
    padding = 0
    payload = struct.pack("!IBBBB", PAYLOAD_MAGIC, (size >> 16) & 0xFF,
                          (size >> 8) & 0xFF, size & 0xFF, padding)
    payload += b"\x00" * (128 - len(payload))
    return header + payload + jpeg + b"\x00" * padding


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        method = req.get("method")
        rid = req.get("id", 1)
        result = None
        if method == "getAvailableApiList":
            result = [["getEvent", "startRecMode", "startLiveview", "stopLiveview",
                       "actTakePicture", "awaitTakePicture", "actHalfPressShutter",
                       "cancelHalfPressShutter", "getAvailableLiveviewSize",
                       "getShootMode", "getIsoSpeedRate", "getVersions"]]
        elif method == "startRecMode":
            result = [0]
        elif method == "getEvent":
            events = [{"type": "cameraStatus", "cameraStatus": "IDLE"}]
            if STATE["pending"]:
                events.append({"type": "takePictureUrl", "takePictureUrl":
                               [STATE["pending"]]})
                STATE["pending"] = None
            result = [events]
        elif method == "startLiveview":
            STATE["live"] = True
            result = [f"http://127.0.0.1:{self.server.server_address[1]}"
                      "/liveview/liveviewstream"]
        elif method == "stopLiveview":
            STATE["live"] = False
            result = [0]
        elif method == "actTakePicture":
            url = f"http://127.0.0.1:{self.server.server_address[1]}/img/DSC0001.JPG"
            STATE["pending"] = url
            result = [[url]]
        elif method == "awaitTakePicture":
            result = [[STATE["pending"]]] if STATE["pending"] else []
        elif method == "actHalfPressShutter":
            STATE["half"] = True
            result = []
        elif method == "cancelHalfPressShutter":
            STATE["half"] = False
            result = []
        elif method == "getAvailableLiveviewSize":
            result = [["L", "M", "S"]]
        elif method == "getShootMode":
            result = ["still"]
        elif method == "getIsoSpeedRate":
            result = ["AUTO"]
        else:
            return self._json({"id": rid, "error": [12, f"unknown method {method}"]})
        self._json({"id": rid, "result": result})

    def do_GET(self):
        if self.path.startswith("/img/"):
            data = IMAGES["still"]
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/liveview"):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            seq = 0
            try:
                while True:
                    self.wfile.write(frame(IMAGES["live"][seq % len(IMAGES["live"])], seq))
                    self.wfile.flush()
                    seq += 1
                    time.sleep(0.22)   # ~4.5 fps, like the real preview
            except (BrokenPipeError, ConnectionResetError):
                return
        self.send_error(404)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8090)
    args = p.parse_args()
    IMAGES["live"] = [make_jpeg(label=str(i)) for i in range(3)]
    IMAGES["still"] = make_jpeg(1600, 1066, label="still")
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    print(f"mock camera on http://127.0.0.1:{args.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
