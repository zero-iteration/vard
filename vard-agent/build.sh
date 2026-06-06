#!/usr/bin/env bash
# Build the VARD instrumentation agent: a ByteBuddy-shaded -javaagent jar.
# Targets JDK 11 bytecode (pom maven.compiler.release=11) so it LOADS on JDK 11 and 17 repos.
# ProcessHandle (per-PID trace files) is Java 9+, so 11 is the floor.
# Produces target/vard-agent.jar; copied to vard-agent/vard-agent.jar where the CLI looks for it.
#
# Falls back to the zero-dep stack-sampler (sampler-VardSampler.java.txt) only if you can't run Maven.
set -euo pipefail
cd "$(dirname "$0")"
mvn -q -DskipTests package
cp -f target/vard-agent.jar vard-agent.jar
echo "✓ built vard-agent.jar (ByteBuddy-shaded, JDK 11 bytecode)"
