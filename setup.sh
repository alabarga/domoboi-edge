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
  cmake \
  libgpiod-dev

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

# 3. Configure Quectel Modem (ECM Mode & APN Setup)
echo "--> Detecting Quectel Cellular Modem..."
if lsusb | grep -qi "quectel"; then
  echo "Quectel modem detected via USB."
  
  # Select the correct serial port (typically ttyUSB2 or ttyUSB3)
  MODEM_PORT=""
  if [ -e /dev/ttyUSB2 ]; then
    MODEM_PORT="/dev/ttyUSB2"
  elif [ -e /dev/ttyUSB3 ]; then
    MODEM_PORT="/dev/ttyUSB3"
  fi
  
  if [ -n "$MODEM_PORT" ]; then
    echo "Reading modem configuration from config.yaml..."
    
    # Helper to parse YAML values from config.yaml, ignoring inline comments
    get_yaml_val() {
      local key=$1
      if [ -f "$CONFIG_PATH" ]; then
        grep -E "^[[:space:]]*$key:" "$CONFIG_PATH" | head -n1 | cut -d '#' -f1 | sed -E 's/.*:[[:space:]]*"?(.*)"?/\1/' | sed 's/"//g' | sed "s/'//g" | sed 's/[[:space:]]*$//'
      fi
    }
    
    SIM_PIN=$(get_yaml_val "pin")
    APN_NAME=$(get_yaml_val "apn")
    APN_USER=$(get_yaml_val "username")
    APN_PASS=$(get_yaml_val "password")
    
    if [ -z "$APN_NAME" ]; then
      APN_NAME="internet"
    fi
    
    echo "Checking SIM PIN lock status..."
    CPIN_STATUS=$(python3 -c "import time; f=open('$MODEM_PORT', 'r+b', buffering=0); f.write(b'AT+CPIN?\r\n'); time.sleep(0.4); print(f.read(1024).decode(errors='ignore'))" 2>/dev/null || true)
    
    if echo "$CPIN_STATUS" | grep -q "+CPIN: READY"; then
      echo "SIM is already unlocked/READY. Skipping PIN disable step."
    else
      # Prompt for SIM PIN if not defined in config and running interactively
      if [ -z "$SIM_PIN" ] && [ -t 0 ]; then
        read -p "Enter your SIM card PIN (or press Enter to skip if no PIN): " -r SIM_PIN
      fi
      
      if [ -n "$SIM_PIN" ]; then
        echo "Disabling SIM PIN lock..."
        python3 -c "import time; f=open('$MODEM_PORT', 'r+b', buffering=0); f.write(b'AT+CLCK=\"SC\",0,\"$SIM_PIN\"\r\n'); time.sleep(0.5); f.read(1024)" || true
        sleep 1
      fi
    fi
    
    # Configure APN in profile 1
    echo "Configuring modem APN to '$APN_NAME'..."
    python3 -c "import time; f=open('$MODEM_PORT', 'r+b', buffering=0); f.write(b'AT+CGDCONT=1,\"IP\",\"$APN_NAME\"\r\n'); time.sleep(0.5); f.read(1024)" || true
    sleep 1
    
    # Configure PAP authentication if credentials are provided
    if [ -n "$APN_USER" ] && [ -n "$APN_PASS" ]; then
      echo "Configuring APN PAP credentials..."
      python3 -c "import time; f=open('$MODEM_PORT', 'r+b', buffering=0); f.write(b'AT+QICSGP=1,1,\"$APN_NAME\",\"$APN_USER\",\"$APN_PASS\",1\r\n'); time.sleep(0.5); f.read(1024)" || true
      sleep 1
    fi
    
    # AT+QCFG="usbnet",1 sets to ECM mode. AT+CFUN=1,1 reboots the modem.
    echo "Configuring modem to ECM mode..."
    python3 -c "import time; f=open('$MODEM_PORT', 'r+b', buffering=0); f.write(b'AT+QCFG=\"usbnet\",1\r\n'); time.sleep(0.5); f.read(1024)" || true
    sleep 1
    
    echo "Rebooting cellular modem to apply changes..."
    python3 -c "import time; f=open('$MODEM_PORT', 'r+b', buffering=0); f.write(b'AT+CFUN=1,1\r\n'); time.sleep(0.5); f.read(1024)" || true
    echo "ECM mode configured. Modem is rebooting."
    
    # Wait for the modem to reboot, attach to the network, and obtain DHCP
    echo "Waiting for cellular modem to boot, register, and establish a data connection..."
    if python3 -c "
import time, sys
port = sys.argv[1]
start_time = time.time()
registered = False
rssi = 99
while time.time() - start_time < 90:
    try:
        with open(port, 'r+b', buffering=0) as f:
            # Query network registration status (AT+CREG?)
            f.write(b'AT+CREG?\\r\\n')
            time.sleep(0.4)
            resp = f.read(1024).decode(errors='ignore')
            if '+CREG: 0,1' in resp or '+CREG: 0,5' in resp:
                registered = True
                
                # Query signal strength (AT+CSQ)
                f.write(b'AT+CSQ\\r\\n')
                time.sleep(0.4)
                csq_resp = f.read(1024).decode(errors='ignore')
                for line in csq_resp.split('\\n'):
                    if '+CSQ:' in line:
                        try:
                            rssi = int(line.split(':')[1].split(',')[0].strip())
                        except Exception:
                            pass
                break
    except Exception:
        pass
    time.sleep(4.0)

if registered:
    print(f'SUCCESS: Registered on network. Signal strength RSSI: {rssi}/31')
    sys.exit(0)
else:
    print('TIMEOUT: Modem failed to register within 90 seconds.')
    sys.exit(1)
" "$MODEM_PORT"; then
      # Wait a brief moment for interface allocation and DHCP lease
      echo "Waiting 5 seconds for IP address assignment..."
      sleep 5
      
      # Auto-detect the active interface corresponding to the modem (usb0, enp, enx)
      IFACE=$(nmcli -t -f DEVICE,TYPE device | grep -E "usb0|enp|enx" | head -n1 | cut -d: -f1)
      if [ -z "$IFACE" ]; then
        # Fallback to any active ethernet connection
        IFACE=$(nmcli -t -f DEVICE,TYPE device | grep -E ":ethernet" | head -n1 | cut -d: -f1)
      fi
      
      if [ -n "$IFACE" ]; then
        echo "Cellular network interface '$IFACE' detected. Performing Google ping test..."
        ping -I "$IFACE" -c 4 google.com || true
      else
        echo "Warning: Could not identify cellular network interface name for ping test."
      fi
    else
      echo "Warning: Registration check failed. Skipping ping test."
    fi
  else
    echo "Modem control ports (/dev/ttyUSB2 or /dev/ttyUSB3) not found. Skipping AT configuration."
  fi

  # Add NetworkManager GSM connection configuration for standard dial fallback
  echo "Adding cellular connection profile in NetworkManager..."
  nmcli connection delete lte-modem 2>/dev/null || true
  nmcli connection add type gsm ifname '*' con-name lte-modem apn "$APN_NAME" || true
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
