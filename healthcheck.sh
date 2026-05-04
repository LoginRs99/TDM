#!/usr/bin/env python3
"""
Healthcheck script that monitors both application status and disk space.
"""
import sys
import shutil
from pathlib import Path
from datetime import datetime

TIMESTAMP_FILE = Path("healthcheck.timestamp")
MAX_AGE_SECONDS = 300  # 5 minutes
MIN_FREE_SPACE_MB = 100  # Minimum 100MB free space required

def check_disk_space():
    """Check if there's enough free disk space."""
    try:
        usage = shutil.disk_usage(".")
        free_mb = usage.free / (1024 * 1024)
        
        if free_mb < MIN_FREE_SPACE_MB:
            print(f"❌ CRITICAL: Only {free_mb:.1f}MB free space (minimum: {MIN_FREE_SPACE_MB}MB)")
            return False
        
        print(f"✅ Disk space OK: {free_mb:.1f}MB free")
        return True
    except Exception as e:
        print(f"⚠️  Could not check disk space: {e}")
        return True  # Don't fail healthcheck if we can't check

def check_timestamp():
    """Check if the application is updating its timestamp."""
    if not TIMESTAMP_FILE.exists():
        print("⚠️  Timestamp file not found (startup in progress)")
        return True  # Don't fail during startup
    
    try:
        with open(TIMESTAMP_FILE, 'r') as f:
            content = f.read().strip().split(',')
            timestamp = int(content[0])
        
        age = int(datetime.now().timestamp()) - timestamp
        
        if age > MAX_AGE_SECONDS:
            print(f"❌ Timestamp too old: {age}s (max: {MAX_AGE_SECONDS}s)")
            return False
        
        print(f"✅ Timestamp OK: {age}s old")
        return True
    except Exception as e:
        print(f"❌ Error reading timestamp: {e}")
        return False

def main():
    print(f"🏥 Running healthcheck at {datetime.now()}")
    
    disk_ok = check_disk_space()
    timestamp_ok = check_timestamp()
    
    if disk_ok and timestamp_ok:
        print("✅ Healthcheck PASSED")
        sys.exit(0)
    else:
        print("❌ Healthcheck FAILED")
        sys.exit(1)

if __name__ == "__main__":
    main()