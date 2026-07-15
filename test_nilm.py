"""
test_nilm.py - Automated tests for verifying NILM processing and transmission queues.
Run with: python3 test_nilm.py
"""

import os
import json
import sqlite3
import asyncio
import unittest
from datetime import datetime, timezone, timedelta
from aiohttp import web

# Import our edge modules
from nilm_processor import NILMProcessor
from sender import LTESender


class TestNILMPipeline(unittest.IsolatedAsyncioTestCase):
    async def test_transient_detection_and_matching(self):
        """Simulates raw power readings and asserts that appliance events are correctly paired."""
        raw_queue = asyncio.Queue()
        event_queue = asyncio.Queue()
        latest_events = []
        
        config = {
            "device_id": "test-device",
            "nilm": {
                "transient_threshold_watts": 40.0,
                "match_window_seconds": 100
            }
        }
        
        processor = NILMProcessor(config, raw_queue, event_queue, latest_events)
        
        # Start processor task
        proc_task = asyncio.create_task(processor.run())
        
        # 1. Feed baseline power (10W)
        now = datetime.now(timezone.utc)
        for i in range(10):
            await raw_queue.put({
                "timestamp": now + timedelta(seconds=i),
                "active_power_a": 10.0
            })
            
        # Allow processor to run
        await asyncio.sleep(0.05)
        self.assertEqual(len(processor.unmatched_on), 0)
        
        # 2. Simulate ON Transient (Kettle turns ON: +2500W)
        kettle_on_time = now + timedelta(seconds=10)
        for i in range(10):
            await raw_queue.put({
                "timestamp": kettle_on_time + timedelta(milliseconds=i*100),
                "active_power_a": 2510.0
            })
            
        await asyncio.sleep(0.05)
        # Should have captured 1 ON transient
        self.assertEqual(len(processor.unmatched_on), 1)
        self.assertEqual(processor.unmatched_on[0]["power"], 2500.0)
        
        # 3. Simulate OFF Transient (Kettle turns OFF: -2500W)
        kettle_off_time = kettle_on_time + timedelta(seconds=30)
        for i in range(10):
            await raw_queue.put({
                "timestamp": kettle_off_time + timedelta(milliseconds=i*100),
                "active_power_a": 10.0
            })
            
        await asyncio.sleep(0.05)
        
        # ON transient should be matched and removed from list
        self.assertEqual(len(processor.unmatched_on), 0)
        
        # Event queue should have the complete measurement segment payload
        self.assertFalse(event_queue.empty())
        meas = await event_queue.get()
        
        self.assertEqual(meas["device_id"], "test-device")
        self.assertTrue("readings" in meas)
        self.assertTrue(len(meas["readings"]) > 0)
        
        # UI events list should have the local display event
        self.assertEqual(len(latest_events), 1)
        self.assertEqual(latest_events[0]["type"], "CYCLE")
        
        # Cleanup
        await raw_queue.put(None)
        await proc_task


class TestLTESender(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db_path = "test_data/test_buffer.db"
        self.config = {
            "api": {
                "base_url": "http://localhost:9099",
                "token": "test-token",
                "timeout_sec": 1
            },
            "buffer": {
                "db_path": self.db_path,
                "sync_interval_sec": 1
            }
        }
        self.event_queue = asyncio.Queue()
        
        # Clean up database if any from previous runs
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
            
        # Clean directory if empty
        db_dir = os.path.dirname(self.db_path)
        if os.path.exists(db_dir) and os.path.exists(db_dir) and not os.listdir(db_dir):
            os.rmdir(db_dir)

    async def test_offline_buffering_and_sync(self):
        """Verifies measurements are saved to local SQLite on network failures, and syncs when connection returns."""
        # 1. Instantiate sender (no server running -> request fails)
        sender = LTESender(self.config, self.event_queue, [])
        sender_task = asyncio.create_task(sender.run())
        
        test_meas = {
            "device_id": "test-device",
            "start_time": datetime.now(timezone.utc).isoformat(),
            "end_time": datetime.now(timezone.utc).isoformat(),
            "readings": [10.5, 20.2, 350.0]
        }
        
        # Push measurement to queue
        await self.event_queue.put(test_meas)
        
        # Wait for transmission attempt & fail
        await asyncio.sleep(0.2)
        
        # Verify it was buffered in SQLite
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT payload FROM offline_measurements")
        rows = cursor.fetchall()
        conn.close()
        
        self.assertEqual(len(rows), 1)
        buffered_payload = json.loads(rows[0][0])
        self.assertEqual(buffered_payload["readings"], [10.5, 20.2, 350.0])
        
        # Stop sender task
        await self.event_queue.put(None)
        await sender_task
        
        # 2. Setup mock local server
        mock_received = []
        async def mock_handler(request):
            auth = request.headers.get("Authorization")
            if auth != "Token test-token":
                return web.json_response({"error": "unauthorized"}, status=401)
            data = await request.json()
            mock_received.append(data)
            return web.json_response({"status": "success"}, status=201)
            
        app = web.Application()
        app.router.add_post("/nilm/api/measurements/", mock_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "localhost", 9099)
        await site.start()
        
        # Restart sender (server is now online)
        self.event_queue = asyncio.Queue()
        sender = LTESender(self.config, self.event_queue, [])
        sender_task = asyncio.create_task(sender.run())
        
        # Wait for sync loop to run (sync_interval = 1 sec)
        await asyncio.sleep(1.5)
        
        # Verify SQLite buffer is drained
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM offline_measurements")
        count = cursor.fetchone()[0]
        conn.close()
        
        self.assertEqual(count, 0)
        
        # Verify server received the event
        self.assertEqual(len(mock_received), 1)
        self.assertEqual(mock_received[0]["readings"], [10.5, 20.2, 350.0])
        
        # Stop mock server and sender task
        await self.event_queue.put(None)
        await sender_task
        await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
