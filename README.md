# DOMOBOI NILM Edge Client

An edge-based Non-Intrusive Load Monitoring (NILM) software stack for the Raspberry Pi 5, designed to measure high-frequency electrical mains telemetry, detect appliance-level events (ON/OFF transients), and transmit telemetry/events over cellular LTE to a central Django backend.

---

## 1. System Architecture

The client operates on a decoupled data capture and analysis pipeline (`client.py` → `nilm_processor.py` → `sender.py`):
* **Capture (SPI Thread)**: A dedicated thread polls the ATM90E36 every `sampling_interval_sec` (default 0.1s = 10Hz). Readings from the first `startup_guard_sec` (default 3s) after chip init are discarded to filter init noise.
* **Estimated Power**: No AC voltage reference is currently used, so active power is computed as `Irms × nominal_voltage` (PF = 1, Q = 0). The CT channel is selected from the calibration gains: CT2 (phase B) if `igain_b > 0`, else CT3 (phase C) if `igain_c > 0`, else CT1 (phase A).
* **Adaptive Transition Detection**: The processor keeps a 10-sample (1s) baseline. When the baseline is stable (std ≤ `max(stability_threshold_watts, baseline × stability_percentage)`) and a new sample deviates by ≥ `max(transient_threshold_watts, baseline × threshold_percentage)`, it records a 3-second window. The transition is confirmed if the mean of the last 10 samples still differs from the pre-event baseline by at least the threshold; then a 1.5s cooldown applies.
* **Independent ON/OFF Events**: Each confirmed transition (ON = positive step, OFF = negative step) is sent on its own. ON/OFF pairing is **not** done on the device.
* **Server-Side NILM Processing**: The client does **not** classify appliances locally. Each transition is POSTed to the Django server (`/nilm/api/measurements/`) as JSON: `device_id`, `start_time`, `end_time`, `readings` (raw window), `value` (signed ΔP in W) and `features` (`avg`, `min`, `max`, `std`).
* **Store-and-Forward Buffering**: If the connection drops, payloads are stored in a local SQLite database (`buffer.db_path`, table `offline_measurements`) and resynced every `sync_interval_sec`.

---

## 2. Hardware Pinout & Wiring

* **SPI Interface**: Standard SPI0 (MOSI on GPIO 10, MISO on GPIO 9, SCLK on GPIO 11).
* **I2C Interface**: Standard I2C1 (SDA on GPIO 2, SCL on GPIO 3).
* **Chip Select (/CS)**: Driven via the onboard **PCA9671 I2C GPIO Expander** mapped to address **`0x21`** (A0 pulled high). Direct raw 2-byte I2C block writes are used to drive the expander without offset sub-registers. Chip Select is mapped to expander output pin **9**.
* **AC Reference**: Connect an 8V-12V AC RMS voltage reference transformer to V1P/V1N. Note: the monitoring service currently always estimates power from the configured nominal voltage (e.g., 230V), even when a reference is connected. Only `--smoke-test` uses the measured voltage and power registers (when V ≥ 5V).
* **Current Clamps**: Connect SCT-013-000 CT clamps to CT1 (Phase A / `IrmsA`), or to CT2/CT3 with the matching `igain_b`/`igain_c` set to a non-zero value. The clamp **must surround the brown (Live) wire only**, never the entire cable, to avoid magnetic field cancellation.
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
git clone https://github.com/alabarga/domoboi-edge.git 
cd domoboi-edge 

```

- CHeck active connections

```
nmcli connection show --active
```

---

## 4. Automated Installation & Setup

We divide deployment and configuration into two sequential phases to handle the physical reboot required by the Raspberry Pi 5 USB current firmware overrides:

### Phase 1: Environment & System Setup (`setup.sh`)
This script configures the system base and registers the core service:
1. **System Packages**: Installs `python3-pip`, `python3-venv`, `libatlas-base-dev`, `i2c-tools`, `cmake`, and `build-essential`.
2. **Locales**: Generates Spanish locale definitions (`es_ES.UTF-8`) to prevent runtime encoding exceptions.
3. **Hardware Modules**: Appends `dtparam=spi=on` and `dtparam=i2c_arm=on` to `/boot/firmware/config.txt` to enable SPI and I2C buses on reboot.
4. **RPi 5 Current Override**: Appends `usb_max_current_enable=1` to the firmware configuration to allow 1.6A output on USB.
5. **Permissions & Python Environment**: Initializes a python virtual environment (`.venv`), chowns it to the local user to prevent permissions errors, and installs dependencies (`rich`, `smbus2`, `spidev`, `pyyaml`, `aiohttp`).
6. **C Utility Compiling**: Generates and compiles `testctread` in `testctread/build`.
7. **Systemd Service**: Registers and enables the `domoboi-edge` service.

### Phase 2: Modem, Registration, & Network Priorities (`net.sh`)
This script configures the cellular connections, performs checks, and sets connection metric priorities:
1. **Modem Configuration**: Auto-detects whether the Quectel control interface is on `/dev/ttyUSB2` or `/dev/ttyUSB3` and configures it into ECM mode, loading SIM PIN and APN credentials directly from `config.yaml`.
2. **Network Registration Wait Loop**: Diagnostics loop that waits for modem registry attachment and reports signal RSSI/BER telemetry in real-time.
3. **Network Priority (WiFi vs. Cellular Backup)**: Configures NetworkManager route metrics to prioritize WiFi (metric 100) over cellular (metric 300) to prevent cellular data charges.
4. **Verification**: Initiates a ping test to Google via the cellular interface.

### Deployment Sequence
To install and deploy:
```bash
# 1. Copy the example configuration file
cp config.example.yaml config.yaml

# 2. Open and fill in the required parameters (API token, base URL, and optionally modem SIM PIN/APN)
nano config.yaml

# 3. Run the system setup script
chmod +x setup.sh net.sh start.sh
sudo ./setup.sh

# 4. REBOOT the Raspberry Pi 5 to apply the USB current overrides!
sudo reboot

# 5. After reboot, configure the modem and network priorities
sudo ./net.sh

# 6. Start the energy monitoring service
sudo ./start.sh
```

### USB Current Override
The Quectel LTE mini-PCIe module can consume significant current under weak cellular coverage, which exceeds the default Raspberry Pi 5 USB current limits.
To prevent the LTE modem from resetting or brown-outing the Pi, you **must override the USB current limit** to allow 1.6A output. This is handled automatically by `setup.sh` appending `usb_max_current_enable=1` to `/boot/firmware/config.txt`.

### Network Interface Configuration (ECM Mode)
The modem is configured in **Ethernet Control Model (ECM)** mode. It exposes a virtual network card interface named **`usb0`**:
* **Modem IP**: `192.168.225.1` (serves as the gateway).
* **RPi IP**: Assigned dynamically via DHCP (typically `192.168.225.x`).
* Ensure system routing sends outbound requests through `usb0` when testing cellular connectivity:
  ```bash
  ping -I usb0 google.com
  ```

---

## 5. Configuration (`config.yaml`)

`config.yaml` is git-ignored; create it from `config.example.yaml`, which documents every option. Main sections:

* `device_id`: optional, defaults to the hostname.
* `mains`: `nominal_voltage` (used for power estimation) and `line_frequency`.
* `spi` / `i2c`: bus settings and the PCA9671 expander address and CS pin.
* `calibration`: `ugain`, `igain_a/b/c` (16-bit registers, max 65535; a non-zero `igain_b`/`igain_c` selects that CT channel), and `current_multiplier` (software gain on top of the register gain).
* `nilm`: `sampling_interval_sec`, `transient_threshold_watts`, `threshold_percentage`, `stability_threshold_watts`, `stability_percentage`, optional `startup_guard_sec`.
* `api`: `base_url`, `token`, `timeout_sec`.
* `buffer`: `db_path`, `sync_interval_sec`.
* `modem`: APN credentials, read by `net.sh`.

```yaml
calibration:
  ugain: 20200
  igain_a: 65535            # ATM90E36 register limit
  igain_b: 0
  igain_c: 0
  current_multiplier: 2.0

nilm:
  sampling_interval_sec: 0.1
  transient_threshold_watts: 40.0
  threshold_percentage: 0.015
  stability_threshold_watts: 8.0
  stability_percentage: 0.05

api:
  base_url: "http://suara.domoboi.es"
  token: "<your-api-token>"
```

---

## 6. Diagnostic & Testing Tools

We provide diagnostic utilities to verify hardware communication:

* **Smoke Test (`client.py --smoke-test`)**:
  Initializes the chip, prints 10 readings at 1Hz and exits.
  ```bash
  .venv/bin/python3 client.py --smoke-test
  ```
* **Backend Registration Check (`echo.py`)**:
  Checks with the server that this `device_id` is registered (`GET /nilm/api/device/`) and prints its model and location.
  ```bash
  python3 echo.py
  ```
* **Unit Tests (`test_nilm.py`)**:
  Exercises transition detection and offline buffering/sync against a mock server. No hardware needed.
  ```bash
  python3 test_nilm.py
  ```

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
sudo journalctl -u domoboi-edge.service -f
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
To prevent generating high cellular billing charges when local WiFi is available, we provide a standalone script `net.sh` that prioritizes connection routes by adjusting the NetworkManager routing metric (lower metric = higher priority):
* **WiFi Connections**: Metric set to `100` (preferred route).
* **Cellular Modem Connection (`usb0`/`lte-modem`)**: Metric set to `300` (failover backup route).

All internet traffic will be routed over WiFi by default. If WiFi drops out, the system automatically switches to the cellular connection without interruption, and reverts to WiFi immediately once coverage is restored.

To apply this routing priority, make the script executable and run it as root:
```bash
chmod +x net.sh
sudo ./net.sh
```

To view your current routing priorities:
```bash
ip route show
# Look for 'metric <value>' in the default routes:
# default via 192.168.1.1 dev wlan0 proto dhcp src 192.168.1.50 metric 100
# default via 192.168.225.1 dev usb0 proto dhcp src 192.168.225.26 metric 300
```


### Cellular Modem Diagnostics (AT Commands)
If the virtual cellular interface is not appearing or you have packet delivery issues, query the modem directly via Python serial inline commands on `/dev/ttyUSB3` (or `/dev/ttyUSB2` depending on hardware firmware):

```bash
# 1. Query SIM card PIN status
sudo python3 -c "import time, os; f=open('/dev/ttyUSB3', 'r+b', buffering=0); os.set_blocking(f.fileno(), False); f.read(1024); f.write(b'AT+CPIN?\r\n'); [time.sleep(0.3) or print((f.read(1024) or b'').decode(errors='ignore'), end='') for _ in range(4)]"
# Expected response: +CPIN: READY (unlocked)

# 2. Query cellular registration status
sudo python3 -c "import time, os; f=open('/dev/ttyUSB3', 'r+b', buffering=0); os.set_blocking(f.fileno(), False); f.read(1024); f.write(b'AT+CREG?\r\n'); [time.sleep(0.3) or print((f.read(1024) or b'').decode(errors='ignore'), end='') for _ in range(4)]"
# Response Codes (+CREG: <mode>, <status>):
#   0,1 or 0,5 : Registered successfully (Home / Roaming)
#   0,2        : Not registered, but searching/scanning for a network (Wait 1-2 mins, check antennas)
#   0,0 or 0,3 : Registration denied or not searching (Verify SIM card activation/carrier contract)

# 3. Query packet/data service attachment
sudo python3 -c "import time, os; f=open('/dev/ttyUSB3', 'r+b', buffering=0); os.set_blocking(f.fileno(), False); f.read(1024); f.write(b'AT+CGATT?\r\n'); [time.sleep(0.3) or print((f.read(1024) or b'').decode(errors='ignore'), end='') for _ in range(4)]"
# Expected response: +CGATT: 1 (packet service attached)

# 4. Query active operator network name
sudo python3 -c "import time, os; f=open('/dev/ttyUSB3', 'r+b', buffering=0); os.set_blocking(f.fileno(), False); f.read(1024); f.write(b'AT+COPS?\r\n'); [time.sleep(0.3) or print((f.read(1024) or b'').decode(errors='ignore'), end='') for _ in range(4)]"
# Expected response: +COPS: 0,0,"Operator_Name"

# 5. Query signal strength (RSSI)
sudo python3 -c "import time, os; f=open('/dev/ttyUSB3', 'r+b', buffering=0); os.set_blocking(f.fileno(), False); f.read(1024); f.write(b'AT+CSQ\r\n'); [time.sleep(0.3) or print((f.read(1024) or b'').decode(errors='ignore'), end='') for _ in range(4)]"
# Expected response: +CSQ: <rssi>,<ber> (e.g., +CSQ: 18,99; where rssi 0-31; 31 is best, 99 is unknown)
```

### Service Logs Management (journalctl)

When running the client as a background service via systemd, the interactive terminal UI is disabled (`--no-ui` flag) and all output is handled by the system journal. You can view, search, and manage these logs using `journalctl`:

* **Tail live logs**:
  ```bash
  sudo journalctl -u domoboi-edge.service -f
  ```
* **View the last 100 log entries**:
  ```bash
  sudo journalctl -u domoboi-edge.service -n 100 --no-pager
  ```
* **Check log disk usage**:
  ```bash
  sudo journalctl --disk-usage
  ```
* **Clean logs (vacuuming) to free disk space**:
  ```bash
  # Keep only the last 7 days of logs:
  sudo journalctl --vacuum-time=7d
  
  # Keep only up to 50MB of logs:
  sudo journalctl --vacuum-size=50M
  ```
```