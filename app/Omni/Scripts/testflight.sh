#!/bin/bash
# Archive the iOS app, export it for App Store Connect, and (with --upload) send it to
# TestFlight. Run on the laptop that holds the "Apple Distribution" identity.
#
#   Scripts/testflight.sh            archive + export → .build/testflight/export/Omni.ipa
#   Scripts/testflight.sh --upload   … then validate and upload it with altool
#
# The archive uses the project's own (development) signing; the export re-signs with the
# local Apple Distribution identity and the "Omni App Store" profile
# (Support/ExportOptions.plist), the same route as Prism's ios-release.sh.
# The Release configuration signs with aps-environment=production, so a TestFlight install
# registers for live push; Debug builds stay on the sandbox.
#
# Env (upload only): ASC_API_KEY_ID, ASC_API_ISSUER_ID, ASC_API_KEY_PATH — an App Store
# Connect API key (App Manager or higher). The .p8 stays outside the repository.
# The build number (CURRENT_PROJECT_VERSION) must be higher than every build already
# uploaded; App Store Connect refuses a repeat.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$HERE/.build/testflight"
UPLOAD=0
for a in "$@"; do
  case "$a" in
    --upload) UPLOAD=1 ;;
    *) echo "usage: $0 [--upload]" >&2; exit 2 ;;
  esac
done
fail() { echo "testflight: $*" >&2; exit 1; }

if [ "$UPLOAD" = 1 ]; then
  : "${ASC_API_KEY_ID:?set ASC_API_KEY_ID}" "${ASC_API_ISSUER_ID:?set ASC_API_ISSUER_ID}"
  ASC_API_KEY_PATH="${ASC_API_KEY_PATH:-$HOME/.appstoreconnect/private_keys/AuthKey_${ASC_API_KEY_ID}.p8}"
  [ -f "$ASC_API_KEY_PATH" ] || fail "no API key at $ASC_API_KEY_PATH"
fi
security find-identity -v -p codesigning | grep -q "Apple Distribution" || fail "no Apple Distribution identity in the keychain"
"$HERE/Scripts/check-release.sh" --source

rm -rf "$OUT"; mkdir -p "$OUT"
xcodebuild archive -project "$HERE/Omni.xcodeproj" -scheme Omni -configuration Release \
  -destination 'generic/platform=iOS' -archivePath "$OUT/Omni.xcarchive" -allowProvisioningUpdates \
  >"$OUT/archive.log" 2>&1 || { tail -30 "$OUT/archive.log" >&2; fail "archive failed (log: $OUT/archive.log)"; }
xcodebuild -exportArchive -archivePath "$OUT/Omni.xcarchive" -exportPath "$OUT/export" \
  -exportOptionsPlist "$HERE/Support/ExportOptions.plist" \
  >"$OUT/export.log" 2>&1 || { tail -30 "$OUT/export.log" >&2; fail "export failed (log: $OUT/export.log)"; }
IPA="$OUT/export/Omni.ipa"
[ -f "$IPA" ] || fail "no IPA at $IPA"

# The exported app must carry the production push entitlement.
rm -rf "$OUT/check"; mkdir -p "$OUT/check"
unzip -q "$IPA" -d "$OUT/check"
APP="$(ls -d "$OUT"/check/Payload/*.app)"
codesign -d --entitlements :- "$APP" 2>/dev/null | grep -A1 aps-environment | grep -q production \
  || fail "the exported app is not signed for production push"
VERSION="$(/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$APP/Info.plist")"
BUILD="$(/usr/libexec/PlistBuddy -c 'Print CFBundleVersion' "$APP/Info.plist")"
echo "testflight: exported Omni $VERSION ($BUILD) → $IPA"
[ "$UPLOAD" = 1 ] || exit 0

AUTH=(--api-key "$ASC_API_KEY_ID" --api-issuer "$ASC_API_ISSUER_ID" --p8-file-path "$ASC_API_KEY_PATH")
xcrun altool --validate-app "$IPA" "${AUTH[@]}" --output-format normal
xcrun altool --upload-package "$IPA" "${AUTH[@]}" --show-progress
echo "testflight: uploaded Omni $VERSION ($BUILD); App Store Connect processes it before it appears in TestFlight"
