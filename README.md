# DOMOBOI NILM Edge Client

An edge-based Non-Intrusive Load Monitoring (NILM) software stack for the Raspberry Pi 5, designed to measure high-frequency electrical mains telemetry, detect appliance-level events (ON/OFF transients), and transmit telemetry/events over cellular LTE to a central Django backend.

## Technology Stack

- **Host Device**: Raspberry Pi 5 (2GB RAM) running Raspberry Pi OS Lite (64-bit).
- **Energy Monitor**: [DitroniX IPEM PiHat](https://github.com/DitroniX/IPEM-PiHat-IoT-Power-Energy-Monitor) based on the **Microchip ATM90E36** polyphase energy metering IC.
- **Cellular Modem**: Quectel mini-PCIe 4G LTE Modem Module (configured in ECM mode).

---

## Hardware Pinout & Wiring

- **SPI Interface**: Standard SPI0 (MOSI on GPIO 10, MISO on GPIO 9, SCLK on GPIO 11).
- **I2C Interface**: Standard I2C1 (SDA on GPIO 2, SCL on GPIO 3).
- **Chip Select (/CS)**: Controlled via the onboard **PCA9671 I2C GPIO Expander** (typically mapped to output pin 9 on the expander). If stacking multiple boards, set DIP switch positions to designate distinct I2C addresses.
- **AC Reference**: Connect an 8V-12V AC RMS voltage reference transformer to V1P/V1N.
- **Current Clamps**: Connect SCT-013-000 CT clamps to CT1 (and optionally CT2/CT3 for multi-phase).

---

## Installation & Setup

We have prepared an automated `setup.sh` script to install all OS dependencies, enable hardware SPI/I2C buses, set up the network, compile the C test utility, and install the edge client as a systemd service.

```bash
chmod +x setup.sh
sudo ./setup.sh
```

---

## Compiling testctread

A native C utility (`testctread`) is provided to verify physical SPI communication and CT measurements directly without running Python.

To compile it using CMake:

```bash
cd testctread
mkdir -p build && cd build
cmake ..
make
```

To run it:
```bash
./testctread
```
This utility will perform 100 consecutive reads of CT channels 1-3, print them to the console, and exit.

---

## Run & Deploy Python Pipeline

The Python pipeline is organized as follows:
`Capture (SPI Thread) -> raw_queue -> Process/NILM (async task) -> event_queue -> Send (async task over LTE)`

### Configurations
Copy the example configuration file and adjust calibration constants for your location:
```bash
cp config.example.yaml config.yaml
nano config.yaml
```

### Manual Run
Activate the virtual environment and launch the main client:
```bash
source .venv/bin/activate
python3 client.py
```

### Running as a System Service
The setup script installs a systemd unit. Manage the service using:
```bash
sudo systemctl status domoboi-edge
sudo systemctl restart domoboi-edge
journalctl -u domoboi-edge -f
```

---

## Calibration

For accurate energy readings, you must calibrate the voltage and current gains in `config.yaml`:
1. **Voltage Calibration (`ugain`)**:
   - Compare the reported voltage against a reference multimeter.
   - Adjust `ugain` using: `new_ugain = old_ugain * (true_voltage / reported_voltage)`.
2. **Current Calibration (`igain`)**:
   - Apply a known resistive load (e.g. electric heater).
   - Adjust `igain` using: `new_igain = old_igain * (true_current / reported_current)`.