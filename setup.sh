#!/usr/bin/env bash
# setup.sh - Automation script to configure Raspberry Pi 5 for Domoboi NILM Edge

set -e

# Ensure script is run as root
if [ "$EUID" -ne 0 ]; then
  echo "Error: Please run as root (sudo ./setup.sh)"
  exit 1
fi

echo "========================================="
echo " Starting Domoboi NILM System Setup      "
echo "========================================="

# 1. Enable SPI & I2C Interfaces
echo "--> Configuring SPI and I2C interfaces..."
CONFIG_FILE="/boot/firmware/config.txt"
if [ ! -f "$CONFIG_FILE" ]; then
  # Fallback for older Pi OS versions
  CONFIG_FILE="/boot/config.txt"
fi

if [ -f "$CONFIG_FILE" ]; then
  # Enable SPI if not already enabled
  if ! grep -q "^dtparam=spi=on" "$CONFIG_FILE"; then
    echo "dtparam=spi=on" >> "$CONFIG_FILE"
    echo "Enabled SPI in $CONFIG_FILE"
  fi
  # Enable I2C if not already enabled
  if ! grep -q "^dtparam=i2c_arm=on" "$CONFIG_FILE"; then
    echo "dtparam=i2c_arm=on" >> "$CONFIG_FILE"
    echo "Enabled I2C in $CONFIG_FILE"
  fi
else
  echo "Warning: Boot configuration file not found. Please enable SPI/I2C manually."
fi

# 1b. Ensure i2c-dev kernel module is loaded on boot
if [ -f /etc/modules ]; then
  if ! grep -q "^i2c-dev" /etc/modules; then
    echo "i2c-dev" >> /etc/modules
    echo "Configured i2c-dev to load on boot in /etc/modules"
  fi
fi

# 1c. Add user to hardware groups to run code without sudo
if [ -n "$SUDO_USER" ]; then
  echo "Adding user $SUDO_USER to spi, i2c, and gpio groups..."
  usermod -aG spi,i2c,gpio "$SUDO_USER" || true
fi


# 2. Install System Packages
echo "--> Updating packages and installing dependencies..."
apt-get update
apt-get install -y \
  python3-pip \
  python3-venv \
  libqmi-utils \
  udhcpc \
  minicom \
  i2c-tools \
  gpiod \
  network-manager \
  git \
  build-essential \
  cmake \
  libgpiod-dev

# 3. Configure Quectel Modem (ECM Mode & APN Setup)
echo "--> Detecting Quectel Cellular Modem..."
if lsusb | grep -qi "quectel"; then
  echo "Quectel modem detected via USB."
  
  # Set modem to ECM Mode (usbnet 1) via serial AT command
  # ECM mode exposes a direct ethernet interface (usb0/eth1) which uses dhcp
  # Typically the command port is /dev/ttyUSB2
  if [ -e /dev/ttyUSB2 ]; then
    echo "Sending configuration commands to modem (/dev/ttyUSB2)..."
    # AT+QCFG="usbnet",1 sets to ECM mode. AT+CFUN=1,1 reboots the modem.
    # Using stty to set baud rate and redirection
    stty -F /dev/ttyUSB2 115200 || true
    echo -e 'AT+QCFG="usbnet",1\r' > /dev/ttyUSB2
    sleep 1
    echo -e 'AT+CFUN=1,1\r' > /dev/ttyUSB2
    echo "ECM mode configured. Modem is rebooting."
  else
    echo "Modem AT port (/dev/ttyUSB2) not found. Skipping AT command configuration."
  fi

  # Add NetworkManager GSM connection configuration for standard dial fallback
  echo "Adding cellular connection profile in NetworkManager..."
  nmcli connection delete lte-modem 2>/dev/null || true
  nmcli connection add type gsm ifname '*' con-name lte-modem apn internet || true
else
  echo "Quectel modem not found on USB. Ensure it is connected and powered."
fi

# 4. Compile native C-based testctread tool
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -d "$SCRIPT_DIR/testctread" ]; then
  echo "--> Compiling testctread utility..."
  cd "$SCRIPT_DIR/testctread"
  mkdir -p build
  cd build
  cmake ..
  make
  echo "testctread successfully compiled inside testctread/build/"
  cd "$SCRIPT_DIR"
else
  echo "Warning: testctread directory not found."
fi

# 5. Create Python Virtual Environment & Install requirements
echo "--> Creating Python virtual environment..."
VENV_PATH="$SCRIPT_DIR/.venv"
python3 -m venv "$VENV_PATH"
"$VENV_PATH/bin/pip" install --upgrade pip
"$VENV_PATH/bin/pip" install spidev smbus2 pyyaml aiohttp

# Create dummy config.yaml if it doesn't exist
if [ ! -f "$SCRIPT_DIR/config.yaml" ] && [ -f "$SCRIPT_DIR/config.example.yaml" ]; then
  echo "Copying config.example.yaml to config.yaml..."
  cp "$SCRIPT_DIR/config.example.yaml" "$SCRIPT_DIR/config.yaml"
fi

# 6. Install Python Edge Client as a Systemd Service
echo "--> Installing Systemd Service..."
SERVICE_FILE="/etc/systemd/system/domoboi-edge.service"
cat <<EOF > "$SERVICE_FILE"
[Unit]
Description=Domoboi NILM Edge Monitoring Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
ExecStart=$VENV_PATH/bin/python3 $SCRIPT_DIR/client.py
Restart=always
RestartSec=10
User=root

[Install]
WantedBy=multi-user.target
EOF

# Reload and enable service (but do not start yet since script files are not fully implemented)
systemctl daemon-reload
systemctl enable domoboi-edge.service

echo "========================================="
echo " Setup Complete!                         "
echo " Please reboot the Raspberry Pi to       "
echo " apply SPI/I2C changes.                  "
echo "========================================="
