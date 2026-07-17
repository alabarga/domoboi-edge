#!/usr/bin/env python3
import sys
import os
import yaml
import socket
import urllib.request
import urllib.parse
import json

def main():
    script_dir = os.path.dirname(os.path.realpath(__file__))
    config_path = os.path.join(script_dir, "config.yaml")
    
    if not os.path.exists(config_path):
        print(f"Error: {config_path} not found.")
        sys.exit(1)
        
    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"Error loading config.yaml: {e}")
        sys.exit(1)
        
    device_id = config.get("device_id")
    if not device_id:
        device_id = socket.gethostname()
        
    api_cfg = config.get("api", {})
    base_url = api_cfg.get("base_url", "http://localhost:8000").rstrip('/')
    token = api_cfg.get("token", "")
    timeout = api_cfg.get("timeout_sec", 5)
    
    if not token:
        print("Error: API token not found in config.yaml.")
        sys.exit(1)
        
    url = f"{base_url}/nilm/api/device/?device_id={urllib.parse.quote(device_id)}"
    
    print(f"Checking device configuration for '{device_id}'...")
    print(f"URL: {url}")
    
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Token {token}")
    
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status_code = response.getcode()
            body = response.read().decode('utf-8')
            
            if status_code == 200:
                data = json.loads(body)
                if data.get("status") == "ok":
                    print("✅ Device configuration: OK")
                    print(f"   Model:    {data.get('model')}")
                    print(f"   Location: {data.get('location')}")
                    sys.exit(0)
                else:
                    print(f"❌ Device configuration: NOK - {data.get('message', 'Not configured')}")
                    sys.exit(1)
            else:
                print(f"❌ Error: Server returned status code {status_code}")
                print(f"   Response: {body}")
                sys.exit(1)
    except urllib.error.HTTPError as e:
        print(f"❌ HTTP Error: {e.code} {e.reason}")
        try:
            body = e.read().decode('utf-8')
            print(f"   Response: {body}")
        except Exception:
            pass
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"❌ Network Error: Failed to reach the server. {e.reason}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
