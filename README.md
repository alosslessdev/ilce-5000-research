# Sony ILCE-5000 (a5000) — USB & WiFi capture research

What this camera can and cannot do for stills and video over USB/WiFi, with the
firmware evidence behind each answer, plus working tools.

Tested against a real ILCE-5000 (USB `054c:094f`, firmware 1.10) on Linux.

## Results

| Capability | Verdict | Basis |
|---|---|---|
| **USB video** (UVC / webcam / PC-Remote liveview) | **No** | single PTP interface, no UVC descriptors, no video-class code in firmware |
| **USB stills** (shutter release + download) | **Yes** | verified on hardware, 5456×3632 JPEG, ~7.4 MB, ~2 s/shot |
| **WiFi liveview + stills** | **Yes, preview quality** | Camera Remote API served by the Smart Remote Control app (Wi-Fi Direct) |
| **WiFi remote control over an existing access point** | **No** | remote path is Wi-Fi Direct only; the radio cannot be station and group owner at once |
| **Real video at all** | **HDMI + capture card** | non-clean by default; clean 1080p is reachable via a settings-memory write |

USB stills are the usable path on this body. `tools/sony_usb.py` does it with
`pyusb` only — no Sony software, no libgphoto2.

## Why USB video is impossible here

* USB descriptors: one interface, class 6/1/1 (PTP), two bulk + one interrupt
  endpoint, one alternate setting. No isochronous endpoints.
* Decrypted firmware (see [docs/firmware.md](docs/firmware.md)) ships
  `usbg_stillimage`, `usbg_storage`, `usbg_sen`, `usbg_buspower` — and no
  `uvcvideo`, `g_webcam`, `videodev`, or any V4L2 module.
* The string `uvc` does not occur anywhere in the decrypted tree.
* In PC Remote mode the camera advertises 16 PTP operations
  (`0x1001`–`0x100a`, `0x101b`, `0x9201`, `0x9202`, `0x9205`, `0x9207`,
  `0x9209`). None of them returns a frame.
* Sony's own *Imaging Edge Webcam* supported list starts at ILCE-5100.

The a5000's object layer also lacks the liveview command set its sibling has:

| Model | FW | `libObj.so` | `CommandLiveView*` | Imaging Edge Webcam |
|---|---|---|---|---|
| ILCE-5000 (`0x41030012`) | 1.10 | 15.6 MB | absent | ✖ |
| ILCE-6000 (`0x41030035`) | 3.21 | 17.4 MB | absent | ✖ |
| ILCE-5100 (`0x42030036`) | 3.10 | 16.4 MB | present | ✔ |

## USB stills — how it works

```
OpenSession
SDIO_Connect 1 → 2 → (SDIO_GetExtDeviceInfo) → 3
SDIO_ControlDevice 0xd2c1 (half press) / 0xd2c2 (full press), 2 = press, 1 = release
wait for event 0xc201 ObjectAdded  → handle 0xffffc001 (RAM buffer)
GetPartialObject 0x101b in 64 KiB chunks
```

Quirks that make or break it on this body:

* `GetObject` (0x1009) is **stalled** for RAM handles — use `GetPartialObject`.
* 512 KiB chunks overflow the camera; 64 KiB is stable.
* A second release **in the same PTP session** loses the `ObjectAdded` event.
  One session per shot.
* One aborted data phase wedges the device until the cable is pulled; a
  `usb.reset()` (~1.8 s) is the software equivalent.
* `GetDevicePropDesc` (0x1014) stalls and `SetDevicePropValue` (0x1016) is not
  advertised, so settings cannot be read or changed over this interface.

## WiFi — what the API gives you

Camera: *Menu → Application List → Smart Remote Control* (or *Wi-Fi → Ctrl w/
Smartphone*). The camera becomes an access point at `192.168.122.1:8080`:

```bash
tools/sony_wifi.py info
tools/sony_wifi.py live --mjpeg 18088     # http://localhost:18088/  (OBS, ffmpeg)
tools/sony_wifi.py still --out shot.jpg --half-press
```

The API is JSON-RPC over HTTP (`POST /sony/camera`). Liveview is a raw TCP
stream of JPEGs: 8-byte header, 128-byte payload header (magic `0x24356879`,
3-byte big-endian size), JPEG, padding. It is the preview pipeline: roughly
640×360, a few fps — framing, not delivery.

`startRecMode` is required first on this generation, then `getEvent` until
`cameraStatus == "IDLE"`.

### Infrastructure (AP) mode does not serve the API

Firmware evidence (`init.rc`, libraries):

* station mode exists (`wpa_supplicant`, `dhcpcd_ath0`, UI strings for AP
  selection/auth failures) — it is used by "Send to Computer" and DLNA;
* remote control runs as Wi-Fi Direct group owner
  (`libGOConnectionManager.so`: `StartGO`/`StopGO`; `p2pif_name ath1`);
* `wlan.com.sony.wifi.dual_if 0` and `wlan.com.sony.wifi.concurrent 0` — one
  radio mode at a time;
* `orb-server` is started on demand by `bin/WebApiLauncher.sh`
  (`orb-server -f /usr/share/network/remotecontrol -p 10000`), not at boot.

So while the camera sits on your access point, nothing listens on `:8080`,
and the `DIRECT-…:ILCE-5000` AP is not up either. Corroborated by a full sweep
of the LAN (254 hosts, ports 8080/80/1900) plus SSDP.

## Android userland: an app *can* read liveview

The firmware's own framework exposes the preview pipeline to PMCA apps —
`CameraEx` (preview, JPEG listener) and `CameraSequence` (`startPreviewSequence`,
`getPreviewSequenceFrames(int) → DeviceMemory[]`, `DeviceBuffer.read(ByteBuffer)`,
plus `JPEG_COMPRESS_*` options). That is how PMCADemo/PMCA-HDMICam put the
preview on screen, and it is enough for an app to encode frames and serve them
over WiFi.

Over USB it changes nothing: an app cannot add a gadget function or answer PTP,
so the port stays stills-only.

Details, class/method table and a code sketch: [docs/android-api.md](docs/android-api.md).

## Existing software, tested

| Project | Result on ILCE-5000 |
|---|---|
| libgphoto2 2.5.34 (`gphoto2`) | detects the body, `capture()` fails after ~36 s |
| pysonycam (olkham) | rejects auth: wants SDIO protocol 3.0 (`0x12c`), camera reports `0xc8` |
| Sony Camera Remote SDK / Command | current models only |
| Sony-PMCA-RE / PMCA-RE RELOADED | apps/settings/firmware, not capture |

libgphoto2's Sony capture sequence is the same one reimplemented here; the
parts that differ are the ones that fail on this camera (property reads,
`GetObject`, one long-lived session).

## Tools

| File | Purpose |
|---|---|
| `tools/sony_usb.py` | USB still capture (verified), multi-shot, half-press |
| `tools/sony_wifi.py` | WiFi liveview (MJPEG/ffmpeg), stills, subnet scan, info |
| `tools/mock_camera.py` | protocol-faithful fake camera for testing the WiFi client |
| `tools/ptp_probe.py` | PTP `GetDeviceInfo` dumper (uses `pyusb`) |
| `tools/ptp_sdio.py` | Sony SDIO capability queries |

```bash
python3 -m pip install pyusb

# USB (camera: Menu → Setup → USB Connection → PC Remote)
python3 tools/sony_usb.py info
python3 tools/sony_usb.py capture --out shot.jpg --focus
python3 tools/sony_usb.py capture --out shot.jpg --count 5 --interval 2
```

USB node permissions:

```bash
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="054c", MODE="0666"' \
  | sudo tee /etc/udev/rules.d/90-sony-camera.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

## Reproducing the firmware analysis

See [docs/firmware.md](docs/firmware.md): official updater → carve the UFU
container → `fwtool.py unpack` (crypter `CXD90014`) → grep. No Sony binaries
or keys are distributed here; only the procedure and the findings.

## Notes / caveats

* This camera has been superseded by later bodies that do UVC or PC-Remote
  liveview; nothing here changes what the a5000 itself implements.
* Movie recording through the remote app is disabled for this model in
  `SmartRemote`'s `ModelInfo.isMovieRecSupported()`; liveview and stills are
  unaffected. Renaming the model ID in settings memory only lifts that
  app-level gate.
* Legal: reverse engineering for interoperability, on hardware we own. Sony
  firmware remains Sony's property.

## Session archive

The full working session that produced this repository — 720 transcript
entries, every tool/background-job log untruncated, plus a standalone
`viewer.html`:

**[session-2026-11-06.zip](https://github.com/alosslessdev/ilce-5000-research/releases/download/session-2026-11-06/ilce-5000-research-session.zip)**

Exported with the omp `export_session_archive` plugin. Camera serial number
and LAN MAC addresses are redacted; nothing else was altered.

## License

MIT — see [LICENSE](LICENSE).
