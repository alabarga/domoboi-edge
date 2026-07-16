#!/usr/bin/env bash
# net.sh - Set connection route metrics to prioritize WiFi over Cellular Backup

set -e

# Ensure script is run as root
if [ "$EUID" -ne 0 ]; then
  echo "Error: Please run as root (sudo ./net.sh)"
  exit 1
fi

echo "========================================="
echo " Configuring Network Metrics Priority    "
echo "========================================="

# 1. Modify metrics for cellular connections to 300 (lower priority)
echo "Setting cellular connection profiles to metric 300..."
sudo nmcli connection modify lte-modem ipv4.route-metric 300 ipv6.route-metric 300 2>/dev/null || true
sudo nmcli connection modify netplan-eth0 ipv4.route-metric 300 ipv6.route-metric 300 2>/dev/null || true

# 2. Modify metrics for all WiFi profiles to 100 (higher priority)
echo "Setting WiFi connection profiles to metric 100..."
for conn in $(nmcli -g NAME,TYPE connection show | grep :vpn -v | grep -E ":802-11-wireless|:wireless|:wifi" | cut -d: -f1); do
  echo "  Prioritizing: $conn"
  sudo nmcli connection modify "$conn" ipv4.route-metric 100 ipv6.route-metric 100 || true
done

sudo nmcli connection reload || true

# 3. Reapply configuration to active interfaces to update the routing table immediately
echo "Applying route metrics changes..."
# Auto-detect cellular interface (usb0, enp, enx)
IFACE=$(nmcli -t -f DEVICE,TYPE device | grep -E "usb0|enp|enx" | head -n1 | cut -d: -f1)
if [ -n "$IFACE" ]; then
  echo "  Reapplying settings to cellular interface '$IFACE'..."
  sudo nmcli device reapply "$IFACE" 2>/dev/null || sudo nmcli connection up netplan-eth0 2>/dev/null || true
fi

# Reapply to WiFi interfaces
for dev in $(nmcli -t -f DEVICE,TYPE device | grep ":wifi" | cut -d: -f1); do
  echo "  Reapplying settings to WiFi interface '$dev'..."
  sudo nmcli device reapply "$dev" 2>/dev/null || true
done

echo "Network metrics configuration complete."
echo "Active Routing Table (ip route show):"
ip route show
