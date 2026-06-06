#!/usr/bin/env bash
# Build the VARD JVM agent. Target JDK 11 bytecode (major 55) so the jar LOADS on the target repo's JVM
# whether it runs JDK 11 or 17 — building with a newer javac default (e.g. 17→major 61) makes it fail to
# load on JDK 11 repos. ProcessHandle (used for per-PID trace files) is a Java 9+ API, so 11 is the floor.
# JDK 8 support would need the multi-release treatment (ProcessHandle via reflection) — not yet.
set -euo pipefail
cd "$(dirname "$0")"
rm -rf build && mkdir build
javac --release 11 -d build src/VardAgent.java
jar cfm vard-agent.jar manifest.txt -C build VardAgent.class
echo "✓ built vard-agent.jar (JDK 11 bytecode)"
