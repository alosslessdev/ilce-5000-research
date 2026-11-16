#!/usr/bin/env python3
"""Sony ILCE-5000: still capture over USB (PC Remote / PTP), no Sony software.

Uses the same sequence Sony's Remote Camera Control and libgphoto2 use:
SDIO_Connect phases 1..3, then SetExtDevicePropValue/ControlDevice with the
Sony shutter properties (0xD2C1 half press, 0xD2C2 full press), then downloads
the new object with GetObject.

    ./sony_usb.py info
    ./sony_usb.py capture --out shot.jpg
    ./sony_usb.py capture --out shot.jpg --half-press-wait 0.6
    ./sony_usb.py prop 0xD2C1          # read one property from the 0x9209 dump
"""

import argparse
import json
import os
import struct
import sys
import time

import usb.core
import usb.util

VID, PID = 0x054c, 0x094f
PTP_INTERFACE = (6, 1, 1)   # still-image class / PTP / PIMA 15740

# operation codes
OC_GET_DEVICE_INFO = 0x1001
OC_OPEN_SESSION = 0x1002
OC_CLOSE_SESSION = 0x1003
OC_GET_STORAGE_IDS = 0x1004
OC_GET_NUM_OBJECTS = 0x1006
OC_GET_OBJECT_HANDLES = 0x1007
OC_GET_OBJECT_INFO = 0x1008
OC_GET_OBJECT = 0x1009
OC_GET_PARTIAL_OBJECT = 0x101B
OC_SDIO_CONNECT = 0x9201
OC_SDIO_GET_EXT_DEVICE_INFO = 0x9202
OC_SDIO_SET_EXT_DEVICE_PROP = 0x9205
OC_SDIO_CONTROL_DEVICE = 0x9207
OC_SDIO_GET_ALL_EXT_PROPS = 0x9209

# Sony vendor device properties (libgphoto2 / ptp.h)
DPC_PRIORITY_MODE = 0xD25A
DPC_SHUTTER_HALF_RELEASE = 0xD2C1
DPC_SHUTTER_RELEASE = 0xD2C2
DPC_OBJECT_IN_MEMORY = 0xD215
DPC_FOCUS_FOUND = 0xD213
EC_OBJECT_ADDED = 0xC201
EC_DEVICE_PROP_CHANGED = 0xC203

SONY_PROTOCOL_3_00 = 0x12C
RAM_OBJECT_HANDLE = 0xFFFFC001


class PtpError(RuntimeError):
    def __init__(self, code, where):
        self.code = code
        super().__init__(f"{where}: PTP error {code:#06x}")


class NoObject(RuntimeError):
    """No ObjectAdded event: the release produced no downloadable object."""


def sony_prop_value(blob, code):
    """Current value of a u16 Sony property out of the 0x9209 blob.

    Record layout (libgphoto2 `ptp_unpack_Sony_DPD`): prop code u16, datatype
    u16, get/set u8, is-enabled u8, default value, current value, form flag and
    form data. `GetDevicePropDesc` (0x1014) stalls on this body, so the whole
    property set has to be read with `GetAllExtDevicePropInfo` (0x9209), and
    the current value is the u16 at record offset 8."""
    want = struct.pack("<HH", code, 0x0004)      # u16 datatype
    off = blob.find(want)
    if off < 0:
        raise KeyError(f"property {code:#06x} not in the SDIO blob")
    if blob[off + 5] > 2:
        raise KeyError(f"property {code:#06x}: not a u16 record")
    return struct.unpack_from("<H", blob, off + 8)[0]


def find_camera(vid=VID, pid=None):
    """Return the Sony device that actually exposes a PTP interface.

    The body re-enumerates as 054c:07c6 (mass storage / MTP) when the USB link
    is reset or the mode setting changes, so matching on the PC-Remote PID
    alone reports "not found" for a camera that is plainly plugged in."""
    others = []
    fallback = None
    for dev in usb.core.find(find_all=True, idVendor=vid):
        try:
            intfs = list(dev.get_active_configuration())
        except usb.core.USBError:
            continue
        if not intfs:
            continue
        kinds = {(i.bInterfaceClass, i.bInterfaceSubClass, i.bInterfaceProtocol)
                 for i in intfs}
        if PTP_INTERFACE not in kinds:
            others.append(dev)
        elif not pid or dev.idProduct == pid:
            return dev
        elif fallback is None:
            fallback = dev          # PTP under a different PID
    if fallback is not None:
        return fallback
    if others:
        seen = ", ".join(f"{d.idProduct:04x}" for d in others)
        raise SystemExit(
            f"camera is not in PC Remote mode (054c:{seen} offers no PTP "
            f"interface). Replug it and set USB Connection -> PC Remote")
    raise SystemExit(f"no Sony camera on USB ({vid:04x}:*); set USB "
                     f"Connection -> PC Remote")


def response_name(code):
    return {
        0x2001: "OK", 0x2002: "GeneralError", 0x2005: "OperationNotSupported",
        0x2019: "DeviceBusy", 0x201D: "InvalidParameter",
        0xA101: "Sony_AuthenticationFailed",
    }.get(code, hex(code))


class PtpUsb:
    def __init__(self, vid=VID, pid=PID, reset=False):
        self.vid, self.pid = vid, pid
        self.dev = find_camera(vid, pid)
        if reset:
            self._usb_reset(vid, pid)
        self._bind()
        self.tid = 0
        self.session_open = False

    def _bind(self):
        """Claim the PTP interface and cache its endpoints. Re-run after a USB
        reset, since the kernel hands out a fresh device handle."""
        self.dev.set_configuration()
        cfg = self.dev.get_active_configuration()
        self.intf = cfg[(0, 0)]
        if self.dev.is_kernel_driver_active(self.intf.bInterfaceNumber):
            self.dev.detach_kernel_driver(self.intf.bInterfaceNumber)
        usb.util.claim_interface(self.dev, self.intf.bInterfaceNumber)
        self.ep_in = self._ep(0x81)
        self.ep_out = self._ep(0x02)
        self.ep_evt = self._ep(0x83)
        # endpoints can stay halted after an aborted transaction
        for addr in (0x02, 0x81, 0x83):
            try:
                self.dev.clear_halt(addr)
            except usb.core.USBError:
                pass

    def reconnect(self, hard=False):
        """Drop the session and re-attach. Soft recovery clears the endpoint
        halts and rebuilds the Sony session; object handles from before are
        gone either way. `hard` additionally resets the USB link, which on
        this body can re-enumerate it as 054c:07c6 (mass storage/MTP) - use it
        only when the soft path keeps failing."""
        self.close()
        if hard:
            self._usb_reset(self.vid, self.pid)
        self._bind()
        self.tid = 0
        self.open()
        self.wake()
        self.sony_connect(verbose=False)

    def _usb_reset(self, vid, pid, timeout=15.0):
        """Reset the USB link and re-bind. Not a free operation on this body:
        the camera re-enumerates as 054c:07c6 (mass storage/MTP) and stays
        there until it is replugged or switched back to PC Remote, so callers
        only reach this through --hard-reset."""
        try:
            self.dev.reset()
        except usb.core.USBError:
            pass
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:
            pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                dev = find_camera(vid, pid)   # raises until PTP is back
            except SystemExit:
                continue
            try:
                dev.set_configuration()
            except usb.core.USBError:
                continue
            self.dev = dev
            time.sleep(1.0)          # let PTP init finish before the first op
            return
        raise SystemExit("camera did not re-enumerate as a PTP device after a "
                         "USB reset; replug it and set USB Connection -> PC "
                         "Remote")

    def _ep(self, addr):
        return usb.util.find_descriptor(
            self.intf, custom_match=lambda e: e.bEndpointAddress == addr)

    # -- transport -------------------------------------------------------- #
    def _read_container(self, timeout=8000, chunk=256 * 1024):
        buf = bytes(self.ep_in.read(chunk, timeout))
        while len(buf) < 12:
            buf += bytes(self.ep_in.read(chunk - len(buf), timeout))
        length, ctype, code, tid = struct.unpack("<IHHI", buf[:12])
        while len(buf) < length:
            buf += bytes(self.ep_in.read(min(chunk, length - len(buf)), timeout))
        return ctype, code, tid, buf[12:length]

    def transact(self, code, params=(), data=None, timeout=8000, want_data=True):
        """Command (+optional data-out). Returns (reply_code, data_in)."""
        self.tid += 1
        body = struct.pack("<%dI" % len(params), *params)
        self.ep_out.write(struct.pack("<IHHI", 12 + len(body), 1, code, self.tid)
                          + body, timeout)
        if data is not None:
            # PTP data phase: header + payload only, parameters are not repeated
            self.ep_out.write(struct.pack("<IHHI", 12 + len(data), 2, code, self.tid)
                              + data, timeout)
        data_in = b""
        while True:
            ctype, rcode, _tid, payload = self._read_container(timeout)
            if ctype == 2:
                data_in = payload
                if not want_data:
                    return rcode, data_in
                continue
            return rcode, data_in  # ctype 3 == response

    def cmd(self, code, params=(), data=None, timeout=8000, ok=(0x2001,)):
        rcode, data_in = self.transact(code, params, data, timeout)
        if rcode not in ok:
            raise PtpError(rcode, f"op {code:#06x}")
        return rcode, data_in

    def poll_event(self, timeout=1000):
        try:
            raw = bytes(self.ep_evt.read(16, timeout))
        except usb.core.USBError:
            return None
        if len(raw) < 12:
            return None
        length, ctype, code, tid = struct.unpack("<IHHI", raw[:12])
        params = struct.unpack("<%dI" % ((length - 12) // 4), raw[12:12 + (length - 12)])
        return {"type": ctype, "code": code, "tid": tid, "params": params}

    # -- session ---------------------------------------------------------- #
    def open(self, session=1):
        rcode, data = self.transact(OC_OPEN_SESSION, (session,))
        if rcode not in (0x2001, 0x2019):  # busy = already open
            raise PtpError(rcode, "OpenSession")
        self.session_open = True
        return data

    def close(self):
        if self.session_open:
            try:
                self.transact(OC_CLOSE_SESSION, timeout=3000)
            except Exception:
                pass          # camera may already be gone; nothing to clean up
            finally:
                self.session_open = False
                try:
                    usb.util.release_interface(self.dev, self.intf.bInterfaceNumber)
                except Exception:
                    pass

    def wake(self, timeout=2500):
        """Cheap liveness probe; the camera drops the PTP link when idle."""
        try:
            self.transact(OC_GET_DEVICE_INFO, timeout=timeout)
            return True
        except usb.core.USBError:
            return False

    def device_info(self):
        rcode, data = self.transact(OC_GET_DEVICE_INFO)
        if rcode != 0x2001:
            raise PtpError(rcode, "GetDeviceInfo")
        return parse_device_info(data)

    # -- Sony session ----------------------------------------------------- #
    def sony_connect(self, verbose=True):
        """SDIO_Connect 1 -> 2, GetExtDeviceInfo, then phase 3.

        GetExtDeviceInfo is sent exactly once. The camera answers the first
        handshake after a power cycle with the vendor-code list and every
        later handshake with an empty OK; a second request hangs the link
        until the camera is power cycled (Remote Camera Control sends it once
        too)."""
        for phase in (1, 2):
            self.cmd(OC_SDIO_CONNECT, (phase, 0, 0), timeout=8000)
        info = None
        rcode, data = self.transact(OC_SDIO_GET_EXT_DEVICE_INFO,
                                    (SONY_PROTOCOL_3_00, 1), timeout=8000)
        if rcode == 0x2001:
            info = data or None
        elif rcode == 0xA101 and verbose:
            print("[usb] GetExtDeviceInfo: authentication failed; continuing",
                  file=sys.stderr)
        elif rcode != 0x2001 and verbose:
            print(f"[usb] GetExtDeviceInfo -> {response_name(rcode)}",
                  file=sys.stderr)
        self.cmd(OC_SDIO_CONNECT, (3, 0, 0), timeout=8000)
        # PriorityMode = 1 (application) - lets the camera accept control
        try:
            self.set_prop(DPC_PRIORITY_MODE, 1, 1)
        except PtpError:
            pass
        return info

    def set_prop(self, prop, value, size=2, control=False):
        data = value.to_bytes(size, "little")
        code = OC_SDIO_CONTROL_DEVICE if control else OC_SDIO_SET_EXT_DEVICE_PROP
        return self.cmd(code, (prop,), data, timeout=8000)

    def all_props(self):
        rcode, data = self.transact(OC_SDIO_GET_ALL_EXT_PROPS, (0,))
        if rcode != 0x2001:
            raise PtpError(rcode, "GetAllExtDevicePropInfo")
        return data

    def object_in_memory(self):
        """0xD215: 0 = no RAM object, 1 = RAM object still being written,
        >= 0x8000 = the object is complete and readable. Downloading at 1
        makes this firmware abort the data phase."""
        return sony_prop_value(self.all_props(), DPC_OBJECT_IN_MEMORY)

    # -- objects ---------------------------------------------------------- #
    def object_handles(self, storage=0xFFFFFFFF, fmt=0, parent=0xFFFFFFFF):
        rcode, data = self.cmd(OC_GET_OBJECT_HANDLES, (storage, fmt, parent))
        (n,) = struct.unpack_from("<I", data, 0)
        return list(struct.unpack_from("<%dI" % n, data, 4))

    def object_info(self, handle):
        rcode, data = self.cmd(OC_GET_OBJECT_INFO, (handle,))
        if len(data) < 12:
            # The camera answers OK with an empty data phase while the RAM
            # object is not registered yet; callers retry on NoSuchObject.
            raise PtpError(0x2009, f"GetObjectInfo({handle:#x})")
        return parse_object_info(data)

    def get_object(self, handle, size=None, chunk=64 * 1024, timeout=5000,
                   retries=3):
        """Download an object with GetPartialObject (the a5000 stalls GetObject
        for RAM handles).

        A chunk can still abort with Errno 5 while the camera finishes the
        file; the endpoint keeps the stall, so clear it and re-issue the same
        offset with a growing delay instead of restarting the shot."""
        if size is None:
            size = self.object_info(handle)["size"]
        out = bytearray()
        while len(out) < size:
            want = min(chunk, size - len(out))
            for attempt in range(retries + 1):
                try:
                    rcode, data = self.transact(
                        OC_GET_PARTIAL_OBJECT, (handle, len(out), want),
                        timeout=timeout)
                except usb.core.USBError:
                    if attempt >= retries:
                        raise
                    try:
                        self.dev.clear_halt(0x81)
                    except usb.core.USBError:
                        pass
                    time.sleep((0.3, 1.0, 3.0)[attempt])
                    continue
                break
            if rcode not in (0x2001, 0x201D) or not data:
                raise PtpError(rcode, "GetPartialObject")
            out += data
        return bytes(out)


# --------------------------------------------------------------------------- #
# PTP parsing helpers
# --------------------------------------------------------------------------- #
def read_ptp_string(buf, off):
    if off >= len(buf):
        return "", off
    n = buf[off]
    if n == 0:
        return "", off + 1
    s = buf[off + 1: off + 1 + 2 * (n - 1)].decode("utf-16-le", "replace")
    return s, off + 1 + 2 * n


def parse_device_info(data):
    off = 0
    std_ver, vendor_ext, vendor_ver = struct.unpack_from("<HIH", data, 0)
    off = 8
    ext_desc, off = read_ptp_string(data, off)
    (func_mode,) = struct.unpack_from("<H", data, off)
    off += 2

    def u16_arr():
        nonlocal off
        (n,) = struct.unpack_from("<I", data, off)
        off += 4
        vals = struct.unpack_from("<%dH" % n, data, off)
        off += 2 * n
        return list(vals)

    ops = u16_arr()
    events = u16_arr()
    props = u16_arr()
    caps = u16_arr()
    fmts = u16_arr()
    mfr, off = read_ptp_string(data, off)
    model, off = read_ptp_string(data, off)
    fw, off = read_ptp_string(data, off)
    sn, off = read_ptp_string(data, off)
    return {"standard": std_ver, "vendor_ext": vendor_ext, "vendor_desc": ext_desc,
            "vendor_ver": vendor_ver, "functional_mode": func_mode,
            "ops": ops, "events": events, "props": props,
            "capture_formats": caps, "image_formats": fmts,
            "manufacturer": mfr, "model": model, "firmware": fw, "serial": sn}


def parse_object_info(data):
    (storage, fmt, prot, comp) = struct.unpack_from("<IHHI", data, 0)
    off = 12
    (thumb_fmt, thumb_w, thumb_h, thumb_size) = struct.unpack_from("<IIII", data, off)
    off += 16
    (img_w, img_h, img_dpth, parent) = struct.unpack_from("<IIII", data, off)
    off += 16
    (assoc, association_desc, seq) = struct.unpack_from("<IHI", data, off)
    off += 10
    name, off = read_ptp_string(data, off)
    created, off = read_ptp_string(data, off)
    modified, off = read_ptp_string(data, off)
    keywords, off = read_ptp_string(data, off)
    return {"format": fmt, "size": comp, "width": img_w, "height": img_h,
            "filename": name, "parent": parent, "seq": seq}


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_info(ptp, args):
    info = ptp.device_info()
    print_json = lambda k, v: print(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    print_json("model", info["model"])
    print_json("firmware", info["firmware"])
    print_json("serial", info["serial"])
    print_json("ops", [f"{o:#06x}" for o in info["ops"]])
    print_json("events", [f"{e:#06x}" for e in info["events"]])
    ext = ptp.sony_connect()
    print_json("vendorInfoBytes", len(ext) if ext else 0)
    if ext:
        n = struct.unpack_from("<H", ext, 0)[0]
        print_json("vendorInfoVersion", f"{n:#06x}")
        print_json("vendorInfo", [f"{c:#06x}" for c in struct.unpack_from("<%dH" % ((len(ext) - 2) // 2), ext, 2)][:40])
    print_json("sdioProps", len(ptp.all_props()))


def shoot(ptp, out_path, args):
    """One released shot, retried if the transfer aborts or no object appears.

    The retry releases the shutter again: an aborted download means the file
    may exist on the card but not on the host. The reset has to happen per
    shot, not around the whole command, or a `--count` run restarts at shot 1
    and overwrites the files it already wrote."""
    for attempt in range(1, args.attempts + 1):
        try:
            return _shoot_once(ptp, out_path, args)
        except (usb.core.USBError, NoObject) as e:
            if attempt >= args.attempts:
                raise
            print(f"[usb] {e}; reconnecting and re-releasing "
                  f"({attempt}/{args.attempts - 1})", file=sys.stderr)
            ptp.reconnect(hard=args.hard_reset)


def wait_quiet(ptp, quiet=2.0, max_wait=15.0, verbose=False):
    """Wait until the camera stops emitting property-change events.

    ObjectAdded fires while the JPEG is still being saved; the 0xc203 events
    that follow track that save. Reading the RAM object in that window makes
    the camera serve a short data phase, which stalls bulk-IN and leaves its
    file handle open (only a power cycle clears it). Once the event stream is
    quiet the file is complete and 64 KiB reads run at full speed.
    """
    start = last = time.monotonic()
    while time.monotonic() - start < max_wait and time.monotonic() - last < quiet:
        if ptp.poll_event(200):
            last = time.monotonic()
    quiet_for = time.monotonic() - last
    if verbose:
        print(f"[usb] save events quiet for {quiet_for:.1f}s", file=sys.stderr)
    return quiet_for


def _shoot_once(ptp, out_path, args):
    """One release: half press (optional), full press, wait for the RAM object,
    then download it in 64 KiB GetPartialObject chunks."""
    t0 = time.monotonic()
    if args.focus:
        ptp.set_prop(DPC_SHUTTER_HALF_RELEASE, 2, control=True)
        time.sleep(args.half_press_wait)
    ptp.set_prop(DPC_SHUTTER_RELEASE, 2, control=True)

    # ObjectAdded is the only readiness signal this body offers before the
    # object can be read. Polling 0x9209 (0xd215) while the JPEG is written is
    # what makes the firmware abort the transfer and drop the link, so the
    # capture path never touches it; object_in_memory() is for diagnostics.
    handle = None
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        ev = ptp.poll_event(500)
        if ev and ev["code"] == EC_OBJECT_ADDED and ev["params"]:
            handle = ev["params"][0]
            break
    for prop in (DPC_SHUTTER_RELEASE, DPC_SHUTTER_HALF_RELEASE):
        try:
            ptp.set_prop(prop, 1, control=True)
        except PtpError:
            pass
    t_release = time.monotonic()

    if handle is None:
        raise NoObject(f"no ObjectAdded within {args.timeout:.0f}s")

    # The handle is announced before the object is readable, and a read that
    # arrives too early aborts the bulk-IN data phase (Errno 5). The camera
    # reports the end of the save as the point where 0xc203 events stop.
    if args.quiet_time:
        wait_quiet(ptp, args.quiet_time, verbose=args.verbose)
    time.sleep(args.settle)
    info = None
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        try:
            info = ptp.object_info(handle)
        except PtpError as e:
            if e.code not in (0x2009, 0x2019, 0x2002):
                raise
            info = None
        if info and info["size"]:
            break
        time.sleep(0.5)
    if not info or not info["size"]:
        raise NoObject("object published but never became readable")

    data = ptp.get_object(handle, size=info["size"])
    with open(out_path, "wb") as f:
        f.write(data)
    total = time.monotonic() - t0
    print(f"[usb] {out_path}: {len(data)} bytes, "
          f"shutter+save {total:.2f}s (image ready in {t_release - t0:.2f}s)",
          file=sys.stderr)
    return out_path


def cmd_capture(ptp, args):
    """One PTP session for the whole burst. Closing and re-opening per shot
    wedges the SDIO layer on this body (GetExtDeviceInfo starts timing out and
    the property dump comes back empty); if a release in the same session
    loses its ObjectAdded event, shoot() reconnects and retries."""
    cam = ptp
    cam.sony_connect(verbose=False)
    for n in range(args.count):
        if args.count > 1:
            base, ext = os.path.splitext(args.out or "shot.JPG")
            out = f"{base}_{n + 1:02d}{ext or '.JPG'}"
        else:
            out = args.out or "shot.JPG"
        shoot(cam, out, args)
        if n + 1 < args.count:
            time.sleep(args.interval)
    return cam


def cmd_prop(ptp, args):
    blob = ptp.all_props()
    print(f"[usb] {len(blob)}-byte SDIO property blob; first bytes: {blob[:32].hex()}")
    with open("sdio_props.bin", "wb") as f:
        f.write(blob)
    print("[usb] saved sdio_props.bin")


def main(argv=None):
    p = argparse.ArgumentParser(description="ILCE-5000 USB still capture (PTP)")
    p.add_argument("--vid", type=lambda x: int(x, 0), default=VID)
    p.add_argument("--pid", type=lambda x: int(x, 0), default=PID)
    p.add_argument("--hard-reset", action="store_true",
                   help="reset the USB link before running; this body then "
                        "re-enumerates as 054c:07c6 (mass storage/MTP) and "
                        "needs a replug before it speaks PTP again")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("info")
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("capture")
    sp.add_argument("--out")
    sp.add_argument("--timeout", type=float, default=20.0)
    sp.add_argument("--count", type=int, default=1)
    sp.add_argument("--interval", type=float, default=2.0)
    sp.add_argument("--focus", action="store_true", help="half press first")
    sp.add_argument("--half-press-wait", type=float, default=0.8)
    sp.add_argument("--attempts", type=int, default=2,
                    help="tries per shot before giving up; a retry releases "
                         "the shutter again")
    sp.add_argument("--quiet-time", type=float, default=2.0,
                    help="wait for this many seconds of 0xc203 event silence "
                         "before reading (the camera is still saving until "
                         "then); 0 disables the wait")
    sp.add_argument("--settle", type=float, default=1.5,
                    help="extra seconds to wait after the save events stop")
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(func=cmd_capture)

    sp = sub.add_parser("prop")
    sp.set_defaults(func=cmd_prop)

    args = p.parse_args(argv)
    ptp = PtpUsb(args.vid, args.pid, reset=args.hard_reset)
    try:
        ptp.open()
    except usb.core.USBError as e:
        raise SystemExit(f"camera went away before OpenSession ({e}); replug "
                         f"it and set USB Connection -> PC Remote")
    if not ptp.wake():
        # Soft recovery only: a USB reset drops this body into its mass-storage
        # personality and costs a physical replug.
        print("[usb] no answer; rebuilding the PTP session", file=sys.stderr)
        ptp.reconnect(hard=args.hard_reset)
        if not ptp.wake():
            raise SystemExit("camera stopped answering; replug it and set USB "
                             "Connection -> PC Remote")
    try:
        args.func(ptp, args)
    except NoObject as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    except PtpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except usb.core.USBError as e:
        print(f"error: camera stopped answering ({e}); rerun, and replug the "
              f"camera if it keeps failing", file=sys.stderr)
        return 2
    finally:
        ptp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
