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


def response_name(code):
    return {
        0x2001: "OK", 0x2002: "GeneralError", 0x2005: "OperationNotSupported",
        0x2019: "DeviceBusy", 0x201D: "InvalidParameter",
        0xA101: "Sony_AuthenticationFailed",
    }.get(code, hex(code))


class PtpUsb:
    def __init__(self, vid=VID, pid=PID, reset=False):
        self.dev = usb.core.find(idVendor=vid, idProduct=pid)
        if self.dev is None:
            raise SystemExit(f"camera {vid:04x}:{pid:04x} not found "
                             "(set USB Connection to PC Remote)")
        if reset:
            self._usb_reset(vid, pid)
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
        self.tid = 0
        self.session_open = False

    def _usb_reset(self, vid, pid, timeout=10.0):
        """Re-enumerate the camera. A single aborted PTP data phase otherwise
        wedges the device until the cable is pulled; reset() is the software
        equivalent and ~2 s."""
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
            time.sleep(0.4)
            dev = usb.core.find(idVendor=vid, idProduct=pid)
            if dev is None:
                continue
            try:
                dev.set_configuration()
            except usb.core.USBError:
                continue
            self.dev = dev
            return
        raise SystemExit("camera did not re-enumerate after a USB reset")

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
        """SDIO_Connect 1 -> 2, then GetExtDeviceInfo, then phase 3."""
        for phase in (1, 2):
            self.cmd(OC_SDIO_CONNECT, (phase, 0, 0), timeout=8000)
        info = None
        for attempt in range(20):
            rcode, data = self.transact(OC_SDIO_GET_EXT_DEVICE_INFO,
                                        (SONY_PROTOCOL_3_00, 1), timeout=8000)
            if rcode == 0x2001 and data:
                info = data
                break
            if rcode not in (0x2001, 0xA101, 0x2019):
                if verbose:
                    print(f"[usb] GetExtDeviceInfo -> {response_name(rcode)}",
                          file=sys.stderr)
                break
            time.sleep(0.2 * (attempt + 1))
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

    # -- objects ---------------------------------------------------------- #
    def object_handles(self, storage=0xFFFFFFFF, fmt=0, parent=0xFFFFFFFF):
        rcode, data = self.cmd(OC_GET_OBJECT_HANDLES, (storage, fmt, parent))
        (n,) = struct.unpack_from("<I", data, 0)
        return list(struct.unpack_from("<%dI" % n, data, 4))

    def object_info(self, handle):
        rcode, data = self.cmd(OC_GET_OBJECT_INFO, (handle,))
        return parse_object_info(data)

    def get_object(self, handle, size=None, chunk=64 * 1024, timeout=60000):
        """Download an object with GetPartialObject (the a5000 stalls GetObject
        for RAM handles)."""
        if size is None:
            size = self.object_info(handle)["size"]
        out = bytearray()
        while len(out) < size:
            want = min(chunk, size - len(out))
            rcode, data = self.transact(OC_GET_PARTIAL_OBJECT,
                                        (handle, len(out), want), timeout=timeout)
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
    """One release: half press (optional), full press, wait, download."""
    t0 = time.monotonic()
    if args.focus:
        ptp.set_prop(DPC_SHUTTER_HALF_RELEASE, 2, control=True)
        time.sleep(args.half_press_wait)
    ptp.set_prop(DPC_SHUTTER_RELEASE, 2, control=True)

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
        handle = RAM_OBJECT_HANDLE
        print("[usb] no ObjectAdded event, trying RAM object 0xffffc001",
              file=sys.stderr)

    info = None
    for _ in range(int(args.timeout * 2)):
        try:
            info = ptp.object_info(handle)
            if info["size"]:
                break
        except PtpError as e:
            if e.code not in (0x2009, 0x2019, 0x2002):
                raise
        time.sleep(0.5)
    if not info:
        raise SystemExit("no object became available")

    data = ptp.get_object(handle, size=info["size"])
    with open(out_path, "wb") as f:
        f.write(data)
    total = time.monotonic() - t0
    print(f"[usb] {out_path}: {len(data)} bytes, "
          f"shutter+save {total:.2f}s (image ready in {t_release - t0:.2f}s)",
          file=sys.stderr)
    return out_path


def cmd_capture(ptp, args):
    """Each shot gets a fresh PTP session: a second release in the same session
    usually loses the ObjectAdded event and has to fall back to a slow poll."""
    cam = ptp
    for n in range(args.count):
        if n:
            cam.close()
            cam = PtpUsb(args.vid, args.pid)
            cam.open()
        cam.sony_connect()
        if args.count > 1:
            base, ext = os.path.splitext(args.out or "shot.JPG")
            out = f"{base}_{n + 1:02d}{ext or '.JPG'}"
        else:
            out = args.out or "shot.JPG"
        shoot(cam, out, args)
        if n + 1 < args.count:
            time.sleep(args.interval)
    if cam is not ptp:
        cam.close()


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
    p.add_argument("--no-reset", action="store_true",
                   help="skip the USB reset at startup (faster, but a stale "
                        "PTP state will make the run fail)")
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
    sp.set_defaults(func=cmd_capture)

    sp = sub.add_parser("prop")
    sp.set_defaults(func=cmd_prop)

    args = p.parse_args(argv)
    ptp = PtpUsb(args.vid, args.pid, reset=not args.no_reset)
    ptp.open()
    if not ptp.wake():
        print("[usb] no answer, resetting USB ...", file=sys.stderr)
        try:
            ptp.dev.reset()
        except usb.core.USBError:
            pass
        time.sleep(2.0)
        ptp = PtpUsb(args.vid, args.pid)
        ptp.open()
        ptp.wake()
    try:
        try:
            args.func(ptp, args)
        except PtpError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        except usb.core.USBError:
            # The camera occasionally stops answering after an aborted
            # transaction. A USB reset re-enumerates it and normal service
            # resumes; retry the command once.
            print("[usb] camera stopped answering, resetting USB ...",
                  file=sys.stderr)
            try:
                ptp.dev.reset()
            except usb.core.USBError:
                pass
            time.sleep(2.0)
            ptp = PtpUsb(args.vid, args.pid, reset=True)
            ptp.open()
            args.func(ptp, args)
    finally:
        ptp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
