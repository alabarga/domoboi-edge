"""
nilm_processor.py - Real-time NILM transient-event detector.
Consumes power readings, identifies step changes (transients), matches ON/OFF cycles,
and generates appliance events.
"""

import asyncio
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def classify_appliance(power):
    """
    Classify appliance type and event class based on active power level (Watts).
    Matches choices in Django nilm.models.Event.APPLIANCE_TYPES.
    """
    p = abs(power)
    if 40 <= p < 80:
        return "LIGHTS", "NORMAL"
    elif 80 <= p < 250:
        return "FRIDGE", "NORMAL"
    elif 250 <= p < 600:
        return "TV", "NORMAL"
    elif 600 <= p < 1300:
        return "MICROWAVE", "NORMAL"
    elif 1300 <= p < 1700:
        return "IRON", "NORMAL"
    elif 1700 <= p < 2300:
        return "OVEN", "NORMAL"
    elif 2300 <= p < 3200:
        return "KETTLE", "NORMAL"
    elif 3200 <= p < 5000:
        # Washing machines under full heating cycle
        return "WASHING MACHINE", "NORMAL"
    else:
        return "LIGHTS", "NORMAL"


class NILMProcessor:
    def __init__(self, config, raw_queue, event_queue):
        self.config = config
        self.raw_queue = raw_queue
        self.event_queue = event_queue
        
        nilm_cfg = config.get("nilm", {})
        self.threshold = nilm_cfg.get("transient_threshold_watts", 40.0)
        self.match_window = nilm_cfg.get("match_window_seconds", 3600)
        self.device_id = config.get("device_id", "domoboi-01")
        
        # Sliding buffer to compute a rolling baseline (e.g. past 1 second)
        # At 10Hz sampling rate, 10 samples = 1 second.
        self.history_size = 10
        self.power_history = []
        
        # List of unmatched ON transients: [{"timestamp": dt, "power": float}]
        self.unmatched_on = []

    async def run(self):
        log.info("NILM Processor started.")
        while True:
            try:
                # Get raw measurement
                meas = await self.raw_queue.get()
                if meas is None:
                    # Sentinel to shut down
                    break
                
                timestamp = meas.get("timestamp")
                power = meas.get("active_power_a", 0.0)  # Main single-phase active power
                
                # Maintain rolling window
                self.power_history.append(power)
                if len(self.power_history) > self.history_size:
                    self.power_history.pop(0)
                
                # Check for transient events once history is full
                if len(self.power_history) == self.history_size:
                    # Calculate baseline as average of the older samples
                    baseline = sum(self.power_history[:-1]) / (self.history_size - 1.0)
                    delta_p = power - baseline
                    
                    if abs(delta_p) >= self.threshold:
                        # Reset window with the new power level to avoid re-triggering
                        self.power_history = [power] * self.history_size
                        
                        await self.handle_transient(timestamp, delta_p)
                        
                self.raw_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error in NILM Processor: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def handle_transient(self, dt, dp):
        log.info(f"Transient detected: Delta P = {dp:.1f}W at {dt.isoformat()}")
        
        if dp > 0:
            # ON Transient: store it for matching
            self.unmatched_on.append({
                "timestamp": dt,
                "power": dp
            })
            log.debug(f"Stored ON transient. Total unmatched: {len(self.unmatched_on)}")
        else:
            # OFF Transient: try to match it with a pending ON transient
            match_index = -1
            min_power_diff = float("inf")
            target_on_power = abs(dp)
            
            # Find the best matching ON transient (closest in magnitude, within window)
            now = dt
            for i, on_tr in enumerate(self.unmatched_on):
                time_diff = (now - on_tr["timestamp"]).total_seconds()
                
                if time_diff > self.match_window:
                    # Expired ON transient. Let's prune it in a later step
                    continue
                
                power_diff = abs(on_tr["power"] - target_on_power)
                # Ensure the power levels are within 20% tolerance of each other
                tolerance = on_tr["power"] * 0.20
                
                if power_diff < tolerance and power_diff < min_power_diff:
                    min_power_diff = power_diff
                    match_index = i

            # Clean up expired ON transients
            self.unmatched_on = [
                on for on in self.unmatched_on
                if (now - on["timestamp"]).total_seconds() <= self.match_window
            ]

            if match_index != -1 and match_index < len(self.unmatched_on):
                # Found a match!
                on_transient = self.unmatched_on.pop(match_index)
                start_time = on_transient["timestamp"]
                end_time = now
                power_watts = on_transient["power"]
                
                duration_sec = (end_time - start_time).total_seconds()
                duration_min = int(duration_sec / 60)
                
                appliance, class_name = classify_appliance(power_watts)
                
                # Check for anomalies / alerts (e.g. Kettle on for too long)
                if appliance == "KETTLE" and duration_sec > 600:  # Kettle on > 10 min
                    class_name = "ALERT"
                elif appliance == "OVEN" and duration_sec > 14400: # Oven on > 4 hours
                    class_name = "ALERT"
                elif class_name == "NORMAL" and duration_sec > 28800: # Any appliance > 8 hours
                    class_name = "UNEXPECTED"
                
                desc = (
                    f"Funcionamiento detectado de {appliance} con potencia de {power_watts:.1f}W "
                    f"por {duration_min} minutos."
                )
                
                event = {
                    "device_id": self.device_id,
                    "start_time": start_time.isoformat(),
                    "end_time": end_time.isoformat(),
                    "type": appliance,
                    "class_name": class_name,
                    "description": desc
                }
                
                log.info(f"Appliance event created: {appliance} ({duration_min} min)")
                await self.event_queue.put(event)
            else:
                log.debug(f"Unmatched OFF transient of {dp:.1f}W. No matching ON transient found.")
