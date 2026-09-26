#!/usr/bin/env bash
# ============================================================================
# NEXORA — build step for a NON-Docker Python host (Render and equivalents).
#
#   ./bin/render-build.sh              install deps, ffmpeg, static, migrate
#   ./bin/render-build.sh --no-migrate same, without migrations (workers)
#
# Migrations run from exactly one service (the web service) so three parallel
# deploys cannot race each other on the same database.
# ============================================================================
set -o errexit
set -o nounset
set -o pipefail

MIGRATE=1
for arg in "$@"; do
  [ "$arg" = "--no-migrate" ] && MIGRATE=0
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/.."

echo "==> Installing Python dependencies"
python -m pip install --upgrade pip
pip install -r requirements.txt

# ---------------------------------------------------------------------------
# FFmpeg / ffprobe
#
# Voice notes, video posters and duration probing need them, and a managed
# Python runtime has no apt access. A static build is unpacked into ./bin and
# pointed at with FFMPEG_BINARY / FFPROBE_BINARY. Failure is NOT fatal: image
# derivatives keep working and video/voice items are recorded as
# FFMPEG_MISSING instead of breaking the deploy.
# ---------------------------------------------------------------------------
if [ ! -x "./bin/ffmpeg" ]; then
  echo "==> Installing static FFmpeg"
  FF_URL="${FFMPEG_STATIC_URL:-https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz}"
  if curl -fsSL --retry 2 "$FF_URL" -o /tmp/ffmpeg.tar.xz; then
    mkdir -p /tmp/ffmpeg && tar -xJf /tmp/ffmpeg.tar.xz -C /tmp/ffmpeg --strip-components=1
    cp /tmp/ffmpeg/ffmpeg /tmp/ffmpeg/ffprobe ./bin/ 2>/dev/null || true
    chmod +x ./bin/ffmpeg ./bin/ffprobe 2>/dev/null || true
    rm -rf /tmp/ffmpeg /tmp/ffmpeg.tar.xz
  else
    echo "!! FFmpeg download failed — video/voice processing will report FFMPEG_MISSING"
  fi
fi

echo "==> Collecting static files"
python manage.py collectstatic --no-input

if [ "$MIGRATE" = "1" ]; then
  echo "==> Applying database migrations"
  python manage.py migrate --no-input
fi

echo "==> Build complete"
