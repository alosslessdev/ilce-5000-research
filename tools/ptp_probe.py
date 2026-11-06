#!/usr/bin/env python3
"""Minimal PTP probe: read DeviceInfo from the connected camera and dump the
supported operation codes (this is what the camera itself advertises over USB)."""
import struct, sys, usb.core, usb.util

VID, PID = 0x054c, 0x094f

def find():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        sys.exit("camera not found")
    return dev

def open_dev(dev):
    cfg = dev.get_active_configuration()
    intf = cfg[(0, 0)]
    if dev.is_kernel_driver_active(intf.bInterfaceNumber):
        dev.detach_kernel_driver(intf.bInterfaceNumber)
    usb.util.claim_interface(dev, intf.bInterfaceNumber)
    return intf

def ep(intf, addr):
    return usb.util.find_descriptor(intf, custom_match=lambda e: e.bEndpointAddress == addr)

def read_container(dev, intf, timeout=5000):
    # Read with a large buffer: a 12-byte read makes libusb bail out with
    # "Overflow" because the device sends the whole container in one transfer.
    buf = bytes(ep(intf, 0x81).read(256 * 1024, timeout))
    if len(buf) < 12:
        raise RuntimeError(f'short PTP header: {len(buf)} bytes')
    length, ctype, code, tid = struct.unpack('<IHHI', buf[:12])
    while len(buf) < length:
        buf += bytes(ep(intf, 0x81).read(length - len(buf), timeout))
    payload = buf[12:length]
    try:
        ep(intf, 0x83).read(8, 1000)   # event: none expected here
    except usb.core.USBError:
        pass
    return length, ctype, code, tid, payload

def send(dev, intf, code, params=(), data=b'', tid=1):
    length = 12 + 4 * len(params) + len(data)
    buf = struct.pack('<IHHI', length, 2, code, tid) + struct.pack('<%dI' % len(params), *params) + data
    ep(intf, 0x02).write(buf, 5000)
    data = b''
    while True:
        length, ctype, code, tid, payload = read_container(dev, intf)
        if ctype == 2:      # data phase
            data = payload
            continue
        return length, ctype, code, tid, data   # 3 = response

def ptp_string(buf, off):
    n = buf[off]
    s = buf[off + 1: off + 1 + 2 * max(n - 1, 0)].decode('utf-16-le', 'replace')
    return s, off + 1 + 2 * n

def main():
    dev = find()
    dev.set_configuration()
    intf = open_dev(dev)
    l, t, c, tid, p = send(dev, intf, 0x1002, (1,))
    print(f'OpenSession -> type={t} code={c:#06x} len={l}')
    l, t, c, tid, p = send(dev, intf, 0x1001, tid=2)
    print(f'GetDeviceInfo -> type={t} code={c:#06x} payload={len(p)}')
    if c != 0x2001:
        sys.exit('GetDeviceInfo failed')

    off = 0
    std_ver, vendor_ext, vendor_ver = struct.unpack_from('<HIH', p, 0)
    off = 8
    desc, off = ptp_string(p, off)
    func_mode, = struct.unpack_from('<H', p, off); off += 2

    def array(fmt):
        nonlocal off
        n, = struct.unpack_from('<I', p, off); off += 4
        vals = struct.unpack_from('<%d%s' % (n, fmt), p, off)
        off += n * struct.calcsize(fmt)
        return vals

    ops = array('H')
    evs = array('H')
    props = array('H')
    caps = array('H')
    imgs = array('H')
    mfr, off = ptp_string(p, off)
    model, off = ptp_string(p, off)
    fw, off = ptp_string(p, off)
    sn, off = ptp_string(p, off)

    print(f'StandardVersion={std_ver:#06x} VendorExt={vendor_ext:#010x} ({desc}) '
          f'VendorExtVer={vendor_ver:#06x} FunctionalMode={func_mode}')
    print(f'Manufacturer={mfr!r} Model={model!r} DeviceVersion={fw!r} Serial={sn!r}')
    print(f'ops={len(ops)} events={len(evs)} props={len(props)} '
          f'capture_fmts={len(caps)} image_fmts={len(imgs)}')
    print('Operations:')
    for o in ops:
        print(f'  {o:#06x}')
    print('Vendor-range operations (0x9000+):', ' '.join(f'{o:#06x}' for o in ops if o >= 0x9000))
    print('DeviceProperties:')
    print('  ' + ' '.join(f'{x:#06x}' for x in props))
    print('Events:')
    print('  ' + ' '.join(f'{x:#06x}' for x in evs))

if __name__ == '__main__':
    main()
