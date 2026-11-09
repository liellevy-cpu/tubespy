#!/bin/bash

# ═══════════════════════════════════════════
#  TubeSpy — Start
#  Double-click this file to launch the app
# ═══════════════════════════════════════════

cd "$(dirname "$0")"

# Check if setup has been run
if [ ! -d "node_modules" ]; then
    echo ""
    echo "❌ Setup hasn't been run yet."
    echo "   Please double-click 'Setup TubeSpy' first."
    echo ""
    read -p "Press Enter to close..."
    exit 1
fi

clear
echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║    Starting TubeSpy...           ║"
echo "  ║    Opening browser in 3 seconds...   ║"
echo "  ║                                      ║"
echo "  ║    Keep this window open while       ║"
echo "  ║    the app is running.               ║"
echo "  ╚══════════════════════════════════════╝"
echo ""

# Open browser after a short delay
(sleep 3 && open "http://localhost:3847") &

# Start the server
node server.js
