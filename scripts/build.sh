#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BUILD_DIR="$ROOT_DIR/build"
CLASSES_DIR="$BUILD_DIR/classes"

rm -rf "$BUILD_DIR"
mkdir -p "$CLASSES_DIR"

javac --release 11 -d "$CLASSES_DIR" \
    "$ROOT_DIR/src/main/java/com/tencentcloud/k8sdemo/App.java"
jar --create --file "$BUILD_DIR/app.jar" \
    --main-class com.tencentcloud.k8sdemo.App \
    -C "$CLASSES_DIR" .

echo "Built $BUILD_DIR/app.jar"
