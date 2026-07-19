"""
atm90e36.py - ATM90E36 Python driver for the IPEM PiHat on Raspberry Pi.
Handles SPI communication, register scaling, and optional PCA9671 I2C CS expander.
"""

import time
import logging
import threading
import spidev

log = logging.getLogger(__name__)

# Register Addresses
REG_SOFT_RESET    = 0x00
REG_SYS_STATUS0   = 0x01
REG_SYS_STATUS1   = 0x02

REG_CONFIG_START  = 0x30
REG_PL_CONST_H    = 0x31
REG_PL_CONST_L    = 0x32
REG_MMODE0        = 0x33
REG_MMODE1        = 0x34
REG_P_START_TH    = 0x35
REG_Q_START_TH    = 0x36
REG_S_START_TH    = 0x37
REG_P_PHASE_TH    = 0x38
REG_Q_PHASE_TH    = 0x39
REG_S_PHASE_TH    = 0x3A
REG_CS_ZERO       = 0x3B

REG_CAL_START     = 0x40
REG_UGAIN_A       = 0x61
REG_IGAIN_A       = 0x62
REG_UGAIN_B       = 0x65
REG_IGAIN_B       = 0x66
REG_UGAIN_C       = 0x69
REG_IGAIN_C       = 0x6A
REG_CS_THREE      = 0x6F

REG_PMEAN_A       = 0xB1
REG_PMEAN_B       = 0xB2
REG_PMEAN_C       = 0xB3
REG_QMEAN_A       = 0xB5
REG_QMEAN_B       = 0xB6
REG_QMEAN_C       = 0xB7
REG_PF_MEAN_A     = 0xBD
REG_PF_MEAN_B     = 0xBE
REG_PF_MEAN_C     = 0xBF

REG_URMS_A        = 0xD9
REG_URMS_B        = 0xDA
REG_URMS_C        = 0xDB
REG_IRMS_A        = 0xDD
REG_IRMS_B        = 0xDE
REG_IRMS_C        = 0xDF

REG_FREQ          = 0xF8
REG_TEMP          = 0xFC


class ATM90E36:
    def __init__(self, config):
        self.config = config
        
        # Parse configuration
        spi_cfg = config.get("spi", {})
        self.spi_bus = spi_cfg.get("bus", 0)
        self.spi_device = spi_cfg.get("device", 0)
        self.spi_speed = spi_cfg.get("speed_hz", 200000)
        self.spi_mode = spi_cfg.get("mode", 0)
        
        i2c_cfg = config.get("i2c", {})
        self.i2c_enabled = i2c_cfg.get("enabled", False)
        self.i2c_bus_num = i2c_cfg.get("bus", 1)
        self.pca_addr = i2c_cfg.get("pca9671_addr", 0x20)
        self.board_cs_bit = i2c_cfg.get("board_cs_bit", 9)
        self.cs_mask = 1 << self.board_cs_bit
        
        # Init SPI
        self._spi = spidev.SpiDev()
        self._spi.open(self.spi_bus, self.spi_device)
        self._spi.max_speed_hz = self.spi_speed
        self._spi.mode = self.spi_mode
        
        # Init I2C
        self._i2c = None
        # Default idle state: P0=0xFF, P1=0x03 (0x03FF) -> CS high + DMA ctrl
        self.pca_idle_state = i2c_cfg.get("pca_idle_state", 0x03FF)
        self._pca_state = self.pca_idle_state
        
        if self.i2c_enabled:
            import smbus2
            self._i2c = smbus2.SMBus(self.i2c_bus_num)
            self._pca_write(self._pca_state)  # set idle state
            
        # Lock to protect SPI/I2C transactions from overlapping threads
        self._lock = threading.Lock()
        
        # Calibrations
        cal = config.get("calibration", {})
        self.ugain = cal.get("ugain", 20200)
        self.igain_a = cal.get("igain_a", 33500)
        self.igain_b = cal.get("igain_b", 33500)
        self.igain_c = cal.get("igain_c", 33500)
        self.i_multiplier = cal.get("current_multiplier", 1.0)

    def _pca_write(self, value):
        """Write 16-bit word to PCA9671 expander."""
        if self._i2c is not None:
            try:
                from smbus2 import i2c_msg
                # Write P00-P07 and P10-P17 (2 bytes) directly without a command register byte
                msg = i2c_msg.write(self.pca_addr, [value & 0xFF, (value >> 8) & 0xFF])
                self._i2c.i2c_rdwr(msg)
            except Exception as e:
                log.error(f"I2C Write to PCA9671 failed: {e}")

    def _cs_assert(self):
        if self._i2c is not None:
            self._pca_state &= ~self.cs_mask
            self._pca_write(self._pca_state)
            time.sleep(0.001)  # Increased to 1ms setup time for stable I2C expander toggling

    def _cs_deassert(self):
        if self._i2c is not None:
            self._pca_state |= self.cs_mask
            self._pca_write(self._pca_state)
            time.sleep(0.001)  # Increased to 1ms hold time for stable I2C expander toggling

    def read_reg(self, reg):
        """Read 16-bit value from register, retrying if RMS registers return SPI noise (0xFFFF or 0xFFFE)."""
        # RMS registers for voltage (0xD9-0xDB) and current (0xDD-0xDF) — all unsigned
        rms_registers = [0xD1, 0xD2, 0xD3, 0xD9, 0xDA, 0xDB, 0xDD, 0xDE, 0xDF]
        import time
        
        for attempt in range(3):
            with self._lock:
                self._cs_assert()
                try:
                    # Read command: bit15 = 1. Address is 16-bit.
                    cmd = [0x80 | (reg >> 8), reg & 0xFF, 0x00, 0x00]
                    resp = self._spi.xfer2(cmd)
                    val = (resp[2] << 8) | resp[3]
                finally:
                    self._cs_deassert()
            
            # If it's a critical RMS register and returned SPI read noise (0xFFFF/0xFFFE), retry
            if reg in rms_registers and val in (0xFFFF, 0xFFFE):
                log.warning(f"SPI read noise (val={hex(val)}) detected on register {hex(reg)}. Retrying (attempt {attempt+1}/3)...")
                time.sleep(0.005)  # 5ms delay
                continue
            
            return val
            
        return 0  # Fallback to 0 if consistently returning SPI noise

    def write_reg(self, reg, val):
        """Write 16-bit value to register."""
        with self._lock:
            self._cs_assert()
            try:
                # Write command: bit15 = 0.
                cmd = [0x00 | (reg >> 8), reg & 0xFF, (val >> 8) & 0xFF, val & 0xFF]
                self._spi.xfer2(cmd)
            finally:
                self._cs_deassert()

    def check_sum(self, start, end):
        """Compute the ATM90E36 checksum algorithm:
        Low Byte: sum of all bytes in register range (modulo 256)
        High Byte: XOR of all bytes in register range
        """
        tmpl = 0
        tmph = 0
        for r in range(start, end + 1):
            v = self.read_reg(r)
            log.info(f"Register {hex(r)} read back: {hex(v)}")
            low_byte = v & 0xFF
            high_byte = (v >> 8) & 0xFF
            tmpl += low_byte + high_byte
            tmph ^= low_byte ^ high_byte
        
        cs_low = tmpl % 256
        cs_high = tmph % 256
        return cs_low + (cs_high << 8)

    def initialize(self):
        """Perform ATM90E36 software reset and config sequence."""
        log.info("Initializing ATM90E36 chip...")
        
        # 1. Reset
        self.write_reg(REG_SOFT_RESET, 0x789A)
        time.sleep(0.1)
        
        # 2. Configure System Registers (unlocked with 0x5678)
        self.write_reg(REG_CONFIG_START, 0x5678)
        
        # Meter Mode Configuration
        self.write_reg(REG_PL_CONST_H, 0x0861)
        self.write_reg(REG_PL_CONST_L, 0xC468)
        
        # MMode0: 50Hz (UK/Europe) / 3P4W (4-wire) / 0x0187
        freq = self.config.get("mains", {}).get("line_frequency", 50)
        mmode0 = 0x0187 if freq == 50 else 0x0087
        self.write_reg(REG_MMODE0, mmode0)
        self.write_reg(REG_MMODE1, 0x0000)
        
        # Thresholds
        self.write_reg(REG_P_START_TH, 0x08C0)
        self.write_reg(REG_Q_START_TH, 0x08C0)
        self.write_reg(REG_S_START_TH, 0x08C0)
        self.write_reg(REG_P_PHASE_TH, 0x02F0)
        self.write_reg(REG_Q_PHASE_TH, 0x02F0)
        self.write_reg(REG_S_PHASE_TH, 0x02F0)
        
        # Checksum of range 0x31-0x3A written to CSZero
        cs_zero = self.check_sum(REG_PL_CONST_H, REG_S_PHASE_TH)
        self.write_reg(REG_CS_ZERO, cs_zero)
        
        # Lock configuration (0x8765)
        self.write_reg(REG_CONFIG_START, 0x8765)
        
        # Verify CSZero status
        sys_status0 = self.read_reg(REG_SYS_STATUS0)
        log.info(f"System status 0: {hex(sys_status0)}")
        if sys_status0 & 0x4000:
            log.warning("ATM90E36 Config Checksum CSZero error!")
        else:
            log.info("CSZero Configuration successfully loaded and verified.")

        # 3. Configure Calibration / Adjust Registers
        self.write_reg(REG_CAL_START, 0x5678)
        self.write_reg(REG_UGAIN_A, self.ugain)
        self.write_reg(REG_IGAIN_A, self.igain_a)
        
        if self.config.get("mains", {}).get("phases", 1) > 1:
            self.write_reg(REG_UGAIN_B, self.ugain)
            self.write_reg(REG_IGAIN_B, self.igain_b)
            self.write_reg(REG_UGAIN_C, self.ugain)
            self.write_reg(REG_IGAIN_C, self.igain_c)
            
        # Checksum range 0x61-0x6E written to CSThree
        cs_three = self.check_sum(REG_UGAIN_A, 0x6E)
        self.write_reg(REG_CS_THREE, cs_three)
        
        self.write_reg(REG_CAL_START, 0x8765)
        
        # Verification
        sys_status1 = self.read_reg(REG_SYS_STATUS1)
        log.info(f"System status 1: {hex(sys_status1)}")
        if sys_status1 & 0x1000:
            log.warning("ATM90E36 Calibration Checksum CSThree error!")
        else:
            log.info("CSThree Calibration successfully loaded and verified.")

    def get_voltage(self, phase="A"):
        reg = REG_URMS_A if phase == "A" else (REG_URMS_B if phase == "B" else REG_URMS_C)
        return self.read_reg(reg) * 0.01

    def get_current(self, phase="A"):
        reg = REG_IRMS_A if phase == "A" else (REG_IRMS_B if phase == "B" else REG_IRMS_C)
        return self.read_reg(reg) * 0.001 * self.i_multiplier
 
    def get_active_power(self, phase="A"):
        reg = REG_PMEAN_A if phase == "A" else (REG_PMEAN_B if phase == "B" else REG_PMEAN_C)
        val = self.read_reg(reg)
        if val & 0x8000:
            val -= 0x10000
        return float(val) * self.i_multiplier
 
    def get_reactive_power(self, phase="A"):
        reg = REG_QMEAN_A if phase == "A" else (REG_QMEAN_B if phase == "B" else REG_QMEAN_C)
        val = self.read_reg(reg)
        if val & 0x8000:
            val -= 0x10000
        return float(val) * self.i_multiplier

    def get_power_factor(self, phase="A"):
        reg = REG_PF_MEAN_A if phase == "A" else (REG_PF_MEAN_B if phase == "B" else REG_PF_MEAN_C)
        val = self.read_reg(reg)
        if val & 0x8000:
            val -= 0x10000
        return val * 0.001

    def get_frequency(self):
        return self.read_reg(REG_FREQ) * 0.01

    def get_temperature(self):
        val = self.read_reg(REG_TEMP)
        if val & 0x8000:
            val -= 0x10000
        return float(val)

    def close(self):
        self._spi.close()
        if self._i2c is not None:
            self._i2c.close()
