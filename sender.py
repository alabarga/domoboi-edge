"""
sender.py - Handles cellular LTE data transmission to Django backend.
Supports local SQLite buffering for offline resilience during network drops.
"""

import os
import json
import sqlite3
import asyncio
import logging
import aiohttp
from datetime import datetime, timezone

log = logging.getLogger(__name__)


class LTESender:
    def __init__(self, config, event_queue):
        self.config = config
        self.event_queue = event_queue
        
        api_cfg = config.get("api", {})
        self.base_url = api_cfg.get("base_url", "http://localhost:8000")
        self.token = api_cfg.get("token", "")
        self.timeout = api_cfg.get("timeout_sec", 5)
        
        buf_cfg = config.get("buffer", {})
        self.db_path = buf_cfg.get("db_path", "data/offline_events.db")
        self.sync_interval = buf_cfg.get("sync_interval_sec", 30)
        
        # Ensure database directory exists
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
            
        self.init_db()

    def init_db(self):
        """Initialize the local SQLite database for offline buffering."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS offline_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def buffer_event(self, event):
        """Buffer event locally in SQLite."""
        log.info("Buffering event locally in SQLite database due to network issues.")
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO offline_events (payload, created_at) VALUES (?, ?)",
                (json.dumps(event), datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"Failed to buffer event to SQLite: {e}", exc_info=True)

    async def send_payload(self, session, url, payload):
        """Perform POST request with token authorization."""
        headers = {
            "Authorization": f"Token {self.token}",
            "Content-Type": "application/json"
        }
        try:
            async with session.post(url, json=payload, headers=headers, timeout=self.timeout) as resp:
                if resp.status in (200, 201):
                    return True
                else:
                    body = await resp.text()
                    log.warning(f"Failed to send. Server returned HTTP status {resp.status}: {body}")
                    return False
        except Exception as e:
            log.warning(f"Network POST request failed: {e}")
            return False

    async def run(self):
        log.info("LTE Sender started.")
        
        # Spawn the database sync loop in the background
        sync_task = asyncio.create_task(self.sync_buffered_events_loop())
        
        async with aiohttp.ClientSession() as session:
            url = f"{self.base_url.rstrip('/')}/nilm/api/events/"
            
            while True:
                try:
                    event = await self.event_queue.get()
                    if event is None:
                        # Sentinel to shut down
                        break
                    
                    log.info(f"Attempting to transmit event: {event['type']}")
                    success = await self.send_payload(session, url, event)
                    
                    if not success:
                        # Failed to send: store it in offline buffer
                        self.buffer_event(event)
                    else:
                        log.info("Event successfully transmitted to Django backend.")
                        
                    self.event_queue.task_done()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    log.error(f"Error in LTE Sender transmit loop: {e}", exc_info=True)
                    await asyncio.sleep(1)
                    
        sync_task.cancel()

    async def sync_buffered_events_loop(self):
        """Periodically runs to drain the offline buffer to Django when network is restored."""
        url = f"{self.base_url.rstrip('/')}/nilm/api/events/"
        
        while True:
            try:
                await asyncio.sleep(self.sync_interval)
                
                # Fetch pending events
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT id, payload FROM offline_events ORDER BY id ASC")
                rows = cursor.fetchall()
                conn.close()
                
                if not rows:
                    continue
                
                log.info(f"Found {len(rows)} buffered events. Attempting to sync...")
                
                async with aiohttp.ClientSession() as session:
                    success_count = 0
                    for row_id, payload_str in rows:
                        event = json.loads(payload_str)
                        # Attempt transmission
                        success = await self.send_payload(session, url, event)
                        
                        if success:
                            # Remove from DB
                            conn = sqlite3.connect(self.db_path)
                            cursor = conn.cursor()
                            cursor.execute("DELETE FROM offline_events WHERE id = ?", (row_id,))
                            conn.commit()
                            conn.close()
                            success_count += 1
                        else:
                            # Stop syncing and try again in next cycle if network is still down
                            log.warning("Sync failed. Aborting buffer drain until next cycle.")
                            break
                            
                    if success_count > 0:
                        log.info(f"Successfully synchronized {success_count} buffered events to Django backend.")
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error in database sync loop: {e}", exc_info=True)
