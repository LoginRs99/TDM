#!/usr/bin/env python3
"""
Enhanced Docker healthcheck script for Twitch Drops Miner
Returns exit code 0 if healthy, 1 if unhealthy
Monitors: timestamp freshness, failure count, error state, and metrics freshness
"""
import sys
import json
from pathlib import Path
from time import time
from datetime import datetime

HEALTHCHECK_FILE = Path("healthcheck.timestamp")
METRICS_FILE = Path("metrics.json")

MAX_AGE = 180  # 3 minutes - how old the timestamp can be
MAX_FAILURES = 3  # Maximum consecutive failures before unhealthy
MAX_METRICS_AGE = 600  # 10 minutes - max age for metrics file


def check_timestamp() -> tuple[bool, str]:
    """Check if the main process is updating its timestamp"""
    if not HEALTHCHECK_FILE.exists():
        return False, "Healthcheck file missing (startup in progress?)"
    
    try:
        content = HEALTHCHECK_FILE.read_text().strip()
        parts = content.split(',')
        
        # Parse: timestamp,failure_count[,ERROR]
        timestamp = int(parts[0])
        failures = int(parts[1]) if len(parts) > 1 else 0
        is_error = len(parts) > 2 and parts[2] == "ERROR"
        
        age = time() - timestamp
        
        # Check if timestamp is stale
        if age > MAX_AGE:
            return False, f"Timestamp stale: {age:.0f}s old (max: {MAX_AGE}s)"
        
        # Check if too many failures
        if failures >= MAX_FAILURES:
            return False, f"Too many failures: {failures} (max: {MAX_FAILURES})"
        
        # Check error state
        if is_error:
            # Still healthy if recent and not too many failures
            if age < 60 and failures < MAX_FAILURES:
                return True, f"Recent error but recovering (failures: {failures}, age: {age:.0f}s)"
            return False, f"Persistent error state (failures: {failures})"
        
        # All checks passed
        status = f"age: {age:.0f}s, failures: {failures}"
        if failures > 0:
            status += f" (recovering)"
        return True, status
        
    except ValueError as e:
        return False, f"Invalid healthcheck format: {e}"
    except Exception as e:
        return False, f"Timestamp check error: {e}"


def check_metrics() -> tuple[bool, str]:
    """Check if metrics file exists and is being updated"""
    if not METRICS_FILE.exists():
        return True, "Metrics file not created yet"
    
    try:
        metrics_age = time() - METRICS_FILE.stat().st_mtime
        
        # Read metrics to get additional info
        with open(METRICS_FILE, 'r') as f:
            metrics = json.load(f)
        
        drops_claimed = metrics.get('drops_claimed', 0)
        watch_success_rate = metrics.get('watch_success_rate', 0)
        uptime_hours = metrics.get('uptime_hours', 0)
        
        info = (
            f"uptime: {uptime_hours:.1f}h, "
            f"drops: {drops_claimed}, "
            f"watch_rate: {watch_success_rate:.1f}%, "
            f"updated: {metrics_age:.0f}s ago"
        )
        
        # Metrics file is very stale
        if metrics_age > MAX_METRICS_AGE:
            return True, f"Metrics stale but app running ({info})"
        
        return True, info
        
    except json.JSONDecodeError:
        return True, "Metrics file corrupt (non-critical)"
    except Exception as e:
        return True, f"Metrics check skipped: {e}"


def main():
    """Run all health checks and report results"""
    print(f"Running healthcheck at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    # Core timestamp check (CRITICAL)
    timestamp_ok, timestamp_msg = check_timestamp()
    print(f"{'OK' if timestamp_ok else 'FAIL'} Timestamp: {timestamp_msg}")
    
    # Metrics check (INFO ONLY)
    metrics_ok, metrics_msg = check_metrics()
    print(f"{'OK' if metrics_ok else 'WARN'} Metrics: {metrics_msg}")
    
    print("=" * 60)
    
    # Overall health decision
    # Only timestamp check is critical for health
    if timestamp_ok:
        print("HEALTHCHECK PASSED")
        sys.exit(0)
    else:
        print("HEALTHCHECK FAILED")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Healthcheck error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
