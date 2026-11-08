# Android userland: liveview access for PMCA apps

The camera's userland is Android 2.3.7 (SDK 10) with Sony's "Scalar" framework.
PMCA apps are ordinary APKs against that framework, and the framework *does*
expose the liveview pipeline — so an app can read preview frames itself. What
it cannot do is put them on the USB wire.

All classes below were read out of the firmware:
`0700_part_image/dev/nflasha16_unpacked_unpacked/framework/sony.cameraex.odex`
(the embedded dex is parsed after the `dey\n100\0` odex header).

## API surface

| Class | Members relevant to liveview |
|---|---|
| `com.sony.scalar.hardware.CameraEx` | `static CameraEx open(int, OpenOptions)`, `getNormalCamera()`, `setPreviewStartListener`, `setPreviewAnalizeListener`, `setFaceDetectionListener`, `getPreviewDisplayColor(int,int)`, `setPreviewMagnification`, `stopPreviewMagnification` |
| `CameraEx$OpenOptions` | `setPreview(boolean)`, `setRecordingMode(int)`, `setTargetMedia(String)`, `setInheritSetting(boolean)` |
| `CameraEx$JpegListener` | `onPictureTaken(byte[] jpeg, CameraEx)` |
| `com.sony.scalar.hardware.CameraSequence` | `static open(CameraEx)`, `startPreviewSequence(Options)`, `updatePreviewSequence(Options)`, `stopPreviewSequence()`, `getPreviewSequenceFrames(int) → DeviceMemory[]`, `getPreviewSequenceFrameInfo()`, `getPreviewPluginResult()`, `setPreviewPlugin(DSP)`, `setOneShotPreviewPluginCallback(...)`, `release()` |
| `CameraSequence$Options` | `PREVIEW_FRAME_WIDTH`, `PREVIEW_FRAME_HEIGHT`, `PREVIEW_FRAME_FORMAT`, `PREVIEW_FRAME_RATE`, `PREVIEW_FRAME_MAX_NUM`, `PREVIEW_FRAME_INFO_VERSION`, `JPEG_COMPRESS_MAX_SIZE`, `JPEG_COMPRESS_RATE_DENOM`, `JPEG_COMPRESS_QUALITY_PARAM_1..8`, `PREVIEW_PLUGIN_OUTPUT_ENABLED`, `PREVIEW_PLUGIN_NOTIFY_MASK`, `MEMORY_MAP_FILE`, `EXPOSURE_COUNT`, `RECORD_COUNT` |
| `com.sony.scalar.hardware.DeviceMemory` | `isValid()`, `release()` |
| `com.sony.scalar.hardware.DeviceBuffer` (extends `DeviceMemory`) | `read(ByteBuffer)`, `read(ByteBuffer,int,int)`, `write(...)` |
| `CameraSequence$PreviewSequenceFrameInfo` | `public byte[] detailData` |
| `CameraSequence$PreviewPluginResult` | `public byte[] analyzedData` |

Two ways in, both used by shipping community apps:

1. **Surface path** — what PMCADemo does:

   ```java
   CameraEx cam = CameraEx.open(0, opts);            // opts.setPreview(true)
   cam.getNormalCamera().setPreviewDisplay(surfaceHolder);
   cam.getNormalCamera().startPreview();
   ```

   The app owns the surface, so it can read it back (GL `glReadPixels`, or
   render into a `Bitmap`-backed canvas) and encode.

2. **Frame path** — `CameraSequence`:

   ```java
   CameraEx cam = CameraEx.open(0, opts);
   CameraSequence seq = CameraSequence.open(cam);
   CameraSequence.Options o = new CameraSequence.Options();
   o.setOption(CameraSequence.Options.PREVIEW_FRAME_WIDTH, 640);
   o.setOption(CameraSequence.Options.PREVIEW_FRAME_HEIGHT, 360);
   o.setOption(CameraSequence.Options.PREVIEW_FRAME_RATE, 15);
   o.setOption(CameraSequence.Options.PREVIEW_FRAME_MAX_NUM, 2);
   seq.startPreviewSequence(o);
   DeviceMemory[] frames = seq.getPreviewSequenceFrames(1);
   // DeviceBuffer.read(ByteBuffer) copies the pixels out of device memory
   ```

   The option values' exact encodings (format strings, quality parameters) are
   not in the SDK stubs; they come from the DSP/preview-plugin layer and would
   need a session of trial on hardware. The `JPEG_COMPRESS_*` options show the
   pipeline can emit JPEG for the plugin path (`getPreviewPluginResult()`).

## What this means for streaming

**WiFi: feasible from an app.** The app gets preview frames, JPEG-encodes them
(Android 2.3 has `Bitmap.compress`), and serves them over a socket — MJPEG or
anything else the host understands. Precedent in the ecosystem:
`Novex/pmca-livestream` (RTMP from action cameras), the AS100's preinstalled
`ustream.apk` that shipped as a PMCA, `dired/PMCA-HDMICam` (preview to HDMI).
Constraints: preview resolution is the camera's preview pipeline (LCD-class,
640×360-ish on this body), and the app has to get Wi-Fi up itself
(`com.sony.scalar.sysnetutil.ScalarWifiInfo` / OpenMemories wrappers).

**USB: not possible from an app.** The USB device controller is owned by the
native OS: `usbg_stillimage`, `usbg_storage`, `usbg_sen`, `usbg_buspower`, with
no UVC function (see [firmware.md](firmware.md)). An Android app cannot add a
gadget function or answer PTP; the host sees one PTP interface and a fixed
16-operation surface. The most an app can do on USB is push bytes into a file
that the host later pulls over PTP/MTP — seconds per frame, not a stream.

So: app-side liveview streaming belongs on WiFi; the USB port stays a still
camera interface (and HDMI stays the only real video output).

## Practical notes

* PMCA apps target Android 2.3.3/2.3.7 (API 10), like `PMCA-HDMICam` and
  `PMCADemo`; the framework classes above are system APIs, not public SDK.
* `CameraSequence` is reference-counted per `CameraEx`; release it before
  closing the camera, and expect the camera to stay in preview mode while open
  (the LCD/EVF is driven by the same pipeline).
* Everything here is read from the shipped dex; nothing was executed on the
  camera. A proof-of-concept app is the obvious next step.
