#!/bin/zsh
# Builds + patches + ad-hoc-signs WebDriverAgentRunner for jailbroken SSH deploy.
# No Apple team/provisioning involved anywhere in this script -- CODE_SIGNING_ALLOWED=NO
# during build, then `ldid` ad-hoc-signs the result. Safe to re-run; wipes and rebuilds
# every time (fast enough, and avoids any stale-signature edge cases).
set -e

WDA_PROJ="$HOME/.appium/node_modules/appium-xcuitest-driver/node_modules/appium-webdriveragent"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$ROOT/build/wda"
APP="$BUILD_DIR/Build/Products/Debug-iphoneos/WebDriverAgentRunner-Runner.app"
BUNDLE_ID="com.rbserver.WebDriverAgentRunner"
ENTITLEMENTS="$ROOT/build/entitlements.plist"

echo "[build] Removing previous build dir..."
rm -rf "$BUILD_DIR"

echo "[build] xcodebuild build-for-testing (no signing, ~30-60s)..."
xcodebuild build-for-testing \
  -project "$WDA_PROJ/WebDriverAgent.xcodeproj" \
  -scheme WebDriverAgentRunner \
  -destination "generic/platform=iOS" \
  -derivedDataPath "$BUILD_DIR" \
  IPHONEOS_DEPLOYMENT_TARGET=16.7 \
  PRODUCT_BUNDLE_IDENTIFIER="$BUNDLE_ID" \
  CODE_SIGNING_ALLOWED=NO \
  CODE_SIGNING_REQUIRED=NO \
  GCC_TREAT_WARNINGS_AS_ERRORS=0 \
  | tail -20

if [ ! -d "$APP" ]; then
  echo "[build] ERROR: app not found at $APP after build" >&2
  exit 1
fi

echo "[build] Patching missing Swift-Testing-family dependencies (not present on iOS 16)..."
FRAMEWORKS="$APP/Frameworks"
STUB_C="$BUILD_DIR/stub.c"
: > "$STUB_C"

xcrun -sdk iphoneos clang -arch arm64 -dynamiclib -miphoneos-version-min=15.0 \
  -o "$FRAMEWORKS/lib_TestingInterop.dylib" "$STUB_C" \
  -install_name "@rpath/lib_TestingInterop.dylib"

TF_DIR="$FRAMEWORKS/_Testing_Foundation.framework"
mkdir -p "$TF_DIR"
xcrun -sdk iphoneos clang -arch arm64 -dynamiclib -miphoneos-version-min=15.0 \
  -o "$TF_DIR/_Testing_Foundation" "$STUB_C" \
  -install_name "@rpath/_Testing_Foundation.framework/_Testing_Foundation"

REAL_FRAMEWORK="/Applications/Xcode.app/Contents/Developer/Platforms/iPhoneOS.platform/Developer/Library/Frameworks/_Testing_Foundation.framework"
cp "$REAL_FRAMEWORK/Info.plist" "$TF_DIR/Info.plist"
[ -f "$REAL_FRAMEWORK/version.plist" ] && cp "$REAL_FRAMEWORK/version.plist" "$TF_DIR/version.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier com.rbserver.stub.TestingFoundation" "$TF_DIR/Info.plist"

echo "[build] Writing ad-hoc entitlements ($ENTITLEMENTS)..."
cat > "$ENTITLEMENTS" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "https://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>application-identifier</key>
	<string>RBSERVER.$BUNDLE_ID.xctrunner</string>
	<key>com.apple.developer.team-identifier</key>
	<string>RBSERVER</string>
	<key>get-task-allow</key>
	<true/>
	<key>keychain-access-groups</key>
	<array>
		<string>RBSERVER.$BUNDLE_ID.xctrunner</string>
	</array>
	<key>com.apple.private.security.no-sandbox</key>
	<true/>
	<key>com.apple.private.skip-library-validation</key>
	<true/>
	<key>platform-application</key>
	<true/>
	<key>com.apple.security.get-task-allow</key>
	<true/>
</dict>
</plist>
PLIST

echo "[build] ldid-signing (ad-hoc, entitled)..."
# ldid signs one Mach-O binary at a time -- it doesn't take bundle directories.
# Every .framework/.dylib ANYWHERE in the app (including nested inside the
# .xctest plugin's own Frameworks dir -- e.g. WebDriverAgentLib.framework,
# which holds all of WDA's actual HTTP-server/automation code) must carry at
# least an ad-hoc signature or dyld refuses to map it at load time with
# "mapped file has no cdhash, completely unsigned" -- confirmed live even
# under a jailbreak's patched AMFI.
LDID=/opt/homebrew/bin/ldid

find "$APP" -name "*.framework" -type d | while read -r fw; do
  name="$(basename "$fw" .framework)"
  bin="$fw/$name"
  [ -f "$bin" ] && "$LDID" -S "$bin"
done
find "$APP" -name "*.dylib" -type f | while read -r dylib; do
  "$LDID" -S "$dylib"
done

XCTEST_BUNDLE="$APP/PlugIns/WebDriverAgentRunner.xctest"
"$LDID" -S "$XCTEST_BUNDLE/WebDriverAgentRunner"

"$LDID" -S"$ENTITLEMENTS" "$APP/WebDriverAgentRunner-Runner"

echo "[build] Done. App at: $APP"
