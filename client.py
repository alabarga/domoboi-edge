"""
client.py - Main orchestrator for the Domoboi NILM Edge Client.
Manages the hardware capture thread and starts the async NILM processing and LTE transmission pipelines.
"""
import sys
import yaml
import argparse
import asyncio
import logging
import threading
import signal
from datetime import datetime, timezone

from atm90e36 import ATM90E36
from nilm_processor import NILMProcessor
from sender import LTESender

# Try importing Rich components for console visualization
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    from rich.live import Live
    from rich.bar import Bar
    from rich.table import Table
    from rich.console import Group
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

# Setup dynamic logging based on UI mode
def setup_logging(use_ui):
    root_logger = logging.getLogger()
    # Remove any existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        
    root_logger.setLevel(logging.INFO)
    log_format = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    
    if use_ui:
        # Save logs to a file so they don't corrupt the Rich interface
        import os
        os.makedirs("data", exist_ok=True)
        file_handler = logging.FileHandler("data/client.log")
        file_handler.setFormatter(log_format)
        root_logger.addHandler(file_handler)
    else:
        # Standard service output directly to stdout/stderr so journald handles it
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(log_format)
        root_logger.addHandler(stream_handler)

log = logging.getLogger("client")

# Global data stores for UI thread updates
latest_meas = {
    "voltage_a": 0.0,
    "current_a": 0.0,
    "active_power_a": 0.0,
    "reactive_power_a": 0.0,
    "power_factor_a": 0.0,
    "frequency": 0.0,
    "temperature": 0.0
}
latest_events = []


def capture_thread_loop(config, chip, loop, raw_queue, stop_event):
    """
    Synchronous hardware polling loop. Runs in a dedicated background thread
    to ensure predictable timing without blocking the asyncio event loop.
    """
    log.info("Starting hardware capture thread...")
    
    nilm_cfg = config.get("nilm", {})
    interval = nilm_cfg.get("sampling_interval_sec", 0.1)
    
    # Initialize hardware chip
    try:
        chip.initialize()
    except Exception as e:
        log.error(f"Failed to initialize ATM90E36 hardware: {e}", exc_info=True)
    
    # Startup guard: discard readings for the first few seconds after chip init
    startup_guard_sec = nilm_cfg.get("startup_guard_sec", 3.0)
    startup_time = time_ms()
    log.info(f"Startup guard active: discarding readings for {startup_guard_sec}s to filter init noise...")
        
    while not stop_event.is_set():
        start_time = time_now = time_ms()
        try:
            # Determine active phase based on non-zero igain in config
            cal = config.get("calibration", {})
            active_phase = "A"
            if cal.get("igain_b", 0) > 0:
                active_phase = "B"
            elif cal.get("igain_c", 0) > 0:
                active_phase = "C"

            # Get mains parameters from configuration
            mains_cfg = config.get("mains", {})
            nominal_v = mains_cfg.get("nominal_voltage", 230.0)
            line_freq = float(mains_cfg.get("line_frequency", 50.0))

            v = chip.get_voltage("A")  # Voltage terminal remains Phase A for logging
            i = chip.get_current(active_phase)
            
            # Since there is no AC-AC reference connected, calculate estimated power
            # using the configured nominal voltage directly.
            voltage = nominal_v
            p = i * voltage
            q = 0.0
            pf = 1.0
            freq = line_freq
                
            temp = chip.get_temperature()
            
            # Pack measurement dictionary
            meas = {
                "timestamp": datetime.now(timezone.utc),
                "voltage_a": v,
                "current_a": i,
                "active_power_a": p,
                "reactive_power_a": q,
                "power_factor_a": pf,
                "frequency": freq,
                "temperature": temp
            }
            
            # Update global real-time display container
            global latest_meas
            latest_meas.update({
                "timestamp": meas["timestamp"],
                "voltage_a": v,
                "current_a": i,
                "active_power_a": p,
                "reactive_power_a": q,
                "power_factor_a": pf,
                "frequency": freq,
                "temperature": temp
            })
            
            # Only enqueue to NILM processor after startup guard has elapsed
            elapsed_since_start = (time_ms() - startup_time) / 1000.0
            if elapsed_since_start >= startup_guard_sec:
                # Safely schedule addition into asyncio queue
                loop.call_soon_threadsafe(raw_queue.put_nowait, meas)
            
        except Exception as e:
            log.error(f"Error reading ATM90E36 hardware registers: {e}")
            
        # Calculate precise sleep delay to keep timing steady
        elapsed = (time_ms() - start_time) / 1000.0
        sleep_time = max(0, interval - elapsed)
        stop_event.wait(sleep_time)

    log.info("Hardware capture thread exiting.")


def time_ms():
    import time
    return int(time.time() * 1000)


async def run_rich_dashboard(config):
    """Real-time terminal visualization using the Rich library."""
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.console import Group
    from rich.bar import Bar
    from rich.text import Text
    from rich.console import Console
    
    console = Console()
    
    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            try:
                # Header Panel
                header_text = Text(f"DOMOBOI NILM CLIENT - {config.get('device_id', 'unknown')}", style="bold cyan", justify="center")
                
                # Telemetry Table
                table = Table(title="Live Telemetry [Phase A]", title_style="bold green")
                table.add_column("Parameter", style="cyan")
                table.add_column("Value", style="magenta")
                table.add_column("Status/Source", style="yellow")
                
                v = latest_meas.get("voltage_a", 0.0)
                i = latest_meas.get("current_a", 0.0)
                p = latest_meas.get("active_power_a", 0.0)
                freq = latest_meas.get("frequency", 0.0)
                temp = latest_meas.get("temperature", 0.0)
                
                v_status = "Software Estimated" if v < 5.0 else "Hardware Reference"
                v_disp = f"230.0 V (Estimated)" if v < 5.0 else f"{v:.2f} V"

                t = latest_meas.get("timestamp")
                t_disp = t.strftime("%H:%M:%S.%f")[:-3] if t else "N/A"
                
                table.add_row("Last Poll Time", t_disp, "10 Hz Polling")
                table.add_row("Voltage", v_disp, v_status)
                table.add_row("Current", f"{i:.3f} A", "Active CT" if i > 0.005 else "Idle")
                table.add_row("Active Power", f"{p:.1f} W", "Load ON" if p > 30.0 else "Idle")
                table.add_row("Frequency", f"{freq:.2f} Hz", "Normal" if 49.0 <= freq <= 51.0 else "Unstable")
                table.add_row("Chip Temp", f"{temp:.1f} °C", "Normal")
                
                # Real-Time Load Bar
                max_w = 3000.0
                bar_color = "red" if p > 2000.0 else ("yellow" if p > 500.0 else "green")
                power_bar = Bar(max_w, 0.0, max(0.0, min(max_w, p)), color=bar_color)
                
                power_panel = Panel(
                    Group(
                        Text(f"Estimated Load Wattage: {p:.1f} W / {max_w:.0f} W", style="bold white"),
                        power_bar
                    ),
                    title="Real-Time Load Profile",
                    border_style="cyan"
                )
                
                # Latest Events Table (Transition-Based)
                events_table = Table(title="Latest Detected NILM Transitions", title_style="bold yellow")
                events_table.add_column("Timestamp", style="dim")
                events_table.add_column("Direction", style="bold magenta")
                events_table.add_column("Step Size (Watts)", style="yellow")
                events_table.add_column("Window", style="cyan")
                events_table.add_column("Details", style="white")
                
                # Show last 5 transition events
                for ev in latest_events[-5:]:
                    ev_p = ev.get("power", 0.0)
                    # Scale: 1 block per 100W up to 1000W
                    blocks = int(ev_p / 100.0)
                    blocks = max(1, min(10, blocks)) if ev_p > 0.0 else 0
                    bar_str = "█" * blocks + "░" * (10 - blocks)
                    power_disp = f"[{bar_str}] {ev_p:.1f}W"
                    
                    dur_sec = ev.get("duration_seconds", 3)
                    dur_disp = f"{dur_sec}s"
                    
                    events_table.add_row(
                        ev.get("start_time", "")[:19].replace("T", " "),
                        ev.get("type", "UNKNOWN"),
                        power_disp,
                        dur_disp,
                        ev.get("description", "")
                    )
                    
                main_group = Group(
                    Panel(header_text, border_style="blue"),
                    table,
                    power_panel,
                    events_table,
                    Text("Press Ctrl+C to terminate client safely. Logs are managed by systemd.", style="dim italic")
                )
                
                live.update(main_group)
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error updating dashboard: {e}")
                await asyncio.sleep(1.0)


async def main_async(config, chip, use_ui):
    # Queues
    raw_queue = asyncio.Queue()
    event_queue = asyncio.Queue()
    
    # Instantiate Pipeline Components
    processor = NILMProcessor(config, raw_queue, event_queue, latest_events)
    sender = LTESender(config, event_queue, latest_events)
    
    # Setup background capture thread
    stop_event = threading.Event()
    loop = asyncio.get_running_loop()
    
    cap_thread = threading.Thread(
        target=capture_thread_loop,
        args=(config, chip, loop, raw_queue, stop_event),
        daemon=True
    )
    cap_thread.start()
    
    # Launch async tasks
    proc_task = asyncio.create_task(processor.run())
    send_task = asyncio.create_task(sender.run())
    
    # Start dashboard if interactive rich mode is enabled
    if use_ui:
        asyncio.create_task(run_rich_dashboard(config))
    
    # Handle OS Shutdown Signals
    def shutdown_handler():
        log.info("Shutdown signal received. Cleaning up...")
        stop_event.set()
        
        # Insert Sentinels to notify loops to terminate
        raw_queue.put_nowait(None)
        event_queue.put_nowait(None)
        
        proc_task.cancel()
        send_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_handler)
        except NotImplementedError:
            # Windows/fallback signal handling
            pass
            
    # Wait for processing and sending tasks to finish draining/canceling
    await asyncio.gather(proc_task, send_task, return_exceptions=True)
    
    # Join capture thread
    stop_event.set()
    cap_thread.join(timeout=2.0)
    
    log.info("System cleanup complete. Goodbye.")


def run_smoke_test(chip, config):
    """Smoke test helper: initialize chip and print readings."""
    print("=========================================")
    print(" Running ATM90E36 Smoke Test...         ")
    print("=========================================")
    try:
        chip.initialize()
    except Exception as e:
        print(f"Error: Hardware initialization failed: {e}")
        return
        
    print("\nReading telemetry 10 times at 1Hz:")
    print("-----------------------------------------")
    for idx in range(1, 11):
        try:
            v = chip.get_voltage("A")
            i = chip.get_current("A")
            
            if v < 5.0:
                nominal_v = config.get("mains", {}).get("nominal_voltage", 230.0)
                p = i * nominal_v
                q = 0.0
                pf = 1.0
                freq = float(config.get("mains", {}).get("line_frequency", 50.0))
                is_estimated = " (Estimated)"
            else:
                p = chip.get_active_power("A")
                q = chip.get_reactive_power("A")
                pf = chip.get_power_factor("A")
                freq = chip.get_frequency()
                is_estimated = ""
                
            temp = chip.get_temperature()
            
            print(
                f"[{idx}/10] V: {v:.2f}V | I: {i:.3f}A | P: {p:.1f}W{is_estimated} | "
                f"Q: {q:.1f}var | PF: {pf:.3f} | Freq: {freq:.2f}Hz | Temp: {temp:.1f}C"
            )
        except Exception as e:
            print(f"Read error: {e}")
        time_ms_sleep(1.0)
        
    print("-----------------------------------------")
    print(" Smoke Test Complete.                    ")
    print("=========================================")


def time_ms_sleep(sec):
    import time
    time.sleep(sec)


def main():
    parser = argparse.ArgumentParser(description="Domoboi NILM Edge Monitoring Agent")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to configuration file (default: config.yaml)"
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Run ATM90E36 diagnostic checks and exit"
    )
    parser.add_argument(
        "--no-ui",
        action="store_true",
        help="Disable the interactive Rich terminal interface"
    )
    args = parser.parse_args()

    # Load config yaml
    try:
        with open(args.config, "r") as f:
            config = yaml.safe_load(f) or {}
    except Exception as e:
        log.error(f"Failed to load configuration file {args.config}: {e}")
        sys.exit(1)

    # Default device_id to system hostname if not specified
    import socket
    if not config.get("device_id"):
        config["device_id"] = socket.gethostname()

    # Determine if UI dashboard should be shown
    use_ui = HAS_RICH and sys.stdout.isatty() and not args.no_ui

    # Setup Logging dynamically based on UI mode
    setup_logging(use_ui)
    
    log.info(f"Device ID: {config['device_id']}")

    # Initialize driver
    try:
        chip = ATM90E36(config)
    except Exception as e:
        log.error(f"Failed to load ATM90E36 driver: {e}")
        sys.exit(1)

    if args.smoke_test:
        run_smoke_test(chip, config)
        chip.close()
        sys.exit(0)

    # Launch daemon
    try:
        asyncio.run(main_async(config, chip, use_ui))
    except Exception as e:
        log.error(f"Application terminated: {e}")
    finally:
        chip.close()


if __name__ == "__main__":
    main()
