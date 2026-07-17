#!/usr/bin/env bash
# net.sh - Configure Quectel cellular modem, wait for network, and prioritize WiFi

set -e

# Ensure script is run as root
if [ "$EUID" -ne 0 ]; then
  echo "Error: Please run as root (sudo ./net.sh)"
  exit 1
fi

echo "========================================="
echo " Starting Cellular Modem & Network Setup "
echo "========================================="

# Stop conflicting services that poll the serial port to prevent read collisions
echo "Stopping conflicting services (domoboi-edge, ModemManager)..."
sudo systemctl stop domoboi-edge 2>/dev/null || true
sudo systemctl stop ModemManager 2>/dev/null || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PATH="$SCRIPT_DIR/config.yaml"

if [ ! -f "$CONFIG_PATH" ]; then
  echo "Error: config.yaml not found. Please create it or run setup.sh first."
  exit 1
fi

# 1. Configure Quectel Modem (ECM Mode & APN Setup)
echo "--> Detecting Quectel Cellular Modem..."
if lsusb | grep -qi "quectel"; then
  echo "Quectel modem detected via USB."
  
  # Select the correct serial port by testing which one actively responds to AT commands
  MODEM_PORT=""
  echo "Scanning serial ports for AT command response..."
  for port in /dev/ttyUSB3 /dev/ttyUSB2 /dev/ttyUSB1; do
    if [ -e "$port" ]; then
      if python3 -c "
import time, sys, os
try:
    with open(sys.argv[1], 'r+b', buffering=0) as f:
        os.set_blocking(f.fileno(), False)
        f.read(1024)
        f.write(b'AT\r\n')
        resp = b''
        for _ in range(4):
            time.sleep(0.15)
            chunk = f.read(1024)
            if chunk:
                resp += chunk
        if b'OK' in resp:
            sys.exit(0)
except Exception:
    pass
sys.exit(1)
" "$port" 2>/dev/null; then
        MODEM_PORT="$port"
        echo "Selected active modem control port: $MODEM_PORT"
        break
      fi
    fi
  done
  
  if [ -z "$MODEM_PORT" ]; then
    # Fallback to checking file existence if none responded (modem might be sleeping)
    if [ -e /dev/ttyUSB3 ]; then
      MODEM_PORT="/dev/ttyUSB3"
    elif [ -e /dev/ttyUSB2 ]; then
      MODEM_PORT="/dev/ttyUSB2"
    fi
    if [ -n "$MODEM_PORT" ]; then
      echo "No ports responded to AT. Using fallback file detection: $MODEM_PORT"
    fi
  fi
  
  if [ -n "$MODEM_PORT" ]; then
    # Configure the modem serial port to RAW mode to disable OS Echo and translations
    echo "Configuring serial port parameters for $MODEM_PORT..."
    sudo stty -F "$MODEM_PORT" 115200 raw -echo -echoe -echok -echoctl -echoprt 2>/dev/null || true

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
    
    while true; do
      echo "Checking SIM card status..."
      SIM_STATUS=$(python3 -c "
import time, sys, os
port = sys.argv[1]
try:
    with open(port, 'r+b', buffering=0) as f:
        os.set_blocking(f.fileno(), False)
        f.read(1024)
        f.write(b'AT+CPIN?\r\n')
        resp = ''
        for _ in range(4):
            time.sleep(0.3)
            chunk = f.read(1024)
            if chunk:
                resp += chunk.decode(errors='ignore')
        
        if 'READY' in resp:
            print('READY')
            sys.exit(0)
        elif 'SIM PIN' in resp:
            print('LOCKED')
            sys.exit(1)
        elif 'NOT INSERTED' in resp or 'ERROR' in resp:
            print('NOT_INSERTED')
            sys.exit(2)
        else:
            print('UNKNOWN')
            sys.exit(3)
except Exception:
    print('PORT_ERROR')
    sys.exit(4)
" "$MODEM_PORT" || true)

      echo "Detected SIM Status: $SIM_STATUS"
      
      if [ "$SIM_STATUS" = "READY" ]; then
        echo "SIM is already unlocked/READY. Skipping PIN disable step."
        break
      else
        # If status is NOT READY (LOCKED, UNKNOWN, PORT_ERROR, etc.)
        # We ask for the PIN if not already defined in config and running interactively
        if [ -z "$SIM_PIN" ] && [ -t 0 ]; then
          read -p "SIM is not READY (Status: $SIM_STATUS). Enter your SIM card PIN (or press Enter to skip): " -r SIM_PIN
        fi
        
        if [ -n "$SIM_PIN" ]; then
          echo "Disabling SIM PIN lock using PIN..."
          python3 -c "import time; f=open('$MODEM_PORT', 'r+b', buffering=0); f.write(b'AT+CLCK=\"SC\",0,\"$SIM_PIN\"\r\n'); time.sleep(0.5); f.read(1024)" || true
          sleep 1
          
          # Verify unlock status
          echo "Verifying unlock status..."
          SIM_STATUS=$(python3 -c "
import time, sys, os
port = sys.argv[1]
try:
    with open(port, 'r+b', buffering=0) as f:
        os.set_blocking(f.fileno(), False)
        f.read(1024)
        f.write(b'AT+CPIN?\r\n')
        resp = ''
        for _ in range(4):
            time.sleep(0.3)
            chunk = f.read(1024)
            if chunk:
                resp += chunk.decode(errors='ignore')
        if 'READY' in resp:
            print('READY')
            sys.exit(0)
        else:
            print('LOCKED')
            sys.exit(1)
except Exception:
    print('PORT_ERROR')
    sys.exit(2)
" "$MODEM_PORT" || true)
          
          if [ "$SIM_STATUS" = "READY" ]; then
            echo "SIM successfully unlocked!"
            break
          fi
        fi
        
        # If still not ready, show warning and retry/skip menu
        if [ -t 0 ]; then
          echo "------------------------------------------------------------------"
          echo "WARNING: SIM card is not ready (Status: $SIM_STATUS)."
          echo "Please ensure:"
          echo " 1. The SIM card is physically inserted in the correct orientation."
          echo " 2. The coaxial pigtail is snapped into the 'MAIN' LTE port."
          echo " 3. The modem is powered on and has completed booting."
          echo "------------------------------------------------------------------"
          read -p "Press [Enter] to retry SIM detection, or type 's' to skip: " -r USER_CHOICE
          if [ "$USER_CHOICE" = "s" ] || [ "$USER_CHOICE" = "S" ]; then
            echo "Skipping SIM configuration as requested."
            break
          fi
        else
          echo "Non-interactive shell: Skipping SIM step since status is $SIM_STATUS."
          break
        fi
      fi
    done
    
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
    python3 -c "import time; f=open('$MODEM_PORT', 'r+b', buffering=0); f.write(b'AT+CFUN=1,1\r\n'); time.sleep(0.5); f.read(1024)" 2>/dev/null || true
    echo "ECM mode configured. Modem is rebooting."
    
    # Wait for the modem to reboot, attach to the network, and obtain DHCP
    echo "Waiting for cellular modem to boot, register, and establish a data connection..."
    echo "Waiting for modem port to disconnect..."
    for i in {1..10}; do
      if [ ! -e "$MODEM_PORT" ]; then
        break
      fi
      sleep 1
    done
    while [ ! -e "$MODEM_PORT" ]; do
      echo "⏳  Waiting for $MODEM_PORT to appear..."
      sleep 1
    done
    echo "✅  $MODEM_PORT is now present"
    sleep 2
    if python3 - "$MODEM_PORT" <<'PY'
import time, sys, re, os
port = sys.argv[1]
start_time = time.time()
registered = False
rssi = 99
attempt = 0

def send_cmd(cmd, wait=0.5):
    try:
        with open(port, 'r+b', buffering=0) as f:
            os.set_blocking(f.fileno(), False)
            # Clean buffers by reading outstanding data
            f.read(1024)
            # Write command
            f.write(cmd + b'\r\n')
            # Read response
            resp = b""
            for _ in range(4):
                time.sleep(wait / 4.0)
                chunk = f.read(1024)
                if chunk:
                    resp += chunk
            return resp.decode(errors='ignore')
    except Exception:
        return ""

while time.time() - start_time < 150:
    attempt += 1
    cpin_resp = send_cmd(b'AT+CPIN?')
    cpin_state = 'UNKNOWN'
    if 'READY' in cpin_resp:
        cpin_state = 'READY'
    elif 'SIM PIN' in cpin_resp:
        cpin_state = 'PIN_LOCKED'
    elif 'NOT INSERTED' in cpin_resp:
        cpin_state = 'NO_SIM'

    creg_resp = send_cmd(b'AT+CREG?')
    creg_state = 'UNKNOWN'
    reg_match = re.search(r'\+CREG:\s*(?:\d\s*,\s*)?([0-9])', creg_resp)
    if reg_match:
        status = int(reg_match.group(1))
        if status == 0: creg_state = 'NOT_REG_NOT_SEARCHING'
        elif status == 1: creg_state = 'REGISTERED_HOME'
        elif status == 2: creg_state = 'SEARCHING'
        elif status == 3: creg_state = 'REGISTRATION_DENIED'
        elif status == 4: creg_state = 'UNKNOWN'
        elif status == 5: creg_state = 'REGISTERED_ROAMING'
        if status in (1, 5):
            registered = True

    csq_resp = send_cmd(b'AT+CSQ')
    csq_state = 'UNKNOWN'
    for line in csq_resp.split('\n'):
        if '+CSQ:' in line:
            csq_state = line.split(':')[1].strip()
            try:
                rssi = int(line.split(':')[1].split(',')[0].strip())
            except Exception:
                pass

    elapsed = int(time.time() - start_time)
    print(f'  [Attempt {attempt}] SIM: {cpin_state} | Net: {creg_state} | Signal: {csq_state} | Elapsed: {elapsed}s')
    sys.stdout.flush()
    if registered:
        break
    time.sleep(8)

if registered:
    print(f'SUCCESS: Registered on network. Signal strength RSSI: {rssi}/31')
    sys.exit(0)
else:
    print('TIMEOUT: Modem failed to register within 150 seconds.')
    sys.exit(1)
PY
then
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
  sudo nmcli connection delete lte-modem 2>/dev/null || true
  sudo nmcli connection add type gsm ifname '*' con-name lte-modem apn "$APN_NAME" || true
else
  echo "Quectel modem not found on USB. Ensure it is connected and powered."
fi

# 2. Configure connection metrics to prioritize WiFi (metric 100) over Cellular (metric 300)
echo "Setting connection route metrics to prioritize WiFi (metric 100) over Cellular (metric 300)..."

# Dynamically find the active connection name for the cellular interface (e.g. usb0)
CELL_CONN=""
if [ -n "$IFACE" ]; then
  CELL_CONN=$(nmcli -t -f DEVICE,NAME connection show --active | grep -E "^${IFACE}:" | head -n1 | cut -d: -f2)
fi

# Deprioritize cellular profile (lte-modem)
sudo nmcli connection modify lte-modem ipv4.route-metric 300 ipv6.route-metric 300 || true

# Deprioritize cellular interface connection if found
if [ -n "$CELL_CONN" ]; then
  echo "Deprioritizing cellular interface connection '$CELL_CONN' (metric 300)..."
  sudo nmcli connection modify "$CELL_CONN" ipv4.route-metric 300 ipv6.route-metric 300 || true
fi

# Deprioritize physical ethernet profiles (including fallback names)
sudo nmcli connection modify netplan-eth0 ipv4.route-metric 300 ipv6.route-metric 300 || true
for eth_conn in $(nmcli -g NAME,TYPE connection show | grep -E ":ethernet" | cut -d: -f1); do
  if [ "$eth_conn" != "$CELL_CONN" ]; then
    sudo nmcli connection modify "$eth_conn" ipv4.route-metric 300 ipv6.route-metric 300 || true
  fi
done

# Prioritize WiFi profiles (metric 100)
for conn in $(nmcli -g NAME,TYPE connection show | grep :vpn -v | grep -E ":802-11-wireless|:wireless|:wifi" | cut -d: -f1); do
  echo "Prioritizing WiFi connection: $conn"
  sudo nmcli connection modify "$conn" ipv4.route-metric 100 ipv6.route-metric 100 || true
done
sudo nmcli connection reload || true

# 3. Apply route metrics immediately by reapplying to devices
echo "Applying route metrics changes..."
if [ -n "$IFACE" ]; then
  echo "  Reapplying settings to cellular interface '$IFACE'..."
  sudo nmcli device reapply "$IFACE" 2>/dev/null || { [ -n "$CELL_CONN" ] && sudo nmcli connection up "$CELL_CONN" 2>/dev/null; } || true
fi
for dev in $(nmcli -t -f DEVICE,TYPE device | grep ":wifi" | cut -d: -f1); do
  echo "  Reapplying settings to WiFi interface '$dev'..."
  sudo nmcli device reapply "$dev" 2>/dev/null || true
done

echo "Network metrics configuration complete."

echo "Active Routing Table (ip route show):"
ip route show
