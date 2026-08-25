#!/bin/sh
# Packages the already-built rbserver.app (see build_wda.sh) as a Sileo/Cydia
# .deb: the app + a pre-generated XCTestConfiguration under /var/mobile/rbserver,
# plus an on-device `rbserver` CLI (start/stop/status/logs) at
# /var/jb/usr/local/bin/rbserver so day-to-day use needs no host machine at all.
#
# Runs anywhere with python3 + dpkg-deb (Linux or macOS) -- no Xcode needed,
# it only repackages what build_wda.sh already produced.
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/build/wda/Build/Products/Debug-iphoneos/rbserver.app"
VERSION="${1:-1.0.0}"
STAGE="$ROOT/build/deb"
OUT="$ROOT/build/rbserver_${VERSION}_iphoneos-arm64.deb"

if [ ! -d "$APP" ]; then
  echo "[build_deb] ERROR: $APP not found -- run scripts/build_wda.sh first" >&2
  exit 1
fi
if ! command -v dpkg-deb >/dev/null 2>&1; then
  echo "[build_deb] ERROR: dpkg-deb not found (apt install dpkg-dev, or brew install dpkg on macOS)" >&2
  exit 1
fi

echo "[build_deb] Staging package tree..."
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN"
mkdir -p "$STAGE/var/jb/Library/rbserver/payload"
mkdir -p "$STAGE/var/jb/usr/local/bin"

cp -R "$APP" "$STAGE/var/jb/Library/rbserver/payload/rbserver.app"

echo "[build_deb] Generating session.xctestconfiguration for the final install path..."
PYTHONPATH="$ROOT/scripts" python3 -c "
import wda_ctl
data = wda_ctl._build_xctestconfiguration('/var/mobile/rbserver/rbserver.app')
with open('$STAGE/var/jb/Library/rbserver/payload/rbserver.app/session.xctestconfiguration', 'wb') as f:
    f.write(data)
"

cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: rbserver
Name: rbserver
Version: $VERSION
Architecture: iphoneos-arm64
Maintainer: techport-dev
Author: techport-dev
Section: Development
Priority: optional
Depends: firmware (>= 16.0)
Description: Deploy and run WebDriverAgent entirely on-device over a jailbreak, no Xcode/USB needed after install. Installs the WDA runner app plus an \`rbserver\` CLI (start/stop/status/logs).
Homepage: https://github.com/techport-dev/rbserver
CONTROL

cat > "$STAGE/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e

PAYLOAD="/var/jb/Library/rbserver/payload/rbserver.app"
TARGET="/var/mobile/rbserver"

rm -rf "$TARGET/rbserver.app"
mkdir -p "$TARGET"
cp -R "$PAYLOAD" "$TARGET/rbserver.app"
rm -rf "$PAYLOAD"

chmod +x "$TARGET/rbserver.app/rbserver"
chmod +x /var/jb/usr/local/bin/rbserver
chown -R mobile:mobile "$TARGET"

echo "rbserver installed. Run: rbserver start"
exit 0
POSTINST

cat > "$STAGE/DEBIAN/prerm" <<'PRERM'
#!/bin/sh
set -e

# grep the full binary path, not the bare package/binary name -- this script's
# own dpkg-invoked path also contains "rbserver" and would otherwise match itself.
PIDS=$(ps ax -o pid,command | grep "/var/mobile/rbserver/rbserver.app/rbserver" | grep -v grep | awk '{print $1}')
if [ -n "$PIDS" ]; then
	kill -9 $PIDS 2>/dev/null || true
fi

exit 0
PRERM

cat > "$STAGE/var/jb/usr/local/bin/rbserver" <<'CLI'
#!/bin/zsh
# rbserver -- on-device control for the installed WebDriverAgent.
# Runs directly on the jailbroken device (no SSH-from-a-host-machine needed
# for day-to-day start/stop). For the reasoning behind this launch approach,
# see: https://github.com/techport-dev/rbserver
set -e

APP="/var/mobile/rbserver/rbserver.app"
PORT="${RBSERVER_PORT:-8100}"
LOG="/tmp/wda_launch.log"

# procursus's minimal shell has no pgrep/pkill by default -- ps+awk+kill always works.
# Grep the full binary path, not just "rbserver": this CLI script's own
# invocation ("/var/jb/usr/local/bin/rbserver start") also shows up in `ps`
# and also contains the literal substring "rbserver" -- a bare-name grep
# would match this script's own process and kill itself mid-run.
_kill_wda() {
	PIDS=$(ps ax -o pid,command | grep "$APP/rbserver" | grep -v grep | awk '{print $1}')
	if [ -n "$PIDS" ]; then
		kill -9 $PIDS
		echo "[rbserver] killed: $PIDS"
	else
		echo "[rbserver] not running"
	fi
}

_wait_for_status() {
	local timeout=20
	echo "[rbserver] waiting for http://127.0.0.1:$PORT/status (up to ${timeout}s) ..."
	for i in $(seq 1 $timeout); do
		sleep 1
		code=$(curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/status" || true)
		if [ "$code" = "200" ]; then
			echo "[rbserver] ready after ${i}s"
			return 0
		fi
	done
	return 1
}

cmd_start() {
	if [ ! -f "$APP/session.xctestconfiguration" ]; then
		echo "[rbserver] ERROR: $APP not found -- is the rbserver package installed?" >&2
		exit 1
	fi
	_kill_wda
	sleep 0.5
	echo "[rbserver] launching WDA on port $PORT ..."
	export CA_ASSERT_MAIN_THREAD_TRANSACTIONS="0"
	export CA_DEBUG_TRANSACTIONS="0"
	export DYLD_FRAMEWORK_PATH="$APP/Frameworks:"
	export DYLD_LIBRARY_PATH="$APP/Frameworks"
	export NSUnbufferedIO="YES"
	export SQLITE_ENABLE_THREAD_ASSERTIONS="1"
	export WDA_PRODUCT_BUNDLE_IDENTIFIER="com.rbserver.app"
	export XCTestBundlePath="$APP/PlugIns/rbserver.xctest"
	export XCTestConfigurationFilePath="$APP/session.xctestconfiguration"
	export XCODE_DBG_XPC_EXCLUSIONS="com.apple.dt.xctestSymbolicator"
	export MJPEG_SERVER_PORT=""
	export USE_PORT="$PORT"
	export OS_ACTIVITY_DT_MODE="YES"
	nohup "$APP/rbserver" > "$LOG" 2>&1 &
	disown
	if _wait_for_status; then
		echo "[rbserver] WDA is up: http://127.0.0.1:$PORT/status"
	else
		echo "[rbserver] did not come up -- tail of $LOG:" >&2
		tail -c 3000 "$LOG" >&2
		exit 1
	fi
}

cmd_stop() {
	_kill_wda
}

cmd_status() {
	curl -s -m 3 "http://127.0.0.1:$PORT/status" || {
		echo "[rbserver] no response -- not running or not reachable on port $PORT" >&2
		exit 1
	}
}

cmd_logs() {
	tail -c 6000 "$LOG" 2>&1
}

case "$1" in
	start)  cmd_start ;;
	stop)   cmd_stop ;;
	status) cmd_status ;;
	logs)   cmd_logs ;;
	*)
		echo "Usage: rbserver {start|stop|status|logs}"
		echo "  RBSERVER_PORT env var overrides the default port (8100)."
		exit 1
		;;
esac
CLI

chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm"
chmod +x "$STAGE/var/jb/usr/local/bin/rbserver"
chmod +x "$STAGE/var/jb/Library/rbserver/payload/rbserver.app/rbserver"

echo "[build_deb] Building $OUT ..."
dpkg-deb --root-owner-group -b "$STAGE" "$OUT"
echo "[build_deb] Done: $OUT"
