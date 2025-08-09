#!/usr/bin/env bash
set -euo pipefail

# UCB post-build helper:
# 1) Upload APK to S3 (private)
# 2) Optionally generate a presigned GET URL
# 3) Trigger GitHub repository_dispatch with apk_url
#
# Requirements on the UCB environment:
# - AWS CLI v2 configured via env vars or default profile (for S3 upload & presign)
# - curl
# - (optional) jq (if not present, a simple printf JSON is used)
#
# Required environment variables:
#   S3_BUCKET              S3 bucket where APK will be stored (private)
#   GITHUB_TOKEN           GitHub token (PAT or GitHub App installation token) with repo: access
#   GH_OWNER               GitHub org/user owning the repository to dispatch
#   GH_REPO                GitHub repository name to dispatch
#
# Optional environment variables:
#   APK_PATH               Path to the APK file. If unset, first *.apk found will be used or $1 is treated as APK path
#   S3_PREFIX              Prefix within the bucket (default: apks)
#   PRESIGN_EXPIRES        Expiration in seconds for presigned URL (default: 3600)
#   DISPATCH_EVENT_TYPE    Event type for repository_dispatch (default: ucb-build-complete)
#   GH_API_URL             GitHub API base (default: https://api.github.com)
#   PACKAGE_NAME           Optional: Android package to include in client_payload
#   USE_S3_URL             If set to "true", send s3:// URL instead of presigned HTTPS
#   BUILD_LABEL            Optional label to include in S3 key (fallbacks to date+time)
#
# Usage:
#   scripts/ucb_post_build.sh [optional_apk_path]

log() { echo "[ucb_post_build] $*"; }
err() { echo "[ucb_post_build][ERROR] $*" >&2; }

APK_PATH_INPUT=${1:-}

# Validate required env vars
: "${S3_BUCKET:?S3_BUCKET is required}"
: "${GITHUB_TOKEN:?GITHUB_TOKEN is required}"
: "${GH_OWNER:?GH_OWNER is required}"
: "${GH_REPO:?GH_REPO is required}"

S3_PREFIX=${S3_PREFIX:-apks}
PRESIGN_EXPIRES=${PRESIGN_EXPIRES:-3600}
DISPATCH_EVENT_TYPE=${DISPATCH_EVENT_TYPE:-ucb-build-complete}
GH_API_URL=${GH_API_URL:-https://api.github.com}
USE_S3_URL=${USE_S3_URL:-false}

# Resolve APK path
if [[ -n "$APK_PATH_INPUT" ]]; then
  APK_PATH="$APK_PATH_INPUT"
elif [[ -n "${APK_PATH:-}" ]]; then
  APK_PATH="$APK_PATH"
else
  APK_PATH=$(find . -type f -name "*.apk" | head -n 1 || true)
fi

if [[ -z "${APK_PATH:-}" ]] || [[ ! -f "$APK_PATH" ]]; then
  err "APK file not found. Set APK_PATH or pass it as the first argument."
  exit 1
fi

log "APK_PATH=$APK_PATH"

# Build S3 key
DATE_TAG=$(date +%Y%m%d_%H%M%S)
LABEL_PART=${BUILD_LABEL:-$DATE_TAG}
FILE_NAME=$(basename "$APK_PATH")
S3_KEY="$S3_PREFIX/$LABEL_PART/$FILE_NAME"

log "Uploading to s3://$S3_BUCKET/$S3_KEY (private)"
aws s3 cp "$APK_PATH" "s3://$S3_BUCKET/$S3_KEY" --acl private

# Choose URL to send in dispatch
APK_URL=""
if [[ "$USE_S3_URL" == "true" ]]; then
  APK_URL="s3://$S3_BUCKET/$S3_KEY"
  log "Using S3 URL in dispatch: $APK_URL"
else
  log "Generating presigned URL (expires in $PRESIGN_EXPIRES s)"
  APK_URL=$(aws s3 presign "s3://$S3_BUCKET/$S3_KEY" --expires-in "$PRESIGN_EXPIRES")
  if [[ -z "$APK_URL" ]]; then
    err "Failed to generate presigned URL."
    exit 1
  fi
  log "Presigned URL ready."
fi

# Build JSON payload for repository_dispatch
PACKAGE_NAME=${PACKAGE_NAME:-}
if command -v jq >/dev/null 2>&1; then
  PAYLOAD=$(jq -n \
    --arg et    "$DISPATCH_EVENT_TYPE" \
    --arg url   "$APK_URL" \
    --arg pkg   "$PACKAGE_NAME" \
    '{event_type:$et, client_payload:{apk_url:$url}} | if $pkg != "" then .client_payload.package=$pkg else . end')
else
  # Minimal JSON builder without jq (note: PACKAGE_NAME omitted if empty)
  if [[ -n "$PACKAGE_NAME" ]]; then
    PAYLOAD=$(printf '{"event_type":"%s","client_payload":{"apk_url":"%s","package":"%s"}}' \
      "$DISPATCH_EVENT_TYPE" "$APK_URL" "$PACKAGE_NAME")
  else
    PAYLOAD=$(printf '{"event_type":"%s","client_payload":{"apk_url":"%s"}}' \
      "$DISPATCH_EVENT_TYPE" "$APK_URL")
  fi
fi

DISPATCH_URL="$GH_API_URL/repos/$GH_OWNER/$GH_REPO/dispatches"

log "Dispatching to $DISPATCH_URL"
HTTP_CODE=$(curl -sS -o /tmp/dispatch_resp.txt -w "%{http_code}" -X POST "$DISPATCH_URL" \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD")

if [[ "$HTTP_CODE" != "204" ]]; then
  err "Dispatch failed (HTTP $HTTP_CODE). Response:" && cat /tmp/dispatch_resp.txt >&2
  exit 1
fi

log "Dispatch sent successfully."






