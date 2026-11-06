#!/usr/bin/env python3
"""Sony ILCE-5000 (a5000) WiFi control: liveview + stills via the Camera Remote API.

Standard library only. Talks to the camera's Smart Remote Control app
(HTTP + JSON-RPC on the camera's own access point, usually 192.168.122.1:8080).

Examples
--------
    ./sony_wifi.py info
    ./sony_wifi.py live --save ./frames --seconds 10
    ./sony_wifi.py live --mjpeg 8088            # http://localhost:8088/
    ./sony_wifi.py live --ffmpeg-loopback /dev/video2
    ./sony_wifi.py still --out shot.jpg --half-press
    ./sony_wifi.py still --count 5 --interval 3 --dir ./stills
"""

import argparse
import http.client
import json
import os
import re
import signal
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

DEFAULT_CAMERA = "http://192.168.122.1:8080"
SSDP_ADDR = ("239.255.255.255", 1900)
SSDP_ST = "urn:schemas-sony-com:service:ScalarWebAPI:1"
PAYLOAD_START_CODE = 0x24356879  # "$5hy", Sony liveview payload header magic


# --------------------------------------------------------------------------- #
# discovery / JSON-RPC
# --------------------------------------------------------------------------- #
def discover(timeout=4.0):
    """SSDP M-SEARCH for the camera's ScalarWebAPI service. Returns base URL."""
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 3\r\n"
        f"ST: {SSDP_ST}\r\n\r\n"
    ).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)
    try:
        sock.sendto(msg, SSDP_ADDR)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                break
            text = data.decode("utf-8", "replace")
            loc = re.search(r"(?i)^location:\s*(\S+)", text, re.M)
            if loc:
                return loc.group(1).rstrip("/")
    finally:
        sock.close()
    return None


class CameraError(RuntimeError):
    pass


class Camera:
    """Minimal Camera Remote API client (a5000 speaks version 1.0-1.4)."""

    def __init__(self, base_url, version="1.0", timeout=30.0):
        base_url = base_url.rstrip("/")
        if not base_url.startswith("http"):
            base_url = "http://" + base_url
        u = urlparse(base_url)
        self.host, self.port = u.hostname, u.port or 80
        self.base = base_url
        self.version = version
        self.timeout = timeout
        self._id = 0

    def _cmd(self, method, params=(), target="camera", version=None, timeout=None):
        self._id += 1
        body = json.dumps({
            "method": method,
            "params": list(params),
            "id": self._id,
            "version": version or self.version,
        }).encode()
        conn = http.client.HTTPConnection(self.host, self.port,
                                          timeout=timeout or self.timeout)
        try:
            conn.request("POST", f"/sony/{target}", body,
                         {"Content-Type": "application/json; charset=UTF-8"})
            resp = conn.getresponse()
            raw = resp.read()
        finally:
            conn.close()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise CameraError(f"{method}: non-JSON reply ({resp.status}): {raw[:200]!r}")
        if "error" in data:
            raise CameraError(f"{method}: error {data['error']}")
        return data.get("result")

    # -- session ---------------------------------------------------------- #
    def api_list(self):
        res = self._cmd("getAvailableApiList", target="camera")
        return res[0] if res else []

    def start_rec_mode(self, timeout=20.0):
        """Cameras of this generation need startRecMode before liveview/shooting."""
        if "startRecMode" not in self.api_list():
            return False
        try:
            self._cmd("startRecMode")
        except CameraError:
            pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if "startLiveview" in self.api_list() and self.status() == "IDLE":
                return True
            time.sleep(0.5)
        raise CameraError("camera did not reach IDLE/rec mode in time")

    def status(self):
        for ev in self.event(poll=False) or []:
            if ev.get("type") == "cameraStatus":
                return ev.get("cameraStatus")
        return None

    # -- events ----------------------------------------------------------- #
    def event(self, poll=True):
        res = self._cmd("getEvent", [bool(poll)], timeout=60)
        return res[0] if res else []

    def take_picture(self):
        res = self._cmd("actTakePicture")
        return res[0] if res else []

    def await_picture(self):
        res = self._cmd("awaitTakePicture")
        return res[0] if res else []

    def half_press(self):
        return self._cmd("actHalfPressShutter")

    def cancel_half_press(self):
        return self._cmd("cancelHalfPressShutter")

    def zoom(self, direction, movement):
        """direction in ('in','out'), movement in ('start','stop','1step')."""
        return self._cmd("actZoom", [direction, movement])

    # -- values ----------------------------------------------------------- #
    def get(self, name):
        return self._cmd("get" + name)

    def set(self, name, value):
        return self._cmd("set" + name, [value])

    def liveview_sizes(self):
        res = self._cmd("getAvailableLiveviewSize")
        return res[0] if res else []

    # -- liveview --------------------------------------------------------- #
    def start_liveview(self, size=None):
        method = "startLiveviewWithSize" if size else "startLiveview"
        params = [size] if size else []
        res = self._cmd(method, params)
        urls = res[0] if res else []
        if isinstance(urls, str):
            urls = [urls]
        if not urls:
            raise CameraError("startLiveview returned no URL")
        return urls[0].replace("\\/", "/")

    def stop_liveview(self):
        try:
            self._cmd("stopLiveview")
        except CameraError:
            pass

    def download(self, url, timeout=30.0):
        u = urlparse(url)
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
        try:
            conn.request("GET", u.path, headers={"Connection": "close"})
            resp = conn.getresponse()
            data = resp.read()
            if resp.status != 200:
                raise CameraError(f"download {url}: HTTP {resp.status}")
            return data
        finally:
            conn.close()


# --------------------------------------------------------------------------- #
# liveview stream
# --------------------------------------------------------------------------- #
class LiveviewReader:
    """Parses the camera's liveview stream: 8-byte header, 128-byte payload
    header, JPEG, padding. Keeps the newest frame; reconnects when needed."""

    def __init__(self, camera, url, on_frame=None, stats_every=5.0):
        self.camera = camera
        self.url = url
        self.on_frame = on_frame
        self.stats_every = stats_every
        self.frame = None
        self.frame_id = 0
        self.frames = 0
        self.bytes = 0
        self.connected = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def latest(self):
        with self._lock:
            return self.frame

    # -- internals -------------------------------------------------------- #
    def _run(self):
        failures = 0
        last_report = time.monotonic()
        while not self._stop.is_set():
            try:
                self._stream_once()
                failures = 0
            except Exception as exc:  # socket/parse errors -> reconnect
                failures += 1
                self.connected = False
                if self._stop.is_set():
                    break
                print(f"[liveview] {type(exc).__name__}: {exc}; reconnecting "
                      f"({failures})", file=sys.stderr)
                time.sleep(min(0.5 * failures, 3.0))
                if failures >= 3:
                    try:
                        self.url = self.camera.start_liveview()
                        failures = 0
                    except CameraError as cerr:
                        print(f"[liveview] restart failed: {cerr}", file=sys.stderr)
                        time.sleep(2.0)
            now = time.monotonic()
            if now - last_report >= self.stats_every:
                last_report = now
                with self._lock:
                    frames, nbytes = self.frames, self.bytes
                if frames:
                    print(f"[liveview] {frames} frames, {nbytes // 1024} KiB, "
                          f"last={frames / self.stats_every:.1f} fps", file=sys.stderr)

    def _stream_once(self):
        u = urlparse(self.url)
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=10.0)
        try:
            conn.request("GET", u.path or "/", headers={"Connection": "keep-alive"})
            resp = conn.getresponse()
            if resp.status != 200:
                raise CameraError(f"liveview HTTP {resp.status}")
            self.connected = True
            print(f"[liveview] connected to {self.url}", file=sys.stderr)
            r = resp.read
            while not self._stop.is_set():
                head = r(8)
                if len(head) != 8:
                    raise ConnectionError("stream closed")
                start, ptype, _seq, _ts = struct.unpack("!BBHI", head)
                if start != 0xFF:
                    raise ValueError(f"bad start byte {start:#04x}")
                phead = r(128)
                if len(phead) != 128:
                    raise ConnectionError("short payload header")
                code, s2, s1, s0, padding = struct.unpack_from("!IBBBB", phead)
                if code != PAYLOAD_START_CODE:
                    raise ValueError(f"bad payload magic {code:#010x}")
                size = (s2 << 16) | (s1 << 8) | s0
                if ptype == 1:
                    data = r(size)
                    if len(data) != size:
                        raise ConnectionError("short JPEG payload")
                    if padding:
                        r(padding)
                    self._publish(data)
                elif ptype == 2:
                    frames = struct.unpack_from("!H", phead, 10)[0]
                    fs = struct.unpack_from("!H", phead, 12)[0]
                    r(frames * fs)
                    if padding:
                        r(padding)
                else:
                    r(size)
                    if padding:
                        r(padding)
        finally:
            conn.close()

    def _publish(self, jpeg):
        with self._lock:
            self.frame = jpeg
            self.frame_id += 1
            self.frames += 1
            self.bytes += len(jpeg)
        if self.on_frame is not None:
            self.on_frame(jpeg)


# --------------------------------------------------------------------------- #
# MJPEG over HTTP (browser / OBS media source / ffmpeg input)
# --------------------------------------------------------------------------- #
class MjpegServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, reader, html=False):
        self.reader = reader
        self.want_html = html
        super().__init__(addr, MjpegHandler)


class MjpegHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            page = (b"<!doctype html><title>ILCE-5000</title>"
                    b"<body style='margin:0;background:#000'>"
                    b"<img src='/stream.mjpg' style='width:100%;height:auto'></body>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return
        if self.path != "/stream.mjpg":
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last_id = -1
        try:
            while True:
                jpeg = self.server.reader.latest()
                cur = self.server.reader.frame_id
                if jpeg is None or cur == last_id:
                    time.sleep(0.01)
                    continue
                last_id = cur
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                 b"Content-Length: %d\r\n\r\n" % len(jpeg))
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def save_writer(directory):
    os.makedirs(directory, exist_ok=True)
    counter = {"n": 0}

    def on_frame(jpeg):
        counter["n"] += 1
        path = os.path.join(directory, f"frame-{counter['n']:06d}.jpg")
        with open(path, "wb") as f:
            f.write(jpeg)

    return on_frame


def ffmpeg_writer(device):
    import subprocess
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "warning",
         "-f", "mjpeg", "-i", "pipe:0",
         "-vf", "format=yuv420p", "-f", "v4l2", device],
        stdin=subprocess.PIPE)
    lock = threading.Lock()

    def on_frame(jpeg):
        with lock:
            try:
                proc.stdin.write(jpeg)
                proc.stdin.flush()
            except BrokenPipeError:
                raise SystemExit("ffmpeg exited")

    return on_frame, proc


def find_camera(args):
    if args.url:
        return args.url
    url = discover()
    if url:
        print(f"[discovery] {url}", file=sys.stderr)
        return url
    print(f"[discovery] no SSDP reply, assuming {DEFAULT_CAMERA}", file=sys.stderr)
    return DEFAULT_CAMERA


def print_json(label, value):
    print(f"{label}: {json.dumps(value, ensure_ascii=False)}")


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_scan(cam, args):
    """Find cameras that answer the ScalarWebAPI JSON-RPC on this subnet."""
    import ipaddress
    import socket
    import concurrent.futures as cf

    net = ipaddress.ip_network(args.net, strict=False)
    hosts = [str(h) for h in net.hosts()]

    def probe(ip):
        s = socket.socket()
        s.settimeout(args.timeout)
        try:
            if s.connect_ex((ip, args.port)) != 0:
                return None
        finally:
            s.close()
        base = f"http://{ip}:{args.port}"
        try:
            cam = Camera(base, timeout=3.0)
            versions = cam._cmd("getVersions", target="system")
            apps = cam._cmd("getApplicationNames", target="system")
            return base, versions, apps
        except Exception:
            return None

    found = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for r in pool.map(probe, hosts):
            if r:
                found.append(r)
    for base, versions, apps in found:
        print(f"{base}  versions={versions}  apps={apps}")
    if not found:
        print(f"no camera API on {net} port {args.port}", file=sys.stderr)
    return found


def cmd_info(cam, args):
    print_json("availableApiList", cam.api_list())
    for name in ("getVersions", "getApplicationNames"):
        try:
            print_json(name, cam._cmd(name, target="system"))
        except CameraError as e:
            print_json(name, f"unavailable ({e})")
    try:
        print_json("availableLiveviewSize", cam.liveview_sizes())
    except CameraError as e:
        print_json("availableLiveviewSize", f"unavailable ({e})")
    for name in ("ShootMode", "IsoSpeedRate", "ShutterSpeed", "FNumber",
                 "ExposureMode", "ExposureCompensation", "WhiteBalance",
                 "FocusMode", "ZoomSetting", "CameraFunction"):
        try:
            print_json(name, cam.get(name))
        except CameraError:
            pass


def cmd_live(cam, args):
    cam.start_rec_mode()
    url = cam.start_liveview(args.size)
    print(f"[liveview] stream {url}", file=sys.stderr)

    proc = None
    on_frame = None
    if args.save:
        on_frame = save_writer(args.save)
    if args.ffmpeg_loopback:
        on_frame, proc = ffmpeg_writer(args.ffmpeg_loopback)

    reader = LiveviewReader(cam, url, on_frame=on_frame,
                            stats_every=args.stats_every).start()
    server = None
    if args.mjpeg:
        server = MjpegServer(("0.0.0.0", args.mjpeg), reader)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"[mjpeg] http://localhost:{args.mjpeg}/  (stream.mjpg)", file=sys.stderr)

    started = time.monotonic()
    try:
        while True:
            time.sleep(0.2)
            if args.seconds and time.monotonic() - started >= args.seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
        if server:
            server.shutdown()
        if proc:
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.wait(timeout=5)
        cam.stop_liveview()
        print(f"[liveview] stopped after {reader.frames} frames", file=sys.stderr)


def cmd_still(cam, args):
    cam.start_rec_mode()
    if args.dir:
        os.makedirs(args.dir, exist_ok=True)
    for n in range(args.count):
        if args.half_press:
            cam.half_press()
            time.sleep(args.focus_wait)
        try:
            urls = cam.take_picture()
        finally:
            if args.half_press:
                try:
                    cam.cancel_half_press()
                except CameraError:
                    pass
        if not urls:
            urls = cam.await_picture()
        print(f"[still] {n + 1}/{args.count}: {urls}", file=sys.stderr)
        for i, url in enumerate(urls):
            if url.startswith("http"):
                data = cam.download(url)
                if args.dir:
                    out = os.path.join(args.dir, f"DSC{int(time.time() * 1000)}_{i}.JPG")
                elif args.count > 1:
                    out = f"shot_{n + 1}.JPG"
                else:
                    out = args.out or "shot.JPG"
                with open(out, "wb") as f:
                    f.write(data)
                print(f"[still] saved {out} ({len(data)} bytes)", file=sys.stderr)
            else:
                print(f"[still] camera-side path: {url}", file=sys.stderr)
        if n + 1 < args.count:
            time.sleep(args.interval)


def main(argv=None):
    p = argparse.ArgumentParser(description="Sony ILCE-5000 WiFi liveview + stills")
    p.add_argument("--url", help="camera base URL (default: SSDP, else "
                                f"{DEFAULT_CAMERA})")
    p.add_argument("--version", default="1.0", help="API version (default 1.0)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan", help="find the camera on a subnet (port 8080)")
    sp.add_argument("--net", default="192.168.0.0/24")
    sp.add_argument("--port", type=int, default=8080)
    sp.add_argument("--timeout", type=float, default=0.4)
    sp.add_argument("--workers", type=int, default=128)
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("info", help="dump supported methods and current settings")
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("live", help="stream liveview")
    sp.add_argument("--size", help="liveview size (L/M/S, see `info`)")
    sp.add_argument("--save", metavar="DIR", help="write every frame as JPEG")
    sp.add_argument("--mjpeg", type=int, metavar="PORT",
                    help="serve MJPEG on this port")
    sp.add_argument("--ffmpeg-loopback", metavar="DEV",
                    help="pipe into ffmpeg -> v4l2loopback device")
    sp.add_argument("--seconds", type=float, default=0, help="stop after N seconds")
    sp.add_argument("--stats-every", type=float, default=5.0)
    sp.set_defaults(func=cmd_live)

    sp = sub.add_parser("still", help="take still pictures over WiFi")
    sp.add_argument("--out", default="shot.JPG", help="output file (count=1)")
    sp.add_argument("--dir", help="save into this directory instead")
    sp.add_argument("--count", type=int, default=1)
    sp.add_argument("--interval", type=float, default=2.0)
    sp.add_argument("--half-press", action="store_true", help="focus first")
    sp.add_argument("--focus-wait", type=float, default=0.8)
    sp.set_defaults(func=cmd_still)

    args = p.parse_args(argv)
    if args.cmd == "scan":
        args.func(None, args)
        return 0
    cam = Camera(find_camera(args), version=args.version)
    try:
        args.func(cam, args)
    except CameraError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    sys.exit(main())
