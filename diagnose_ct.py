#!/usr/bin/env python3
"""
diagnose_ct.py - Diagnostic tool to read ALL CT channels and raw registers from the ATM90E36.
Run this on the Raspberry Pi to determine:
  1. Which CT input the clamp is physically connected to (A, B, or C)
  2. Whether there's a directionality issue (negative power values)
  3. Whether the current clamp is registering any readings at all

Usage:
  python3 diagnose_ct.py

Turn your kettle ON/OFF while this runs to observe changes.
"""

import time
import yaml
import sys

# Add parent dir to path
sys.path.insert(0, '.')

from atm90e36 import ATM90E36

def main():
    with open("config.yaml") as f:
        config = yaml.safe_load(f)
    
    chip = ATM90E36(config)
    chip.initialize()
    
    print("=" * 80)
    print("  DOMOBOI CT CLAMP DIAGNOSTIC TOOL")
    print("  Turn your kettle ON/OFF during this test")
    print("  Press Ctrl+C to stop")
    print("=" * 80)
    print()
    
    # Read all 3 phases continuously
    sample = 0
    try:
        while True:
            sample += 1
            
            # Read all RMS current registers directly
            irms_a_raw = chip.read_reg(0xDD)
            irms_b_raw = chip.read_reg(0xDE)
            irms_c_raw = chip.read_reg(0xDF)
            
            # Read all RMS voltage registers
            urms_a_raw = chip.read_reg(0xD9)
            urms_b_raw = chip.read_reg(0xDA)
            urms_c_raw = chip.read_reg(0xDB)
            
            # Read active power registers (signed)
            pmean_a_raw = chip.read_reg(0xB1)
            pmean_b_raw = chip.read_reg(0xB2)
            pmean_c_raw = chip.read_reg(0xB3)
            
            # Convert current RMS: register * 0.001 = Amperes (applying current_multiplier calibration)
            multiplier = config.get("calibration", {}).get("current_multiplier", 1.0)
            irms_a = irms_a_raw * 0.001 * multiplier
            irms_b = irms_b_raw * 0.001 * multiplier
            irms_c = irms_c_raw * 0.001 * multiplier
            
            # Convert voltage RMS: register * 0.01 = Volts
            urms_a = urms_a_raw * 0.01
            urms_b = urms_b_raw * 0.01
            urms_c = urms_c_raw * 0.01
            
            # Convert active power (signed two's complement, calibrated)
            def signed16(v):
                return v - 0x10000 if v & 0x8000 else v
            
            pmean_a = signed16(pmean_a_raw) * multiplier
            pmean_b = signed16(pmean_b_raw) * multiplier
            pmean_c = signed16(pmean_c_raw) * multiplier
            
            # Estimated power (current * nominal voltage)
            nominal_v = config.get("mains", {}).get("nominal_voltage", 230.0)
            est_power_a = irms_a * nominal_v
            est_power_b = irms_b * nominal_v
            est_power_c = irms_c * nominal_v
            
            print(f"\r--- Sample {sample} ---")
            print(f"  Phase A | Irms: {irms_a:8.3f}A (raw=0x{irms_a_raw:04X}) | Urms: {urms_a:7.2f}V (raw=0x{urms_a_raw:04X}) | Pmean: {pmean_a:6d} (raw=0x{pmean_a_raw:04X}) | Est.P: {est_power_a:8.1f}W")
            print(f"  Phase B | Irms: {irms_b:8.3f}A (raw=0x{irms_b_raw:04X}) | Urms: {urms_b:7.2f}V (raw=0x{urms_b_raw:04X}) | Pmean: {pmean_b:6d} (raw=0x{pmean_b_raw:04X}) | Est.P: {est_power_b:8.1f}W")
            print(f"  Phase C | Irms: {irms_c:8.3f}A (raw=0x{irms_c_raw:04X}) | Urms: {urms_c:7.2f}V (raw=0x{urms_c_raw:04X}) | Pmean: {pmean_c:6d} (raw=0x{pmean_c_raw:04X}) | Est.P: {est_power_c:8.1f}W")
            
            # Highlight which channel has the highest current
            max_irms = max(irms_a, irms_b, irms_c)
            if max_irms > 0.05:  # More than 50mA
                if max_irms == irms_a:
                    active = "A"
                elif max_irms == irms_b:
                    active = "B"
                else:
                    active = "C"
                print(f"  >>> ACTIVE CHANNEL: Phase {active} ({max_irms:.3f}A / {max_irms * nominal_v:.1f}W)")
            else:
                print(f"  >>> No significant current detected on any channel (max={max_irms:.4f}A)")
            
            print()
            time.sleep(1.0)
            
    except KeyboardInterrupt:
        print("\nDiagnostic stopped.")
    finally:
        chip.close()

if __name__ == "__main__":
    main()
