#!/usr/bin/env python3
"""
diagnose_spi.py - Low-level SPI/I2C diagnostic for the IPEM PiHat + ATM90E36.
Tests communication layer-by-layer to find where the failure is.

Run as: python3 diagnose_spi.py
"""

import sys
import time

def test_i2c():
    """Step 1: Verify the PCA9671 I2C GPIO expander is reachable."""
    print("=" * 70)
    print("STEP 1: I2C Bus Scan — Looking for PCA9671 GPIO Expander")
    print("=" * 70)
    
    try:
        import smbus2
        bus = smbus2.SMBus(1)
        
        found = []
        for addr in range(0x08, 0x78):
            try:
                bus.read_byte(addr)
                found.append(addr)
            except:
                pass
        
        bus.close()
        
        if found:
            print(f"  Found {len(found)} device(s) on I2C bus 1:")
            for addr in found:
                label = ""
                if addr == 0x20:
                    label = " ← PCA9671 (default address)"
                elif addr == 0x21:
                    label = " ← PCA9671 (A0=1 address)"
                elif addr == 0x22:
                    label = " ← PCA9671 (A1=1 address)"
                print(f"    0x{addr:02X}{label}")
            
            if 0x21 in found:
                print("\n  ✅ PCA9671 found at 0x21 (matches config.yaml)")
                return 0x21
            elif 0x20 in found:
                print("\n  ⚠️  PCA9671 found at 0x20, but config.yaml says 0x21!")
                print("     → Update config.yaml: pca9671_addr: 0x20")
                return 0x20
            else:
                print(f"\n  ⚠️  No PCA9671 found at 0x20 or 0x21.")
                print(f"     Devices found at: {[hex(a) for a in found]}")
                return found[0] if found else None
        else:
            print("  ❌ No I2C devices found at all!")
            print("     Check: Is I2C enabled? (sudo raspi-config → Interface Options → I2C)")
            print("     Check: Is the IPEM PiHat properly seated on the GPIO header?")
            return None
            
    except ImportError:
        print("  ❌ smbus2 not installed. Run: pip install smbus2")
        return None
    except Exception as e:
        print(f"  ❌ I2C error: {e}")
        return None


def test_pca_cs_toggle(pca_addr):
    """Step 2: Toggle the CS line through the PCA9671 and verify it works."""
    print()
    print("=" * 70)
    print(f"STEP 2: PCA9671 CS Toggle Test (addr=0x{pca_addr:02X})")
    print("=" * 70)
    
    try:
        import smbus2
        bus = smbus2.SMBus(1)
        
        cs_bit = 9  # Bit 9 = CS for ATM90E36 on IPEM board
        cs_mask = 1 << cs_bit
        
        # Idle state: all high (CS deasserted)
        idle = 0x03FF
        # Active state: CS low
        active = idle & ~cs_mask
        
        p0_idle = idle & 0xFF
        p1_idle = (idle >> 8) & 0xFF
        p0_active = active & 0xFF
        p1_active = (active >> 8) & 0xFF
        
        print(f"  CS bit: {cs_bit} (mask=0x{cs_mask:04X})")
        print(f"  Idle state:  0x{idle:04X} (P0=0x{p0_idle:02X} P1=0x{p1_idle:02X}) — CS HIGH")
        print(f"  Active state: 0x{active:04X} (P0=0x{p0_active:02X} P1=0x{p1_active:02X}) — CS LOW")
        
        # Write idle
        bus.write_i2c_block_data(pca_addr, p0_idle, [p1_idle])
        time.sleep(0.01)
        print("  ✅ Wrote IDLE state (CS HIGH) successfully")
        
        # Write active  
        bus.write_i2c_block_data(pca_addr, p0_active, [p1_active])
        time.sleep(0.01)
        print("  ✅ Wrote ACTIVE state (CS LOW) successfully")
        
        # Return to idle
        bus.write_i2c_block_data(pca_addr, p0_idle, [p1_idle])
        time.sleep(0.01)
        print("  ✅ Returned to IDLE state")
        
        bus.close()
        return True
        
    except Exception as e:
        print(f"  ❌ PCA9671 CS toggle failed: {e}")
        return False


def test_spi_raw_read(pca_addr):
    """Step 3: Try raw SPI register reads with different configurations."""
    print()
    print("=" * 70)
    print("STEP 3: Raw SPI Register Reads — Testing All Modes")
    print("=" * 70)
    
    try:
        import spidev
        import smbus2
    except ImportError as e:
        print(f"  ❌ Missing module: {e}")
        return
    
    i2c = smbus2.SMBus(1)
    
    cs_bit = 9
    cs_mask = 1 << cs_bit
    idle = 0x03FF
    active = idle & ~cs_mask
    
    def pca_write(state):
        p0 = state & 0xFF
        p1 = (state >> 8) & 0xFF
        i2c.write_i2c_block_data(pca_addr, p0, [p1])
    
    def pca_cs_assert():
        pca_write(active)
        time.sleep(0.001)
    
    def pca_cs_deassert():
        pca_write(idle)
        time.sleep(0.001)
    
    # Test register: SYS_STATUS0 (0x01) — should have some non-zero init value
    # Test register: TEMP (0xFC) — chip temperature, usually room temp ~25
    test_regs = [
        (0x01, "SYS_STATUS0"),
        (0x02, "SYS_STATUS1"),
        (0x2C, "LAST_SPI_DATA"),
        (0xFC, "TEMP"),
        (0xD9, "URMS_A"),
        (0xDD, "IRMS_A"),
    ]
    
    # Try SPI modes 0 and 3, and different speeds
    for spi_mode in [3, 0]:
        for speed in [200000, 100000, 50000]:
            spi = spidev.SpiDev()
            spi.open(0, 0)
            spi.max_speed_hz = speed
            spi.mode = spi_mode
            
            print(f"\n  --- SPI Mode {spi_mode}, Speed {speed // 1000}kHz ---")
            
            all_ffff = True
            all_zero = True
            
            for reg_addr, reg_name in test_regs:
                # ATM90E36 read: bit7=1 for read, bits 6:0 = address
                cmd_byte = 0x80 | (reg_addr & 0x7F)
                
                pca_cs_assert()
                result = spi.xfer2([cmd_byte, 0x00, 0x00])
                pca_cs_deassert()
                
                val = (result[1] << 8) | result[2]
                
                if val != 0xFFFF:
                    all_ffff = False
                if val != 0x0000:
                    all_zero = False
                
                flag = ""
                if val == 0xFFFF:
                    flag = " ← MISO floating HIGH (no communication)"
                elif val == 0x0000:
                    flag = " ← all zeros"
                    
                print(f"    Reg 0x{reg_addr:02X} ({reg_name:15s}): 0x{val:04X} ({val:5d}){flag}")
            
            spi.close()
            
            if all_ffff:
                print(f"    ❌ ALL registers return 0xFFFF — chip not responding in this mode")
            elif all_zero:
                print(f"    ⚠️  All registers return 0x0000 — possible but unusual")
            else:
                print(f"    ✅ Got non-trivial data! This SPI configuration WORKS")
                i2c.close()
                return spi_mode, speed
    
    # Also try WITHOUT I2C CS (using hardware CE0 directly)
    print(f"\n  --- Testing WITHOUT I2C CS (hardware CE0 only) ---")
    for spi_mode in [3, 0]:
        spi = spidev.SpiDev()
        spi.open(0, 0)
        spi.max_speed_hz = 200000
        spi.mode = spi_mode
        
        print(f"\n  --- HW CS + SPI Mode {spi_mode}, Speed 200kHz ---")
        
        all_ffff = True
        for reg_addr, reg_name in test_regs:
            cmd_byte = 0x80 | (reg_addr & 0x7F)
            # No I2C CS toggle — let hardware CE0 handle it
            result = spi.xfer2([cmd_byte, 0x00, 0x00])
            val = (result[1] << 8) | result[2]
            
            if val != 0xFFFF:
                all_ffff = False
            
            flag = " ← FLOATING" if val == 0xFFFF else ""
            print(f"    Reg 0x{reg_addr:02X} ({reg_name:15s}): 0x{val:04X} ({val:5d}){flag}")
        
        spi.close()
        
        if not all_ffff:
            print(f"    ✅ Hardware CS works! I2C CS expander may be misconfigured.")
            i2c.close()
            return spi_mode, 200000
    
    # Try different CS bits on PCA9671
    print(f"\n  --- Testing different CS bits on PCA9671 ---")
    spi = spidev.SpiDev()
    spi.open(0, 0)
    spi.max_speed_hz = 200000
    spi.mode = 3
    
    for test_bit in range(0, 16):
        test_mask = 1 << test_bit
        test_active = idle & ~test_mask
        
        p0 = test_active & 0xFF
        p1 = (test_active >> 8) & 0xFF
        i2c.write_i2c_block_data(pca_addr, p0, [p1])
        time.sleep(0.002)
        
        cmd_byte = 0x80 | (0x01 & 0x7F)  # Read SYS_STATUS0
        result = spi.xfer2([cmd_byte, 0x00, 0x00])
        val = (result[1] << 8) | result[2]
        
        # Deassert
        pca_write(idle)
        time.sleep(0.001)
        
        flag = ""
        if val != 0xFFFF:
            flag = f" ← ✅ RESPONDS! Use board_cs_bit: {test_bit}"
        
        print(f"    CS bit {test_bit:2d} (mask=0x{test_mask:04X}): SYS_STATUS0 = 0x{val:04X}{flag}")
    
    spi.close()
    i2c.close()
    
    print("\n  If no CS bit works, the SPI bus may have a hardware issue.")
    return None


def main():
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║      DOMOBOI IPEM PiHat — SPI/I2C Hardware Diagnostic     ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()
    
    # Step 1
    pca_addr = test_i2c()
    if pca_addr is None:
        print("\n❌ Cannot proceed without I2C. Fix I2C first.")
        sys.exit(1)
    
    # Step 2
    cs_ok = test_pca_cs_toggle(pca_addr)
    if not cs_ok:
        print("\n❌ Cannot toggle CS. Fix PCA9671 communication first.")
        sys.exit(1)
    
    # Step 3
    result = test_spi_raw_read(pca_addr)
    
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    if result:
        mode, speed = result
        print(f"  ✅ Working configuration found: SPI Mode {mode}, Speed {speed // 1000}kHz")
        print(f"  → Update config.yaml if needed")
    else:
        print("  ❌ No working SPI configuration found.")
        print("  Possible causes:")
        print("    1. IPEM PiHat not properly seated on GPIO header")
        print("    2. ATM90E36 chip not receiving power (check 3.3V rail)")
        print("    3. SPI bus conflict with another device")
        print("    4. Hardware defect on the IPEM board")
        print("    5. Wrong PCA9671 CS bit (check IPEM board documentation)")


if __name__ == "__main__":
    main()
