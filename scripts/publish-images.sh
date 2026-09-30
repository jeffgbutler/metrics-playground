#!/usr/bin/env bash
# Build the two home-grown images (simulator, docs) for amd64 + arm64 and push them to a registry.
#
#   scripts/publish-images.sh 0.1.0                         # -> harbor.jbcodes.net/library/..., tags 0.1.0 + latest
#   REGISTRY=registry.example.com/team scripts/publish-images.sh 0.2.0
#   PUSH=false scripts/publish-images.sh test               # build both platforms, push nothing (a dry run)
#
# Everything else in docker-compose.yml is a public image and is pulled on the VM directly.
set -euo pipefail

TAG="${1:?usage: scripts/publish-images.sh <tag>   e.g. 0.1.0}"
REGISTRY="${REGISTRY:-harbor.jbcodes.net/library}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
PUSH="${PUSH:-true}"
cd "$(dirname "$0")/.."

build() {  # name, context
  local image="$REGISTRY/metrics-playground-$1"
  local out=()
  [[ "$PUSH" == "true" ]] && out=(--push)
  echo "==> $image:$TAG ($PLATFORMS)"
  docker buildx build --platform "$PLATFORMS" -t "$image:$TAG" -t "$image:latest" "${out[@]}" "$2"
}

build simulator simulator
build docs docs-server

if [[ "$PUSH" == "true" ]]; then
  echo
  echo "Pushed. On the VM, set in .env:"
  echo "  PLAYGROUND_REGISTRY=$REGISTRY"
  echo "  PLAYGROUND_TAG=$TAG"
  echo "then: docker compose pull && docker compose up -d --no-build"
fi
