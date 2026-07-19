sudo apt install htpdate -y
echo "--> Syncing clock immediately over HTTP using htpdate..."
htpdate -s -a google.com

echo "--> Disabling conflicting systemd-timesyncd NTP service..."
timedatectl set-ntp false || true
systemctl stop systemd-timesyncd || true
systemctl disable systemd-timesyncd || true
