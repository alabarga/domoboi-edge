"""
nilm_processor.py - Real-time NILM transition-event detector.
Consumes power readings, identifies step changes (positive and negative transitions),
extracts statistical features, and sends them to the Django server.
"""

import asyncio
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


class NILMProcessor:
    def __init__(self, config, raw_queue, event_queue, latest_events=None):
        self.config = config
        self.raw_queue = raw_queue
        self.event_queue = event_queue
        self.latest_events = latest_events if latest_events is not None else []
        
        nilm_cfg = config.get("nilm", {})
        self.device_id = config.get("device_id", "domoboi-01")
        
        # Adaptive threshold settings
        self.min_threshold = nilm_cfg.get("min_threshold_watts", 5.0)
        self.threshold_percentage = nilm_cfg.get("threshold_percentage", 0.015)
        self.active_threshold = self.min_threshold
        
        # Sampling settings
        self.interval = nilm_cfg.get("sampling_interval_sec", 0.1)
        # Capture window: 3.0 seconds of samples
        self.window_size = max(10, int(3.0 / self.interval))
        
        # Stability threshold for baseline (in Watts)
        self.stability_threshold = nilm_cfg.get("stability_threshold_watts", 3.0)
        
        # State machine variables
        self.state = "idle"  # "idle" or "recording"
        self.baseline_history = []  # Last 10 samples (1 second at 10Hz)
        self.baseline_size = 10
        
        self.transient_buffer = []
        self.samples_to_collect = 0
        self.pre_event_power = 0.0
        self.start_time = None
        self.cooldown = 0

    def _calculate_mean_and_std(self, samples):
        n = len(samples)
        if n == 0:
            return 0.0, 0.0
        mean = sum(samples) / n
        variance = sum((x - mean) ** 2 for x in samples) / n
        std = variance ** 0.5
        return mean, std

    async def run(self):
        log.info("NILM Processor started (Transition-Based Mode).")
        while True:
            try:
                # Get raw measurement
                meas = await self.raw_queue.get()
                if meas is None:
                    # Sentinel to shut down
                    break
                
                timestamp = meas.get("timestamp")
                power = meas.get("active_power_a", 0.0)  # Main active power
                
                if self.cooldown > 0:
                    self.cooldown -= 1
                
                if self.state == "idle":
                    # Check for triggers once baseline is full and cooldown is 0
                    if len(self.baseline_history) == self.baseline_size and self.cooldown == 0:
                        mean_base, std_base = self._calculate_mean_and_std(self.baseline_history)
                        
                        # Only trigger from a stable baseline state
                        if std_base <= self.stability_threshold:
                            # Dynamic threshold based on current base load (min 5W, scaling at 1.5%)
                            dynamic_threshold = max(self.min_threshold, mean_base * self.threshold_percentage)
                            
                            delta_trigger = power - mean_base
                            if abs(delta_trigger) >= dynamic_threshold:
                                # Trigger!
                                self.state = "recording"
                                self.pre_event_power = mean_base
                                self.active_threshold = dynamic_threshold  # Capture threshold for confirmation
                                self.start_time = timestamp
                                self.transient_buffer = [power]
                                self.samples_to_collect = self.window_size - 1
                                log.info(f"Transition Triggered! Delta={delta_trigger:.1f}W, Baseline={mean_base:.1f}W, Threshold={dynamic_threshold:.1f}W")
                                
                    # If we did not trigger, add the new sample to the baseline history
                    if self.state == "idle":
                        self.baseline_history.append(power)
                        if len(self.baseline_history) > self.baseline_size:
                            self.baseline_history.pop(0)
                                
                elif self.state == "recording":
                    self.transient_buffer.append(power)
                    self.samples_to_collect -= 1
                    
                    if self.samples_to_collect <= 0:
                        # Characterization window complete
                        # Use the last 10 samples in the buffer as the settled post-event state
                        post_samples = self.transient_buffer[-10:] if len(self.transient_buffer) >= 10 else self.transient_buffer
                        mean_post, std_post = self._calculate_mean_and_std(post_samples)
                        
                        delta_p = mean_post - self.pre_event_power
                        
                        # Verify the step size exceeds the dynamic threshold computed at trigger time
                        if abs(delta_p) >= self.active_threshold:
                            post_avg, post_std = self._calculate_mean_and_std(self.transient_buffer)
                            post_min = min(self.transient_buffer)
                            post_max = max(self.transient_buffer)
                            
                            log.info(f"Transition Confirmed: Delta P = {delta_p:.1f}W (Pre: {self.pre_event_power:.1f}W -> Post: {mean_post:.1f}W)")
                            
                            # Package payload
                            measurement_payload = {
                                "device_id": self.device_id,
                                "start_time": self.start_time.isoformat(),
                                "end_time": timestamp.isoformat(),
                                "readings": self.transient_buffer,
                                "value": float(delta_p),  # The step size (positive/negative)
                                "features": {
                                    "avg": float(post_avg),
                                    "min": float(post_min),
                                    "max": float(post_max),
                                    "std": float(post_std)
                                }
                            }
                            
                            # Add to LTE transmission queue
                            await self.event_queue.put(measurement_payload)
                            
                            # Add to UI events list
                            direction = "ON" if delta_p > 0 else "OFF"
                            desc = f"Salto {direction} de {abs(delta_p):.1f}W (Ppre: {self.pre_event_power:.1f}W -> Ppost: {mean_post:.1f}W)"
                            local_event = {
                                "start_time": self.start_time.isoformat(),
                                "type": direction,
                                "power": float(abs(delta_p)),
                                "duration_seconds": int(self.window_size * self.interval),
                                "description": desc
                            }
                            self.latest_events.append(local_event)
                        else:
                            log.info(f"Transition Discarded: Delta P = {delta_p:.1f}W was below threshold after settling.")
                            
                        # Reset state and update baseline history to the post-event settled samples
                        self.state = "idle"
                        self.baseline_history = list(post_samples)
                        self.cooldown = int(1.5 / self.interval)  # 1.5s cooldown
                
                self.raw_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error in NILM Processor: {e}", exc_info=True)
                await asyncio.sleep(1)
