#!/usr/bin/env bash
# Build the Java benchmark. Invoked from the repo root (bash impl/java/build.sh),
# so all paths are computed relative to this script's own directory.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONNECTOR_VERSION="8.4.0"
JAR="$DIR/lib/mysql-connector-j.jar"

if ! command -v javac >/dev/null 2>&1; then
    echo "javac not found: install a JDK (e.g. 'apt install default-jdk' or 'dnf install java-17-openjdk-devel')" >&2
    exit 1
fi

if [ ! -f "$JAR" ]; then
    if ! command -v curl >/dev/null 2>&1; then
        echo "curl not found: install curl (needed to download MySQL Connector/J from Maven Central)" >&2
        exit 1
    fi
    mkdir -p "$DIR/lib"
    URL="https://repo1.maven.org/maven2/com/mysql/mysql-connector-j/${CONNECTOR_VERSION}/mysql-connector-j-${CONNECTOR_VERSION}.jar"
    echo "downloading MySQL Connector/J ${CONNECTOR_VERSION} from Maven Central..." >&2
    if ! curl -fL -o "$JAR.tmp" "$URL"; then
        rm -f "$JAR.tmp"
        echo "failed to download Connector/J from $URL (check network access to repo1.maven.org)" >&2
        exit 1
    fi
    mv "$JAR.tmp" "$JAR"
fi

javac -cp "$JAR" -d "$DIR" "$DIR/Bench.java"
