#!/usr/bin/env bash
# Builds "PV Monitor.app" for Apple Silicon (arm64 only) into macos-app/build/.
#
#   macos-app/build.sh            build
#   macos-app/build.sh --install  build and copy to /Applications
#
# The app is signed ad hoc (no developer account): fine for running on this Mac. If it is copied to
# another Mac and macOS complains, right-click > Open once.
set -euo pipefail
cd "$(dirname "$0")"

app="build/PV Monitor.app"
swift build -c release --arch arm64
bin="$(swift build -c release --arch arm64 --show-bin-path)/PVMonitor"

rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$bin" "$app/Contents/MacOS/PVMonitor"

cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>PVMonitor</string>
  <key>CFBundleIdentifier</key><string>net.ersatzworld.pv-monitor</string>
  <key>CFBundleName</key><string>PV Monitor</string>
  <key>CFBundleDisplayName</key><string>PV Monitor</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSArchitecturePriority</key><array><string>arm64</string></array>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# App icon: the sun from the home-screen app, scaled to the sizes an .icns needs.
tmp="$(mktemp -d)"
python3 - "$tmp/icon-1024.png" <<'PY'
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location("icons", pathlib.Path("../scripts/make-pwa-icons.py"))
icons = importlib.util.module_from_spec(spec); spec.loader.exec_module(icons)
icons.write_png(pathlib.Path(sys.argv[1]), 1024, 0.8)
PY
iconset="$tmp/AppIcon.iconset"; mkdir "$iconset"
for size in 16 32 128 256 512; do
  sips -z $size $size "$tmp/icon-1024.png" --out "$iconset/icon_${size}x${size}.png" >/dev/null
  sips -z $((size*2)) $((size*2)) "$tmp/icon-1024.png" --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$iconset" -o "$app/Contents/Resources/AppIcon.icns"
rm -rf "$tmp"

codesign --force --sign - "$app"
echo "Built $app"
lipo -archs "$app/Contents/MacOS/PVMonitor" | sed 's/^/Architectures: /'

if [ "${1:-}" = "--install" ]; then
  rm -rf "/Applications/PV Monitor.app"
  cp -R "$app" /Applications/
  echo "Installed to /Applications/PV Monitor.app"
fi
