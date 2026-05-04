"""
Healthcheck timestamp writer for Twitch Drops Miner
Manages the healthcheck.timestamp file that Docker monitors
"""
from __future__ import annotations

import logging
from pathlib import Path
from time import time
from typing import TYPE_CHECKING

logger = logging.getLogger("TwitchDrops")


class HealthcheckWriter:
    """
    Manages healthcheck timestamp file for Docker health monitoring
    
    The healthcheck file format: "timestamp,failure_count[,ERROR]"
    - timestamp: Unix timestamp of last update
    - failure_count: Number of consecutive failures (0-3)
    - ERROR: Optional flag indicating error state
    """
    
    def __init__(self, healthcheck_path: Path | str = "healthcheck.timestamp"):
        self.healthcheck_file = Path(healthcheck_path)
        self.failure_count = 0
        self.max_failures = 3
        self.last_update = 0
        
        # Initialize file if it doesn't exist
        self._initialize()
    
    def _initialize(self):
        """Initialize healthcheck file on startup"""
        try:
            if not self.healthcheck_file.exists():
                self.update_healthy()
                logger.info("✅ Healthcheck file initialized")
            else:
                # Read existing state
                try:
                    content = self.healthcheck_file.read_text().strip()
                    parts = content.split(',')
                    if len(parts) >= 2:
                        self.failure_count = int(parts[1])
                        logger.info(f"📊 Healthcheck file loaded (failures: {self.failure_count})")
                except Exception as e:
                    logger.warning(f"⚠️  Could not read existing healthcheck: {e}")
                    self.update_healthy()
        except Exception as e:
            logger.error(f"❌ Failed to initialize healthcheck: {e}")
    
    def update_healthy(self):
        """
        Mark application as healthy
        Resets failure count and clears error flag
        """
        try:
            timestamp = int(time())
            self.healthcheck_file.write_text(f"{timestamp},0")
            self.failure_count = 0
            self.last_update = timestamp
            logger.debug("✅ Healthcheck: Healthy")
        except Exception as e:
            logger.error(f"❌ Failed to update healthcheck (healthy): {e}")
    
    def update_error(self, error_msg: str = ""):
        """
        Mark application as having an error but still running
        Increments failure count and sets ERROR flag
        
        Args:
            error_msg: Optional error message for logging
        """
        try:
            timestamp = int(time())
            self.failure_count = min(self.failure_count + 1, self.max_failures)
            self.healthcheck_file.write_text(f"{timestamp},{self.failure_count},ERROR")
            self.last_update = timestamp
            
            if error_msg:
                logger.warning(f"⚠️  Healthcheck: Error state - {error_msg}")
            else:
                logger.warning(f"⚠️  Healthcheck: Error state (failures: {self.failure_count})")
        except Exception as e:
            logger.error(f"❌ Failed to update healthcheck (error): {e}")
    
    def update_failure(self):
        """
        Increment failure count without setting error flag
        Used for recoverable issues
        """
        try:
            timestamp = int(time())
            self.failure_count = min(self.failure_count + 1, self.max_failures)
            self.healthcheck_file.write_text(f"{timestamp},{self.failure_count}")
            self.last_update = timestamp
            logger.debug(f"⚠️  Healthcheck: Failure count {self.failure_count}/{self.max_failures}")
        except Exception as e:
            logger.error(f"❌ Failed to update healthcheck (failure): {e}")
    
    def update_recovering(self):
        """
        Update timestamp while in recovery mode
        Maintains current failure count but updates timestamp
        """
        try:
            timestamp = int(time())
            # Keep existing failure count but update timestamp
            if self.failure_count > 0:
                self.failure_count = max(0, self.failure_count - 1)
            self.healthcheck_file.write_text(f"{timestamp},{self.failure_count}")
            self.last_update = timestamp
            logger.debug(f"🔄 Healthcheck: Recovering (failures: {self.failure_count})")
        except Exception as e:
            logger.error(f"❌ Failed to update healthcheck (recovering): {e}")
    
    def heartbeat(self):
        """
        Simple heartbeat update - maintains current state
        Call this regularly (every 30-60 seconds) to show the app is alive
        """
        try:
            timestamp = int(time())
            
            # Only update if significant time has passed (avoid excessive writes)
            if timestamp - self.last_update >= 30:
                # If no failures, mark as healthy
                if self.failure_count == 0:
                    self.update_healthy()
                else:
                    # Otherwise just update timestamp with current failure count
                    self.healthcheck_file.write_text(f"{timestamp},{self.failure_count}")
                    self.last_update = timestamp
        except Exception as e:
            logger.error(f"❌ Failed to update healthcheck (heartbeat): {e}")
    
    def get_status(self) -> dict:
        """
        Get current healthcheck status
        
        Returns:
            dict with status information
        """
        try:
            if not self.healthcheck_file.exists():
                return {"status": "unknown", "message": "Healthcheck file not found"}
            
            content = self.healthcheck_file.read_text().strip()
            parts = content.split(',')
            
            timestamp = int(parts[0])
            failures = int(parts[1]) if len(parts) > 1 else 0
            is_error = len(parts) > 2 and parts[2] == "ERROR"
            age = int(time()) - timestamp
            
            if is_error:
                status = "error"
                message = f"Error state with {failures} failures, {age}s old"
            elif failures >= self.max_failures:
                status = "unhealthy"
                message = f"Too many failures ({failures}), {age}s old"
            elif age > 180:
                status = "stale"
                message = f"Timestamp is {age}s old (max 180s)"
            else:
                status = "healthy"
                message = f"Healthy, {age}s old, {failures} failures"
            
            return {
                "status": status,
                "message": message,
                "timestamp": timestamp,
                "age_seconds": age,
                "failures": failures,
                "is_error": is_error
            }
        except Exception as e:
            return {"status": "error", "message": f"Failed to read status: {e}"}


# Global instance for easy access
_healthcheck_writer = None


def get_healthcheck_writer() -> HealthcheckWriter:
    """Get or create the global healthcheck writer instance"""
    global _healthcheck_writer
    if _healthcheck_writer is None:
        _healthcheck_writer = HealthcheckWriter()
    return _healthcheck_writer
