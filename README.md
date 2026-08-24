# rbserver

Deploy and control [WebDriverAgent](https://github.com/appium/WebDriverAgent) on a **jailbroken** iOS device entirely over SSH — no Xcode team/provisioning, no USB, no `tidevice`/DTX/`testmanagerd` handshake at runtime.

## How this works

WDA's `-Runner` app is an XCTest "runner" host. Normally, launching one requires Apple's private instruments/`testmanagerd` protocol (what Appium's own `xcodebuild`-driven flow, and `tidevice`, use), because a regular device can't fork/exec an arbitrary signed app outside that path.

A jailbroken device's root SSH access bypasses that launch confinement entirely: as long as every Mach-O in the bundle carries at least an ad-hoc signature (dyld refuses to map a *completely* unsigned binary even with AMFI patched), the `-Runner` binary can be exec'd directly from a shell with the same environment variables + `XCTestConfiguration` file Apple's own tooling would set up. XCTest's bootstrap runs the WDA test method and starts its HTTP server on its own — no IDE/`testmanagerd` session needs to exist for that part, as long as the config's `reportResultsToIDE` is `False`.

## Requirements

- **A Mac with Xcode** — only needed to *build* WDA once (`xcodebuild` + `ldid` for ad-hoc signing). Not needed afterward; deployment and control run entirely from any machine (Linux, macOS, Windows/WSL) over SSH.
  - [Appium](https://appium.io/) with the XCUITest driver installed (`appium driver install xcuitest`) — `build_wda.sh` builds from the WebDriverAgent source bundled inside `appium-xcuitest-driver`.
  - [`ldid`](https://github.com/ProcursusTeam/ldid) (`brew install ldid`)
- **A jailbroken iOS device** (rootless, e.g. palera1n/Dopamine on a procursus filesystem) with:
  - OpenSSH installed and running (via Sileo/your package manager)
  - Root/mobile SSH access
- **Python 3** on whatever machine you'll run `wda_ctl.py` from.

## One-time device setup

Two things need to exist on the device before WDA can boot, and both are one-time, regardless of how many times you redeploy afterward:

1. **A Developer Disk Image mounted at `/Developer`.** On iOS ≤16, `testmanagerd` (which XCTest's bootstrap unconditionally looks up, even in this SSH-only flow) physically lives *inside* this disk image — it isn't part of the base OS. Mount it once via [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3), which fetches a matching image from a public, no-auth mirror:

   ```sh
   pip install pymobiledevice3
   # connect the device via USB once for this step
   python3 -m pymobiledevice3 mounter auto-mount
   ```

   This requires the device to trust this computer over USB (a one-time "Trust This Computer?" tap — no Apple ID, no Xcode). After this, the mount persists and no cable is needed for anything else in this project.

2. **Developer Mode enabled** (Settings → Privacy & Security → Developer Mode, if present). If the toggle isn't showing yet, it can appear only after the first attempt to run a developer-signed binary; this isn't always required depending on jailbreak/AMFI patch state, but check it if `start` fails immediately.

## Install

```sh
git clone <this repo>
cd rbserver
pip install -r requirements.txt

# on a Mac, with Appium's xcuitest driver installed:
./scripts/build_wda.sh
```

`build_wda.sh` builds the WebDriverAgent runner unsigned, renames it to `rbserver.app`/`rbserver.xctest`, patches in stub Swift-Testing dependencies missing on older iOS targets, writes ad-hoc entitlements, and `ldid`-signs every Mach-O in the bundle. Safe to re-run — it wipes and rebuilds every time.

### Alternative: install as a Sileo `.deb` (no host machine needed afterward)

Prebuilt `.deb`s are published under [Releases](https://github.com/techport-dev/rbserver/releases) — download directly on the device (Safari → the `.deb` link) and tap it, or over SSH:

```sh
curl -LO https://github.com/techport-dev/rbserver/releases/download/v1.0.0/rbserver_1.0.0_iphoneos-arm64.deb
sudo dpkg -i rbserver_1.0.0_iphoneos-arm64.deb
```

To build your own instead (e.g. after making changes), package a fresh build:

```sh
./scripts/build_deb.sh          # -> build/rbserver_1.0.0_iphoneos-arm64.deb
```

Either way, this packages the app plus an on-device `rbserver` CLI (`start`/`stop`/`status`/`logs`) installed to `/var/jb/usr/local/bin/rbserver`.

After that, everything runs **on-device**, no host machine needed:

```sh
rbserver start
rbserver status
rbserver stop
rbserver logs
```

This doesn't replace `wda_ctl.py` — `install-daemon`/`uninstall-daemon` (background persistence) are still host-orchestrated only, since they need `sudo` handling `wda_ctl.py` already does. The `.deb` is for the common case: get WDA running on-device with nothing but Sileo and a terminal app.

## Usage

All commands read the SSH password from `WDA_SSH_PASSWORD`, or prompt for it.

```sh
# upload the built app bundle + XCTestConfiguration to the device
WDA_SSH_PASSWORD=xxxx python3 scripts/wda_ctl.py deploy --host <device-ip>

# launch it (kills any previous instance first)
WDA_SSH_PASSWORD=xxxx python3 scripts/wda_ctl.py start --host <device-ip>

# check it's alive
python3 scripts/wda_ctl.py status --host <device-ip>

# tail launch logs (both the foreground `start` log and the daemon log)
python3 scripts/wda_ctl.py logs --host <device-ip>

# kill it
python3 scripts/wda_ctl.py stop --host <device-ip>

# install as a LaunchDaemon so it survives reboots / auto-restarts on crash
WDA_SSH_PASSWORD=xxxx python3 scripts/wda_ctl.py install-daemon --host <device-ip>
python3 scripts/wda_ctl.py uninstall-daemon --host <device-ip>
```

Useful flags: `--username` (default `mobile`), `--port` (default `8100`), `--remote-dir` (default `/var/mobile/rbserver`), `--local-app` (default the build output path), `--sudo-password` (for `install-daemon`/`uninstall-daemon`, usually the same as the SSH password).

> **Note on `install-daemon`:** it registers WDA as a real LaunchDaemon (persists across reboots, auto-restarts on crash). On memory-constrained devices (e.g. older iPhones with 2GB RAM) running several other apps/tweaks at once, iOS's jetsam OOM-killer can evict a backgrounded WDA almost immediately regardless of plist tuning — this is a device memory constraint, not a bug in this tool. If `install-daemon` doesn't stick, just use `start` on demand instead; it runs as a foreground SSH-spawned process, which isn't subject to the same background jetsam eviction, and comes up in ~2 seconds.

## Connecting a WebDriver client

### Appium Inspector, direct (no Appium server needed)

WDA speaks the WebDriver protocol itself. Point Inspector straight at it:

- Remote Host: `<device-ip>`, Remote Port: `8100`, Remote Path: empty
- Capabilities: `{"platformName": "iOS"}`

### Through a real Appium server

Useful if you also want Appium's own extension commands (`getSettings`, live MJPEG preview, etc.) to work cleanly, since WDA's raw session doesn't advertise itself as an Appium session and some Inspector features silently no-op against it directly.

```sh
npm install -g appium
appium driver install xcuitest
appium server -p 4723 --allow-cors   # --allow-cors only needed for the browser-hosted Inspector
```

Then connect with:

```json
{
  "platformName": "iOS",
  "appium:automationName": "XCUITest",
  "appium:udid": "<device-udid>",
  "appium:webDriverAgentUrl": "http://<device-ip>:8100",
  "appium:skipWDAInstall": true,
  "appium:usePrebuiltWDA": true,
  "appium:settings[screenshotQuality]": 2
}
```

`webDriverAgentUrl` + `skipWDAInstall` tell Appium to use the already-running WDA instead of trying to build/launch its own over USB/DTX.

> **`screenshotQuality`**: on some very new SDK builds, WDA's default screenshot path (`screenshotQuality: 0`) emits HEIC-encoded bytes labeled as PNG, which no browser/Inspector `<img>` tag can render, leaving the screen preview blank even though the underlying call succeeds (200 OK, correct payload size). Setting `screenshotQuality: 2` forces a code path that returns real PNG bytes. This is a WDA session setting, so it also works talking to WDA directly (no Appium server) via `POST /session/:id/appium/settings`.

### Appium Python client example

```python
# pip install Appium-Python-Client
from appium import webdriver
from appium.options.common.base import AppiumOptions

options = AppiumOptions()
options.load_capabilities({
    "platformName": "iOS",
    "appium:automationName": "XCUITest",
    "appium:udid": "<device-udid>",
    "appium:webDriverAgentUrl": "http://<device-ip>:8100",
    "appium:skipWDAInstall": True,
    "appium:usePrebuiltWDA": True,
    "appium:settings[screenshotQuality]": 2,
})

driver = webdriver.Remote("http://127.0.0.1:4723", options=options)
print(driver.page_source)
driver.quit()
```

Note the Appium server URL (`127.0.0.1:4723`) and the device's WDA URL (`appium:webDriverAgentUrl`) are two different things — don't point `webdriver.Remote()` at WDA's own port.

## Troubleshooting

- **`Failed to initiate daemon session: ... com.apple.testmanagerd ... No such process`** — the Developer Disk Image isn't mounted. See [One-time device setup](#one-time-device-setup).
- **`Failure: Unrecognized argument`** at launch — some XCTest/SDK builds reject extra Cocoa launch flags. `wda_ctl.py` no longer passes any; if you're running an older copy, update it.
- **`start` works but `install-daemon` doesn't** — see the jetsam note under [Usage](#usage).
- **Procursus shells have no `pgrep`/`pkill`** — `wda_ctl.py` uses `ps` + `awk` + `kill` instead; keep this in mind if you're editing the kill logic.

## License

See [LICENSE](LICENSE).
