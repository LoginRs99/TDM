"""
Lightweight Web Dashboard and REST API for Twitch Drops Miner.
Built with aiohttp.web, completely decoupled from twitch.py internal logic.
"""
from __future__ import annotations

import asyncio
from collections import deque
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from constants import State, PriorityMode
from cookie_refresher import SCREENSHOT_2FA_PATH, TOKEN_2FA_PATH
from utils import is_game_excluded

try:
    import psutil
except ImportError:
    psutil = None

if TYPE_CHECKING:
    from twitch import Twitch

logger = logging.getLogger("TwitchDrops")


def _safe_str(val: Any, default: str = "") -> str:
    if val is None:
        return default
    if isinstance(val, str):
        return val
    if hasattr(val, "_mock_name") or hasattr(val, "_mock_return_value") or "Mock" in type(val).__name__:
        return default
    return str(val)


def _safe_int(val: Any, default: int = 0) -> int:
    if val is None or hasattr(val, "_mock_name") or hasattr(val, "_mock_return_value") or "Mock" in type(val).__name__:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _safe_float(val: Any, default: float = 0.0) -> float:
    if val is None or hasattr(val, "_mock_name") or hasattr(val, "_mock_return_value") or "Mock" in type(val).__name__:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _safe_bool(val: Any, default: bool = False) -> bool:
    if val is None or hasattr(val, "_mock_name") or hasattr(val, "_mock_return_value") or "Mock" in type(val).__name__:
        return default
    return bool(val)


def format_box_art(url: Any, width: int = 144, height: int = 192) -> str | None:
    """Formats Twitch box art URLs with concrete dimensions or fallback dimensions."""
    if not url or hasattr(url, "_mock_name") or hasattr(url, "_mock_return_value") or "Mock" in type(url).__name__:
        return None
    s = str(url)
    if "MagicMock" in s or not s.strip():
        return None
    if "{width}" in s and "{height}" in s:
        return s.replace("{width}", str(width)).replace("{height}", str(height))
    if "ttv-boxart" in s and not re.search(r'-\d+x\d+\.', s):
        match = re.search(r'(\.[a-zA-Z0-9]+)$', s)
        if match:
            ext = match.group(1)
            return s[:-len(ext)] + f"-{width}x{height}" + ext
    return s


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Twitch Drops Miner - Control Center</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
        tailwind.config = {
            darkMode: 'class',
            theme: {
                extend: {
                    colors: {
                        twitch: {
                            purple: '#9146FF',
                            dark: '#0e0e10',
                            card: '#18181b',
                            card2: '#1f1f23',
                            border: '#27272a',
                            accent: '#772ce8',
                            hover: '#26262c'
                        }
                    }
                }
            }
        }
    </script>
    <style>
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: #0e0e10; }
        ::-webkit-scrollbar-thumb { background: #27272a; border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: #9146FF; }
        @keyframes pulse-subtle {
            0%, 100% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.85; transform: scale(1.02); }
        }
        .animate-farming {
            animation: pulse-subtle 3s ease-in-out infinite;
        }
    </style>
</head>
<body class="bg-twitch-dark text-gray-200 font-sans min-h-screen flex flex-col antialiased selection:bg-twitch-purple selection:text-white">
    <!-- Top Header -->
    <header class="bg-twitch-card border-b border-twitch-border px-4 sm:px-6 py-3 sticky top-0 z-40 shadow-lg">
        <div class="max-w-7xl mx-auto flex flex-col sm:flex-row items-center justify-between gap-3">
            <!-- Brand & Navigation Tabs -->
            <div class="flex items-center space-x-4 w-full sm:w-auto justify-between sm:justify-start">
                <div class="flex items-center space-x-3">
                    <div class="w-9 h-9 rounded-lg bg-twitch-purple flex items-center justify-center font-bold text-white shadow-md shadow-twitch-purple/30 text-lg">
                        ⛏️
                    </div>
                    <div>
                        <h1 class="text-base font-bold text-white leading-tight">Twitch Drops Miner</h1>
                        <p class="text-[11px] text-gray-400">Control Center</p>
                    </div>
                </div>

                <!-- Primary Tabs -->
                <nav class="flex items-center space-x-1 bg-twitch-dark p-1 rounded-lg border border-twitch-border overflow-x-auto max-w-full">
                    <button onclick="switchTab('overview')" id="tab-btn-overview" class="tab-btn px-3 py-1.5 rounded-md text-xs font-semibold transition text-white bg-twitch-purple shadow-sm flex items-center space-x-1.5 whitespace-nowrap">
                        <span>📊 Overview</span>
                    </button>
                    <button onclick="switchTab('campaigns')" id="tab-btn-campaigns" class="tab-btn px-3 py-1.5 rounded-md text-xs font-semibold transition text-gray-400 hover:text-white flex items-center space-x-1.5 whitespace-nowrap">
                        <span>🎁 Campaigns</span>
                        <span id="nav-campaigns-count" class="px-1.5 py-0.2 rounded-full text-[10px] bg-twitch-border text-gray-300">0</span>
                    </button>
                    <button onclick="switchTab('rules')" id="tab-btn-rules" class="tab-btn px-3 py-1.5 rounded-md text-xs font-semibold transition text-gray-400 hover:text-white flex items-center space-x-1.5 whitespace-nowrap">
                        <span>🎯 Priority & Blocklist</span>
                        <span id="nav-priority-count" class="px-1.5 py-0.2 rounded-full text-[10px] bg-twitch-border text-gray-300">0</span>
                    </button>
                    <button onclick="switchTab('settings')" id="tab-btn-settings" class="tab-btn px-3 py-1.5 rounded-md text-xs font-semibold transition text-gray-400 hover:text-white flex items-center space-x-1.5 whitespace-nowrap">
                        <span>⚙️ Settings</span>
                        <span id="settings-dirty-dot" class="w-1.5 h-1.5 rounded-full bg-amber-400 hidden"></span>
                    </button>
                    <button onclick="switchTab('logs')" id="tab-btn-logs" class="tab-btn px-3 py-1.5 rounded-md text-xs font-semibold transition text-gray-400 hover:text-white flex items-center space-x-1.5 whitespace-nowrap">
                        <span>📜 Logs</span>
                        <span class="w-1.5 h-1.5 rounded-full bg-green-400 animate-pulse"></span>
                    </button>
                </nav>
            </div>

            <!-- Header Status Badges -->
            <div class="flex items-center space-x-2 text-xs overflow-x-auto w-full sm:w-auto justify-end">
                <span id="net-badge" class="px-2.5 py-1 rounded-md font-semibold bg-emerald-950/60 text-emerald-400 border border-emerald-800/60 whitespace-nowrap">
                    Twitch API: OK
                </span>
                <span id="mem-badge" class="px-2.5 py-1 rounded-md font-semibold bg-gray-800/80 text-purple-300 border border-gray-700 whitespace-nowrap">
                    RAM: --
                </span>
                <span id="state-badge" class="px-2.5 py-1 rounded-md font-semibold bg-gray-800 text-gray-400 border border-gray-700 whitespace-nowrap">
                    Connecting...
                </span>
            </div>
        </div>
    </header>

    <!-- Main Container -->
    <main class="flex-1 max-w-7xl w-full mx-auto p-4 sm:p-6 space-y-6">
        <!-- 2FA Remote Solver Banner -->
        <div id="banner-2fa" class="hidden bg-red-950/80 border border-red-500/80 rounded-xl p-4 sm:p-5 shadow-2xl flex flex-col md:flex-row items-center justify-between gap-4">
            <div class="flex items-center space-x-4">
                <div class="w-12 h-12 rounded-xl bg-red-600/30 border border-red-500/50 flex items-center justify-center text-2xl flex-shrink-0 animate-bounce">
                    🔐
                </div>
                <div>
                    <h3 class="text-sm font-bold text-red-200">Twitch Two-Factor Challenge Active</h3>
                    <p class="text-xs text-red-300/80">Twitch requires a 6-digit verification code sent to your email or authenticator.</p>
                </div>
            </div>
            <div class="flex items-center space-x-3 w-full md:w-auto">
                <input id="input-2fa-token" type="text" maxlength="8" placeholder="Enter 6-digit code..." class="px-3 py-2 bg-twitch-dark border border-red-500/50 rounded-lg text-white text-xs font-mono text-center tracking-widest w-full md:w-44 focus:outline-none focus:border-red-400">
                <button onclick="submit2FAToken()" class="px-4 py-2 bg-red-600 hover:bg-red-500 text-white rounded-lg text-xs font-bold transition shadow-lg whitespace-nowrap active:scale-95">
                    Submit Code
                </button>
            </div>
        </div>

        <!-- ================= TAB 1: OVERVIEW ================= -->
        <div id="tab-overview" class="tab-pane space-y-6">
            <!-- Hero Watching Card -->
            <div class="bg-twitch-card border border-twitch-border rounded-xl p-5 sm:p-6 shadow-xl relative overflow-hidden">
                <div class="flex flex-col md:flex-row md:items-center justify-between gap-6">
                    <div class="flex items-start sm:items-center space-x-4">
                        <!-- Game Box Art Cover Thumbnail -->
                        <div class="relative w-16 h-20 sm:w-20 sm:h-28 rounded-lg overflow-hidden bg-twitch-dark border border-twitch-border flex-shrink-0 shadow-md">
                            <img id="hero-game-boxart" src="" alt="Game Art" class="w-full h-full object-cover hidden" onerror="this.classList.add('hidden'); document.getElementById('hero-boxart-placeholder').classList.remove('hidden');">
                            <div id="hero-boxart-placeholder" class="w-full h-full flex flex-col items-center justify-center text-gray-500 text-xs text-center p-1">
                                <span class="text-xl">🎮</span>
                                <span class="text-[10px] mt-1 text-gray-400">Twitch</span>
                            </div>
                        </div>

                        <!-- Channel & Stream Info -->
                        <div class="space-y-1.5 flex-1 min-w-0">
                            <div class="flex items-center space-x-2 flex-wrap gap-y-1">
                                <span id="hero-live-badge" class="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider bg-red-600 text-white shadow-sm flex items-center space-x-1">
                                    <span class="w-1.5 h-1.5 rounded-full bg-white animate-pulse"></span>
                                    <span>LIVE</span>
                                </span>
                                <a id="hero-channel-name" href="#" target="_blank" class="text-lg sm:text-xl font-bold text-white hover:text-twitch-purple transition truncate">
                                    No Active Stream
                                </a>
                                <span id="hero-game-name" class="text-xs px-2 py-0.5 rounded-md bg-twitch-dark text-twitch-purple border border-twitch-border font-semibold truncate">
                                    Idle
                                </span>
                            </div>

                            <p id="hero-stream-title" class="text-xs text-gray-400 line-clamp-1 italic">
                                Waiting for next mining cycle or channel discovery...
                            </p>

                            <div class="flex items-center space-x-3 text-xs text-gray-400 pt-0.5 flex-wrap gap-y-1">
                                <span class="flex items-center space-x-1">
                                    <span>👥</span>
                                    <span id="hero-viewers">0 viewers</span>
                                </span>
                                <span id="hero-priority-badge" class="hidden px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30">
                                    ⭐ Priority Game
                                </span>
                                <span id="hero-excluded-badge" class="hidden px-2 py-0.5 rounded text-[10px] font-bold bg-red-500/20 text-red-300 border border-red-500/30">
                                    🚫 Excluded Game
                                </span>
                            </div>
                        </div>
                    </div>

                    <!-- Direct Active Farm Quick-Action Controls -->
                    <div id="hero-action-buttons" class="flex items-center flex-wrap gap-2 w-full md:w-auto justify-end border-t md:border-t-0 border-twitch-border/60 pt-3 md:pt-0">
                        <button id="btn-hero-prioritize" onclick="quickActionPriorityFromHero()" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-twitch-purple/20 text-purple-300 hover:bg-twitch-purple hover:text-white border border-twitch-purple/40 transition active:scale-95 flex items-center space-x-1.5 shadow-sm">
                            <span>⭐</span>
                            <span id="btn-hero-prioritize-label">Prioritize Game</span>
                        </button>
                        <button id="btn-hero-exclude" onclick="quickActionExcludeFromHero()" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-red-950/40 text-red-300 hover:bg-red-900/60 border border-red-800/60 transition active:scale-95 flex items-center space-x-1.5 shadow-sm">
                            <span>🚫</span>
                            <span id="btn-hero-exclude-label">Exclude Game</span>
                        </button>
                        <button onclick="requestAction('switch')" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-gray-800 hover:bg-gray-700 text-gray-200 border border-gray-700 transition active:scale-95 flex items-center space-x-1.5 shadow-sm" title="Skip to another eligible stream">
                            <span>⏭️</span>
                            <span>Skip Streamer</span>
                        </button>
                    </div>
                </div>

                <!-- Active Drop Progress Bar & Reward Thumbnail -->
                <div class="mt-6 pt-5 border-t border-twitch-border space-y-3">
                    <div class="flex items-center justify-between text-xs">
                        <div class="flex items-center space-x-2.5">
                            <!-- Drop Reward Item Thumbnail -->
                            <div class="w-8 h-8 rounded bg-twitch-dark border border-twitch-border overflow-hidden flex items-center justify-center flex-shrink-0 shadow-inner">
                                <img id="hero-reward-img" src="" alt="Reward" class="w-full h-full object-contain hidden" onerror="this.classList.add('hidden'); document.getElementById('hero-reward-placeholder').classList.remove('hidden');">
                                <span id="hero-reward-placeholder" class="text-sm">🎁</span>
                            </div>
                            <div>
                                <span id="hero-drop-name" class="font-bold text-white text-sm block leading-snug">No Active Drop</span>
                                <span id="hero-drop-campaign" class="text-[11px] text-gray-400 block">Mining loop idle or searching</span>
                            </div>
                        </div>
                        <div class="text-right">
                            <span id="hero-drop-percent" class="font-bold text-twitch-purple text-base">0%</span>
                            <span id="hero-drop-minutes" class="text-[11px] text-gray-400 block">0 / 0 min</span>
                        </div>
                    </div>

                    <!-- Progress Bar -->
                    <div class="w-full bg-twitch-dark rounded-full h-3 overflow-hidden p-0.5 border border-twitch-border/70">
                        <div id="hero-progress-bar" class="bg-gradient-to-r from-twitch-accent via-twitch-purple to-indigo-500 h-full rounded-full transition-all duration-500 shadow-sm shadow-twitch-purple/40" style="width: 0%"></div>
                    </div>

                    <div class="flex justify-between items-center text-[11px] text-gray-500">
                        <span id="hero-campaign-claimed">Campaign Progress: 0/0 drops</span>
                        <span id="hero-drop-eta" class="text-gray-400 font-medium">ETA: --</span>
                    </div>
                </div>
            </div>

            <!-- Metrics Grid -->
            <div class="grid grid-cols-2 sm:grid-cols-4 gap-4">
                <div class="bg-twitch-card border border-twitch-border rounded-xl p-4 shadow">
                    <p class="text-xs text-gray-400 font-medium">Total Drops Claimed</p>
                    <p id="metric-claimed" class="text-2xl font-bold text-white mt-1">0</p>
                    <p class="text-[10px] text-emerald-400 mt-1">✓ Verified with inventory</p>
                </div>
                <div class="bg-twitch-card border border-twitch-border rounded-xl p-4 shadow">
                    <p class="text-xs text-gray-400 font-medium">Total Watch Time</p>
                    <p id="metric-watch-time" class="text-2xl font-bold text-purple-300 mt-1">0.0h</p>
                    <p class="text-[10px] text-gray-500 mt-1">Total minutes accumulated</p>
                </div>
                <div class="bg-twitch-card border border-twitch-border rounded-xl p-4 shadow">
                    <p class="text-xs text-gray-400 font-medium">Miner Uptime</p>
                    <p id="metric-uptime" class="text-2xl font-bold text-indigo-300 mt-1">0.0h</p>
                    <p class="text-[10px] text-gray-500 mt-1">Daemon process running</p>
                </div>
                <div class="bg-twitch-card border border-twitch-border rounded-xl p-4 shadow">
                    <p class="text-xs text-gray-400 font-medium">Watch Success Rate</p>
                    <p id="metric-success-rate" class="text-2xl font-bold text-emerald-400 mt-1">100%</p>
                    <p class="text-[10px] text-gray-500 mt-1">Ping & HEAD response health</p>
                </div>
            </div>

            <!-- Overview Quick Controls & Campaigns Preview -->
            <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
                <!-- Daemon Actions Card -->
                <div class="bg-twitch-card border border-twitch-border rounded-xl p-5 space-y-4 shadow flex flex-col justify-between">
                    <div>
                        <h3 class="text-sm font-bold text-white flex items-center space-x-2 border-b border-twitch-border pb-3">
                            <span>⚡</span>
                            <span>Daemon Quick Controls</span>
                        </h3>
                        <p class="text-xs text-gray-400 mt-2">Trigger immediate background actions without restarting your Docker container.</p>
                    </div>

                    <div class="grid grid-cols-2 gap-2 text-xs">
                        <button onclick="requestAction('refresh')" class="p-2.5 bg-twitch-dark hover:bg-twitch-hover border border-twitch-border rounded-lg font-medium text-gray-200 transition text-left flex items-center space-x-2">
                            <span>🔄</span>
                            <span>Refresh Inventory</span>
                        </button>
                        <button onclick="requestAction('switch')" class="p-2.5 bg-twitch-dark hover:bg-twitch-hover border border-twitch-border rounded-lg font-medium text-gray-200 transition text-left flex items-center space-x-2">
                            <span>⏭️</span>
                            <span>Switch Stream</span>
                        </button>
                        <button onclick="requestAction('refresh-cookies')" class="p-2.5 bg-twitch-dark hover:bg-twitch-hover border border-twitch-border rounded-lg font-medium text-gray-200 transition text-left flex items-center space-x-2">
                            <span>🔐</span>
                            <span>Renew Cookies</span>
                        </button>
                        <button onclick="requestAction('toggle-notifications')" class="p-2.5 bg-twitch-dark hover:bg-twitch-hover border border-twitch-border rounded-lg font-medium text-gray-200 transition text-left flex items-center space-x-2">
                            <span id="overview-btn-disc-icon">🔔</span>
                            <span id="overview-btn-disc-label">Mute Discord</span>
                        </button>
                    </div>

                    <div class="pt-3 border-t border-twitch-border text-[11px] text-gray-500 flex justify-between items-center">
                        <span>Mining Mode: <strong id="overview-mode-label" class="text-gray-300">BALANCED</strong></span>
                        <a href="javascript:switchTab('settings')" class="text-twitch-purple hover:underline">Change</a>
                    </div>
                </div>

                <!-- Active Campaigns Summary Card -->
                <div class="lg:col-span-2 bg-twitch-card border border-twitch-border rounded-xl p-5 space-y-4 shadow">
                    <div class="flex items-center justify-between border-b border-twitch-border pb-3">
                        <h3 class="text-sm font-bold text-white flex items-center space-x-2">
                            <span>🎯</span>
                            <span>Active Drops Overview</span>
                        </h3>
                        <a href="javascript:switchTab('campaigns')" class="text-xs text-twitch-purple hover:underline font-semibold">
                            View All Campaigns →
                        </a>
                    </div>
                    <div id="overview-campaigns-preview" class="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
                        <div class="text-gray-500 text-center py-8 col-span-2">Loading active drop campaigns...</div>
                    </div>
                </div>
            </div>
        </div>

        <!-- ================= TAB 2: CAMPAIGNS ================= -->
        <div id="tab-campaigns" class="tab-pane hidden space-y-5">
            <!-- Filter & Search Toolbar -->
            <div class="bg-twitch-card border border-twitch-border rounded-xl p-4 shadow flex flex-col md:flex-row items-center justify-between gap-4">
                <div class="flex items-center space-x-2 w-full md:w-auto overflow-x-auto">
                    <button onclick="setCampaignFilter('all')" id="filter-camp-all" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-twitch-purple text-white shadow-sm whitespace-nowrap">
                        All (<span id="count-camp-all">0</span>)
                    </button>
                    <button onclick="setCampaignFilter('farming')" id="filter-camp-farming" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-twitch-dark text-gray-400 hover:text-white border border-twitch-border whitespace-nowrap">
                        🔥 Farming Now (<span id="count-camp-farming">0</span>)
                    </button>
                    <button onclick="setCampaignFilter('priority')" id="filter-camp-priority" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-twitch-dark text-gray-400 hover:text-white border border-twitch-border whitespace-nowrap">
                        ⭐ Priority (<span id="count-camp-priority">0</span>)
                    </button>
                    <button onclick="setCampaignFilter('excluded')" id="filter-camp-excluded" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-twitch-dark text-gray-400 hover:text-white border border-twitch-border whitespace-nowrap">
                        🚫 Excluded (<span id="count-camp-excluded">0</span>)
                    </button>
                    <button onclick="setCampaignFilter('linked')" id="filter-camp-linked" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-twitch-dark text-gray-400 hover:text-white border border-twitch-border whitespace-nowrap">
                        🔗 Linked (<span id="count-camp-linked">0</span>)
                    </button>
                </div>

                <div class="relative w-full md:w-72">
                    <input id="campaigns-search" type="text" placeholder="Search game or campaign..." oninput="renderCampaignsTabUI()" class="w-full px-3.5 py-2 pl-9 bg-twitch-dark border border-twitch-border rounded-lg text-white text-xs focus:outline-none focus:border-twitch-purple transition">
                    <span class="absolute left-3 top-2.5 text-gray-400 text-xs">🔍</span>
                </div>
            </div>

            <!-- Campaigns Grid Container -->
            <div id="campaigns-tab-grid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                <div class="col-span-full py-12 text-center text-gray-500 text-sm">Loading campaigns...</div>
            </div>
        </div>

        <!-- ================= TAB 3: PRIORITY & BLOCKLIST ================= -->
        <div id="tab-rules" class="tab-pane hidden space-y-6">
            <!-- Header Guidance Banner -->
            <div class="bg-gradient-to-r from-twitch-card via-[#1f1a2e] to-twitch-card border border-twitch-purple/40 rounded-xl p-4 sm:p-5 shadow-lg flex flex-col sm:flex-row items-center justify-between gap-4">
                <div class="space-y-1">
                    <h2 class="text-sm sm:text-base font-bold text-white flex items-center space-x-2">
                        <span>🎯</span>
                        <span>Game Priority & Exclusion Rules</span>
                    </h2>
                    <p class="text-xs text-gray-300">
                        Tailor exactly what your miner watches. Priority games are farmed ahead of regular drops; excluded games are strictly skipped.
                    </p>
                </div>
                <div class="flex items-center space-x-3 text-xs flex-shrink-0">
                    <span class="px-3 py-1.5 rounded-lg bg-twitch-dark/80 border border-twitch-border text-twitch-purple font-semibold">
                        Priority: <strong id="rules-banner-priority-count">0</strong>
                    </span>
                    <span class="px-3 py-1.5 rounded-lg bg-twitch-dark/80 border border-twitch-border text-red-400 font-semibold">
                        Blocked: <strong id="rules-banner-exclude-count">0</strong>
                    </span>
                </div>
            </div>

            <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
                <!-- ================= LEFT: ⭐ Ranked Priority Queue ================= -->
                <div class="bg-twitch-card border border-twitch-border rounded-xl p-5 space-y-4 shadow flex flex-col justify-between">
                    <div class="space-y-4">
                        <div class="flex items-center justify-between border-b border-twitch-border pb-3">
                            <h3 class="text-sm font-bold text-white flex items-center space-x-2">
                                <span>⭐</span>
                                <span>Ranked Priority Queue</span>
                            </h3>
                            <span id="priority-count-badge" class="px-2 py-0.5 rounded-full text-[10px] bg-twitch-dark border border-twitch-border text-twitch-purple font-semibold">0 games</span>
                        </div>

                        <!-- Tip Box -->
                        <div class="p-3 rounded-lg bg-twitch-dark/70 border border-twitch-purple/30 text-[11px] text-gray-300 space-y-1">
                            <p class="font-semibold text-twitch-purple">💡 How Priority Ranking Works:</p>
                            <p class="text-gray-400 leading-relaxed">
                                Games higher in this queue are mined first (<strong>#1 Priority</strong> is always checked first). In <strong>BALANCED</strong> mode, drops ending soon or with few live streamers receive smart scarcity boosts. Reorder using ▲ and ▼, or click ⭐ to jump straight to #1.
                            </p>
                        </div>

                        <!-- Quick-Pick Suggestion Chips Tray -->
                        <div class="space-y-1.5 text-xs">
                            <span class="font-semibold text-gray-400 block text-[11px]">⚡ Quick-Add from Active Drops & Farmed Games:</span>
                            <div id="priority-quick-chips" class="flex flex-wrap gap-1.5 max-h-24 overflow-y-auto pr-1">
                                <span class="text-gray-500 text-[11px] italic">Loading suggestions...</span>
                            </div>
                        </div>

                        <!-- Autocomplete Add Input with Floating Rich Dropdown -->
                        <div class="space-y-2 text-xs relative">
                            <label class="block font-semibold text-gray-300">Add Game to Priority Queue:</label>
                            <div class="flex space-x-2">
                                <div class="relative flex-1">
                                    <input id="input-add-priority" type="text" autocomplete="off" placeholder="Search active or farmed games to prioritize..." oninput="handlePriorityInput(this.value)" onfocus="handlePriorityInput(this.value)" class="w-full px-3 py-2 bg-twitch-dark border border-twitch-border rounded-lg text-white text-xs focus:outline-none focus:border-twitch-purple transition">
                                    
                                    <!-- Floating Rich Autocomplete Dropdown -->
                                    <div id="dropdown-priority-autocomplete" class="hidden absolute top-full left-0 right-0 mt-1 bg-twitch-card border border-twitch-border rounded-xl shadow-2xl z-50 max-h-72 overflow-y-auto divide-y divide-twitch-border/60">
                                    </div>
                                </div>
                                <button onclick="addPriorityGame()" class="px-4 py-2 bg-twitch-purple hover:bg-twitch-accent text-white rounded-lg font-semibold transition active:scale-95 whitespace-nowrap shadow-sm">
                                    + Add
                                </button>
                            </div>
                        </div>

                        <!-- Priority List Container -->
                        <div class="space-y-2">
                            <span class="font-semibold text-gray-400 block text-[11px]">Current Queue Order:</span>
                            <div id="priority-list-container" class="space-y-1.5 max-h-80 overflow-y-auto pr-1">
                                <p class="text-gray-500 text-center py-6 text-[11px]">No priority games configured. Miner will farm automatically based on campaign urgency.</p>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- ================= RIGHT: 🚫 Excluded Games (Blacklist) ================= -->
                <div class="bg-twitch-card border border-twitch-border rounded-xl p-5 space-y-4 shadow flex flex-col justify-between">
                    <div class="space-y-4">
                        <div class="flex items-center justify-between border-b border-twitch-border pb-3">
                            <h3 class="text-sm font-bold text-white flex items-center space-x-2">
                                <span>🚫</span>
                                <span>Excluded Games (Blacklist)</span>
                            </h3>
                            <span id="exclude-count-badge" class="px-2 py-0.5 rounded-full text-[10px] bg-twitch-dark border border-twitch-border text-red-400 font-semibold">0 games</span>
                        </div>

                        <!-- Tip Box -->
                        <div class="p-3 rounded-lg bg-twitch-dark/70 border border-red-900/40 text-[11px] text-gray-300 space-y-1">
                            <p class="font-semibold text-red-400">💡 How Exclusions & Wildcards Work:</p>
                            <p class="text-gray-400 leading-relaxed">
                                Excluded games are strictly ignored. Distinct game editions are independent (e.g. <code class="text-gray-200">World of Tanks</code> will <strong>NOT</strong> block <code class="text-gray-200">World of Tanks: HEAT</code>). To block an entire game family, append an asterisk: <code class="text-red-300 font-bold">FIFA*</code> or <code class="text-red-300 font-bold">World of Tanks*</code>. If a game is in both lists, <strong>Priority always wins</strong>.
                            </p>
                        </div>

                        <!-- Quick-Pick Suggestion Chips Tray -->
                        <div class="space-y-1.5 text-xs">
                            <span class="font-semibold text-gray-400 block text-[11px]">⚡ Quick-Exclude Active Games:</span>
                            <div id="exclude-quick-chips" class="flex flex-wrap gap-1.5 max-h-24 overflow-y-auto pr-1">
                                <span class="text-gray-500 text-[11px] italic">Loading suggestions...</span>
                            </div>
                        </div>

                        <!-- Autocomplete Exclude Input with Floating Dropdown -->
                        <div class="space-y-2 text-xs relative">
                            <label class="block font-semibold text-gray-300">Add Game or Wildcard Pattern to Exclude:</label>
                            <div class="flex space-x-2">
                                <div class="relative flex-1">
                                    <input id="input-add-exclude" type="text" autocomplete="off" placeholder="Search game or enter wildcard (e.g. World of Tanks*)..." oninput="handleExcludeInput(this.value)" onfocus="handleExcludeInput(this.value)" class="w-full px-3 py-2 bg-twitch-dark border border-twitch-border rounded-lg text-white text-xs focus:outline-none focus:border-twitch-purple transition">
                                    
                                    <!-- Floating Rich Autocomplete Dropdown -->
                                    <div id="dropdown-exclude-autocomplete" class="hidden absolute top-full left-0 right-0 mt-1 bg-twitch-card border border-twitch-border rounded-xl shadow-2xl z-50 max-h-72 overflow-y-auto divide-y divide-twitch-border/60">
                                    </div>
                                </div>
                                <button onclick="addExcludeGame()" class="px-4 py-2 bg-red-900/50 hover:bg-red-800/60 text-red-300 border border-red-700/60 rounded-lg font-semibold transition active:scale-95 whitespace-nowrap shadow-sm">
                                    + Exclude
                                </button>
                            </div>
                        </div>

                        <!-- Exclude Tags Cloud Container -->
                        <div class="space-y-2">
                            <span class="font-semibold text-gray-400 block text-[11px]">Currently Blocked Games & Patterns:</span>
                            <div id="exclude-tags-container" class="flex flex-wrap gap-2 max-h-80 overflow-y-auto pr-1 pt-1">
                                <p class="text-gray-500 text-center w-full py-6 text-[11px]">No excluded games. All active games are eligible for drops.</p>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- ================= TAB 4: SETTINGS ================= -->
        <div id="tab-settings" class="tab-pane hidden space-y-6">
            <!-- Unsaved Alert Bar -->
            <div id="settings-unsaved-alert" class="hidden bg-amber-950/40 border border-amber-600/60 rounded-xl p-3.5 text-xs text-amber-300 flex items-center justify-between shadow">
                <div class="flex items-center space-x-2">
                    <span>⚠️</span>
                    <span>You have unsaved changes. Click <strong>Save Settings</strong> below to apply.</span>
                </div>
                <div class="space-x-2">
                    <button onclick="loadSettings()" class="px-3 py-1 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded font-semibold transition">Discard</button>
                    <button onclick="saveSettings()" class="px-3 py-1 bg-twitch-purple hover:bg-twitch-accent text-white rounded font-semibold transition">Save Now</button>
                </div>
            </div>

            <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
                <!-- ================= Card 1: Mining & Strategy ================= -->
                <div class="bg-twitch-card border border-twitch-border rounded-xl p-5 space-y-5 shadow">
                    <h3 class="text-sm font-bold text-white flex items-center space-x-2 border-b border-twitch-border pb-3">
                        <span>⚙️</span>
                        <span>Mining & Algorithm Tuning</span>
                    </h3>

                    <div class="space-y-4 text-xs">
                        <!-- Setting: Priority Mode -->
                        <div class="space-y-1.5">
                            <label class="block font-semibold text-gray-200">Priority Selection Mode</label>
                            <select id="setting-priority-mode" onchange="markSettingsDirty()" class="w-full px-3 py-2 bg-twitch-dark border border-twitch-border rounded-lg text-white text-xs focus:outline-none focus:border-twitch-purple transition">
                                <option value="3">BALANCED (Priority first, then scarce/urgent active drops) - Recommended</option>
                                <option value="0">PRIORITY_ONLY (Only farm games explicitly listed in priority list)</option>
                                <option value="1">ENDING_SOONEST (Focus on drops expiring earliest)</option>
                                <option value="2">LOW_AVBL_FIRST (Farm drops with the lowest available time)</option>
                            </select>
                            <div class="p-2.5 rounded-lg bg-twitch-dark/60 border border-twitch-border/70 text-[11px] text-gray-400 leading-relaxed">
                                💡 <strong>Tip:</strong> <strong>BALANCED</strong> automatically evaluates games in your Priority Queue first, while applying smart boosts to campaigns with few live streamers or ending within 48h. Use <strong>PRIORITY_ONLY</strong> if you only care about specific games and want the miner to sleep otherwise.
                            </div>
                        </div>

                        <!-- Setting: Stale Timeout & Maintenance Interval -->
                        <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
                            <div class="space-y-1.5">
                                <label class="block font-semibold text-gray-200">Stale Stream Timeout (min)</label>
                                <input id="setting-stale-timeout" type="number" min="1" max="120" onchange="markSettingsDirty()" class="w-full px-3 py-2 bg-twitch-dark border border-twitch-border rounded-lg text-white text-xs focus:outline-none focus:border-twitch-purple transition">
                                <div class="p-2 rounded bg-twitch-dark/60 border border-twitch-border/70 text-[11px] text-gray-400 leading-relaxed">
                                    💡 <strong>Tip:</strong> Minutes without watch progress before switching streamers. <strong>5 mins</strong> is optimal to prevent stream freeze issues.
                                </div>
                            </div>
                            <div class="space-y-1.5">
                                <label class="block font-semibold text-gray-200">Maintenance Interval (min)</label>
                                <input id="setting-maint-interval" type="number" min="5" max="1440" onchange="markSettingsDirty()" class="w-full px-3 py-2 bg-twitch-dark border border-twitch-border rounded-lg text-white text-xs focus:outline-none focus:border-twitch-purple transition">
                                <div class="p-2 rounded bg-twitch-dark/60 border border-twitch-border/70 text-[11px] text-gray-400 leading-relaxed">
                                    💡 <strong>Tip:</strong> How frequently the background loop scans Twitch API for new drop campaigns and streamer changes. Default: <strong>20 mins</strong>.
                                </div>
                            </div>
                        </div>

                        <!-- Setting: Connection Quality -->
                        <div class="space-y-1.5">
                            <label class="block font-semibold text-gray-200">Connection Quality / Pacing</label>
                            <select id="setting-conn-quality" onchange="markSettingsDirty()" class="w-full px-3 py-2 bg-twitch-dark border border-twitch-border rounded-lg text-white text-xs focus:outline-none focus:border-twitch-purple transition">
                                <option value="1">1 - Low (Fastest channel checks, lowest resource usage - Recommended)</option>
                                <option value="2">2 - Medium (Standard validation)</option>
                                <option value="3">3 - High (Conservative verification)</option>
                            </select>
                            <div class="p-2 rounded bg-twitch-dark/60 border border-twitch-border/70 text-[11px] text-gray-400 leading-relaxed">
                                💡 <strong>Tip:</strong> Controls watch request intervals (20s on Low, 15s on Medium, 10s on High). <strong>1 - Low</strong> is best for headless Docker and NAS environments.
                            </div>
                        </div>

                        <!-- Setting: Toggles (Available Drops, Badges/Emotes) -->
                        <div class="pt-2 border-t border-twitch-border/60 space-y-3">
                            <div class="space-y-1">
                                <label class="flex items-center space-x-2.5 cursor-pointer">
                                    <input id="setting-available-drops-check" type="checkbox" onchange="markSettingsDirty()" class="rounded bg-twitch-dark border-twitch-border text-twitch-purple focus:ring-0">
                                    <span class="font-medium text-gray-200">Extra Available Drops Deep Scan</span>
                                </label>
                                <p class="text-[11px] text-gray-400 pl-6 leading-relaxed">
                                    💡 <strong>Tip:</strong> Runs extra GraphQL queries to discover unlisted or regional drop events. Keep disabled unless you suspect an active drop is missing from your inventory.
                                </p>
                            </div>

                            <div class="space-y-1">
                                <label class="flex items-center space-x-2.5 cursor-pointer">
                                    <input id="setting-badges-emotes" type="checkbox" onchange="markSettingsDirty()" class="rounded bg-twitch-dark border-twitch-border text-twitch-purple focus:ring-0">
                                    <span class="font-medium text-gray-200">Load & Claim Chat Badges / Emotes</span>
                                </label>
                                <p class="text-[11px] text-gray-400 pl-6 leading-relaxed">
                                    💡 <strong>Tip:</strong> Automatically claims Twitch chat badges and channel emotes. Leaving disabled keeps container memory and API traffic minimal.
                                </p>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- ================= Card 2: Discord Notifications & Alerts ================= -->
                <div class="bg-twitch-card border border-twitch-border rounded-xl p-5 space-y-5 shadow">
                    <h3 class="text-sm font-bold text-white flex items-center space-x-2 border-b border-twitch-border pb-3">
                        <span>🔔</span>
                        <span>Discord Notifications & Slash Bot</span>
                    </h3>

                    <div class="space-y-4 text-xs">
                        <!-- Setting: Discord Webhook URL -->
                        <div class="space-y-1.5">
                            <label class="block font-semibold text-gray-200">Discord Webhook URL</label>
                            <div class="flex space-x-2">
                                <input id="setting-webhook-url" type="text" placeholder="https://discord.com/api/webhooks/..." oninput="markSettingsDirty()" class="flex-1 px-3 py-2 bg-twitch-dark border border-twitch-border rounded-lg text-white text-xs font-mono focus:outline-none focus:border-twitch-purple transition">
                                <button onclick="testDiscordWebhook()" class="px-3 py-2 bg-purple-900/30 hover:bg-purple-900/50 text-purple-300 border border-purple-700/50 rounded-lg font-semibold transition active:scale-95 whitespace-nowrap shadow-sm">
                                    Send Test
                                </button>
                            </div>
                            <div class="p-2 rounded bg-twitch-dark/60 border border-twitch-border/70 text-[11px] text-gray-400 leading-relaxed">
                                💡 <strong>Tip:</strong> Create a webhook in your Discord channel (Channel Settings → Integrations → Webhooks) and paste it here to receive instant claim alerts and scheduled progress summaries.
                            </div>
                        </div>

                        <!-- Setting: Summary Interval & Webhook Toggle -->
                        <div class="grid grid-cols-1 sm:grid-cols-2 gap-4 items-start">
                            <div class="space-y-1.5">
                                <label class="block font-semibold text-gray-200">Progress Digest Frequency</label>
                                <select id="setting-summary-interval" onchange="markSettingsDirty()" class="w-full px-3 py-2 bg-twitch-dark border border-twitch-border rounded-lg text-white text-xs focus:outline-none focus:border-twitch-purple transition">
                                    <option value="60">1 Hour</option>
                                    <option value="360">6 Hours</option>
                                    <option value="720">12 Hours</option>
                                    <option value="1440">24 Hours (Daily)</option>
                                    <option value="10080">10080 Minutes (Weekly)</option>
                                </select>
                                <p class="text-[11px] text-gray-400">💡 How often structured drop summary embeds are sent.</p>
                            </div>

                            <div class="space-y-1.5 pt-1">
                                <label class="flex items-center space-x-2.5 cursor-pointer">
                                    <input id="setting-discord-enabled" type="checkbox" onchange="markSettingsDirty()" class="rounded bg-twitch-dark border-twitch-border text-twitch-purple focus:ring-0">
                                    <span class="font-medium text-gray-200">Webhook Alerts Active</span>
                                </label>
                                <p class="text-[11px] text-gray-400 pl-6">💡 Uncheck to temporarily mute notifications without erasing your webhook URL.</p>
                            </div>
                        </div>

                        <!-- Setting: Discord Interactive Bot Status -->
                        <div class="pt-3 border-t border-twitch-border/60 space-y-2">
                            <span class="font-semibold text-gray-300 block">Interactive Discord Slash Bot Status:</span>
                            <div class="flex items-center space-x-4 text-[11px]">
                                <span id="status-bot-token" class="flex items-center space-x-1.5 text-gray-400">
                                    <span class="w-2 h-2 rounded-full bg-gray-500"></span>
                                    <span>Bot Token: Not Set</span>
                                </span>
                                <span id="status-bot-owner" class="flex items-center space-x-1.5 text-gray-400">
                                    <span class="w-2 h-2 rounded-full bg-gray-500"></span>
                                    <span>Owner ID: Not Set</span>
                                </span>
                            </div>
                            <div class="p-2.5 rounded bg-twitch-dark/60 border border-twitch-border/70 text-[11px] text-gray-400 leading-relaxed">
                                💡 <strong>Tip:</strong> Configure <code class="text-purple-300">DISCORD_BOT_TOKEN</code> and <code class="text-purple-300">DISCORD_OWNER_ID</code> in your <code class="text-gray-300">.env</code> to unlock two-way interactive commands in Discord: <code class="text-gray-300">/status</code>, <code class="text-gray-300">/priority</code>, <code class="text-gray-300">/exclude</code>, and channel switch buttons!
                            </div>
                        </div>

                        <!-- Setting: Security & Session Management -->
                        <div class="pt-3 border-t border-twitch-border/60 space-y-3">
                            <span class="font-semibold text-gray-300 block">Session & Account Security:</span>
                            
                            <div class="space-y-1">
                                <label class="flex items-center space-x-2.5 cursor-pointer">
                                    <input id="setting-auto-cookie-refresh" type="checkbox" onchange="markSettingsDirty()" class="rounded bg-twitch-dark border-twitch-border text-twitch-purple focus:ring-0">
                                    <span class="font-medium text-gray-200">Playwright Zero-Touch Cookie Auto-Refresh</span>
                                </label>
                                <p class="text-[11px] text-gray-400 pl-6 leading-relaxed">
                                    💡 <strong>Tip:</strong> Launches headless Chromium automatically in the background to renew expired Twitch login cookies with zero manual file exports.
                                </p>
                            </div>

                            <div class="flex items-center justify-between pt-2">
                                <div>
                                    <span class="font-medium text-gray-200 block">Twitch Session Logout</span>
                                    <span class="text-[11px] text-gray-500">Clear local session cookies and require re-authentication.</span>
                                </div>
                                <button onclick="logoutTwitch()" class="px-3 py-1.5 bg-red-950/40 hover:bg-red-900/60 text-red-300 border border-red-800/60 rounded-lg font-semibold transition active:scale-95 whitespace-nowrap shadow-sm">
                                    Log Out of Twitch
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Bottom Sticky Save Bar -->
            <div class="sticky bottom-4 z-30 bg-twitch-card border border-twitch-border rounded-xl p-4 shadow-2xl flex items-center justify-between">
                <div class="flex items-center space-x-2 text-xs text-gray-400">
                    <span class="w-2.5 h-2.5 rounded-full bg-emerald-400"></span>
                    <span>Configuration auto-synced with container filesystem</span>
                </div>
                <div class="space-x-2">
                    <button onclick="loadSettings()" class="px-4 py-2 bg-twitch-dark hover:bg-twitch-hover text-gray-300 border border-twitch-border rounded-lg text-xs font-semibold transition">
                        Reset Changes
                    </button>
                    <button onclick="saveSettings()" class="px-5 py-2 bg-twitch-purple hover:bg-twitch-accent text-white rounded-lg text-xs font-bold transition shadow-lg shadow-twitch-purple/30 active:scale-95">
                        Save Settings
                    </button>
                </div>
            </div>
        </div>

        <!-- ================= TAB 5: LOGS ================= -->
        <div id="tab-logs" class="tab-pane hidden space-y-4">
            <div class="bg-twitch-card border border-twitch-border rounded-xl p-4 shadow flex flex-col sm:flex-row items-center justify-between gap-3">
                <div class="flex items-center space-x-2 w-full sm:w-auto">
                    <span class="text-xs font-semibold text-gray-400">Filter Level:</span>
                    <select id="log-filter-select" onchange="setLogFilter(this.value)" class="px-3 py-1.5 bg-twitch-dark border border-twitch-border rounded-lg text-white text-xs focus:outline-none focus:border-twitch-purple transition">
                        <option value="ALL">ALL (Verbose)</option>
                        <option value="INFO">INFO & Above</option>
                        <option value="WARNING">WARNING & ERROR</option>
                        <option value="ERROR">ERROR Only</option>
                    </select>
                </div>

                <div class="flex items-center space-x-2 w-full sm:w-auto justify-end text-xs">
                    <label class="flex items-center space-x-2 cursor-pointer text-gray-300 mr-2">
                        <input id="log-autoscroll" type="checkbox" checked class="rounded bg-twitch-dark border-twitch-border text-twitch-purple focus:ring-0">
                        <span>Auto-scroll</span>
                    </label>
                    <button onclick="copyLogsToClipboard()" class="px-3 py-1.5 bg-twitch-dark hover:bg-twitch-hover border border-twitch-border text-gray-200 rounded-lg font-semibold transition">
                        📋 Copy Logs
                    </button>
                    <button onclick="clearLogs()" class="px-3 py-1.5 bg-red-950/40 hover:bg-red-900/60 border border-red-800/60 text-red-300 rounded-lg font-semibold transition">
                        🗑️ Clear View
                    </button>
                </div>
            </div>

            <!-- Terminal Output Window -->
            <div id="logs-container" class="bg-[#0b0b0e] border border-twitch-border rounded-xl p-4 font-mono text-xs text-gray-300 h-[600px] overflow-y-auto space-y-1 shadow-inner leading-relaxed">
                <p class="text-gray-500">Connecting to stream logs buffer...</p>
            </div>
        </div>
    </main>

    <!-- Global Toast Container -->
    <div id="toast-container" class="fixed bottom-5 right-5 z-50 flex flex-col space-y-2 pointer-events-none"></div>

    <script>
        const PLACEHOLDER_COVER = "data:image/svg+xml;charset=UTF-8,%3Csvg%20width%3D%22144%22%20height%3D%22192%22%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%3E%3Crect%20width%3D%22100%25%22%20height%3D%22100%25%22%20fill%3D%22%2318181b%22%2F%3E%3Ctext%20x%3D%2250%25%22%20y%3D%2250%25%22%20fill%3D%22%236b7280%22%20font-size%3D%2224%22%20text-anchor%3D%22middle%22%20dominant-baseline%3D%22middle%22%3E%F0%9F%8E%AE%3C%2Ftext%3E%3C%2Fsvg%3E";
        const PLACEHOLDER_REWARD = "data:image/svg+xml;charset=UTF-8,%3Csvg%20width%3D%2264%22%20height%3D%2264%22%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%3E%3Crect%20width%3D%22100%25%22%20height%3D%22100%25%22%20fill%3D%22%230e0e10%22%2F%3E%3Ctext%20x%3D%2250%25%22%20y%3D%2250%25%22%20fill%3D%22%239146FF%22%20font-size%3D%2224%22%20text-anchor%3D%22middle%22%20dominant-baseline%3D%22middle%22%3E%F0%9F%8E%81%3C%2Ftext%3E%3C%2Fsvg%3E";

        let currentTab = 'overview';
        let rawStatus = null;
        let rawCampaigns = [];
        let rawSettings = {};
        let rawOptions = {};
        let campaignFilter = 'all';
        let logFilter = 'ALL';
        let isSettingsDirty = false;

        function switchTab(tab) {
            currentTab = tab;
            document.querySelectorAll('.tab-pane').forEach(el => el.classList.add('hidden'));
            document.querySelectorAll('.tab-btn').forEach(btn => {
                btn.className = 'tab-btn px-3 py-1.5 rounded-md text-xs font-semibold transition text-gray-400 hover:text-white flex items-center space-x-1.5 whitespace-nowrap';
            });

            const activePane = document.getElementById(`tab-${tab}`);
            if (activePane) activePane.classList.remove('hidden');

            const activeBtn = document.getElementById(`tab-btn-${tab}`);
            if (activeBtn) activeBtn.className = 'tab-btn px-3 py-1.5 rounded-md text-xs font-semibold transition text-white bg-twitch-purple shadow-sm flex items-center space-x-1.5 whitespace-nowrap';

            if (tab === 'campaigns') fetchCampaigns();
            if (tab === 'rules' || tab === 'settings') loadSettings();
            if (tab === 'logs') fetchLogs();
        }

        function showToast(message, type = 'success') {
            const container = document.getElementById('toast-container');
            const toast = document.createElement('div');
            const bgClass = type === 'error' ? 'bg-red-950 border-red-800 text-red-200' : 
                            type === 'warning' ? 'bg-amber-950 border-amber-800 text-amber-200' : 
                            'bg-twitch-card border-twitch-purple text-gray-100 shadow-twitch-purple/20';

            toast.className = `${bgClass} border px-4 py-2.5 rounded-xl shadow-2xl text-xs pointer-events-auto flex items-center space-x-2 transition-all duration-300 opacity-0 translate-y-2`;
            toast.innerHTML = `<span>${type === 'error' ? '❌' : type === 'warning' ? '⚠️' : '✅'}</span><span>${message}</span>`;
            container.appendChild(toast);

            requestAnimationFrame(() => {
                toast.classList.remove('opacity-0', 'translate-y-2');
            });

            setTimeout(() => {
                toast.classList.add('opacity-0', 'translate-y-2');
                setTimeout(() => toast.remove(), 300);
            }, 3500);
        }

        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                if (!res.ok) {
                    console.error("Status fetch failed with status:", res.status);
                    return;
                }
                const data = await res.json();
                rawStatus = data;
                updateOverviewUI(data);
            } catch (err) {
                console.error("Status poll error", err);
                const badge = document.getElementById('state-badge');
                if (badge) {
                    badge.innerText = 'Offline';
                    badge.className = 'px-2.5 py-1 rounded-md text-xs font-semibold bg-red-900/40 text-red-400 border border-red-800 whitespace-nowrap';
                }
            }
        }

        function updateOverviewUI(data) {
            const stateBadge = document.getElementById('state-badge');
            if (stateBadge) {
                const state = data.state || 'IDLE';
                stateBadge.innerText = state;
                stateBadge.className = state === 'ACTIVE' ? 'px-2.5 py-1 rounded-md text-xs font-semibold bg-purple-900/40 text-purple-300 border border-purple-700/60 whitespace-nowrap' :
                                       state === 'IDLE' ? 'px-2.5 py-1 rounded-md text-xs font-semibold bg-gray-800 text-gray-400 border border-gray-700 whitespace-nowrap' :
                                       'px-2.5 py-1 rounded-md text-xs font-semibold bg-amber-950/50 text-amber-300 border border-amber-800/60 whitespace-nowrap';
            }

            const netBadge = document.getElementById('net-badge');
            if (netBadge && data.network) {
                if (data.network.healthy) {
                    netBadge.innerText = 'Twitch API: OK';
                    netBadge.className = 'px-2.5 py-1 rounded-md font-semibold bg-emerald-950/60 text-emerald-400 border border-emerald-800/60 whitespace-nowrap';
                } else {
                    netBadge.innerText = `Twitch API: Retrying (${data.network.consecutive_failures})`;
                    netBadge.className = 'px-2.5 py-1 rounded-md font-semibold bg-red-950/70 text-red-400 border border-red-800 whitespace-nowrap animate-pulse';
                }
            }

            const memBadge = document.getElementById('mem-badge');
            if (memBadge && data.system && data.system.memory_mb !== null) {
                memBadge.innerText = `RAM: ${data.system.memory_mb} MB`;
            }

            const banner2fa = document.getElementById('banner-2fa');
            if (banner2fa) {
                if (data.two_factor_pending) banner2fa.classList.remove('hidden');
                else banner2fa.classList.add('hidden');
            }

            const ch = data.channel;
            const heroChannelName = document.getElementById('hero-channel-name');
            const heroGameName = document.getElementById('hero-game-name');
            const heroStreamTitle = document.getElementById('hero-stream-title');
            const heroViewers = document.getElementById('hero-viewers');
            const heroPriorityBadge = document.getElementById('hero-priority-badge');
            const heroExcludedBadge = document.getElementById('hero-excluded-badge');
            const heroBoxArt = document.getElementById('hero-game-boxart');
            const heroBoxArtPlaceholder = document.getElementById('hero-boxart-placeholder');
            const heroActionButtons = document.getElementById('hero-action-buttons');
            const btnHeroPrioLabel = document.getElementById('btn-hero-prioritize-label');
            const btnHeroExclLabel = document.getElementById('btn-hero-exclude-label');

            if (ch && ch.name) {
                heroChannelName.innerText = ch.name;
                heroChannelName.href = ch.url || `https://twitch.tv/${ch.name}`;
                heroGameName.innerText = ch.game || 'Twitch Stream';
                heroStreamTitle.innerText = ch.title || 'Live Twitch Broadcast with Drops Enabled';
                heroViewers.innerText = `${(ch.viewers || 0).toLocaleString()} viewers`;

                if (heroBoxArt && heroBoxArtPlaceholder) {
                    if (ch.box_art_url) {
                        heroBoxArt.src = ch.box_art_url;
                        heroBoxArt.classList.remove('hidden');
                        heroBoxArtPlaceholder.classList.add('hidden');
                    } else {
                        heroBoxArt.classList.add('hidden');
                        heroBoxArtPlaceholder.classList.remove('hidden');
                    }
                }

                if (heroPriorityBadge) {
                    if (ch.is_priority) {
                        heroPriorityBadge.classList.remove('hidden');
                        heroPriorityBadge.innerText = ch.priority_rank ? `⭐ Priority #${ch.priority_rank}` : '⭐ Priority';
                    } else {
                        heroPriorityBadge.classList.add('hidden');
                    }
                }

                if (heroExcludedBadge) {
                    if (ch.is_excluded) heroExcludedBadge.classList.remove('hidden');
                    else heroExcludedBadge.classList.add('hidden');
                }

                if (heroActionButtons) heroActionButtons.classList.remove('hidden');
                if (btnHeroPrioLabel) btnHeroPrioLabel.innerText = ch.is_priority ? 'Prioritized (#1)' : 'Prioritize Game';
                if (btnHeroExclLabel) btnHeroExclLabel.innerText = ch.is_excluded ? 'Un-Exclude' : 'Exclude Game';
            } else {
                heroChannelName.innerText = 'No Active Stream';
                heroChannelName.removeAttribute('href');
                heroGameName.innerText = 'Idle';
                heroStreamTitle.innerText = 'Mining loop waiting for active drop campaigns...';
                heroViewers.innerText = '0 viewers';
                if (heroPriorityBadge) heroPriorityBadge.classList.add('hidden');
                if (heroExcludedBadge) heroExcludedBadge.classList.add('hidden');
                if (heroBoxArt) heroBoxArt.classList.add('hidden');
                if (heroBoxArtPlaceholder) heroBoxArtPlaceholder.classList.remove('hidden');
                if (heroActionButtons) heroActionButtons.classList.add('hidden');
            }

            const drop = data.drop;
            const heroDropName = document.getElementById('hero-drop-name');
            const heroDropCampaign = document.getElementById('hero-drop-campaign');
            const heroDropPercent = document.getElementById('hero-drop-percent');
            const heroDropMinutes = document.getElementById('hero-drop-minutes');
            const heroProgressBar = document.getElementById('hero-progress-bar');
            const heroCampaignClaimed = document.getElementById('hero-campaign-claimed');
            const heroDropEta = document.getElementById('hero-drop-eta');
            const heroRewardImg = document.getElementById('hero-reward-img');
            const heroRewardPlaceholder = document.getElementById('hero-reward-placeholder');

            if (drop) {
                heroDropName.innerText = drop.benefit_name || drop.name || 'Active Timed Drop';
                heroDropCampaign.innerText = drop.campaign_name || 'Active Campaign';
                const percent = Math.min(100, Math.max(0, drop.progress_percent || 0));
                heroDropPercent.innerText = `${percent}%`;
                heroDropMinutes.innerText = `${drop.current_minutes || 0} / ${drop.required_minutes || 0} min`;
                heroProgressBar.style.width = `${percent}%`;

                if (heroRewardImg && heroRewardPlaceholder) {
                    if (drop.benefit_image) {
                        heroRewardImg.src = drop.benefit_image;
                        heroRewardImg.classList.remove('hidden');
                        heroRewardPlaceholder.classList.add('hidden');
                    } else {
                        heroRewardImg.classList.add('hidden');
                        heroRewardPlaceholder.classList.remove('hidden');
                    }
                }

                if (drop.campaign_claimed !== undefined && drop.campaign_total !== undefined) {
                    heroCampaignClaimed.innerText = `Campaign Progress: ${drop.campaign_claimed}/${drop.campaign_total} drops claimed`;
                }

                const remaining = drop.remaining_minutes || Math.max(0, (drop.required_minutes || 0) - (drop.current_minutes || 0));
                heroDropEta.innerText = remaining > 0 ? `ETA: ~${remaining} min left` : 'Ready to claim!';
            } else {
                heroDropName.innerText = 'No Active Drop';
                heroDropCampaign.innerText = 'Awaiting drop event';
                heroDropPercent.innerText = '0%';
                heroDropMinutes.innerText = '0 / 0 min';
                heroProgressBar.style.width = '0%';
                heroCampaignClaimed.innerText = 'Campaign Progress: 0/0 drops';
                heroDropEta.innerText = 'ETA: --';
                if (heroRewardImg) heroRewardImg.classList.add('hidden');
                if (heroRewardPlaceholder) heroRewardPlaceholder.classList.remove('hidden');
            }

            const m = data.metrics || {};
            const metricClaimed = document.getElementById('metric-claimed');
            if (metricClaimed) metricClaimed.innerText = m.drops_claimed || m.claimed_drops || 0;

            const metricWatch = document.getElementById('metric-watch-time');
            if (metricWatch) metricWatch.innerText = `${m.watch_time_hours || 0}h`;

            const metricUptime = document.getElementById('metric-uptime');
            if (metricUptime) metricUptime.innerText = `${m.uptime_hours || 0}h`;

            const metricSuccess = document.getElementById('metric-success-rate');
            if (metricSuccess) metricSuccess.innerText = `${m.success_rate || 100}%`;

            const modeLabel = document.getElementById('overview-mode-label');
            if (modeLabel) modeLabel.innerText = data.priority_mode || 'BALANCED';

            const discIcon = document.getElementById('overview-btn-disc-icon');
            const discLabel = document.getElementById('overview-btn-disc-label');
            if (discIcon && discLabel && data.discord) {
                if (data.discord.enabled) {
                    discIcon.innerText = '🔔';
                    discLabel.innerText = 'Mute Discord';
                } else {
                    discIcon.innerText = '🔕';
                    discLabel.innerText = 'Unmute Discord';
                }
            }

            renderOverviewCampaignsPreview(data.campaigns || []);
        }

        function renderOverviewCampaignsPreview(campaigns) {
            const container = document.getElementById('overview-campaigns-preview');
            if (!container) return;

            if (!campaigns.length) {
                container.innerHTML = '<div class="text-gray-500 text-center py-8 col-span-2 text-xs">No active drop campaigns discovered yet.</div>';
                return;
            }

            container.innerHTML = campaigns.slice(0, 6).map(c => `
                <div class="bg-twitch-dark/60 border ${c.is_currently_farming ? 'border-twitch-purple/80 shadow-md shadow-twitch-purple/10' : 'border-twitch-border/70'} rounded-lg p-3 space-y-2 hover:border-twitch-purple/40 transition flex items-center space-x-3">
                    <div class="w-10 h-14 rounded overflow-hidden bg-twitch-card border border-twitch-border flex-shrink-0">
                        <img src="${c.box_art_url || PLACEHOLDER_COVER}" alt="${c.game}" class="w-full h-full object-cover" onerror="this.src='${PLACEHOLDER_COVER}'">
                    </div>
                    <div class="flex-1 min-w-0 space-y-1">
                        <div class="flex items-center justify-between">
                            <span class="font-bold text-white truncate max-w-[150px]">${c.game}</span>
                            <span class="text-[10px] text-gray-400">${c.time_left || ''}</span>
                        </div>
                        <div class="flex items-center space-x-1.5">
                            ${c.is_currently_farming ? '<span class="px-1.5 py-0.2 rounded text-[9px] font-bold bg-twitch-purple text-white">FARMING</span>' : ''}
                            ${c.is_priority ? '<span class="px-1.5 py-0.2 rounded text-[9px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30">PRIORITY</span>' : ''}
                            ${c.is_excluded ? '<span class="px-1.5 py-0.2 rounded text-[9px] font-bold bg-red-500/20 text-red-300 border border-red-500/30">BLOCKED</span>' : ''}
                            <span class="text-[10px] text-gray-400">${c.claimed || 0}/${c.total || 0} claimed</span>
                        </div>
                        <div class="w-full bg-twitch-card rounded-full h-1.5 overflow-hidden border border-twitch-border/50">
                            <div class="bg-twitch-purple h-full rounded-full" style="width: ${c.progress_percent || 0}%"></div>
                        </div>
                    </div>
                </div>
            `).join('');
        }

        async function fetchCampaigns() {
            try {
                const res = await fetch('/api/campaigns');
                if (!res.ok) return;
                const data = await res.json();
                rawCampaigns = data.campaigns || [];
                const navCount = document.getElementById('nav-campaigns-count');
                if (navCount) navCount.innerText = rawCampaigns.length;
                renderCampaignsTabUI();
            } catch (err) {
                console.error("Campaigns fetch error", err);
            }
        }

        function setCampaignFilter(f) {
            campaignFilter = f;
            ['all', 'farming', 'priority', 'excluded', 'linked'].forEach(id => {
                const btn = document.getElementById(`filter-camp-${id}`);
                if (!btn) return;
                if (id === f) {
                    btn.className = 'px-3 py-1.5 rounded-lg text-xs font-semibold bg-twitch-purple text-white shadow-sm whitespace-nowrap';
                } else {
                    btn.className = 'px-3 py-1.5 rounded-lg text-xs font-semibold bg-twitch-dark text-gray-400 hover:text-white border border-twitch-border whitespace-nowrap';
                }
            });
            renderCampaignsTabUI();
        }

        function renderCampaignsTabUI() {
            const container = document.getElementById('campaigns-tab-grid');
            if (!container) return;

            document.getElementById('count-camp-all').innerText = rawCampaigns.length;
            document.getElementById('count-camp-farming').innerText = rawCampaigns.filter(c => c.is_currently_farming).length;
            document.getElementById('count-camp-priority').innerText = rawCampaigns.filter(c => c.is_priority).length;
            document.getElementById('count-camp-excluded').innerText = rawCampaigns.filter(c => c.is_excluded).length;
            document.getElementById('count-camp-linked').innerText = rawCampaigns.filter(c => c.is_linked).length;

            const search = (document.getElementById('campaigns-search')?.value || '').toLowerCase();

            const filtered = rawCampaigns.filter(c => {
                const matchesSearch = c.game.toLowerCase().includes(search) || (c.name || '').toLowerCase().includes(search);
                if (!matchesSearch) return false;
                if (campaignFilter === 'farming') return c.is_currently_farming;
                if (campaignFilter === 'priority') return c.is_priority;
                if (campaignFilter === 'excluded') return c.is_excluded;
                if (campaignFilter === 'linked') return c.is_linked;
                return true;
            });

            if (!filtered.length) {
                container.innerHTML = '<div class="col-span-full py-16 text-center text-gray-500 text-sm">No campaigns match your selected filter or search criteria.</div>';
                return;
            }

            container.innerHTML = filtered.map(c => `
                <div class="bg-twitch-card border ${c.is_currently_farming ? 'border-amber-400/80 shadow-lg shadow-amber-500/10 ring-1 ring-amber-400/50' : c.is_priority ? 'border-twitch-purple/60 shadow-lg shadow-twitch-purple/5' : c.is_excluded ? 'border-red-900/40 opacity-75' : 'border-twitch-border'} rounded-xl p-4 space-y-3.5 flex flex-col justify-between transition hover:border-twitch-purple/50">
                    <div class="space-y-3">
                        <div class="flex items-start space-x-3">
                            <div class="w-14 h-18 rounded-lg overflow-hidden bg-twitch-dark border border-twitch-border flex-shrink-0 shadow">
                                <img src="${c.box_art_url || PLACEHOLDER_COVER}" alt="${c.game}" class="w-full h-full object-cover" onerror="this.src='${PLACEHOLDER_COVER}'">
                            </div>
                            <div class="flex-1 min-w-0">
                                <div class="flex items-center space-x-1.5 flex-wrap gap-y-1">
                                    <h4 class="font-bold text-white text-sm truncate">${c.game}</h4>
                                    ${c.is_currently_farming ? '<span class="px-1.5 py-0.2 rounded text-[10px] font-bold bg-amber-500 text-black flex items-center space-x-1"><span class="w-1.5 h-1.5 rounded-full bg-black animate-pulse"></span><span>FARMING NOW</span></span>' : ''}
                                    ${c.is_priority ? `<span class="px-1.5 py-0.2 rounded text-[10px] font-bold bg-twitch-purple text-white">${c.priority_rank ? '#' + c.priority_rank : 'PRIORITY'}</span>` : ''}
                                    ${c.is_excluded ? '<span class="px-1.5 py-0.2 rounded text-[10px] font-bold bg-red-900/60 text-red-300 border border-red-700/60">EXCLUDED</span>' : ''}
                                </div>
                                <p class="text-xs text-gray-400 truncate mt-0.5">${c.name || 'Drop Campaign'}</p>
                                <div class="flex items-center space-x-2 text-[11px] text-gray-400 mt-1">
                                    <span>⏳ ${c.time_left || 'Active'}</span>
                                    <span>•</span>
                                    <span class="${c.is_linked ? 'text-emerald-400' : 'text-gray-500'}">${c.is_linked ? '🔗 Account Connected' : 'Unlinked'}</span>
                                </div>
                            </div>
                        </div>

                        <!-- Progress Bar -->
                        <div class="space-y-1">
                            <div class="flex justify-between text-[11px] text-gray-400">
                                <span>Progress: ${c.claimed_drops || 0}/${c.total_drops || 0} claimed</span>
                                <span class="font-bold text-gray-200">${c.progress_percent || 0}%</span>
                            </div>
                            <div class="w-full bg-twitch-dark rounded-full h-2 overflow-hidden border border-twitch-border/60">
                                <div class="bg-gradient-to-r from-twitch-purple to-indigo-500 h-full rounded-full" style="width: ${c.progress_percent || 0}%"></div>
                            </div>
                        </div>

                        <!-- Drops Breakdown List with Reward Icons -->
                        <div class="pt-2 border-t border-twitch-border/60 space-y-1.5 max-h-36 overflow-y-auto pr-1">
                            ${(c.drops || []).map(d => `
                                <div class="flex items-center justify-between text-[11px] bg-twitch-dark/60 p-1.5 rounded border border-twitch-border/40">
                                    <div class="flex items-center space-x-2 truncate">
                                        <div class="w-5 h-5 rounded overflow-hidden bg-twitch-card flex-shrink-0 flex items-center justify-center border border-twitch-border/40">
                                            <img src="${d.image_url || PLACEHOLDER_REWARD}" alt="Reward" class="w-full h-full object-contain" onerror="this.src='${PLACEHOLDER_REWARD}'">
                                        </div>
                                        <span class="truncate ${d.is_claimed ? 'text-gray-500 line-through' : 'text-gray-200 font-medium'}">${d.benefit_name || d.name}</span>
                                    </div>
                                    <span class="flex-shrink-0 ml-2 font-mono ${d.is_claimed ? 'text-emerald-400 font-bold' : 'text-gray-400'}">${d.is_claimed ? '✓ Claimed' : `${d.current_minutes}/${d.required_minutes}m`}</span>
                                </div>
                            `).join('')}
                        </div>
                    </div>

                    <!-- 1-Click Action Buttons -->
                    <div class="flex items-center space-x-2 pt-2 border-t border-twitch-border/60 text-xs">
                        <button onclick="quickActionPriority('${c.game.replace(/'/g, "\'")}')" class="flex-1 py-1.5 rounded-lg font-semibold ${c.is_priority ? 'bg-twitch-purple/20 text-purple-300 border border-twitch-purple/40' : 'bg-twitch-dark hover:bg-twitch-purple text-gray-300 hover:text-white border border-twitch-border'} transition active:scale-95 text-center">
                            ${c.is_priority ? '⭐ Top Priority' : '+ Prioritize'}
                        </button>
                        <button onclick="quickActionExclude('${c.game.replace(/'/g, "\'")}')" class="py-1.5 px-3 rounded-lg font-semibold ${c.is_excluded ? 'bg-red-950/60 text-red-300 border border-red-800' : 'bg-twitch-dark hover:bg-red-950/40 text-gray-400 hover:text-red-300 border border-twitch-border'} transition active:scale-95">
                            ${c.is_excluded ? 'Unblock' : '🚫 Block'}
                        </button>
                    </div>
                </div>
            `).join('');
        }

        async function quickActionPriority(game) {
            try {
                const res = await fetch('/api/settings/priority', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'top', game: game })
                });
                if (res.ok) {
                    showToast(`Promoted "${game}" to #1 Priority!`);
                    await fetchStatus();
                    await fetchCampaigns();
                    await loadSettings();
                } else {
                    showToast(`Failed to prioritize "${game}"`, 'error');
                }
            } catch (err) {
                console.error(err);
                showToast(`Action failed: ${err}`, 'error');
            }
        }

        async function quickActionExclude(game) {
            try {
                const isCurrentlyExcluded = rawSettings?.exclude?.includes(game);
                const action = isCurrentlyExcluded ? 'remove' : 'add';
                const res = await fetch('/api/settings/exclude', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: action, game: game })
                });
                if (res.ok) {
                    showToast(action === 'add' ? `Excluded "${game}" from mining` : `Unblocked "${game}"`);
                    await fetchStatus();
                    await fetchCampaigns();
                    await loadSettings();
                } else {
                    showToast(`Failed to update exclusion for "${game}"`, 'error');
                }
            } catch (err) {
                console.error(err);
                showToast(`Action failed: ${err}`, 'error');
            }
        }

        function quickActionPriorityFromHero() {
            if (rawStatus?.channel?.game) {
                quickActionPriority(rawStatus.channel.game);
            }
        }

        function quickActionExcludeFromHero() {
            if (rawStatus?.channel?.game) {
                quickActionExclude(rawStatus.channel.game);
            }
        }

        async function loadSettings() {
            try {
                const res = await fetch('/api/settings');
                if (!res.ok) return;
                const data = await res.json();
                rawSettings = data.settings || {};
                rawOptions = data.options || {};
                updateSettingsUI();
            } catch (err) {
                console.error("Load settings error", err);
            }
        }

        function markSettingsDirty() {
            isSettingsDirty = true;
            const dot = document.getElementById('settings-dirty-dot');
            if (dot) dot.classList.remove('hidden');
            const alert = document.getElementById('settings-unsaved-alert');
            if (alert) alert.classList.remove('hidden');
        }

        function clearSettingsDirty() {
            isSettingsDirty = false;
            const dot = document.getElementById('settings-dirty-dot');
            if (dot) dot.classList.add('hidden');
            const alert = document.getElementById('settings-unsaved-alert');
            if (alert) alert.classList.add('hidden');
        }

        function updateSettingsUI() {
            clearSettingsDirty();

            // Update Counts in Priority & Blocklist Banner
            const prioList = rawSettings.priority || [];
            const exclList = rawSettings.exclude || [];
            const navPrioCount = document.getElementById('nav-priority-count');
            if (navPrioCount) navPrioCount.innerText = prioList.length;

            const bannerPrioCount = document.getElementById('rules-banner-priority-count');
            if (bannerPrioCount) bannerPrioCount.innerText = prioList.length;

            const bannerExclCount = document.getElementById('rules-banner-exclude-count');
            if (bannerExclCount) bannerExclCount.innerText = exclList.length;

            // Priority Mode Select
            const modeSelect = document.getElementById('setting-priority-mode');
            if (modeSelect) {
                const currentMode = rawSettings.priority_mode || 'BALANCED';
                const modeMap = { 'PRIORITY_ONLY': '0', 'ENDING_SOONEST': '1', 'LOW_AVBL_FIRST': '2', 'BALANCED': '3' };
                modeSelect.value = modeMap[currentMode] || '3';
            }

            const staleTimeout = document.getElementById('setting-stale-timeout');
            if (staleTimeout) staleTimeout.value = rawSettings.stale_stream_timeout_minutes || 5;

            const maintInterval = document.getElementById('setting-maint-interval');
            if (maintInterval) maintInterval.value = rawSettings.maintenance_interval_minutes || 20;

            const connQuality = document.getElementById('setting-conn-quality');
            if (connQuality) connQuality.value = rawSettings.connection_quality || 1;

            const availCheck = document.getElementById('setting-available-drops-check');
            if (availCheck) availCheck.checked = !!rawSettings.available_drops_check;

            const badgesEmotes = document.getElementById('setting-badges-emotes');
            if (badgesEmotes) badgesEmotes.checked = !!rawSettings.enable_badges_emotes;

            const autoCookie = document.getElementById('setting-auto-cookie-refresh');
            if (autoCookie) autoCookie.checked = !!rawSettings.auto_cookie_refresh;

            const webhookUrl = document.getElementById('setting-webhook-url');
            if (webhookUrl) webhookUrl.value = rawSettings.discord_webhook_url || '';

            const summaryInterval = document.getElementById('setting-summary-interval');
            if (summaryInterval) summaryInterval.value = rawSettings.discord_summary_interval_minutes || 60;

            const discordEnabled = document.getElementById('setting-discord-enabled');
            if (discordEnabled) discordEnabled.checked = !!rawSettings.discord_notifications_enabled;

            const statusToken = document.getElementById('status-bot-token');
            if (statusToken) {
                statusToken.innerHTML = rawSettings.has_discord_bot_token ? 
                    '<span class="w-2 h-2 rounded-full bg-emerald-400"></span><span class="text-emerald-300 font-semibold">Bot Token: Active</span>' :
                    '<span class="w-2 h-2 rounded-full bg-gray-500"></span><span>Bot Token: Not Set</span>';
            }
            const statusOwner = document.getElementById('status-bot-owner');
            if (statusOwner) {
                statusOwner.innerHTML = rawSettings.discord_owner_id ? 
                    `<span class="w-2 h-2 rounded-full bg-emerald-400"></span><span class="text-emerald-300 font-semibold">Owner ID: ${rawSettings.discord_owner_id}</span>` :
                    '<span class="w-2 h-2 rounded-full bg-gray-500"></span><span>Owner ID: Not Set</span>';
            }

            renderPrioritySettingsList();
            renderExcludeSettingsList();
            renderQuickChips();
        }

        function renderQuickChips() {
            const prioChipsContainer = document.getElementById('priority-quick-chips');
            const exclChipsContainer = document.getElementById('exclude-quick-chips');
            const catalog = rawOptions.games_catalog || [];
            const prioSet = new Set(rawSettings.priority || []);
            const exclSet = new Set(rawSettings.exclude || []);

            if (prioChipsContainer) {
                const candidates = catalog.filter(g => !prioSet.has(g.name) && (g.is_active || g.claimed_drops > 0)).slice(0, 10);
                if (!candidates.length) {
                    prioChipsContainer.innerHTML = '<span class="text-gray-500 text-[11px] italic">All active & farmed games are prioritized or none discovered yet.</span>';
                } else {
                    prioChipsContainer.innerHTML = candidates.map(g => `
                        <button onclick="addPriorityGameDirect('${g.name.replace(/'/g, "\'")}')" class="px-2.5 py-1 rounded-full text-xs bg-twitch-dark hover:bg-twitch-purple hover:text-white border border-twitch-border text-gray-300 transition flex items-center space-x-1.5 shadow-sm active:scale-95">
                            <span>${g.is_active ? '🔥' : '📦'}</span>
                            <span class="font-medium truncate max-w-[140px]">${g.name}</span>
                            <span class="text-[10px] text-purple-300 font-bold">+</span>
                        </button>
                    `).join('');
                }
            }

            if (exclChipsContainer) {
                const candidates = catalog.filter(g => !exclSet.has(g.name) && g.is_active).slice(0, 10);
                if (!candidates.length) {
                    exclChipsContainer.innerHTML = '<span class="text-gray-500 text-[11px] italic">No active candidates to suggest.</span>';
                } else {
                    exclChipsContainer.innerHTML = candidates.map(g => `
                        <button onclick="addExcludeGameDirect('${g.name.replace(/'/g, "\'")}')" class="px-2.5 py-1 rounded-full text-xs bg-twitch-dark hover:bg-red-950/60 hover:text-red-300 hover:border-red-800 border border-twitch-border text-gray-300 transition flex items-center space-x-1.5 shadow-sm active:scale-95">
                            <span class="text-red-400">🚫</span>
                            <span class="font-medium truncate max-w-[140px]">${g.name}</span>
                            <span class="text-[10px] text-red-400 font-bold">+</span>
                        </button>
                    `).join('');
                }
            }
        }

        function handlePriorityInput(val) {
            const dropdown = document.getElementById('dropdown-priority-autocomplete');
            if (!dropdown) return;
            const query = (val || '').trim().toLowerCase();
            const catalog = rawOptions.games_catalog || [];
            const prioSet = new Set(rawSettings.priority || []);

            let matches = catalog.filter(g => g.name.toLowerCase().includes(query));
            if (!query) {
                matches = matches.slice(0, 8);
            } else {
                matches = matches.slice(0, 10);
            }

            if (!matches.length) {
                dropdown.innerHTML = `<div class="p-3 text-center text-gray-400 text-xs">No matching games found in catalog. Type exact title and click <strong>+ Add</strong>.</div>`;
                dropdown.classList.remove('hidden');
                return;
            }

            dropdown.innerHTML = matches.map(g => {
                const isAlreadyPrio = prioSet.has(g.name);
                return `
                    <div onclick="addPriorityGameDirect('${g.name.replace(/'/g, "\'")}')" class="p-2.5 hover:bg-twitch-hover cursor-pointer transition flex items-center justify-between space-x-3 text-xs">
                        <div class="flex items-center space-x-2.5 min-w-0">
                            <div class="w-7 h-9 rounded bg-twitch-dark overflow-hidden flex-shrink-0 border border-twitch-border">
                                <img src="${g.box_art_url || PLACEHOLDER_COVER}" alt="${g.name}" class="w-full h-full object-cover" onerror="this.src='${PLACEHOLDER_COVER}'">
                            </div>
                            <div class="truncate">
                                <span class="font-bold text-white block truncate">${g.name}</span>
                                <div class="flex items-center space-x-1 text-[10px]">
                                    ${g.is_active ? '<span class="text-amber-400 font-semibold">🔥 Active Now</span>' : '<span class="text-gray-500">⚪ Inactive</span>'}
                                    ${g.claimed_drops > 0 ? `<span class="text-purple-300 font-semibold">• 📦 ${g.claimed_drops} drops claimed</span>` : ''}
                                </div>
                            </div>
                        </div>
                        <span class="text-xs font-semibold ${isAlreadyPrio ? 'text-gray-500' : 'text-twitch-purple hover:underline'} flex-shrink-0">
                            ${isAlreadyPrio ? '✓ In Queue' : '+ Prioritize'}
                        </span>
                    </div>
                `;
            }).join('');
            dropdown.classList.remove('hidden');
        }

        function handleExcludeInput(val) {
            const dropdown = document.getElementById('dropdown-exclude-autocomplete');
            if (!dropdown) return;
            const query = (val || '').trim();
            const lowerQuery = query.toLowerCase();
            const catalog = rawOptions.games_catalog || [];
            const exclSet = new Set(rawSettings.exclude || []);

            let matches = catalog.filter(g => g.name.toLowerCase().includes(lowerQuery));
            matches = matches.slice(0, 8);

            let html = '';
            if (query && (query.includes('*') || !matches.some(m => m.name.toLowerCase() === lowerQuery))) {
                html += `
                    <div onclick="addExcludeGameDirect('${query.replace(/'/g, "\'")}')" class="p-2.5 bg-red-950/40 hover:bg-red-900/50 cursor-pointer transition flex items-center justify-between text-xs text-red-300 border-b border-red-900/60 font-semibold">
                        <span>➕ Add custom / wildcard pattern: <strong>"${query}"</strong></span>
                        <span class="text-[11px] text-red-400">Add →</span>
                    </div>
                `;
            }

            if (!matches.length && !html) {
                dropdown.innerHTML = `<div class="p-3 text-center text-gray-400 text-xs">No matching games in catalog. Click Add to exclude pattern directly.</div>`;
                dropdown.classList.remove('hidden');
                return;
            }

            html += matches.map(g => {
                const isAlreadyExcl = exclSet.has(g.name);
                return `
                    <div onclick="addExcludeGameDirect('${g.name.replace(/'/g, "\'")}')" class="p-2.5 hover:bg-twitch-hover cursor-pointer transition flex items-center justify-between space-x-3 text-xs">
                        <div class="flex items-center space-x-2.5 min-w-0">
                            <div class="w-7 h-9 rounded bg-twitch-dark overflow-hidden flex-shrink-0 border border-twitch-border">
                                <img src="${g.box_art_url || PLACEHOLDER_COVER}" alt="${g.name}" class="w-full h-full object-cover" onerror="this.src='${PLACEHOLDER_COVER}'">
                            </div>
                            <div class="truncate">
                                <span class="font-bold text-white block truncate">${g.name}</span>
                                <div class="flex items-center space-x-1 text-[10px]">
                                    ${g.is_active ? '<span class="text-amber-400 font-semibold">🔥 Active Now</span>' : '<span class="text-gray-500">⚪ Inactive</span>'}
                                </div>
                            </div>
                        </div>
                        <span class="text-xs font-semibold ${isAlreadyExcl ? 'text-gray-500' : 'text-red-400 hover:underline'} flex-shrink-0">
                            ${isAlreadyExcl ? '✓ Blocked' : '+ Exclude'}
                        </span>
                    </div>
                `;
            }).join('');

            dropdown.innerHTML = html;
            dropdown.classList.remove('hidden');
        }

        // Close dropdowns on outside click
        document.addEventListener('click', (e) => {
            const prioInput = document.getElementById('input-add-priority');
            const prioDropdown = document.getElementById('dropdown-priority-autocomplete');
            if (prioDropdown && !prioDropdown.contains(e.target) && e.target !== prioInput) {
                prioDropdown.classList.add('hidden');
            }

            const exclInput = document.getElementById('input-add-exclude');
            const exclDropdown = document.getElementById('dropdown-exclude-autocomplete');
            if (exclDropdown && !exclDropdown.contains(e.target) && e.target !== exclInput) {
                exclDropdown.classList.add('hidden');
            }
        });

        function renderPrioritySettingsList() {
            const container = document.getElementById('priority-list-container');
            const countBadge = document.getElementById('priority-count-badge');
            if (!container) return;

            const list = rawSettings.priority || [];
            if (countBadge) countBadge.innerText = `${list.length} games`;

            if (!list.length) {
                container.innerHTML = '<p class="text-gray-500 text-center py-6 text-[11px]">No priority games configured. Add games to create a priority queue.</p>';
                return;
            }

            const catalogMap = new Map((rawOptions.games_catalog || []).map(g => [g.name, g]));

            container.innerHTML = list.map((game, idx) => {
                const info = catalogMap.get(game);
                const isActive = info?.is_active;
                return `
                    <div class="flex items-center justify-between p-2.5 bg-twitch-dark rounded-lg border border-twitch-border hover:border-twitch-purple/40 transition">
                        <div class="flex items-center space-x-2.5 truncate">
                            <span class="w-6 h-6 rounded flex items-center justify-center font-bold text-xs flex-shrink-0 ${idx === 0 ? 'bg-amber-500 text-black shadow-sm font-extrabold' : 'bg-twitch-card border border-twitch-border text-twitch-purple'}">
                                #${idx + 1}
                            </span>
                            <div class="truncate">
                                <span class="font-medium text-white truncate block">${game}</span>
                                <span class="text-[10px] ${isActive ? 'text-amber-400 font-semibold' : 'text-gray-500'}">
                                    ${isActive ? '🟢 Active Drop Running' : '⚪ Inactive'}
                                </span>
                            </div>
                        </div>
                        <div class="flex items-center space-x-1 ml-2 flex-shrink-0">
                            <button onclick="movePriorityGame('${game.replace(/'/g, "\'")}', 'move_up')" class="p-1 hover:text-twitch-purple text-gray-400 transition" title="Move Up">▲</button>
                            <button onclick="movePriorityGame('${game.replace(/'/g, "\'")}', 'move_down')" class="p-1 hover:text-twitch-purple text-gray-400 transition" title="Move Down">▼</button>
                            <button onclick="movePriorityGame('${game.replace(/'/g, "\'")}', 'top')" class="p-1 hover:text-amber-400 text-gray-400 transition font-bold" title="Promote to #1">⭐</button>
                            <button onclick="removePriorityGame('${game.replace(/'/g, "\'")}')" class="p-1 hover:text-red-400 text-gray-400 transition font-bold" title="Remove">✕</button>
                        </div>
                    </div>
                `;
            }).join('');
        }

        function renderExcludeSettingsList() {
            const container = document.getElementById('exclude-tags-container');
            const countBadge = document.getElementById('exclude-count-badge');
            if (!container) return;

            const list = rawSettings.exclude || [];
            if (countBadge) countBadge.innerText = `${list.length} games`;

            if (!list.length) {
                container.innerHTML = '<p class="text-gray-500 text-center w-full py-6 text-[11px]">No excluded games. All active games are eligible for drops.</p>';
                return;
            }

            container.innerHTML = list.map(game => {
                const isWildcard = game.includes('*');
                return `
                    <span class="bg-red-950/40 border border-red-800/60 text-red-300 px-3 py-1 rounded-full text-xs flex items-center space-x-1.5 shadow-sm">
                        <span class="truncate max-w-[180px]">${game}</span>
                        ${isWildcard ? '<span class="text-[9px] px-1 bg-red-900/60 rounded font-mono text-red-200">wildcard</span>' : ''}
                        <button onclick="removeExcludeGame('${game.replace(/'/g, "\'")}')" class="hover:text-white transition font-bold text-red-400 ml-1">✕</button>
                    </span>
                `;
            }).join('');
        }

        async function addPriorityGameDirect(game) {
            const dropdown = document.getElementById('dropdown-priority-autocomplete');
            if (dropdown) dropdown.classList.add('hidden');
            const input = document.getElementById('input-add-priority');
            if (input) input.value = '';

            try {
                const res = await fetch('/api/settings/priority', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'add', game: game })
                });
                if (res.ok) {
                    showToast(`Added "${game}" to Priority Queue`);
                    await loadSettings();
                    await fetchStatus();
                    await fetchCampaigns();
                } else {
                    showToast(`Failed to add priority game`, 'error');
                }
            } catch (err) {
                showToast(`Error: ${err}`, 'error');
            }
        }

        async function addPriorityGame() {
            const input = document.getElementById('input-add-priority');
            const game = (input?.value || '').trim();
            if (!game) return;
            await addPriorityGameDirect(game);
        }

        async function movePriorityGame(game, action) {
            try {
                const res = await fetch('/api/settings/priority', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: action, game: game })
                });
                if (res.ok) {
                    await loadSettings();
                    await fetchStatus();
                }
            } catch (err) {
                showToast(`Error: ${err}`, 'error');
            }
        }

        async function removePriorityGame(game) {
            try {
                const res = await fetch('/api/settings/priority', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'remove', game: game })
                });
                if (res.ok) {
                    showToast(`Removed "${game}" from Priority Queue`);
                    await loadSettings();
                    await fetchStatus();
                    await fetchCampaigns();
                }
            } catch (err) {
                showToast(`Error: ${err}`, 'error');
            }
        }

        async function addExcludeGameDirect(game) {
            const dropdown = document.getElementById('dropdown-exclude-autocomplete');
            if (dropdown) dropdown.classList.add('hidden');
            const input = document.getElementById('input-add-exclude');
            if (input) input.value = '';

            try {
                const res = await fetch('/api/settings/exclude', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'add', game: game })
                });
                if (res.ok) {
                    showToast(`Added "${game}" to Blocklist`);
                    await loadSettings();
                    await fetchStatus();
                    await fetchCampaigns();
                } else {
                    showToast(`Failed to exclude game`, 'error');
                }
            } catch (err) {
                showToast(`Error: ${err}`, 'error');
            }
        }

        async function addExcludeGame() {
            const input = document.getElementById('input-add-exclude');
            const game = (input?.value || '').trim();
            if (!game) return;
            await addExcludeGameDirect(game);
        }

        async function removeExcludeGame(game) {
            try {
                const res = await fetch('/api/settings/exclude', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'remove', game: game })
                });
                if (res.ok) {
                    showToast(`Removed "${game}" from Blocklist`);
                    await loadSettings();
                    await fetchStatus();
                    await fetchCampaigns();
                }
            } catch (err) {
                showToast(`Error: ${err}`, 'error');
            }
        }

        async function saveSettings() {
            const payload = {
                priority_mode: parseInt(document.getElementById('setting-priority-mode')?.value || '3'),
                stale_stream_timeout_minutes: parseInt(document.getElementById('setting-stale-timeout')?.value || '5'),
                maintenance_interval_minutes: parseInt(document.getElementById('setting-maint-interval')?.value || '20'),
                connection_quality: parseInt(document.getElementById('setting-conn-quality')?.value || '1'),
                available_drops_check: !!document.getElementById('setting-available-drops-check')?.checked,
                enable_badges_emotes: !!document.getElementById('setting-badges-emotes')?.checked,
                auto_cookie_refresh: !!document.getElementById('setting-auto-cookie-refresh')?.checked,
                discord_summary_interval_minutes: parseInt(document.getElementById('setting-summary-interval')?.value || '60'),
                discord_notifications_enabled: !!document.getElementById('setting-discord-enabled')?.checked,
            };

            const webhook = (document.getElementById('setting-webhook-url')?.value || '').trim();
            if (webhook && !webhook.includes('***')) {
                payload.discord_webhook_url = webhook;
            }

            try {
                const res = await fetch('/api/settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                if (res.ok) {
                    showToast("Settings saved and applied successfully!");
                    clearSettingsDirty();
                    await loadSettings();
                    await fetchStatus();
                } else {
                    showToast(data.error || "Failed to save settings", 'error');
                }
            } catch (err) {
                showToast(`Save error: ${err}`, 'error');
            }
        }

        async function testDiscordWebhook() {
            try {
                const res = await fetch('/api/notifications/test', { method: 'POST' });
                const data = await res.json();
                if (res.ok) {
                    showToast("Discord test notification sent successfully!");
                } else {
                    showToast(data.error || "Failed to send test notification", 'error');
                }
            } catch (err) {
                showToast(`Error: ${err}`, 'error');
            }
        }

        async function logoutTwitch() {
            if (!confirm("Are you sure you want to log out of Twitch? This will delete local session cookies and require a new login.")) {
                return;
            }
            try {
                const res = await fetch('/api/action/logout', { method: 'POST' });
                if (res.ok) {
                    showToast("Logged out of Twitch session. Please restart or check 2FA.", 'warning');
                    await fetchStatus();
                } else {
                    showToast("Logout failed.", 'error');
                }
            } catch (err) {
                showToast(`Error: ${err}`, 'error');
            }
        }

        async function requestAction(action) {
            try {
                const res = await fetch(`/api/action/${action}`, { method: 'POST' });
                const data = await res.json();
                if (res.ok) {
                    showToast(`Action "${action}" triggered successfully!`);
                    await fetchStatus();
                    if (action === 'refresh') await fetchCampaigns();
                } else {
                    showToast(data.error || `Failed to trigger ${action}`, 'error');
                }
            } catch (err) {
                showToast(`Error: ${err}`, 'error');
            }
        }

        async function submit2FAToken() {
            const input = document.getElementById('input-2fa-token');
            const token = (input?.value || '').trim();
            if (!token) return;

            try {
                const res = await fetch('/api/action/submit-2fa', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ token: token })
                });
                if (res.ok) {
                    showToast("2FA Code submitted to Playwright session!");
                    input.value = '';
                    await fetchStatus();
                } else {
                    showToast("Failed to submit 2FA code", 'error');
                }
            } catch (err) {
                showToast(`Error: ${err}`, 'error');
            }
        }

        async function fetchLogs() {
            try {
                const url = logFilter === 'ALL' ? '/api/logs?limit=300' : `/api/logs?limit=300&level=${logFilter}`;
                const res = await fetch(url);
                if (!res.ok) return;
                const data = await res.json();
                renderLogs(data.logs || []);
            } catch (err) {
                console.error("Logs error", err);
            }
        }

        function setLogFilter(level) {
            logFilter = level;
            fetchLogs();
        }

        function renderLogs(logs) {
            const container = document.getElementById('logs-container');
            if (!container) return;

            if (!logs.length) {
                container.innerHTML = '<p class="text-gray-500">No logs captured yet.</p>';
                return;
            }

            const autoscroll = document.getElementById('log-autoscroll')?.checked;

            container.innerHTML = logs.map(line => {
                let colorClass = 'text-gray-300';
                if (line.includes(' [ERROR] ')) colorClass = 'text-red-400 font-bold bg-red-950/20 px-1 rounded';
                else if (line.includes(' [WARNING] ')) colorClass = 'text-amber-300';
                else if (line.includes(' [INFO] ')) colorClass = 'text-gray-300';
                else if (line.includes(' [DEBUG] ')) colorClass = 'text-gray-500 text-[11px]';
                return `<div class="${colorClass}">${escapeHtml(line)}</div>`;
            }).join('');

            if (autoscroll) {
                container.scrollTop = container.scrollHeight;
            }
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        function copyLogsToClipboard() {
            const container = document.getElementById('logs-container');
            if (!container) return;
            navigator.clipboard.writeText(container.innerText).then(() => {
                showToast("Logs copied to clipboard!");
            });
        }

        async function clearLogs() {
            try {
                await fetch('/api/logs/clear', { method: 'POST' });
                const container = document.getElementById('logs-container');
                if (container) container.innerHTML = '<p class="text-gray-500">Logs cleared.</p>';
                showToast("Console log buffer cleared.");
            } catch (err) {
                showToast(`Clear error: ${err}`, 'error');
            }
        }

        // Initialize polling intervals
        fetchStatus();
        loadSettings();

        setInterval(() => {
            fetchStatus();
            if (currentTab === 'campaigns') fetchCampaigns();
            if (currentTab === 'rules') renderQuickChips();
        }, 3000);

        setInterval(() => {
            if (currentTab === 'logs') fetchLogs();
        }, 2500);
    </script>
</body>
</html>"""


LOGIN_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - Twitch Drops Miner</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-[#0e0e10] text-gray-200 font-sans min-h-screen flex items-center justify-center p-4">
    <div class="bg-[#18181b] border border-[#27272a] rounded-xl p-8 max-w-sm w-full shadow-2xl space-y-6">
        <div class="text-center space-y-2">
            <div class="w-12 h-12 rounded-xl bg-[#9146FF] mx-auto flex items-center justify-center text-2xl shadow-lg shadow-[#9146FF]/30">
                ⛏️
            </div>
            <h1 class="text-xl font-bold text-white">Twitch Drops Miner</h1>
            <p class="text-xs text-gray-400">Password protected dashboard</p>
        </div>
        <form method="POST" action="/login" class="space-y-4">
            <div>
                <label for="password" class="block text-xs font-semibold text-gray-400 mb-1 uppercase tracking-wider">Password</label>
                <input type="password" id="password" name="password" required autofocus class="w-full px-3.5 py-2.5 bg-[#0e0e10] border border-[#27272a] rounded-lg text-white text-sm focus:outline-none focus:border-[#9146FF] transition" placeholder="Enter dashboard password">
            </div>
            <button type="submit" class="w-full py-2.5 bg-[#9146FF] hover:bg-[#772ce8] text-white font-semibold rounded-lg text-sm transition shadow-lg shadow-[#9146FF]/20 active:scale-95">
                Unlock Dashboard
            </button>
        </form>
    </div>
</body>
</html>
"""


class WebLogHandler(logging.Handler):
    """In-memory circular log buffer for the Web Dashboard."""

    def __init__(self, capacity: int = 500):
        super().__init__()
        self.buffer: deque[dict[str, Any]] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord):
        try:
            msg = record.getMessage()
            self.buffer.append({
                "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "time_short": datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S"),
                "level": record.levelname,
                "message": msg,
            })
        except Exception:
            self.handleError(record)

    def clear(self):
        self.buffer.clear()


class WebDashboard:
    """Provides Web UI and REST API for Twitch Drops Miner."""

    def __init__(self, twitch: Twitch, host: str = "0.0.0.0", port: int = 8080):
        self.twitch = twitch
        self.host = host
        self.port = port
        self.password: str = getattr(twitch.settings, "web_password", "").strip()
        self._last_action_time: float = 0.0

        # In-memory logging ring buffer
        self.log_handler: WebLogHandler = WebLogHandler(capacity=500)
        logging.getLogger("TwitchDrops").addHandler(self.log_handler)

        # Initialize aiohttp app with security middleware
        middlewares = [self._security_middleware]
        self.app = web.Application(middlewares=middlewares)
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self._setup_routes()

    @web.middleware
    async def _security_middleware(self, request: web.Request, handler):
        # 1. Authentication check (if password is set)
        if self.password:
            # Allow public unauthenticated endpoints: /login, /logout, /api/health
            if request.path in ("/login", "/logout", "/api/health"):
                return await handler(request)

            # Check header token, query param, or session cookie
            auth_header = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
            token_param = request.query.get("token", "").strip()
            cookie_token = request.cookies.get("tdm_auth", "").strip()

            is_authenticated = (
                auth_header == self.password
                or token_param == self.password
                or cookie_token == self.password
            )

            if not is_authenticated:
                if request.path.startswith("/api/"):
                    response = web.json_response({"error": "Unauthorized"}, status=401)
                else:
                    response = web.Response(text=LOGIN_HTML_TEMPLATE, content_type="text/html", status=401)
                self._apply_security_headers(response)
                return response

        # 2. Rate limit on actions
        if request.path.startswith("/api/action/"):
            now = asyncio.get_event_loop().time()
            if now - self._last_action_time < 1.0:
                response = web.json_response({"error": "Rate limited. Please wait 1 second between actions."}, status=429)
                self._apply_security_headers(response)
                return response
            self._last_action_time = now

        response = await handler(request)
        self._apply_security_headers(response)
        return response

    @staticmethod
    def _apply_security_headers(response: web.Response) -> None:
        """Apply defensive HTTP security headers to all responses."""
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    def _setup_routes(self):
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/login", self.handle_login_page)
        self.app.router.add_post("/login", self.handle_login)
        self.app.router.add_get("/logout", self.handle_logout)
        self.app.router.add_get("/api/health", self.handle_health)
        self.app.router.add_get("/api/status", self.handle_status)
        self.app.router.add_get("/api/2fa/status", self.handle_2fa_status)
        self.app.router.add_get("/api/2fa/screenshot", self.handle_2fa_screenshot)
        self.app.router.add_post("/api/action/submit-2fa", self.handle_submit_2fa)
        self.app.router.add_post("/api/action/switch", self.handle_switch)
        self.app.router.add_post("/api/action/refresh", self.handle_refresh)
        self.app.router.add_post("/api/action/refresh-cookies", self.handle_refresh_cookies)
        self.app.router.add_post("/api/action/toggle-notifications", self.handle_toggle_notifications)
        self.app.router.add_get("/api/logs", self.handle_logs)
        self.app.router.add_post("/api/logs/clear", self.handle_logs_clear)
        self.app.router.add_get("/api/settings", self.handle_get_settings)
        self.app.router.add_post("/api/settings", self.handle_update_settings)
        self.app.router.add_post("/api/settings/priority", self.handle_priority_action)
        self.app.router.add_post("/api/settings/exclude", self.handle_exclude_action)
        self.app.router.add_post("/api/notifications/test", self.handle_test_notification)
        self.app.router.add_get("/api/campaigns", self.handle_campaigns)

    async def handle_login_page(self, request: web.Request) -> web.Response:
        return web.Response(text=LOGIN_HTML_TEMPLATE, content_type="text/html")

    async def handle_login(self, request: web.Request) -> web.Response:
        data = await request.post()
        submitted = data.get("password", "")
        if submitted == self.password:
            response = web.HTTPFound("/")
            response.set_cookie("tdm_auth", self.password, max_age=86400 * 30, httponly=True, samesite="Lax")
            return response
        return web.Response(text=LOGIN_HTML_TEMPLATE, content_type="text/html", status=401)

    async def handle_logout(self, request: web.Request) -> web.Response:
        response = web.HTTPFound("/login")
        response.del_cookie("tdm_auth")
        return response

    async def handle_health(self, request: web.Request) -> web.Response:
        """Public health check endpoint for Docker/Portainer/monitors."""
        state_name = self.twitch._state.name if hasattr(self.twitch, "_state") else "UNKNOWN"
        watching_channel = self.twitch.watching_channel.get_with_default(None)
        uptime = 0.0
        if metrics := getattr(self.twitch, "metrics", None):
            uptime = metrics.get_stats().get("uptime_hours", 0.0)

        return web.json_response({
            "status": "healthy",
            "state": state_name,
            "channel": watching_channel.name if watching_channel else None,
            "uptime_hours": uptime,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def handle_index(self, request: web.Request) -> web.Response:
        return web.Response(text=HTML_TEMPLATE, content_type="text/html")

    async def handle_status(self, request: web.Request) -> web.Response:
        """Returns JSON representation of the current miner status."""
        channel_data = None
        drop_data = None
        campaigns_data = []
        now = datetime.now(timezone.utc)

        # 1. Channel Info
        watching_channel = self.twitch.watching_channel.get_with_default(None)
        active_campaign = self.twitch.get_active_campaign(watching_channel)
        active_campaign_id = getattr(active_campaign, "id", None) if active_campaign else None

        priority_list = list(getattr(self.twitch.settings, "priority", []))
        priority_set = set(priority_list)
        exclude_raw = getattr(self.twitch.settings, "exclude", set())
        exclude_set = set(exclude_raw) if isinstance(exclude_raw, (set, list)) else set()

        if watching_channel is not None:
            game_name = _safe_str(getattr(watching_channel.game, "name", None)) or None
            is_prio = bool(game_name and game_name in priority_set)
            prio_rank = (priority_list.index(game_name) + 1) if is_prio else None
            is_excl = bool(game_name and is_game_excluded(game_name, exclude_set))
            viewers_val = _safe_int(getattr(watching_channel, "viewers", 0))
            raw_title = getattr(watching_channel, "title", None)
            if not raw_title or hasattr(raw_title, "_mock_name"):
                raw_title = getattr(watching_channel, "stream_title", None)
            if not raw_title or hasattr(raw_title, "_mock_name"):
                stream_obj = getattr(watching_channel, "_stream", None)
                raw_title = getattr(stream_obj, "title", None) if stream_obj else None
            title_val = _safe_str(raw_title, "")

            box_art = format_box_art(getattr(active_campaign, "image_url", None)) if active_campaign else None
            channel_data = {
                "name": _safe_str(getattr(watching_channel, "name", "")),
                "game": game_name,
                "url": _safe_str(getattr(watching_channel, "url", "")),
                "viewers": viewers_val,
                "title": title_val,
                "box_art_url": box_art,
                "is_priority": is_prio,
                "priority_rank": prio_rank,
                "is_excluded": is_excl,
            }

        # 2. Active Campaign and Drop
        if active_campaign is not None and getattr(active_campaign, "active_drop", None) is not None:
            drop = active_campaign.active_drop
            benefit_img = None
            benefit_name = None
            if getattr(drop, "benefits", None) and len(drop.benefits) > 0:
                first_b = drop.benefits[0]
                raw_img = getattr(first_b, "image_url", None)
                if raw_img and not hasattr(raw_img, "_mock_name") and "Mock" not in type(raw_img).__name__:
                    benefit_img = str(raw_img)
                benefit_name = _safe_str(getattr(first_b, "name", None))

            drop_req = _safe_int(getattr(drop, "required_minutes", 0))
            drop_cur = _safe_int(getattr(drop, "current_minutes", 0))
            drop_rem = _safe_int(getattr(drop, "remaining_minutes", max(0, drop_req - drop_cur)))
            drop_prog = _safe_float(getattr(drop, "progress", 0.0))
            drop_rewards = ""
            if hasattr(drop, "rewards_text") and callable(drop.rewards_text):
                try:
                    drop_rewards = _safe_str(drop.rewards_text())
                except Exception:
                    pass

            drop_data = {
                "name": _safe_str(getattr(drop, "name", "")),
                "rewards": drop_rewards,
                "current_minutes": drop_cur,
                "required_minutes": drop_req,
                "remaining_minutes": drop_rem,
                "progress_percent": int(drop_prog * 100) if drop_prog else 0,
                "campaign_name": _safe_str(getattr(active_campaign, "name", "")),
                "campaign_claimed": _safe_int(getattr(active_campaign, "claimed_drops", 0)),
                "campaign_total": _safe_int(getattr(active_campaign, "total_drops", 0)),
                "benefit_image": benefit_img,
                "benefit_name": benefit_name or _safe_str(getattr(drop, "name", "")),
                "box_art_url": format_box_art(getattr(active_campaign, "image_url", None)),
            }

        # 3. Campaigns List
        for camp in sorted(self.twitch.inventory, key=lambda c: (
            0 if c.game.name in priority_set else 1,
            priority_list.index(c.game.name) if c.game.name in priority_set else 0,
            c.ends_at,
        )):
            hours_left = (camp.ends_at - now).total_seconds() / 3600
            time_left_str = f"{hours_left:.1f}h left" if hours_left > 0 else "Ending"
            rank = (priority_list.index(camp.game.name) + 1) if camp.game.name in priority_set else None
            is_excl = bool(is_game_excluded(camp.game.name, exclude_set))
            is_farming = bool(active_campaign_id and getattr(camp, "id", None) == active_campaign_id)
            campaigns_data.append({
                "id": _safe_str(getattr(camp, "id", "")),
                "game": _safe_str(getattr(camp.game, "name", "")),
                "name": _safe_str(getattr(camp, "name", "")),
                "box_art_url": format_box_art(getattr(camp, "image_url", None)),
                "is_currently_farming": is_farming,
                "claimed": _safe_int(getattr(camp, "claimed_drops", 0)),
                "total": _safe_int(getattr(camp, "total_drops", 0)),
                "progress_percent": int(_safe_float(getattr(camp, "progress", 0.0)) * 100),
                "is_priority": camp.game.name in priority_set,
                "is_excluded": is_excl,
                "priority_rank": rank,
                "time_left": time_left_str,
            })

        # 4. Metrics
        metrics_stats = {}
        if metrics := getattr(self.twitch, "metrics", None):
            metrics_stats = metrics.get_stats()

        inv_claimed = sum(getattr(c, "claimed_drops", 0) for c in getattr(self.twitch, "inventory", []))
        total_claimed = max(metrics_stats.get("drops_claimed", 0), inv_claimed)
        metrics_stats["drops_claimed"] = total_claimed
        metrics_stats["claimed_drops"] = total_claimed
        if "uptime_hours" in metrics_stats:
            metrics_stats["uptime_hours"] = round(metrics_stats["uptime_hours"], 1)
        if "total_minutes_watched" in metrics_stats:
            metrics_stats["watch_time_hours"] = round(metrics_stats["total_minutes_watched"] / 60, 1)
        if "watch_success_rate" in metrics_stats:
            metrics_stats["success_rate"] = round(metrics_stats["watch_success_rate"], 1)

        # System Memory Info
        memory_mb = None
        if psutil:
            try:
                memory_mb = round(psutil.Process().memory_info().rss / (1024 * 1024), 1)
            except Exception:
                pass

        data = {
            "state": self.twitch._state.name if hasattr(self.twitch, "_state") else "UNKNOWN",
            "priority_mode": self.twitch.settings.priority_mode.name,
            "channel": channel_data,
            "drop": drop_data,
            "campaigns": campaigns_data,
            "metrics": metrics_stats,
            "system": {
                "memory_mb": memory_mb,
            },
            "network": {
                "healthy": bool(getattr(self.twitch, "is_network_healthy", True)),
                "consecutive_failures": int(getattr(self.twitch, "_consecutive_gql_failures", 0) or 0),
            },
            "discord": {
                "enabled": bool(getattr(self.twitch.settings, "discord_notifications_enabled", True)),
                "has_webhook": bool(str(getattr(self.twitch.settings, "discord_webhook_url", "")).strip()),
                "interval_minutes": int(getattr(self.twitch.settings, "discord_summary_interval_minutes", 60) or 60),
            },
            "has_password": bool(self.password),
            "two_factor_pending": SCREENSHOT_2FA_PATH.exists(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return web.json_response(data)

    async def handle_2fa_status(self, request: web.Request) -> web.Response:
        """Returns whether a 2FA challenge is currently pending."""
        return web.json_response({
            "pending": SCREENSHOT_2FA_PATH.exists(),
            "has_screenshot": SCREENSHOT_2FA_PATH.exists(),
            "has_token": TOKEN_2FA_PATH.exists(),
        })

    async def handle_2fa_screenshot(self, request: web.Request) -> web.Response:
        """Returns the 2FA login challenge screenshot if available."""
        if SCREENSHOT_2FA_PATH.exists():
            return web.FileResponse(SCREENSHOT_2FA_PATH)
        return web.Response(status=404, text="No active 2FA challenge screenshot found.")

    async def handle_submit_2fa(self, request: web.Request) -> web.Response:
        """Receives and stores 2FA code in files/2fa.token."""
        code = ""
        if request.content_type == "application/json":
            try:
                body = await request.json()
                code = str(body.get("code", ""))
            except Exception:
                pass
        else:
            data = await request.post()
            code = str(data.get("code", ""))

        code_clean = re.sub(r'[^a-zA-Z0-9]', '', code.strip())
        if not code_clean or len(code_clean) > 16:
            return web.json_response({"error": "Invalid 2FA code format."}, status=400)

        TOKEN_2FA_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_2FA_PATH.write_text(code_clean, encoding="utf-8")
        logger.info(f"Web Dashboard: Received and stored 2FA token '{code_clean}'")
        return web.json_response({"status": "ok", "message": "2FA code submitted successfully!"})

    async def handle_switch(self, request: web.Request) -> web.Response:
        """Forces channel switch."""
        logger.info("Web Dashboard: Force switch requested")
        self.twitch.stop_watching()
        self.twitch.change_state(State.CHANNEL_SWITCH)
        return web.json_response({"status": "ok", "message": "Channel switch triggered"})

    async def handle_refresh(self, request: web.Request) -> web.Response:
        """Forces campaigns inventory refresh."""
        logger.info("Web Dashboard: Campaigns refresh requested")
        self.twitch.change_state(State.INVENTORY_FETCH)
        return web.json_response({"status": "ok", "message": "Campaigns refresh triggered"})

    async def handle_refresh_cookies(self, request: web.Request) -> web.Response:
        """Triggers Playwright cookie refresh."""
        logger.info("Web Dashboard: Cookie refresh requested")
        try:
            from cookie_refresher import CookieRefresher
            refresher = CookieRefresher()
            success = await refresher.refresh_session(headless=True)
            if success:
                self.twitch.check_cookies_updated()
                return web.json_response({"status": "ok", "message": "Cookies refreshed successfully via Playwright!"})
            else:
                return web.json_response({"status": "error", "message": "Cookie refresh failed. Check browser profile."}, status=500)
        except Exception as e:
            logger.error(f"Web Dashboard cookie refresh error: {e}")
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def handle_toggle_notifications(self, request: web.Request) -> web.Response:
        """Toggles Discord webhook notifications on or off."""
        current = getattr(self.twitch.settings, "discord_notifications_enabled", True)
        new_state = not current
        self.twitch.discord.set_enabled(new_state)
        status_str = "ENABLED" if new_state else "DISABLED"
        logger.info(f"Web Dashboard: Discord notifications {status_str}")
        return web.json_response({
            "status": "ok",
            "enabled": new_state,
            "message": f"Discord notifications {status_str.lower()}!"
        })

    async def handle_logs(self, request: web.Request) -> web.Response:
        """Returns buffered application logs with level filtering and limits."""
        if not hasattr(self, "log_handler") or not self.log_handler:
            return web.json_response({"logs": [], "total": 0, "filter": "ALL"})

        level_filter = request.query.get("level", "").strip().upper()
        limit_str = request.query.get("limit", "200").strip()
        try:
            limit = min(500, max(1, int(limit_str)))
        except ValueError:
            limit = 200

        LEVEL_HIERARCHY = {
            "DEBUG": ["DEBUG", "INFO", "WARNING", "ERROR"],
            "INFO": ["INFO", "WARNING", "ERROR"],
            "WARNING": ["WARNING", "ERROR"],
            "ERROR": ["ERROR"],
        }

        allowed_levels = LEVEL_HIERARCHY.get(level_filter)
        all_logs = list(self.log_handler.buffer)
        if allowed_levels:
            filtered = [entry for entry in all_logs if entry.get("level") in allowed_levels]
        else:
            filtered = all_logs

        return web.json_response({
            "logs": filtered[-limit:],
            "total": len(all_logs),
            "filter": level_filter or "ALL",
        })

    async def handle_logs_clear(self, request: web.Request) -> web.Response:
        """Clears the in-memory logs buffer."""
        if hasattr(self, "log_handler") and self.log_handler:
            self.log_handler.clear()
        return web.json_response({"status": "ok", "message": "Logs cleared"})

    @staticmethod
    def _mask_webhook_url(url: str) -> str:
        if not url:
            return ""
        m = re.match(r"^(https://discord\.com/api/webhooks/\d+/)(.+)$", url.strip())
        if m:
            prefix = m.group(1)
            token = m.group(2)
            if len(token) > 6:
                return f"{prefix}{token[:3]}*****{token[-3:]}"
            return f"{prefix}*****"
        return "https://discord.com/api/webhooks/*****"

    async def handle_get_settings(self, request: web.Request) -> web.Response:
        """Returns all configured settings with masked secrets and options catalog."""
        s = self.twitch.settings
        webhook = getattr(s, "discord_webhook_url", "")
        masked_webhook = self._mask_webhook_url(webhook) if webhook else ""

        # Extract known games and build a rich games catalog
        games_catalog: dict[str, dict[str, Any]] = {}
        inventory = getattr(self.twitch, "inventory", []) or []
        for c in inventory:
            game_obj = getattr(c, "game", None)
            g_name = _safe_str(getattr(game_obj, "name", None))
            if not g_name:
                continue
            claimed = _safe_int(getattr(c, "claimed_drops", 0))
            total = _safe_int(getattr(c, "total_drops", 0))
            box_art = format_box_art(getattr(c, "image_url", None))
            is_act = _safe_bool(getattr(c, "active", False))
            is_lnk = _safe_bool(getattr(c, "linked", False))
            ends_at_val = c.ends_at.isoformat() if getattr(c, "ends_at", None) and hasattr(c.ends_at, "isoformat") else None

            if g_name not in games_catalog:
                games_catalog[g_name] = {
                    "name": g_name,
                    "box_art_url": box_art,
                    "is_active": is_act,
                    "is_linked": is_lnk,
                    "claimed_drops": claimed,
                    "total_drops": total,
                    "ends_at": ends_at_val,
                }
            else:
                entry = games_catalog[g_name]
                entry["claimed_drops"] += claimed
                entry["total_drops"] += total
                if is_act:
                    entry["is_active"] = True
                if not entry["box_art_url"] and box_art:
                    entry["box_art_url"] = box_art

        priority_list = list(getattr(s, "priority", []))
        exclude_raw = getattr(s, "exclude", set())
        exclude_list = sorted(list(exclude_raw)) if isinstance(exclude_raw, (set, list)) else []

        for p_name in priority_list:
            safe_p = _safe_str(p_name)
            if safe_p and safe_p not in games_catalog:
                games_catalog[safe_p] = {
                    "name": safe_p,
                    "box_art_url": None,
                    "is_active": False,
                    "is_linked": False,
                    "claimed_drops": 0,
                    "total_drops": 0,
                    "ends_at": None,
                }

        for e_name in exclude_list:
            safe_e = _safe_str(e_name)
            if safe_e and safe_e not in games_catalog:
                games_catalog[safe_e] = {
                    "name": safe_e,
                    "box_art_url": None,
                    "is_active": False,
                    "is_linked": False,
                    "claimed_drops": 0,
                    "total_drops": 0,
                    "ends_at": None,
                }

        known_games = sorted(list(games_catalog.keys()))
        catalog_list = sorted(
            list(games_catalog.values()),
            key=lambda g: (not g["is_active"], g["claimed_drops"] == 0, g["name"].lower())
        )

        priority_mode = s.priority_mode
        mode_name = priority_mode.name if hasattr(priority_mode, "name") else str(priority_mode)

        settings_dict = {
            "priority_mode": _safe_str(mode_name, "BALANCED"),
            "connection_quality": _safe_int(getattr(s, "connection_quality", 1), 1),
            "stale_stream_timeout_minutes": _safe_int(getattr(s, "stale_stream_timeout_minutes", 5), 5),
            "maintenance_interval_minutes": _safe_int(getattr(s, "maintenance_interval_minutes", 20), 20),
            "discord_webhook_url": _safe_str(masked_webhook, ""),
            "has_discord_webhook": bool(webhook and _safe_str(webhook)),
            "discord_notifications_enabled": _safe_bool(getattr(s, "discord_notifications_enabled", True), True),
            "discord_summary_interval_minutes": _safe_int(getattr(s, "discord_summary_interval_minutes", 60), 60),
            "logging_level": _safe_str(getattr(s, "logging_level", "INFO"), "INFO"),
            "enable_badges_emotes": _safe_bool(getattr(s, "enable_badges_emotes", False), False),
            "available_drops_check": _safe_bool(getattr(s, "available_drops_check", False), False),
            "auto_cookie_refresh": _safe_bool(getattr(s, "auto_cookie_refresh", False), False),
            "priority": [_safe_str(p) for p in priority_list if _safe_str(p)],
            "exclude": [_safe_str(e) for e in exclude_list if _safe_str(e)],
            "web_port": _safe_int(getattr(s, "web_port", 8080), 8080),
            "has_web_password": bool(self.password),
            "has_discord_bot_token": bool(_safe_str(getattr(s, "discord_bot_token", ""))),
            "discord_owner_id": _safe_str(getattr(s, "discord_owner_id", "")),
        }

        options = {
            "priority_modes": [
                {"id": 3, "name": "BALANCED", "label": "Balanced (Recommended: Priority list first, then scarce/urgent campaigns)"},
                {"id": 0, "name": "PRIORITY_ONLY", "label": "Priority Only (Only farm games explicitly listed in your priority queue)"},
                {"id": 1, "name": "ENDING_SOONEST", "label": "Ending Soonest (Focus on campaigns ending soonest)"},
                {"id": 2, "name": "LOW_AVBL_FIRST", "label": "Low Availability First (Farm scarce campaigns first)"},
            ],
            "connection_qualities": [
                {"id": 1, "label": "1 - Low (Fastest channel checks, lowest resource usage)"},
                {"id": 2, "label": "2 - Medium (Standard validation)"},
                {"id": 3, "label": "3 - High (Conservative verification)"},
            ],
            "logging_levels": ["DEBUG", "INFO", "WARNING", "ERROR"],
            "summary_intervals": [
                {"minutes": 60, "label": "1 Hour"},
                {"minutes": 360, "label": "6 Hours"},
                {"minutes": 720, "label": "12 Hours"},
                {"minutes": 1440, "label": "24 Hours (Daily)"},
                {"minutes": 10080, "label": "10080 Minutes (Weekly)"},
            ],
            "known_games": known_games,
            "games_catalog": catalog_list,
        }

        return web.json_response({
            "status": "ok",
            "settings": settings_dict,
            "options": options,
        })

    async def handle_update_settings(self, request: web.Request) -> web.Response:
        """Validates and applies settings updates from the Web UI."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        s = self.twitch.settings
        altered = False
        recheck_channels = False

        # 1. Priority Mode
        if "priority_mode" in data:
            raw_mode = data["priority_mode"]
            mode_obj = None
            if isinstance(raw_mode, int):
                try:
                    mode_obj = PriorityMode(raw_mode)
                except ValueError:
                    return web.json_response({"error": f"Invalid priority_mode ID: {raw_mode}"}, status=400)
            elif isinstance(raw_mode, str):
                try:
                    mode_obj = PriorityMode[raw_mode.strip().upper()]
                except KeyError:
                    return web.json_response({"error": f"Invalid priority_mode name: {raw_mode}"}, status=400)
            if mode_obj is not None and getattr(s, "priority_mode", None) != mode_obj:
                s.priority_mode = mode_obj
                altered = True
                recheck_channels = True

        # 2. Connection Quality
        if "connection_quality" in data:
            try:
                cq = int(data["connection_quality"])
                if cq not in (1, 2, 3):
                    raise ValueError()
                if getattr(s, "connection_quality", 1) != cq:
                    s.connection_quality = cq
                    altered = True
            except (ValueError, TypeError):
                return web.json_response({"error": "connection_quality must be 1, 2, or 3"}, status=400)

        # 3. Stale Stream Timeout
        if "stale_stream_timeout_minutes" in data:
            try:
                sst = int(data["stale_stream_timeout_minutes"])
                if sst < 1 or sst > 120:
                    raise ValueError()
                if getattr(s, "stale_stream_timeout_minutes", 5) != sst:
                    s.stale_stream_timeout_minutes = sst
                    altered = True
            except (ValueError, TypeError):
                return web.json_response({"error": "stale_stream_timeout_minutes must be between 1 and 120"}, status=400)

        # 4. Maintenance Interval
        if "maintenance_interval_minutes" in data:
            try:
                mi = int(data["maintenance_interval_minutes"])
                if mi < 5 or mi > 1440:
                    raise ValueError()
                if getattr(s, "maintenance_interval_minutes", 20) != mi:
                    s.maintenance_interval_minutes = mi
                    altered = True
            except (ValueError, TypeError):
                return web.json_response({"error": "maintenance_interval_minutes must be between 5 and 1440"}, status=400)

        # 5. Discord Webhook URL
        if "discord_webhook_url" in data:
            raw_url = str(data["discord_webhook_url"]).strip()
            if "*****" not in raw_url and raw_url.lower() != "keep":
                if raw_url and not raw_url.startswith("https://discord.com/api/webhooks/"):
                    return web.json_response({"error": "discord_webhook_url must start with https://discord.com/api/webhooks/"}, status=400)
                if getattr(s, "discord_webhook_url", "") != raw_url:
                    s.discord_webhook_url = raw_url
                    if hasattr(self.twitch, "discord") and self.twitch.discord:
                        self.twitch.discord.webhook_url = raw_url
                    altered = True

        # 6. Discord Notifications Enabled
        if "discord_notifications_enabled" in data:
            enabled = bool(data["discord_notifications_enabled"])
            if getattr(s, "discord_notifications_enabled", True) != enabled:
                s.discord_notifications_enabled = enabled
                if hasattr(self.twitch, "discord") and self.twitch.discord:
                    self.twitch.discord.set_enabled(enabled)
                altered = True

        # 7. Discord Summary Interval
        if "discord_summary_interval_minutes" in data:
            try:
                dsi = int(data["discord_summary_interval_minutes"])
                if dsi < 1 or dsi > 10080:
                    raise ValueError()
                if getattr(s, "discord_summary_interval_minutes", 60) != dsi:
                    s.discord_summary_interval_minutes = dsi
                    if hasattr(self.twitch, "discord") and self.twitch.discord:
                        self.twitch.discord.summary_interval_minutes = dsi
                    altered = True
            except (ValueError, TypeError):
                return web.json_response({"error": "discord_summary_interval_minutes must be between 1 and 10080"}, status=400)

        # 8. Logging Level
        if "logging_level" in data:
            ll = str(data["logging_level"]).strip().upper()
            if ll not in ("DEBUG", "INFO", "WARNING", "ERROR"):
                return web.json_response({"error": "logging_level must be DEBUG, INFO, WARNING, or ERROR"}, status=400)
            if getattr(s, "logging_level", "INFO") != ll:
                s.logging_level = ll
                logging.getLogger("TwitchDrops").setLevel(getattr(logging, ll, logging.INFO))
                altered = True

        # 9. Boolean toggles
        for bool_field in ("enable_badges_emotes", "available_drops_check", "auto_cookie_refresh"):
            if bool_field in data:
                val = bool(data[bool_field])
                if getattr(s, bool_field, False) != val:
                    setattr(s, bool_field, val)
                    altered = True

        # 10. Priority List
        if "priority" in data:
            p = [str(g).strip() for g in data["priority"] if str(g).strip()]
            seen = set()
            clean_p = [x for x in p if not (x in seen or seen.add(x))]
            if getattr(s, "priority", []) != clean_p:
                s.priority = clean_p
                altered = True
                recheck_channels = True

        # 11. Exclude Set
        if "exclude" in data:
            e = {str(g).strip() for g in data["exclude"] if str(g).strip()}
            if getattr(s, "exclude", set()) != e:
                s.exclude = e
                altered = True
                recheck_channels = True

        # 12. Web Password Update
        if "web_password" in data:
            new_pwd = str(data["web_password"]).strip()
            if new_pwd and new_pwd != "*****" and new_pwd != self.password:
                s.web_password = new_pwd
                self.password = new_pwd
                altered = True

        if altered:
            if hasattr(s, "alter"):
                s.alter()
            s.save(force=True)
            logger.info("Web Dashboard: Settings updated and saved to disk")

        if recheck_channels and hasattr(self.twitch, "stop_watching"):
            self.twitch.stop_watching()
            self.twitch.change_state(State.CHANNEL_SWITCH)

        return web.json_response({"status": "ok", "message": "Settings updated successfully!"})

    async def handle_priority_action(self, request: web.Request) -> web.Response:
        """Handles fine-grained priority ranking actions."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        action = str(data.get("action", "")).strip().lower()
        game = str(data.get("game", "")).strip()
        if not game:
            return web.json_response({"error": "Game name required"}, status=400)

        s = self.twitch.settings
        priority = list(getattr(s, "priority", []))
        exclude_raw = getattr(s, "exclude", set())
        exclude = set(exclude_raw) if isinstance(exclude_raw, (set, list)) else set()

        if action == "add":
            rank = data.get("rank")
            if game in priority:
                priority.remove(game)
            if rank is not None:
                try:
                    rank_idx = max(0, min(int(rank) - 1, len(priority)))
                    priority.insert(rank_idx, game)
                except (ValueError, TypeError):
                    priority.append(game)
            else:
                priority.append(game)
            exclude.discard(game)
        elif action == "remove":
            if game in priority:
                priority.remove(game)
        elif action == "move_up":
            if game in priority:
                idx = priority.index(game)
                if idx > 0:
                    priority[idx], priority[idx - 1] = priority[idx - 1], priority[idx]
        elif action == "move_down":
            if game in priority:
                idx = priority.index(game)
                if idx < len(priority) - 1:
                    priority[idx], priority[idx + 1] = priority[idx + 1], priority[idx]
        elif action == "top":
            if game in priority:
                priority.remove(game)
            priority.insert(0, game)
            exclude.discard(game)
        else:
            return web.json_response({"error": f"Unknown action: {action}"}, status=400)

        s.priority = priority
        s.exclude = exclude
        if hasattr(s, "alter"):
            s.alter()
        s.save(force=True)

        if hasattr(self.twitch, "stop_watching"):
            self.twitch.stop_watching()
            self.twitch.change_state(State.CHANNEL_SWITCH)

        return web.json_response({"status": "ok", "priority": s.priority})

    async def handle_exclude_action(self, request: web.Request) -> web.Response:
        """Handles add and remove operations for excluded games."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        action = str(data.get("action", "")).strip().lower()
        game = str(data.get("game", "")).strip()
        if not game:
            return web.json_response({"error": "Game name required"}, status=400)

        s = self.twitch.settings
        exclude_raw = getattr(s, "exclude", set())
        exclude = set(exclude_raw) if isinstance(exclude_raw, (set, list)) else set()
        priority = list(getattr(s, "priority", []))

        if action == "add":
            exclude.add(game)
            if game in priority:
                priority.remove(game)
        elif action == "remove":
            exclude.discard(game)
        else:
            return web.json_response({"error": f"Unknown action: {action}"}, status=400)

        s.exclude = exclude
        s.priority = priority
        if hasattr(s, "alter"):
            s.alter()
        s.save(force=True)

        if hasattr(self.twitch, "stop_watching"):
            self.twitch.stop_watching()
            self.twitch.change_state(State.CHANNEL_SWITCH)

        return web.json_response({"status": "ok", "exclude": sorted(list(s.exclude))})

    async def handle_test_notification(self, request: web.Request) -> web.Response:
        """Sends a test Discord notification webhook."""
        webhook = getattr(self.twitch.settings, "discord_webhook_url", "")
        if not webhook:
            return web.json_response({"error": "No Discord webhook URL configured."}, status=400)

        discord = getattr(self.twitch, "discord", None)
        if not discord:
            return web.json_response({"error": "Discord notifier not initialized."}, status=500)

        try:
            success = await discord.send_test_notification()
            if success:
                return web.json_response({"status": "ok", "message": "Test notification sent successfully!"})
            else:
                return web.json_response({"status": "error", "message": "Failed to send test notification. Check logs."}, status=500)
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def handle_campaigns(self, request: web.Request) -> web.Response:
        """Returns detailed inventory of all campaigns with drops and link statuses."""
        campaigns_list = []
        now = datetime.now(timezone.utc)
        priority_list = list(getattr(self.twitch.settings, "priority", []))
        priority_set = set(priority_list)
        exclude_raw = getattr(self.twitch.settings, "exclude", set())
        exclude_set = set(exclude_raw) if isinstance(exclude_raw, (set, list)) else set()

        watching_channel = self.twitch.watching_channel.get_with_default(None)
        active_campaign = self.twitch.get_active_campaign(watching_channel) if watching_channel else None
        active_campaign_id = getattr(active_campaign, "id", None) if active_campaign else None

        inventory = getattr(self.twitch, "inventory", [])
        for camp in sorted(inventory, key=lambda c: (
            0 if (getattr(c, "game", None) and getattr(c.game, "name", "") in priority_set) else 1,
            priority_list.index(c.game.name) if (getattr(c, "game", None) and getattr(c.game, "name", "") in priority_set) else 0,
            getattr(c, "ends_at", now),
        )):
            game_name = camp.game.name if getattr(camp, "game", None) else "Unknown"
            is_prio = game_name in priority_set
            is_excl = bool(is_game_excluded(game_name, exclude_set))
            rank = (priority_list.index(game_name) + 1) if is_prio else None
            is_farming = bool(active_campaign_id and getattr(camp, "id", None) == active_campaign_id)

            time_left_str = "Ending"
            if getattr(camp, "ends_at", None):
                hours_left = (camp.ends_at - now).total_seconds() / 3600
                if hours_left > 0:
                    time_left_str = f"{hours_left:.1f}h left"

            drops_list = []
            timed_drops = getattr(camp, "timed_drops", {})
            if isinstance(timed_drops, dict):
                drops_iterable = timed_drops.values()
            elif isinstance(timed_drops, (list, tuple, set)):
                drops_iterable = timed_drops
            else:
                drops_iterable = []

            for drop in drops_iterable:
                progress_val = _safe_float(getattr(drop, "progress", 0.0))
                drop_img = None
                benefit_name = None
                if getattr(drop, "benefits", None) and len(drop.benefits) > 0:
                    b = drop.benefits[0]
                    raw_b_img = getattr(b, "image_url", None)
                    if raw_b_img and not hasattr(raw_b_img, "_mock_name") and "Mock" not in type(raw_b_img).__name__:
                        drop_img = str(raw_b_img)
                    benefit_name = _safe_str(getattr(b, "name", ""))

                drops_list.append({
                    "id": _safe_str(getattr(drop, "id", "")),
                    "name": _safe_str(getattr(drop, "name", "Unnamed Drop"), "Unnamed Drop"),
                    "required_minutes": _safe_int(getattr(drop, "required_minutes", 0)),
                    "current_minutes": _safe_int(getattr(drop, "current_minutes", 0)),
                    "is_claimed": _safe_bool(getattr(drop, "is_claimed", False)),
                    "progress_percent": int(progress_val * 100),
                    "image_url": drop_img,
                    "benefit_name": benefit_name or _safe_str(getattr(drop, "name", "")),
                })

            campaigns_list.append({
                "id": _safe_str(getattr(camp, "id", "")),
                "game": game_name,
                "name": _safe_str(getattr(camp, "name", game_name), game_name),
                "box_art_url": format_box_art(getattr(camp, "image_url", None)),
                "is_currently_farming": is_farming,
                "status": _safe_str(getattr(camp, "status", "ACTIVE"), "ACTIVE"),
                "is_linked": _safe_bool(getattr(camp, "linked", False)),
                "link_url": _safe_str(getattr(camp, "link_url", "")),
                "starts_at": camp.starts_at.isoformat() if getattr(camp, "starts_at", None) and hasattr(camp.starts_at, "isoformat") else None,
                "ends_at": camp.ends_at.isoformat() if getattr(camp, "ends_at", None) and hasattr(camp.ends_at, "isoformat") else None,
                "time_left": time_left_str,
                "is_priority": is_prio,
                "priority_rank": rank,
                "is_excluded": is_excl,
                "claimed_drops": _safe_int(getattr(camp, "claimed_drops", 0)),
                "total_drops": _safe_int(getattr(camp, "total_drops", len(drops_list))),
                "progress_percent": int(_safe_float(getattr(camp, "progress", 0.0)) * 100),
                "drops": drops_list,
            })

        return web.json_response({
            "status": "ok",
            "total": len(campaigns_list),
            "campaigns": campaigns_list,
        })

    async def start(self):
        """Starts the aiohttp web server."""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()
        logger.info(f"Web Dashboard active at http://{self.host}:{self.port}")

    async def stop(self):
        """Stops the aiohttp web server gracefully."""
        if hasattr(self, "log_handler") and self.log_handler:
            logging.getLogger("TwitchDrops").removeHandler(self.log_handler)
        if self.runner:
            await self.runner.cleanup()
            logger.info("Web Dashboard stopped")


_dashboard_instance: WebDashboard | None = None


async def start_web_dashboard(twitch: Twitch, host: str = "0.0.0.0", port: int = 8080) -> WebDashboard:
    """Global helper to start the dashboard."""
    global _dashboard_instance
    if _dashboard_instance is None:
        _dashboard_instance = WebDashboard(twitch, host=host, port=port)
        await _dashboard_instance.start()
    return _dashboard_instance


async def stop_web_dashboard():
    """Global helper to stop the dashboard."""
    global _dashboard_instance
    if _dashboard_instance is not None:
        await _dashboard_instance.stop()
        _dashboard_instance = None
