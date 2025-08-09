#!/usr/bin/env bash
set -euo pipefail

echo "Waiting for device..."
adb wait-for-device

echo "Waiting for sys.boot_completed=1 ..."
until [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; do
  sleep 2
done

echo "Waiting for Package Manager service ..."
until adb shell 'cmd package list packages' >/dev/null 2>&1; do
  sleep 2
done

echo "Device is ready."


