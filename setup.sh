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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
  # Enable USB Max Current for Raspberry Pi 5
  if ! grep -q "^usb_max_current_enable=1" "$CONFIG_FILE"; then
    echo "usb_max_current_enable=1" >> "$CONFIG_FILE"
    echo "Enabled high USB current limit (usb_max_current_enable=1) in $CONFIG_FILE"
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

# 1d. Configure Spanish locale to avoid SSH warnings
if [ -f /etc/locale.gen ]; then
  if ! grep -q "^es_ES.UTF-8 UTF-8" /etc/locale.gen && grep -q "# es_ES.UTF-8 UTF-8" /etc/locale.gen; then
    sed -i 's/# es_ES.UTF-8 UTF-8/es_ES.UTF-8 UTF-8/' /etc/locale.gen
    echo "Generating es_ES.UTF-8 locale..."
    locale-gen || true
  fi
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
  libgpiod-dev \
  htpdate

echo "--> Syncing clock immediately over HTTP using htpdate..."
htpdate -s -a google.com

echo "--> Disabling conflicting systemd-timesyncd NTP service..."
timedatectl set-ntp false || true
systemctl stop systemd-timesyncd || true
systemctl disable systemd-timesyncd || true


# 2b. Ensure config.yaml exists
CONFIG_PATH="$SCRIPT_DIR/config.yaml"
if [ ! -f "$CONFIG_PATH" ]; then
  if [ -f "$SCRIPT_DIR/config.example.yaml" ]; then
    cp "$SCRIPT_DIR/config.example.yaml" "$CONFIG_PATH"
    echo "Created default config.yaml from config.example.yaml."
  else
    echo "Warning: config.example.yaml not found. Skipping config.yaml creation."
  fi
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

# 4b. Ensure config.yaml permissions are correct
echo "--> Configuring config.yaml permissions..."
if [ -n "$SUDO_USER" ] && [ -f "$CONFIG_PATH" ]; then
  chown "$SUDO_USER":"$SUDO_USER" "$CONFIG_PATH" 2>/dev/null || true
fi


# 5. Create Python Virtual Environment & Install requirements
echo "--> Creating Python virtual environment..."
VENV_PATH="$SCRIPT_DIR/.venv"
python3 -m venv "$VENV_PATH"
"$VENV_PATH/bin/pip" install --upgrade pip
"$VENV_PATH/bin/pip" install spidev smbus2 pyyaml aiohttp rich

# Create local data directory and ensure correct user ownership
mkdir -p "$SCRIPT_DIR/data"
if [ -n "$SUDO_USER" ]; then
  chown -R "$SUDO_USER":"$SUDO_USER" "$SCRIPT_DIR/data" "$VENV_PATH" 2>/dev/null || true
  if [ -f "$SCRIPT_DIR/config.yaml" ]; then
    chown "$SUDO_USER":"$SUDO_USER" "$SCRIPT_DIR/config.yaml" 2>/dev/null || true
  fi
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
ExecStart=$VENV_PATH/bin/python3 $SCRIPT_DIR/client.py --no-ui
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
