#!/usr/bin/env python3
import os
import sys
from dotenv import load_dotenv

# Configure SSL certificates using certifi to prevent verification errors on macOS
try:
    import certifi
    os.environ["SSL_CERT_FILE"] = certifi.where()
    os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
except ImportError:
    pass

from daytona import Daytona, DaytonaConfig

def main():
    load_dotenv()
    
    use_stage = os.getenv("USE_DAYTONA_STAGE", "false").lower() == "true"
    if use_stage:
        api_key = os.getenv("DAYTONA_STAGE_API_KEY")
        server_url = os.getenv("DAYTONA_STAGE_SERVER_URL", "https://stage.daytona.work/api")
        print("Using Daytona staging environment")
    else:
        api_key = os.getenv("DAYTONA_API_KEY")
        server_url = os.getenv("DAYTONA_SERVER_URL", "https://app.daytona.io/api")
        print("Using Daytona production environment")
        
    if not api_key:
        print("Error: DAYTONA_API_KEY (or DAYTONA_STAGE_API_KEY) not set in environment.")
        return

    # Check region
    import yaml
    config = {}
    config_path = os.path.join(os.path.dirname(__file__), 'config.yml')
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f) or {}
        except Exception:
            pass
    target_region = config.get('providers', {}).get('daytona', {}).get('default_region', 'eu')
    
    print(f"Connecting to Daytona at {server_url} (region: {target_region})...")
    
    config = DaytonaConfig(
        api_key=api_key,
        api_url=server_url,
        target=target_region
    )
    daytona = Daytona(config=config)
    
    print("Fetching active sandboxes...")
    try:
        sandboxes = list(daytona.list())
    except Exception as e:
        print(f"Error fetching sandboxes: {e}")
        return
        
    if not sandboxes:
        print("No active sandboxes found.")
        return
        
    print(f"Found {len(sandboxes)} active sandbox(es):")
    for s in sandboxes:
        print(f" - {s.id} (image: {getattr(s, 'image', 'N/A')}, state: {getattr(s, 'state', 'N/A')})")
        
    confirm = input("\nDo you want to delete all these sandboxes? (y/N): ").strip().lower()
    if confirm != 'y':
        print("Cleanup aborted.")
        return
        
    print("\nStarting cleanup...")
    for s in sandboxes:
        try:
            print(f"Removing sandbox {s.id}...")
            daytona.delete(s)
            print(f"Successfully removed {s.id}")
        except Exception as e:
            print(f"Error removing sandbox {s.id}: {e}")
            
    print("\nCleanup complete.")

if __name__ == "__main__":
    main()
