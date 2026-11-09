#!/bin/bash
# TubeSpy: every YouTube ad for one Google Ads Transparency advertiser -> CSV.
# Usage: tubespy_export.sh "https://adstransparency.google.com/advertiser/AR..."
# Prints one JSON line: {"ok":..., "csv": "/path.csv", counts...}
set -u
DIR=/Users/maxopenclaw/.hermes/hermes-agent/youtube-competitor-library
exec "$DIR/.venv/bin/python" "$DIR/tubespy_export.py" "$@"
