# DOMOBOI NILM Edge Client

An edge-based Non-Intrusive Load Monitoring (NILM) software stack for the Raspberry Pi 5, designed to measure high-frequency electrical mains telemetry, detect appliance-level events (ON/OFF transients), and transmit telemetry/events over cellular LTE to a central Django backend.

---

## 1. System Architecture

The client operates on a decoupled data capture and analysis pipeline:
* **Capture (SPI Thread)**: Polls the ATM90E36 hardware registers at 10Hz, reading voltage and current.
* **Bilateral Step Detection**: Instead of simple delta thresholding, the processor splits a 1.0-second sliding buffer (10 samples) into pre-event (0.5s) and post-event (0.5s) windows. A load step change (transient) is declared **only if both windows are stable** (standard deviation below `stability_threshold_watts`), filtering out high-frequency startup spikes or register noise.
* **Event Segmenting**: When a positive step (ON transient) is detected, the client starts buffering the raw 10Hz readings in memory. Once a matching negative step (OFF transient) is paired, the segment is closed.
* **Server-Side NILM Processing**: The client does **not** classify appliances locally. It transmits the complete raw segment readings array as a JSON payload to the Django server (`/nilm/api/measurements/`), which executes the heavy classification and stores the resulting event.
* **Store-and-Forward Buffering**: If the LTE connection drops, payloads are automatically stored in a local SQLite database (`offline_measurements`) and synced once the connection is restored.

---

## 2. Hardware Pinout & Wiring

* **SPI Interface**: Standard SPI0 (MOSI on GPIO 10, MISO on GPIO 9, SCLK on GPIO 11).
* **I2C Interface**: Standard I2C1 (SDA on GPIO 2, SCL on GPIO 3).
* **Chip Select (/CS)**: Driven via the onboard **PCA9671 I2C GPIO Expander** mapped to address **`0x21`** (A0 pulled high). Direct raw 2-byte I2C block writes are used to drive the expander without offset sub-registers. Chip Select is mapped to expander output pin **9**.
* **AC Reference**: Connect an 8V-12V AC RMS voltage reference transformer to V1P/V1N. If absent, the driver falls back to software estimation using nominal voltage (e.g., 230V).
* **Current Clamps**: Connect SCT-013-000 CT clamps to CT1 (Phase A / `IrmsA`). The clamp **must surround the brown (Live) wire only**, never the entire cable, to avoid magnetic field cancellation.
* **LTE Antenna Connector**: Ensure the internal U.FL pigtail coaxial connector on the Quectel mini-PCIe module is snapped into the **MAIN** (LTE MAIN) port, not the DIV/AUX port. Connecting it to AUX/DIV will prevent the modem from registering on the cellular network.

---

## 3. Raspberry Pi 5 first setup

- Enable RPI CONNECT
rpi-connect on
rpi-connect signin

- Install git
```
sudo apt install git
```

- Download domoboi-edge package

```
git clone https://github.com/alabarga/domoboi-edge.git && cd domoboi-edge && sudo ./setup.sh
```

- CHeck active connections

```
nmcli connection show --active
```

---

## 4. Automated Installation & Setup

We provide a `setup.sh` script that automates the deployment:
1. **System Packages**: Installs `python3-pip`, `python3-venv`, `libatlas-base-dev`, `i2c-tools`, `cmake`, and `build-essential`.
2. **Locales**: Generates Spanish locale definitions (`es_ES.UTF-8`) to prevent runtime encoding exceptions.
3. **Hardware Modules**: Appends `dtparam=spi=on` and `dtparam=i2c_arm=on` to `/boot/firmware/config.txt` to enable SPI and I2C buses on reboot.
4. **RPi 5 Current Override**: Appends `usb_max_current_enable=1` to the firmware configuration.
5. **Permissions & Python Environment**: Initializes a python virtual environment (`.venv`), chowns it to the local user to prevent permissions errors, and installs dependencies (`rich`, `smbus2`, `spidev`, `pyyaml`, `aiohttp`).
6. **C Utility Compiling**: Generates and compiles `testctread` in `testctread/build`.
7. **Systemd Service**: Registers and enables the `domoboi-edge` service.
8. **Modem Configuration**: Auto-detects whether the Quectel control interface is on `/dev/ttyUSB2` or `/dev/ttyUSB3` and configures it into ECM mode, loading SIM PIN and APN credentials directly from `config.yaml`.
9. **Automatic Configuration Provisioning**: Automatically generates `config.yaml` from `config.example.yaml` (if not already present) and reads configuration parameters from it without interactive prompting.

To install:
```bash
# 1. Copy the example configuration file
cp config.example.yaml config.yaml

# 2. Open and fill in the required parameters (API token, base URL, and modem SIM PIN/APN)
nano config.yaml

# 3. Make setup script executable and run it
chmod +x setup.sh
sudo ./setup.sh
```



### USB Current Override

The Quectel LTE mini-PCIe module can consume significant current under weak cellular coverage, which exceeds the default Raspberry Pi 5 USB current limits.

To prevent the LTE modem from resetting or brown-outing the Pi, you **must override the USB current limit** to allow 1.6A output. Add the following line to the end of your Pi's configuration file:
```ini
# Edit /boot/firmware/config.txt (or /boot/config.txt on older OS versions)
usb_max_current_enable=1
```

### Network Interface Configuration (ECM Mode)
The modem is configured in **Ethernet Control Model (ECM)** mode. It exposes a virtual network card interface named **`usb0`**:
* **Modem IP**: `192.168.225.1` (serves as the gateway).
* **RPi IP**: Assigned dynamically via DHCP (typically `192.168.225.x`).
* Ensure system routing sends outbound requests through `usb0` when testing cellular connectivity:
  ```bash
  ping -I usb0 google.com
  ```

### SIM Card PIN Unlock
If your SIM card is locked, permanent locking must be disabled. This is handled during setup using:
```bash
# Unlock SIM via QMI tool (assuming PIN 1121)
sudo qmicli -d /dev/cdc-wdm0 --uim-verify-pin=PIN1,1121
```

---

## 5. Configuration (`config.yaml`)

Configure gains, thresholds, and endpoints in `config.yaml`:
```yaml
mains:
  nominal_voltage: 230
  line_frequency: 50

calibration:
  ugain: 20200            # Voltage gain
  igain_a: 65535          # Max 16-bit register value (ATM90E36 register limit)
  current_multiplier: 2.1973 # Software multiplier to achieve 144000 equivalent gain (65535 * 2.1973)

nilm:
  sampling_interval_sec: 0.1
  transient_threshold_watts: 10.0 # Umbral de escalón de potencia para detectar transitorios
  stability_threshold_watts: 3.0  # Desviación estándar máxima para considerar estable un estado

api:
  base_url: "http://192.168.1.133:8000"
  token: "40ab35abf09e99c44313c8bd2b47e71b32d0b30ca801480c"
```

---

## 6. Diagnostic & Testing Tools

We provide diagnostic utilities to verify hardware communication:

* **Low-Level SPI/I2C Scan (`diagnose_spi.py`)**:
  Performs an I2C scan for the expander, tests the CS toggling mechanism, tries all SPI modes/speeds, and brute-forces expander pins to verify registration connectivity.
  ```bash
  python3 diagnose_spi.py
  ```
* **Real-time Phase Reading (`diagnose_ct.py`)**:
  Continuously polls and formats RMS Current, Voltage, Active Power, and Estimated Power on all 3 phases. Useful for verifying clamp directionality and physical port mapping.
  ```bash
  python3 diagnose_ct.py
  ```
* **Native C Utility (`testctread`)**:
  Bypasses Python entirely to read current metering values directly from SPI.
  ```bash
  cd testctread/build
  ./testctread
  ```

---

## 7. Operational Management & Logs

### Checking Services Status
Verify the status of the systemd background monitoring daemon on the Raspberry Pi:
```bash
sudo systemctl status domoboi-edge
```

### Viewing Logs in Real-Time
Follow log outputs to confirm SPI register polling, baseline calculations, and cell transmission:
```bash
# View systemd service logs
journalctl -u domoboi-edge -f

# View application-specific log files
tail -f data/client.log
```

### Service Controls
Control the edge client background task:
```bash
sudo systemctl start domoboi-edge
sudo systemctl stop domoboi-edge
sudo systemctl restart domoboi-edge
```

### Network Interface Diagnostics
On modern Raspberry Pi OS, USB network cards/modems use predictable interface names (e.g., `enp0s20u2`, `enx...`) instead of `usb0`. Use the following commands to locate and verify your cellular connection:
```bash
# List all network devices and status
nmcli device

# List all active NetworkManager connection profiles
nmcli connection show --active

# Inspect IP address configurations on a specific device
ip addr show usb0  # replace 'usb0' with the interface name from nmcli
```

### Network Priority (WiFi vs. Cellular Backup)
To prevent generating high cellular billing charges when local WiFi is available, the system prioritizes connection routes by adjusting the NetworkManager routing metric (lower metric = higher priority):
* **WiFi Connections**: Metric set to `100` (preferred route).
* **Cellular Modem Connection (`usb0`/`lte-modem`)**: Metric set to `300` (failover backup route).

All internet traffic will be routed over WiFi by default. If WiFi drops out, the system automatically switches to the cellular connection without interruption, and reverts to WiFi immediately once coverage is restored.

To view your current routing priorities, execute:
```bash
ip route show
# Look for 'metric <value>' in the default routes:
# default via 192.168.1.1 dev wlan0 proto dhcp src 192.168.1.50 metric 100
# default via 192.168.225.1 dev usb0 proto dhcp src 192.168.225.26 metric 300
```


### Cellular Modem Diagnostics (AT Commands)
If the virtual cellular interface is not appearing or you have packet delivery issues, query the modem directly via Python serial inline commands on `/dev/ttyUSB2` (or `/dev/ttyUSB3` depending on hardware firmware):

```bash
# 1. Query SIM card PIN status
sudo python3 -c "import time; f=open('/dev/ttyUSB2', 'r+b', buffering=0); f.write(b'AT+CPIN?\r\n'); [time.sleep(0.3) or print(f.read(1024).decode(errors='ignore'), end='') for _ in range(4)]"
# Expected response: +CPIN: READY (unlocked)

# 2. Query cellular registration status
sudo python3 -c "import time; f=open('/dev/ttyUSB2', 'r+b', buffering=0); f.write(b'AT+CREG?\r\n'); [time.sleep(0.3) or print(f.read(1024).decode(errors='ignore'), end='') for _ in range(4)]"
# Response Codes (+CREG: <mode>, <status>):
#   0,1 or 0,5 : Registered successfully (Home / Roaming)
#   0,2        : Not registered, but searching/scanning for a network (Wait 1-2 mins, check antennas)
#   0,0 or 0,3 : Registration denied or not searching (Verify SIM card activation/carrier contract)

# 3. Query packet/data service attachment
sudo python3 -c "import time; f=open('/dev/ttyUSB2', 'r+b', buffering=0); f.write(b'AT+CGATT?\r\n'); [time.sleep(0.3) or print(f.read(1024).decode(errors='ignore'), end='') for _ in range(4)]"
# Expected response: +CGATT: 1 (packet service attached)

# 4. Query active operator network name
sudo python3 -c "import time; f=open('/dev/ttyUSB2', 'r+b', buffering=0); f.write(b'AT+COPS?\r\n'); [time.sleep(0.3) or print(f.read(1024).decode(errors='ignore'), end='') for _ in range(4)]"
# Expected response: +COPS: 0,0,"Operator_Name"
```