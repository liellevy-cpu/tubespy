#!/bin/bash

# ═══════════════════════════════════════════
#  TubeSpy — First-Time Setup
#  Double-click this file to install everything
# ═══════════════════════════════════════════

clear
echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║    TubeSpy — Setup               ║"
echo "  ║    This will install everything       ║"
echo "  ╚══════════════════════════════════════╝"
echo ""

cd "$(dirname "$0")"

# Check for Node.js
if ! command -v node &> /dev/null; then
    echo "❌ Node.js is not installed."
    echo ""
    echo "Please install Node.js first:"
    echo "  1. Go to https://nodejs.org"
    echo "  2. Download the LTS version"
    echo "  3. Install it"
    echo "  4. Then double-click this setup file again"
    echo ""
    read -p "Press Enter to close..."
    exit 1
fi

echo "✓ Node.js found: $(node --version)"
echo ""

# Install dependencies
echo "Installing packages..."
npm install

if [ $? -ne 0 ]; then
    echo ""
    echo "❌ npm install failed. Please check the errors above."
    read -p "Press Enter to close..."
    exit 1
fi

echo ""
echo "Installing browser (Chromium)..."
npx playwright install chromium

if [ $? -ne 0 ]; then
    echo ""
    echo "❌ Browser install failed. Please check the errors above."
    read -p "Press Enter to close..."
    exit 1
fi

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║    ✓ Setup Complete!                 ║"
echo "  ║                                      ║"
echo "  ║    Double-click 'Start TubeSpy'  ║"
echo "  ║    to launch the app.                ║"
echo "  ╚══════════════════════════════════════╝"
echo ""
read -p "Press Enter to close..."
