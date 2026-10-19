#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TEST_DIR="$ROOT_DIR/build/test-classes"

"$ROOT_DIR/scripts/build.sh"
mkdir -p "$TEST_DIR"

javac --release 11 \
    -cp "$ROOT_DIR/build/classes" \
    -d "$TEST_DIR" \
    "$ROOT_DIR/src/test/java/com/tencentcloud/k8sdemo/AppTest.java"

java -cp "$ROOT_DIR/build/classes:$TEST_DIR" com.tencentcloud.k8sdemo.AppTest
