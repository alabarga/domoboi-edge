#!/usr/bin/env bash
# start.sh - Start the domoboi-edge energy monitoring service

set -e

# Ensure script is run as root
if [ "$EUID" -ne 0 ]; then
  echo "Error: Please run as root (sudo ./start.sh)"
  exit 1
fi

echo "Starting energy monitoring service (domoboi-edge)..."
sudo systemctl restart domoboi-edge

echo "Checking service status..."
sudo systemctl status domoboi-edge --no-pager -l

echo "Checking service activity... ( Press Ctrl+C to stop this )"
sudo journalctl -u domoboi-edge.service -f
