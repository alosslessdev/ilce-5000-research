# Remote Camera Control 3.8 — protocol notes

Sony's desktop tethering app is the reference client for this camera. It was
disassembled to recover the exact PTP/SDIO sequence and to explain the two
quirks that break naive clients on the ILCE-5000.

No Sony binaries are stored in this repository; only the procedure and the
findings.

## Getting the binary

Downloads were discontinued in September 2018, but the 2017 installer is still
served from Sony's download host. The 2017 support page linked:

```
http://support.d-imaging.sony.co.jp/download/RCC/PddSDfoDAj/RCC38_1705c.exe?fm=jp
```

(Archived copy: `web.archive.org`, `sony.jp/support/software/rcc/download/win.html`,
2017 snapshot.)

`RCC38_1705c.exe` is a WinZip self-extractor: a PE32 stub followed by a ZIP at
the first `PK\x03\x04` (offset `0x143491`). The ZIP contains a WiX-built
`SetupRemote.msi`; its embedded cabinet (4.5 MB `MSCF` stream) holds
`RCCAppFile` — a 926 KB PE32 MFC application with the whole PTP stack
statically linked.

```python
data = open("RCC38_1705c.exe", "rb").read()
zip_start = data.index(b"PK\x03\x04")
# unzip data[zip_start:] -> RCC38_1705c/SetupRemote.msi
# unzip the MSCF stream from the MSI OLE storage -> members incl. RCCAppFile
```

Relevant RTTI classes: `CPTPControlWin`, `CPTPProxy`, `PTPProxy1_1`,
`PTPProxy2_0`, `CPTPControlThread`, `PTPSendParameter`.

## Connect sequence

`PTPControlWin::Connect` asks the camera for its interface version and picks a
proxy: version `< 2` → `PTPProxy1_1`, `>= 2` → `PTPProxy2_0` (this camera
reports 2.0 / `0x00c8`).

`PTPProxy2_0` (decompiled):

```
OpenSession
SDIO_Connect(1,0,0)
SDIO_Connect(2,0,0)
SDIO_GetExtDeviceInfo(0x00c8)      # exactly one request, one parameter
SDIO_Connect(3,0,0)
```

`SDIOConnect` is a `0x9201` command with three parameters. The result is
unpacked as `(protocol_version, count, codes...)`, and the proxy accepts any
version below 300 (`version / 100 < 3`) — it does not require 3.0, unlike
libgphoto2 and pysonycam.

`GetSDIOGetExtDeviceInfo` sends `0x9202` with **one** parameter:
`PTPBlockSize`-style protocol value `200` (`0x00c8`). libgphoto2 sends
`(0x012c, 1)` instead; both are accepted. The difference that matters is the
retry policy: the app sends the request once and treats an empty `OK` as valid.
This firmware only returns the vendor list on the first handshake after a
power cycle; a second request hangs the link until the camera is power cycled.

## Block size

`CPTPProxy` initialises the transfer block to `0x20000` (128 KiB), then each
proxy overwrites it from the app settings:

```
PTPBlockSize = GetPrivateProfileInt("Settings", "PTPBlockSize", 0x100) << 10
```

so the default is 256 KiB. `GetPartialObject` is issued with
`min(block, remaining)` and the reply length is validated to be exactly the
requested length.

On this body a data phase started while the JPEG is still being written can
come back short, which stalls the bulk-IN endpoint. The app's event thread is
what makes this work in practice: by the time the image is downloaded the save
has finished. `tools/sony_usb.py` waits for the camera's `0xc203` events to go
quiet and then uses 64 KiB reads, verified end to end (6 MB JPEG, ~0.6 s).

## Capture trigger

The app's shutter worker (`FUN_0042f730`) sends, through
`SDIO_ControlDevice` (`0x9207`, one property parameter + u16 data):

```
0xd2c7 = 2      # RequestOneShooting
Sleep(700)
0xd2c2 = 1      # ShutterRelease release
Sleep(100)
0xd2c1 = 1      # ShutterHalfRelease release
```

On the ILCE-5000 `0xd2c7` returns `OK` but no `ObjectAdded` event is emitted;
the verified path is a `0xd2c2 = 2` press (plus optional `0xd2c1 = 2`
half-press). Both are exposed by the tool.

## Object transfer

After `ObjectAdded` (`0xc201`) with the RAM handle `0xffffc001`:

```
GetObjectInfo  0x1008(0xffffc001)         # 0x90-byte reply, size at +8
GetPartialObject 0x101b(handle, offset, len)
```

or `GetObject 0x1009(handle)` when the download helper is compiled the other
way. The app allocates a 40 MB buffer, validates every partial reply length,
and advances the offset by exactly the reply size. There is no transfer-mode
property on this body: the protocol-1.x `0xd21b = 0x8000` write belongs to
`PTPProxy1_1` only.

## Property codes seen in the app

| Code | Meaning |
|---|---|
| `0xd2c1` | ShutterHalfRelease (2 = press, 1 = release) |
| `0xd2c2` | ShutterRelease (2 = press, 1 = release) |
| `0xd2c7` | RequestOneShooting (2 = shoot) |
| `0xd213` | FocusFound |
| `0xd215` | ObjectInMemory (set once the RAM object can be read) |
| `0xd21b` | transfer/mode flag used by the protocol-1.x proxy (`0x8000`) |
| `0xd21c` | mode byte used by the protocol-1.x proxy (`0x80`) |
| `0xd25a` | PriorityMode (1 = application) — set by libgphoto2, not by RCC |

Commands used: `0x9201` connect, `0x9202` ext device info, `0x9203` ext
property descriptor, `0x9205` set ext device property, `0x9207` control device,
`0x9208`? (not needed here). `0x9207` and `0x9205` both take one property
parameter and a 1/2/4-byte value; the app chooses by property data type.

## Useful log strings in the binary

```
PTPControlWin::Connect(L%04d) PTPProxy CHECK
PTPControlWin::Connect(L%04d) camera(version=%d)PTPProxy reuslt(%ld)
SDIOConnect(L%04d): HR=%04x,RC=%04x
GetSDIOGetExtDeviceInfo(L%04d): HR=%04x,RC=%04x
SetControlDevice(L%04d): HR=%04x,RC=%04x,PC=%04x,PV=%04x
GetObjectInfo(L%04d): HR=%04x,RC=%04x
```
