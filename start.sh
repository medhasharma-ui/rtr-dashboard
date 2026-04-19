#!/bin/bash
# RTR Dashboard Startup Script
# Starts the dashboard server at http://rtr-dashboard.local:8080

DIR="$(cd "$(dirname "$0")" && pwd)"

# Check if hostname is set up
if ! grep -q "rtr-dashboard.local" /etc/hosts 2>/dev/null; then
    echo "Setting up rtr-dashboard.local hostname (requires sudo)..."
    echo "127.0.0.1 rtr-dashboard.local" | sudo tee -a /etc/hosts > /dev/null
    echo "Done."
fi

# Check for .env
if [ ! -f "$DIR/.env" ]; then
    echo ""
    echo "ERROR: No .env file found."
    echo "Create one with your Close API key:"
    echo ""
    echo "  echo 'CLOSE_API_KEY=your_key_here' > '$DIR/.env'"
    echo ""
    exit 1
fi

# Kill any existing server on port 8080
lsof -ti:8080 | xargs kill 2>/dev/null

echo ""
echo "Starting RTR Dashboard..."
echo "URL: http://rtr-dashboard.local:8080"
echo ""

cd "$DIR"
python3 server.py
