"""
Metrics collection system for Twitch Drops Miner
Tracks drops claimed, streams watched, and performance statistics
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict
from typing import Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from constants import JsonType

logger = logging.getLogger("TwitchDrops")


class Metrics:
    """Collects and tracks application metrics"""
    
    def __init__(self, metrics_path: Path | None = None):
        self.metrics_path = metrics_path
        self.drops_claimed = 0
        self.drops_failed = 0
        self.streams_watched: Dict[str, int] = defaultdict(int)  # channel -> minutes
        self.watch_attempts = 0
        self.watch_successes = 0
        self.uptime_start = datetime.now(timezone.utc)
        self.last_drop_time: datetime | None = None
        self.errors: Dict[str, int] = defaultdict(int)  # error_type -> count
        
        # Load existing metrics if available
        if self.metrics_path and self.metrics_path.exists():
            self._load_metrics()
    
    def record_drop(self, success: bool, drop_name: str = "unknown"):
        """Record a drop claim attempt"""
        if success:
            self.drops_claimed += 1
            self.last_drop_time = datetime.now(timezone.utc)
            logger.info(f"📊 Metrics: Total drops claimed: {self.drops_claimed}")
        else:
            self.drops_failed += 1
            logger.warning(f"📊 Metrics: Drop claim failed for {drop_name}")
        
        self._save_metrics()
    
    def record_stream_watch(self, channel_name: str, minutes: int = 1):
        """Record time spent watching a channel"""
        self.streams_watched[channel_name] += minutes
    
    def record_watch_attempt(self, success: bool):
        """Record a watch request attempt"""
        self.watch_attempts += 1
        if success:
            self.watch_successes += 1
    
    def record_error(self, error_type: str):
        """Record an error occurrence"""
        self.errors[error_type] += 1
    
    def get_watch_success_rate(self) -> float:
        """Calculate the success rate of watch attempts"""
        if self.watch_attempts == 0:
            return 0.0
        return (self.watch_successes / self.watch_attempts) * 100
    
    def get_uptime_seconds(self) -> float:
        """Get application uptime in seconds"""
        return (datetime.now(timezone.utc) - self.uptime_start).total_seconds()
    
    def get_stats(self) -> JsonType:
        """Get all metrics as a dictionary"""
        return {
            "drops_claimed": self.drops_claimed,
            "drops_failed": self.drops_failed,
            "drop_success_rate": (
                (self.drops_claimed / (self.drops_claimed + self.drops_failed) * 100)
                if (self.drops_claimed + self.drops_failed) > 0 else 0.0
            ),
            "watch_attempts": self.watch_attempts,
            "watch_successes": self.watch_successes,
            "watch_success_rate": self.get_watch_success_rate(),
            "uptime_seconds": self.get_uptime_seconds(),
            "uptime_hours": self.get_uptime_seconds() / 3600,
            "streams_watched": dict(self.streams_watched),
            "total_minutes_watched": sum(self.streams_watched.values()),
            "last_drop_time": (
                self.last_drop_time.isoformat() if self.last_drop_time else None
            ),
            "errors": dict(self.errors),
            "last_updated": datetime.now(timezone.utc).isoformat()
        }
    
    def get_summary(self) -> str:
        """Get a human-readable summary of metrics"""
        stats = self.get_stats()
        uptime_hours = stats["uptime_hours"]
        
        lines = [
            "📊 === Metrics Summary ===",
            f"Uptime: {uptime_hours:.1f} hours",
            f"Drops Claimed: {stats['drops_claimed']} (Success Rate: {stats['drop_success_rate']:.1f}%)",
            f"Watch Requests: {stats['watch_successes']}/{stats['watch_attempts']} (Success Rate: {stats['watch_success_rate']:.1f}%)",
            f"Total Watch Time: {stats['total_minutes_watched']} minutes",
            f"Channels Watched: {len(self.streams_watched)}",
        ]
        
        if self.last_drop_time:
            time_since_last = (datetime.now(timezone.utc) - self.last_drop_time).total_seconds() / 60
            lines.append(f"Last Drop: {time_since_last:.0f} minutes ago")
        
        if self.errors:
            lines.append(f"Errors: {sum(self.errors.values())} total")
        
        return "\n".join(lines)
    
    def _save_metrics(self):
        """Save metrics to disk"""
        if not self.metrics_path:
            return
        
        try:
            with open(self.metrics_path, 'w', encoding='utf-8') as f:
                json.dump(self.get_stats(), f, indent=2)
        except Exception as e:
            logger.debug(f"Failed to save metrics: {e}")
    
    def _load_metrics(self):
        """Load metrics from disk"""
        if not self.metrics_path or not self.metrics_path.exists():
            return
        
        try:
            with open(self.metrics_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            self.drops_claimed = data.get("drops_claimed", 0)
            self.drops_failed = data.get("drops_failed", 0)
            self.watch_attempts = data.get("watch_attempts", 0)
            self.watch_successes = data.get("watch_successes", 0)
            self.streams_watched = defaultdict(int, data.get("streams_watched", {}))
            self.errors = defaultdict(int, data.get("errors", {}))
            
            if last_drop := data.get("last_drop_time"):
                self.last_drop_time = datetime.fromisoformat(last_drop)
            
            logger.info("📊 Loaded existing metrics from disk")
        except Exception as e:
            logger.warning(f"Failed to load metrics: {e}")
    
    def reset(self):
        """Reset all metrics to zero"""
        self.drops_claimed = 0
        self.drops_failed = 0
        self.streams_watched.clear()
        self.watch_attempts = 0
        self.watch_successes = 0
        self.last_drop_time = None
        self.errors.clear()
        self.uptime_start = datetime.now(timezone.utc)
        self._save_metrics()
        logger.info("📊 Metrics reset")
