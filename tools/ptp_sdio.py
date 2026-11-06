#!/usr/bin/env python3
"""Ask the camera (over PTP/SDIO) what extended capabilities it has.

0x9201 SDIO_Connect, 0x9202 SDIO_GetExtDeviceInfo, 0x9209 SDIO_GetAllExtDevicePropInfo
(read-only queries; the same ones Sony's remote-control software issues).
"""
import re, sys, usb.core
from ptp_probe import find, open_dev, send

def dump(name, kind, code, data):
    print(f'{name}: type={kind} code={code:#06x} bytes={len(data)}')
    if not data:
        return
    printable = re.findall(rb'[ -~]{5,}', data)
    if printable:
        print('  strings:', [s.decode() for s in printable[:25]])
    print('  head:', data[:48].hex())

def main():
    dev = find()
    dev.set_configuration()
    intf = open_dev(dev)
    print('OpenSession:', hex(send(dev, intf, 0x1002, (1,), tid=1)[2]))
    for code, params, name in [
        (0x9201, (1,), 'SDIO_Connect(session=1)'),
        (0x9202, (), 'SDIO_GetExtDeviceInfo'),
        (0x9209, (), 'SDIO_GetAllExtDevicePropInfo'),
    ]:
        try:
            l, t, c, tid, data = send(dev, intf, code, params, tid=code & 0xffff)
            dump(name, t, c, data)
        except usb.core.USBError as e:
            print(f'{name}: transport error {e}')

if __name__ == '__main__':
    main()
