# RF Attenuation Sweep — Setup & Usage

Automated RF link quality sweep for DJI Matrice / OcuSync drones.
Controls a Mini-Circuits programmable attenuator to sweep signal strength,
collects live metrics from the DJI SDK via ADB logcat, and produces an
interactive HTML report.

---

## DJI API Key

The API key is already included in `gradle.properties` and is tied to the app package name
(`com.jose.dronetest`). As long as you keep that package name, no changes are needed —
the key works on any machine without any login.

If you rename the package (change `applicationId` in `build.gradle`), you'll need a new key:
1. Register the new package name at the [DJI Developer Portal](https://developer.dji.com)
2. Paste the generated key into `SampleCode-V5/android-sdk-v5-as/gradle.properties` as `AIRCRAFT_API_KEY`
3. Full setup reference: [DJI Mobile SDK V5 Docs](https://developer.dji.com/doc/mobile-sdk-tutorial/en/quick-start/run-sample-code.html)

---

## Hardware

| Item | Details |
|------|---------|
| Attenuator | Mini-Circuits RC4DAT-8G-120H (USB, 4-channel, 0–120 dB) |
| Connection | USB to the laptop running the sweep script |
| Drone | DJI Matrice series (tested on OcuSync / O3) |
| Controller | Connected via USB to the Android device running the test APK |
| Android device | Must have ADB enabled (Developer Options → USB Debugging) |

The attenuator sits in-line between the drone antenna and the controller
antenna. Both the up-sweep (increasing attenuation = weaker signal) and
down-sweep (decreasing attenuation = recovering signal) are recorded.

---

## File Locations

All paths are relative to the repo root (`Mobile-SDK-Android-V5/`).

| File | Path | Purpose |
|------|------|---------|
| Sweep script | `sweep.py` | Main sweep runner + plotting |
| Attenuator driver | `mc_atten.py` | Mini-Circuits USB control via libusb |
| Android app source | `SampleCode-V5/android-sdk-v5-sample/src/main/java/dji/sampleV5/aircraft/` | Modified DJI sample APK |
| Key files modified | `DJIAircraftApplication.kt`, `ChunkLogger.kt` | Metric polling + chunk logging |
| Sweep config | Top of `sweep.py` | Attenuation range, timing, TX power |
| Sweep output (CSV + HTML) | `logs/` | Created at runtime, not committed |

---

## Android APK — What Was Modified

### `DJIAircraftApplication.kt`
Starts two background pollers after a 3-second SDK init delay:

**`startFastBitratePoll()` — every 200 ms**
Polls the following `AirLinkKey` values and logs them to logcat:
- `KeyDynamicDataRate` → tag `DATA`, line `Bitrate: X.X Mbps`
- `KeyFrequencyPoint` → tag `DATA`, line `Frequency Point: XXXX`
- `KeyBandwidth` → tag `DATA`, line `Bandwidth: BANDWIDTH_20MHZ`

**`startPeriodicPolling()` — every 1 s (WLM block)**
Polls and logs to tag `WLM`:
- `KeyWlmLinkQualityLevel` → JSON link quality object
- `KeyVideoDataRate` → `VideoDataRate: X.X Mbps` (essentially same as KeyDynamicDataRate)
- `KeySDRCurrentDataRate`, `KeySDRAveragePower`, `KeySDRQualityDetect`, `KeySDRDistanceLossReason`

Also locks the drone to **5.8 GHz / 5825 MHz / 20 MHz bandwidth** on startup
and on every AirLink reconnect via `applyFrequencyConfig()`.

### `ChunkLogger.kt` (new file)
Separate Kotlin `object` that registers with
`MediaDataCenter.getInstance().cameraStreamManager.addReceiveStreamListener()`
to receive raw encoded H.264/H.265 video chunks directly from the RF link.

Logs one summary line per second to tag `CHUNK`:
```
chunks=30 totalBytes=91968 avg=3065B min=2048B max=13344B keyframes=1 dropped=0 fps=30 res=1920x1080 mime=H264
```

Fields:
- `chunks` — callbacks received in the last second (should ≈ fps)
- `totalBytes` — total encoded bytes received (payload only, no FEC)
- `avg/min/max` — per-chunk byte sizes
- `keyframes` — I-frames in the window (typically 1/sec)
- `dropped` — frames inferred dropped via PTS gap detection (gap > 1.5× frame duration)
- `fps` / `res` / `mime` — stream metadata from `StreamInfo`

If no data arrives for 2+ consecutive seconds (link drop), the listener
automatically re-registers to recover when the link comes back.

> **Note:** The chunk-derived bitrate (~0.7 Mbps for a static scene) represents
> the actual H.264 payload bytes the app receives. This is much lower than
> `KeyDynamicDataRate` (~24 Mbps) because the RF link includes heavy FEC
> overhead, and H.264 VBR produces very small P-frames for static content.
> Expect 3–8 Mbps when the drone is flying with camera movement.

---

## Building the APK

**Option A — Android Studio (easiest)**

Open `SampleCode-V5/android-sdk-v5-as/` as a project in Android Studio, then:
`Build → Generate App Bundles or APKs → Generate APKs`

The APK will be written to:
`SampleCode-V5/android-sdk-v5-sample/build/outputs/apk/debug/sample-debug.apk`

**Option B — Command line**

From the repo root:
```bash
cd SampleCode-V5/android-sdk-v5-as
JAVA_HOME=/path/to/jdk ./gradlew :sample:assembleDebug
```

> `JAVA_HOME` must point to a JDK 17+. On the original test machine this was
> `/opt/android-studio/jbr`. On a new machine use `which java` or check your
> Android Studio installation to find the right path.

**Install to connected device:**
```bash
adb install -r SampleCode-V5/android-sdk-v5-sample/build/outputs/apk/debug/sample-debug.apk
```

---

## Running a Sweep

### 1. Verify device is connected
```bash
adb devices
```

### 2. Verify attenuator is reachable
```bash
sudo python3 mc_atten.py
# Should print current attenuation on all 4 channels
```

### 3. Verify logcat is streaming metrics
```bash
adb logcat DATA:D CHUNK:D WLM:D *:S -v raw
```
You should see `Bitrate:` lines every 200 ms and `chunks=` lines every second
once the drone is connected.

### 4. Configure sweep parameters (edit top of `sweep.py`)
```python
ATTEN_START  = 45     # dB — starting attenuation (strong signal)
ATTEN_END    = 72     # dB — ending attenuation (weak/no signal)
ATTEN_STEP   = 1      # dB — step size
SETTLE_TIME  = 3.0    # seconds — wait at each step before recording
MEASURE_TIME = 3.0    # seconds — recording window at each step
ONE_WAY      = False  # False = up then back down; True = up only

TX_POWER_DBM = 21.0   # dBm — transmit power of the controller antenna
PATH_LOSS_DB = 57.0   # dB — estimated cable/connector losses
# RSSI = TX_POWER_DBM - PATH_LOSS_DB - attenuation_dB
```

### 5. Run the sweep

From the repo root:
```bash
python3 sweep.py
```

The script will:
1. Clear stale logcat
2. Start the attenuator at `ATTEN_START`
3. Step through each attenuation value, settle, then record
4. Do the return sweep (unless `ONE_WAY = True`)
5. Write a timestamped CSV to `logs/` in the repo root
6. Open an interactive HTML report in your browser

### 6. Re-plot from a saved CSV
```bash
python3 sweep.py logs/your_sweep.csv
```

---

## HTML Report — Tabs

| Tab | Content |
|-----|---------|
| Bitrate vs Time | Raw `KeyDynamicDataRate` over the entire sweep timeline |
| Frequency vs Time | OcuSync frequency point over time |
| Bandwidth vs Time | Channel bandwidth (e.g. 20 MHz) over time |
| Median Bitrates | Dynamic Mbps + Chunk-derived Mbps vs RSSI (up & down) |
| Median Freq & BW | Frequency and bandwidth medians vs RSSI |
| Chunk Stats | Avg chunk size, chunk Mbps vs dynamic Mbps, keyframes/sec, dropped frames/sec |
| Bitrate Comparison | Table: Dynamic vs Chunk Mbps for each attenuation step, both sweeps |

---

## Logcat Tags Reference

| Tag | Source | Content |
|-----|--------|---------|
| `DATA` | `DJIAircraftApplication.startFastBitratePoll()` | `Bitrate:`, `VideoFeedBW:`, `Frequency Point:`, `Bandwidth:` |
| `WLM` | `DJIAircraftApplication.startPeriodicPolling()` | `WlmLinkQualityLevel:` JSON, `VideoDataRate:` |
| `CHUNK` | `ChunkLogger` | Per-second video chunk summary |
| `FREQ_CONTROL` | `DJIAircraftApplication` | Frequency/bandwidth lock status |
| `FC` | `DJIAircraftApplication` | Flight controller connection state |

Monitor all sweep-relevant tags:
```bash
adb logcat DATA:D CHUNK:D WLM:D *:S -v raw
```

---

## Key SDK APIs Used

| API | Purpose |
|-----|---------|
| `KeyManager.getInstance().getValue(KeyTools.createKey(AirLinkKey.KeyDynamicDataRate))` | Total RF link data rate (Mbps) |
| `KeyManager.getInstance().getValue(KeyTools.createKey(AirLinkKey.KeyFrequencyPoint))` | Current OcuSync frequency (MHz) |
| `KeyManager.getInstance().getValue(KeyTools.createKey(AirLinkKey.KeyBandwidth))` | Channel bandwidth |
| `MediaDataCenter.getInstance().cameraStreamManager.addReceiveStreamListener()` | Raw H.264/H.265 encoded video chunks |
| `ICameraStreamManager.StreamInfo` | Per-chunk metadata: width, height, frameRate, isKeyFrame, presentationTimeMs, mimeType |

---

## Dependencies

Python (sweep script):
```
plotly
```
Install: `pip install plotly`

Android build:
- JDK 17+ (Android Studio's bundled JBR works; set `JAVA_HOME` to its path)
- DJI Mobile SDK V5 (5.18.0) — already in project dependencies

Attenuator driver:
- `libusb-1.0` system library (`sudo apt install libusb-1.0-0`)
- Requires `sudo` for USB access (or configure udev rules for the Mini-Circuits VID `0x20CE`)
