# Protocols

Two independent interfaces, both reverse engineered from libgphoto2 and from
the firmware itself.

## USB / PTP (PC Remote mode)

Device: `054c:094f`, one interface class 6 subclass 1 protocol 1, bulk IN `0x81`,
bulk OUT `0x02`, interrupt `0x83`.

Advertised operations:

```
0x1001 GetDeviceInfo      0x1007 GetObjectHandles   0x9201 SDIO_Connect
0x1002 OpenSession        0x1008 GetObjectInfo      0x9202 SDIO_GetExtDeviceInfo
0x1003 CloseSession       0x1009 GetObject          0x9205 SDIO_SetExtDevicePropValue
0x1004 GetStorageIDs      0x100a GetThumb           0x9207 SDIO_ControlDevice
0x1005 GetStorageInfo     0x101b GetPartialObject   0x9209 SDIO_GetAllExtDevicePropInfo
0x1006 GetNumObjects
```

Events: `0xc201 ObjectAdded`, `0xc202 ObjectRemoved`, `0xc203
DevicePropChanged`.

### Vendor properties used

| Code | Meaning | Encoding |
|---|---|---|
| `0xd25a` | PriorityMode (`1` = application) | u8 |
| `0xd2c1` | ShutterHalfRelease | u16, `2` press / `1` release |
| `0xd2c2` | ShutterRelease | u16, `2` press / `1` release |
| `0xd213` | FocusFound | u8, `2`/`3` = focused |
| `0xd215` | ObjectInMemory | u16, `>= 0x8000` = RAM object ready |

`0x9202` must be sent with parameters `(0x012c, 1)`; the camera answers with its
actual protocol version (`0x00c8` = 2.0 on this body) and a list of vendor
codes. Phases `1`, `2`, `3` of `0x9201` correspond to setup, query, and
"apply".

### Capture sequence

```
OpenSession(1)
SDIO_Connect(1,0,0); SDIO_Connect(2,0,0)
SDIO_GetExtDeviceInfo(0x12c,1)
SDIO_Connect(3,0,0)
SDIO_SetExtDevicePropValue(0xd25a, u8 1)
SDIO_ControlDevice(0xd2c1, u16 2)      # half press (optional)
SDIO_ControlDevice(0xd2c2, u16 2)      # full press
wait event 0xc201 → handle 0xffffc001
SDIO_ControlDevice(0xd2c2, u16 1); SDIO_ControlDevice(0xd2c1, u16 1)
GetObjectInfo(handle) → compressed size
GetPartialObject(handle, offset, 65536) ... until size
```

Failure modes on this model:

* `GetObjectHandles` (0x1007) and `GetDevicePropDesc` (0x1014) stall the
  endpoint — touching either leaves the camera unresponsive until a USB reset;
* `GetObject` (0x1009) stalls for RAM handles;
* requesting 512 KiB per partial object overflows the camera;
* a second release in one session misses `0xc201` (20 s fallback).

PTP container: `u32 length, u16 type (1 command / 2 data / 3 response / 4
event), u16 code, u32 transaction id`, then parameters (command/event) or
payload (data).

## WiFi / Camera Remote API

Base URL `http://192.168.122.1:8080` while the camera runs *Smart Remote
Control* as a Wi-Fi Direct group owner.

```
POST /sony/camera
{"method":"startLiveview","params":[],"id":1,"version":"1.0"}
```

Order of operations:

1. `getAvailableApiList` — if `startRecMode` is present, call it, then poll
   `getEvent(false)` until `cameraStatus == "IDLE"`.
2. `startLiveview` (or `startLiveviewWithSize` with `L`/`M`/`S`) returns a URL.
3. Still: `actTakePicture` → poll `getEvent` for `takePictureUrl` → HTTP GET.
4. Half press: `actHalfPressShutter` / `cancelHalfPressShutter`.

### Liveview framing

```
8-byte header:  0xff | payload type | u16 sequence | u32 timestamp (ms)
128-byte header: u32 magic 0x24356879 | 3-byte BE jpeg size | u8 padding
                 (rest reserved)
payload:        JPEG bytes
padding:        `padding` bytes
```

Payload type `2` carries frame info (version, frame count, frame size, then
rectangles) and must be skipped. There is no HTTP framing on the response:
the stream is close-delimited, so read exactly the sizes from the headers.
