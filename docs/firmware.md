# Firmware analysis (ILCE-5000, v1.10)

Goal: decide from the firmware itself whether USB video exists, how the remote
API is wired, and which connectivity modes the body implements.

No Sony binaries, keys or dumps are distributed with this repository. The
procedure below is reproducible from Sony's public updater.

## 1. Get the official updater

`W0010069` on Sony's support site is *ILCE-5000 Camera Firmware Update*
(`Update_ILCE5000V110.exe`, 192 MB, released 2014-10-29). Sony's pages reject
non-browser clients with HTTP 403; the real CDN URL can be recovered from a
Wayback snapshot of that page, or used directly:

```
https://support.d-imaging.sony.co.jp/download/IMT/Update_ILCE5000V110.exe?fm=na
→ https://di.update.sony.net/IMT/Update_ILCE5000V110.exe
sha256 1c17535dd13ef777589805ae5a2edb6c45798d1c4f95d204e8b8d483f2780c8f
```

`fwtool.py`'s EXE unpacker rejects this installer (`Unknown exe file`); the
`FirmwareData_ILCE5000V110.dat` container is embedded uncompressed instead.

## 2. Carve the UFU container

The PE image ends at `0x6e800`; the payload starts there and the `.dat` begins
at the `\x89UFU\r\n\x1a\n` magic:

```python
data = open('Update_ILCE5000V110.exe','rb').read()
start = data.index(b'\x89UFU\r\n\x1a\n')        # 0x80242 for this file
open('FirmwareData_ILCE5000V110.dat','wb').write(data[start:])
```

Inside is the standard chunk list (`DATV`, `PROV`, `UDID`, `FDAT`). The
`FDAT` payload is AES encrypted.

## 3. Decrypt

```bash
git clone https://github.com/ma1co/fwtool.py && cd fwtool.py
pip install pycryptodomex pyyaml
python3 fwtool.py unpack -f ../FirmwareData_ILCE5000V110.dat -o ../out
# Decrypting firmware image → Extracting files
```

Result: `config.yaml` (crypter `CXD90014`, model `0x41030012`, version `1.10`),
`firmware.tar` (189 MB) with the partition images, `updater.img`.

CXD90014 is the generation Sony did not cryptographically sign for updates;
`fwtool` decrypts it offline. (Per-partition CRC32 manifests, `07xx_*_sum/*.sum`,
are checked by the updater body `uc_crc32sum.elf` at install time — integrity
bookkeeping, not a signature.)

## 4. Findings

### Android 2.3.7, not "Android 2.3.6"

`0700_part_image/dev/nflasha16_unpacked_unpacked/build.prop`:

```
ro.build.version.release=2.3.7
ro.build.version.sdk=10
ro.build.id=GWK74
ro.product.model=ScalarA
ro.product.device=dslr-diadem
```

### No video class, only PTP and mass storage

`.../nflasha15_unpacked_unpacked/kmod/` contains `usbg_stillimage.ko`,
`usbg_storage.ko`, `usbg_sen.ko`, `usbg_buspower.ko`, `usb_extcmd.ko`,
`usb_portMonitor.ko`. There is no `uvcvideo`, `g_webcam`, `videodev` or V4L2
module, and a case-insensitive scan of the whole decrypted tree returns zero
hits for `uvcvideo|usb_f_uvc|g_webcam|webcam|Video Class`.

### The liveview command set is compiled out of this model

`lib/libObj.so` is the object/scalar layer. Only the a5100 build contains
`CommandLiveViewStart / Stop / Change / PauseOn / PauseOff /
SetAvailableStatus / SetNotAvailableStatus`:

```
a5000 0x41030012 v1.10  15,614,064 B   CommandLiveView* = 0
a6000 0x41030035 v3.21  17,436,172 B   CommandLiveView* = 0
a5100 0x42030036 v3.10  16,354,724 B   CommandLiveView* = 7 (unique)
```

`libObj.so` also references an out-of-tree plugin, `modelUsbLiveView.so`, that
neither firmware ships. So "enable USB liveview with a flag" is not a thing on
this body — the handlers are absent.

### The remote API exists and is JSON-RPC over HTTP

`bin/orb-server` exports `service::Remote::v1_0::StartLiveview`,
`StopLiveview`, `GetEventLiveviewStatus`, `GetEventLiveviewOrientation` over
`libj` HTTP; `libInfraStreamingServer.so` holds the JPEG frame state
(`m_default_width`, `m_default_framerate`, `m_compressrate`).
It is launched on demand:

```
bin/WebApiLauncher.sh → orb-server -f /usr/share/network/remotecontrol -p 10000
```

and `lib/libIMDB.so` is what invokes the launcher.

### Connectivity modes

`bin/ramdisk.img_unpacked_unpacked/init.rc`:

```
setprop wifi.interface ath0
setprop wlan.com.sony.wifi.p2pdev_name ath1
setprop wlan.com.sony.wifi.p2pif_name ath1
setprop wlan.com.sony.wifi.dual_if 0
setprop wlan.com.sony.wifi.concurrent 0
setprop dhcp.com.sony.wifi.serv_address 192.168.122.1
setprop dhcp.com.sony.wifi.base_address 192.168.122.100
service dhcpcd_ath0 /system/bin/wlan_tool dhcpcd ath0
```

* station mode: `bin/wpa_supplicant`, `bin/wpa_cli`, `dhcpcd_ath0` — used by
  "Send to Computer" / DLNA;
* direct mode: `lib/libGOConnectionManager.so` (`StartGO`, `StopGO`,
  `GetSSID`, `GetKeyInfo`, `GetPinCode`) — used by the remote app;
* `dual_if 0` / `concurrent 0`: the radio cannot be a station and a group
  owner simultaneously, which is why the API is unreachable while the camera
  is joined to an access point.

### App gating (movie recording only)

`SmartRemote.apk` (system app, `nflasha16/app/`) resolves `isMovieRecSupported()`
against a hardcoded blocklist containing `ILCE-7`, `ILCE-7R`, `ILCE-5000`,
`ILCE-6000`. Renaming the model ID in settings memory makes the app offer
`movie` in `getAvailableShootMode()`, but the recording still goes to the SD
card; it is not a video stream.

## 5. Reference material

* [ma1co/fwtool.py](https://github.com/ma1co/fwtool.py) — decryption
* [ma1co/Sony-PMCA-RE](https://github.com/ma1co/Sony-PMCA-RE),
  [masterflai/pmca-re-reloaded](https://github.com/masterflai/pmca-re-reloaded)
* [Bloodevil/sony_camera_api](https://github.com/Bloodevil/sony_camera_api) —
  a5000 API method dump used for the WiFi client
* [gphoto/libgphoto2](https://github.com/gphoto/libgphoto2) — Sony
  `camera_sony_capture`, the sequence the USB tool reimplements
