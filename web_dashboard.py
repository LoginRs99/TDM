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

try:
    import psutil
except ImportError:
    psutil = None

if TYPE_CHECKING:
    from twitch import Twitch

logger = logging.getLogger("TwitchDrops")

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
    </style>
</head>
<body class="bg-twitch-dark text-gray-200 font-sans min-h-screen flex flex-col antialiased">
    <!-- Top Header -->
    <header class="bg-twitch-card border-b border-twitch-border px-4 sm:px-6 py-3.5 sticky top-0 z-40 shadow-lg">
        <div class="max-w-7xl mx-auto flex flex-col sm:flex-row items-center justify-between gap-3">
            <!-- Brand & Navigation Tabs -->
            <div class="flex items-center space-x-6 w-full sm:w-auto justify-between sm:justify-start">
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
                <nav class="flex items-center space-x-1 bg-twitch-dark p-1 rounded-lg border border-twitch-border">
                    <button onclick="switchTab('overview')" id="tab-btn-overview" class="tab-btn px-3 py-1.5 rounded-md text-xs font-semibold transition text-white bg-twitch-purple shadow-sm">
                        📊 Overview
                    </button>
                    <button onclick="switchTab('campaigns')" id="tab-btn-campaigns" class="tab-btn px-3 py-1.5 rounded-md text-xs font-semibold transition text-gray-400 hover:text-white flex items-center space-x-1.5">
                        <span>🎯 Campaigns</span>
                        <span id="nav-campaigns-count" class="px-1.5 py-0.2 rounded-full text-[10px] bg-twitch-border text-gray-300">0</span>
                    </button>
                    <button onclick="switchTab('settings')" id="tab-btn-settings" class="tab-btn px-3 py-1.5 rounded-md text-xs font-semibold transition text-gray-400 hover:text-white flex items-center space-x-1.5">
                        <span>⚙️ Settings</span>
                        <span id="settings-dirty-dot" class="w-1.5 h-1.5 rounded-full bg-amber-400 hidden"></span>
                    </button>
                    <button onclick="switchTab('logs')" id="tab-btn-logs" class="tab-btn px-3 py-1.5 rounded-md text-xs font-semibold transition text-gray-400 hover:text-white flex items-center space-x-1.5">
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
                <span id="uptime-text" class="text-gray-400 whitespace-nowrap hidden md:inline">Uptime: --</span>
                <a id="logout-link" href="/logout" class="text-red-400/80 hover:text-red-400 border border-red-900/50 hover:border-red-700/80 px-2 py-1 rounded-md transition hidden whitespace-nowrap" title="Logout">
                    🚪 Logout
                </a>
            </div>
        </div>
    </header>

    <!-- Main Container -->
    <main class="flex-1 max-w-7xl w-full mx-auto p-4 sm:p-6 space-y-6">
        <!-- 2FA Remote Solver Banner (Shown when challenge is active) -->
        <div id="banner-2fa" class="hidden bg-yellow-950/40 border-2 border-yellow-500/70 rounded-xl p-5 shadow-2xl relative space-y-4">
            <div class="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
                <div class="flex items-center space-x-3">
                    <div class="w-10 h-10 rounded-lg bg-yellow-500/20 text-yellow-400 flex items-center justify-center text-xl font-bold border border-yellow-500/30">
                        🔐
                    </div>
                    <div>
                        <h3 class="text-base font-bold text-yellow-300">Twitch 2FA Challenge Required!</h3>
                        <p class="text-xs text-yellow-200/70">Enter the verification code sent to your email or authenticator app.</p>
                    </div>
                </div>
                <div class="flex items-center space-x-2 w-full sm:w-auto">
                    <input id="input-2fa" type="text" maxlength="16" placeholder="Enter 2FA Code" class="px-3 py-2 bg-black/70 border border-yellow-500/50 rounded-lg text-white text-sm focus:outline-none focus:border-yellow-400 w-full sm:w-44 text-center font-mono tracking-widest uppercase">
                    <button onclick="submit2FA()" class="px-4 py-2 bg-yellow-500 hover:bg-yellow-400 text-black font-bold rounded-lg text-xs transition active:scale-95 whitespace-nowrap shadow-lg shadow-yellow-500/20">
                        Submit
                    </button>
                </div>
            </div>
            <div id="container-2fa-screenshot" class="text-center pt-2 border-t border-yellow-500/20 hidden">
                <p class="text-[11px] text-yellow-400/80 mb-2 font-mono">Twitch Challenge Screen:</p>
                <img id="img-2fa" src="/api/2fa/screenshot" class="max-h-60 mx-auto rounded border border-yellow-500/40 shadow-md" alt="2FA Screen" />
            </div>
        </div>

        <!-- ================= TAB 1: OVERVIEW ================= -->
        <div id="tab-overview" class="tab-pane space-y-6">
            <!-- Hero Watching Card -->
            <div class="bg-twitch-card border border-twitch-border rounded-xl p-5 sm:p-6 shadow-xl relative overflow-hidden">
                <div class="absolute top-0 left-0 w-2 h-full bg-twitch-purple"></div>
                
                <div class="flex flex-col lg:flex-row lg:items-center justify-between gap-6">
                    <!-- Stream & Channel Info -->
                    <div class="space-y-3 flex-1">
                        <div class="flex items-center space-x-2">
                            <span class="text-xs font-semibold uppercase tracking-wider text-twitch-purple">Currently Watching</span>
                            <span id="pulse-dot" class="w-2 h-2 rounded-full bg-green-500 animate-ping"></span>
                        </div>

                        <div class="flex items-start sm:items-center space-x-4">
                            <div id="channel-avatar" class="w-14 h-14 rounded-full bg-gray-800 border-2 border-twitch-purple/50 flex items-center justify-center text-xl font-bold text-white overflow-hidden shadow">
                                📺
                            </div>
                            <div>
                                <h2 id="channel-name" class="text-2xl font-extrabold text-white leading-none">Scanning for streams...</h2>
                                <p id="game-name" class="text-sm text-gray-400 mt-1 font-medium">Waiting for active campaign</p>
                                <a id="stream-link" href="#" target="_blank" class="text-xs text-twitch-purple hover:underline mt-0.5 inline-block hidden">Open stream ↗</a>
                            </div>
                        </div>
                    </div>

                    <!-- Drop Progress Section -->
                    <div class="flex-1 bg-twitch-dark/70 border border-twitch-border/70 rounded-xl p-4 space-y-3">
                        <div class="flex justify-between items-center text-sm">
                            <span id="drop-name" class="font-semibold text-white truncate max-w-[280px]">No active drop</span>
                            <span id="drop-percent" class="font-bold text-twitch-purple">0%</span>
                        </div>

                        <!-- Progress Bar -->
                        <div class="w-full bg-gray-800 h-3 rounded-full overflow-hidden border border-gray-700/50">
                            <div id="progress-bar" class="bg-gradient-to-r from-twitch-accent to-twitch-purple h-full rounded-full transition-all duration-500" style="width: 0%"></div>
                        </div>

                        <div class="flex justify-between text-xs text-gray-400">
                            <span id="drop-minutes">0 / 0 min</span>
                            <span id="drop-eta" class="text-twitch-purple/90 font-medium">ETA: --</span>
                            <span id="campaign-drops-count">Drops: 0/0</span>
                        </div>
                    </div>
                </div>

                <!-- Quick Action Buttons -->
                <div class="mt-6 pt-4 border-t border-twitch-border/60 flex flex-wrap gap-3">
                    <button onclick="triggerAction('switch')" class="px-3.5 py-2 bg-twitch-purple/20 hover:bg-twitch-purple/30 text-twitch-purple border border-twitch-purple/40 rounded-lg text-xs font-semibold transition active:scale-95 flex items-center space-x-1.5">
                        <span>🔄</span>
                        <span>Switch Channel</span>
                    </button>
                    <button onclick="triggerAction('refresh')" class="px-3.5 py-2 bg-gray-800 hover:bg-gray-700 text-gray-200 border border-gray-700 rounded-lg text-xs font-semibold transition active:scale-95 flex items-center space-x-1.5">
                        <span>📦</span>
                        <span>Refresh Inventory</span>
                    </button>
                    <button onclick="triggerAction('refresh-cookies')" class="px-3.5 py-2 bg-blue-500/10 hover:bg-blue-500/20 text-blue-400 border border-blue-500/30 rounded-lg text-xs font-semibold transition active:scale-95 flex items-center space-x-1.5">
                        <span>🔐</span>
                        <span>Refresh Cookies (Playwright)</span>
                    </button>
                    <button onclick="triggerAction('toggle-notifications')" class="px-3.5 py-2 bg-purple-900/20 hover:bg-purple-900/30 text-purple-300 border border-purple-700/40 rounded-lg text-xs font-semibold transition active:scale-95 flex items-center space-x-1.5" title="Toggle Discord webhook notifications ON or OFF">
                        <span>🔔</span>
                        <span id="label-notifications">Discord: Alerts</span>
                    </button>
                </div>
            </div>

            <!-- Metrics Grid -->
            <div class="grid grid-cols-2 sm:grid-cols-4 gap-4">
                <div class="bg-twitch-card border border-twitch-border rounded-xl p-4 shadow">
                    <p class="text-xs text-gray-400 font-medium">Drops Claimed</p>
                    <p id="stat-claimed" class="text-2xl font-black text-white mt-1">0</p>
                </div>
                <div class="bg-twitch-card border border-twitch-border rounded-xl p-4 shadow">
                    <p class="text-xs text-gray-400 font-medium">Watch Time</p>
                    <p id="stat-watch-time" class="text-2xl font-black text-white mt-1">0h</p>
                </div>
                <div class="bg-twitch-card border border-twitch-border rounded-xl p-4 shadow">
                    <p class="text-xs text-gray-400 font-medium">Success Rate</p>
                    <p id="stat-rate" class="text-2xl font-black text-green-400 mt-1">100%</p>
                </div>
                <div class="bg-twitch-card border border-twitch-border rounded-xl p-4 shadow">
                    <p class="text-xs text-gray-400 font-medium">Priority Mode</p>
                    <p id="stat-mode" class="text-2xl font-black text-twitch-purple mt-1 truncate">BALANCED</p>
                </div>
            </div>

            <!-- Quick Overview Campaigns Card -->
            <div class="bg-twitch-card border border-twitch-border rounded-xl p-5 space-y-4 shadow-lg">
                <div class="flex items-center justify-between border-b border-twitch-border pb-3">
                    <h3 class="text-sm font-bold text-white flex items-center space-x-2">
                        <span>⭐</span>
                        <span>Priority & Active Campaigns</span>
                    </h3>
                    <button onclick="switchTab('campaigns')" class="text-xs text-twitch-purple hover:underline">View All Campaigns &rarr;</button>
                </div>
                <div id="overview-campaigns-list" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
                    <div class="col-span-full py-6 text-center text-xs text-gray-500">Loading priority campaigns...</div>
                </div>
            </div>
        </div>

        <!-- ================= TAB 2: CAMPAIGNS ================= -->
        <div id="tab-campaigns" class="tab-pane hidden space-y-5">
            <!-- Toolbar & Search -->
            <div class="bg-twitch-card border border-twitch-border rounded-xl p-4 flex flex-col md:flex-row items-center justify-between gap-3 shadow">
                <div class="relative w-full md:w-96">
                    <input id="campaigns-search" type="text" oninput="renderCampaignsTab()" placeholder="Filter by game or reward..." class="w-full pl-9 pr-3.5 py-2 bg-twitch-dark border border-twitch-border rounded-lg text-xs text-white placeholder-gray-500 focus:outline-none focus:border-twitch-purple transition">
                    <span class="absolute left-3 top-2.5 text-gray-500 text-xs">🔍</span>
                </div>
                
                <div class="flex items-center space-x-2 w-full md:w-auto overflow-x-auto justify-start md:justify-end">
                    <div class="flex items-center space-x-1 bg-twitch-dark p-1 rounded-lg border border-twitch-border text-xs">
                        <button onclick="setCampaignFilter('all')" id="cfilter-all" class="px-2.5 py-1 rounded font-medium bg-twitch-purple text-white">All</button>
                        <button onclick="setCampaignFilter('linked')" id="cfilter-linked" class="px-2.5 py-1 rounded font-medium text-gray-400 hover:text-white">Linked</button>
                        <button onclick="setCampaignFilter('priority')" id="cfilter-priority" class="px-2.5 py-1 rounded font-medium text-gray-400 hover:text-white">Priority</button>
                        <button onclick="setCampaignFilter('excluded')" id="cfilter-excluded" class="px-2.5 py-1 rounded font-medium text-gray-400 hover:text-white">Excluded</button>
                    </div>
                    <button onclick="triggerAction('refresh')" class="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 text-gray-200 border border-gray-700 rounded-lg text-xs font-semibold transition active:scale-95 whitespace-nowrap">
                        🔄 Refresh
                    </button>
                </div>
            </div>

            <!-- Campaigns Grid -->
            <div id="campaigns-tab-grid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                <div class="col-span-full py-12 text-center text-gray-500 text-sm">Loading campaigns...</div>
            </div>
        </div>

        <!-- ================= TAB 3: SETTINGS ================= -->
        <div id="tab-settings" class="tab-pane hidden space-y-6">
            <!-- Unsaved alert bar -->
            <div id="settings-unsaved-alert" class="hidden bg-amber-950/40 border border-amber-600/60 rounded-xl p-3.5 text-xs text-amber-300 flex items-center justify-between shadow">
                <div class="flex items-center space-x-2">
                    <span>⚠️</span>
                    <span>You have unsaved changes. Remember to click <strong>Save Settings</strong> below to apply.</span>
                </div>
                <div class="space-x-2">
                    <button onclick="loadSettings()" class="px-3 py-1 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded font-semibold transition">Discard</button>
                    <button onclick="saveSettings()" class="px-3 py-1 bg-twitch-purple hover:bg-twitch-accent text-white rounded font-semibold transition">Save Now</button>
                </div>
            </div>

            <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
                <!-- Card 1: Mining & Strategy -->
                <div class="bg-twitch-card border border-twitch-border rounded-xl p-5 space-y-4 shadow">
                    <h3 class="text-sm font-bold text-white flex items-center space-x-2 border-b border-twitch-border pb-3">
                        <span>🎯</span>
                        <span>Mining & Priority Strategy</span>
                    </h3>

                    <div class="space-y-4 text-xs">
                        <div>
                            <label class="block font-semibold text-gray-300 mb-1">Priority Mode</label>
                            <select id="setting-priority-mode" onchange="markSettingsDirty()" class="w-full px-3 py-2 bg-twitch-dark border border-twitch-border rounded-lg text-white text-xs focus:outline-none focus:border-twitch-purple transition">
                                <option value="3">BALANCED (Priority first, then scarce/urgent active drops) - Recommended</option>
                                <option value="0">PRIORITY_ONLY (Only farm games explicitly listed in priority list)</option>
                                <option value="1">ENDING_SOONEST (Focus on drops expiring earliest)</option>
                                <option value="2">LOW_AVBL_FIRST (Farm drops with the lowest available time)</option>
                            </select>
                            <p class="text-[11px] text-gray-500 mt-1">Controls how campaigns are chosen when multiple drop events are active.</p>
                        </div>

                        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                            <div>
                                <label class="block font-semibold text-gray-300 mb-1">Stale Stream Timeout (min)</label>
                                <input id="setting-stale-timeout" type="number" min="1" max="120" onchange="markSettingsDirty()" class="w-full px-3 py-2 bg-twitch-dark border border-twitch-border rounded-lg text-white text-xs focus:outline-none focus:border-twitch-purple transition">
                                <p class="text-[11px] text-gray-500 mt-1">Minutes before switching if progress freezes (Default: 5)</p>
                            </div>
                            <div>
                                <label class="block font-semibold text-gray-300 mb-1">Maintenance Interval (min)</label>
                                <input id="setting-maint-interval" type="number" min="5" max="1440" onchange="markSettingsDirty()" class="w-full px-3 py-2 bg-twitch-dark border border-twitch-border rounded-lg text-white text-xs focus:outline-none focus:border-twitch-purple transition">
                                <p class="text-[11px] text-gray-500 mt-1">Background scan interval (Default: 20)</p>
                            </div>
                        </div>

                        <div>
                            <label class="block font-semibold text-gray-300 mb-1">Connection Quality</label>
                            <select id="setting-conn-quality" onchange="markSettingsDirty()" class="w-full px-3 py-2 bg-twitch-dark border border-twitch-border rounded-lg text-white text-xs focus:outline-none focus:border-twitch-purple transition">
                                <option value="1">1 - Low (Fastest channel checks, lowest resource usage)</option>
                                <option value="2">2 - Medium (Standard validation)</option>
                                <option value="3">3 - High (Conservative verification)</option>
                            </select>
                        </div>

                        <div class="pt-2 border-t border-twitch-border/60 space-y-3">
                            <label class="flex items-center space-x-2.5 cursor-pointer">
                                <input id="setting-available-drops-check" type="checkbox" onchange="markSettingsDirty()" class="rounded bg-twitch-dark border-twitch-border text-twitch-purple focus:ring-0">
                                <div>
                                    <span class="font-medium text-gray-200">Extra Available Drops Check</span>
                                    <p class="text-[11px] text-gray-500">Perform additional queries to discover unlinked campaigns.</p>
                                </div>
                            </label>

                            <label class="flex items-center space-x-2.5 cursor-pointer">
                                <input id="setting-badges-emotes" type="checkbox" onchange="markSettingsDirty()" class="rounded bg-twitch-dark border-twitch-border text-twitch-purple focus:ring-0">
                                <div>
                                    <span class="font-medium text-gray-200">Load Badges & Emotes</span>
                                    <p class="text-[11px] text-gray-500">Leave disabled for minimal memory and network usage.</p>
                                </div>
                            </label>
                        </div>
                    </div>
                </div>

                <!-- Card 2: Discord Notifications & Alerts -->
                <div class="bg-twitch-card border border-twitch-border rounded-xl p-5 space-y-4 shadow">
                    <h3 class="text-sm font-bold text-white flex items-center space-x-2 border-b border-twitch-border pb-3">
                        <span>🔔</span>
                        <span>Discord Integration & Notifications</span>
                    </h3>

                    <div class="space-y-4 text-xs">
                        <div>
                            <label class="block font-semibold text-gray-300 mb-1">Discord Webhook URL</label>
                            <div class="flex space-x-2">
                                <input id="setting-webhook-url" type="text" placeholder="https://discord.com/api/webhooks/..." oninput="markSettingsDirty()" class="flex-1 px-3 py-2 bg-twitch-dark border border-twitch-border rounded-lg text-white text-xs font-mono focus:outline-none focus:border-twitch-purple transition">
                                <button onclick="testDiscordWebhook()" class="px-3 py-2 bg-purple-900/30 hover:bg-purple-900/50 text-purple-300 border border-purple-700/50 rounded-lg font-semibold transition active:scale-95 whitespace-nowrap">
                                    Send Test
                                </button>
                            </div>
                            <p class="text-[11px] text-gray-500 mt-1">Receive drop progress, claim notifications, and login status alerts.</p>
                        </div>

                        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 items-center">
                            <div>
                                <label class="block font-semibold text-gray-300 mb-1">Summary Interval</label>
                                <select id="setting-summary-interval" onchange="markSettingsDirty()" class="w-full px-3 py-2 bg-twitch-dark border border-twitch-border rounded-lg text-white text-xs focus:outline-none focus:border-twitch-purple transition">
                                    <option value="60">1 Hour</option>
                                    <option value="360">6 Hours</option>
                                    <option value="720">12 Hours</option>
                                    <option value="1440">24 Hours (Daily)</option>
                                    <option value="10080">10080 Minutes (Weekly)</option>
                                </select>
                            </div>
                            <div class="pt-2">
                                <label class="flex items-center space-x-2.5 cursor-pointer">
                                    <input id="setting-discord-enabled" type="checkbox" onchange="markSettingsDirty()" class="rounded bg-twitch-dark border-twitch-border text-twitch-purple focus:ring-0">
                                    <span class="font-medium text-gray-200">Webhook Notifications Active</span>
                                </label>
                            </div>
                        </div>

                        <!-- Discord Bot Status -->
                        <div class="pt-3 border-t border-twitch-border/60 space-y-2">
                            <span class="font-semibold text-gray-400 block">Interactive Discord Slash Bot Status:</span>
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
                            <p class="text-[11px] text-gray-500">Configure DISCORD_BOT_TOKEN and DISCORD_OWNER_ID in .env or docker-compose to use /status, /priority, and interactive buttons.</p>
                        </div>
                    </div>
                </div>

                <!-- Card 3: Priority Games Management -->
                <div class="bg-twitch-card border border-twitch-border rounded-xl p-5 space-y-4 shadow">
                    <div class="flex items-center justify-between border-b border-twitch-border pb-3">
                        <h3 class="text-sm font-bold text-white flex items-center space-x-2">
                            <span>⭐</span>
                            <span>Ranked Priority Queue</span>
                        </h3>
                        <span id="priority-count-badge" class="px-2 py-0.5 rounded-full text-[10px] bg-twitch-dark border border-twitch-border text-twitch-purple font-semibold">0 games</span>
                    </div>

                    <div class="space-y-3 text-xs">
                        <!-- Add Game Input with Autocomplete -->
                        <div class="flex space-x-2">
                            <input id="input-add-priority" list="known-games-datalist" type="text" placeholder="Enter game name to prioritize..." class="flex-1 px-3 py-2 bg-twitch-dark border border-twitch-border rounded-lg text-white text-xs focus:outline-none focus:border-twitch-purple transition">
                            <button onclick="addPriorityGame()" class="px-3.5 py-2 bg-twitch-purple hover:bg-twitch-accent text-white rounded-lg font-semibold transition active:scale-95 whitespace-nowrap">
                                + Add
                            </button>
                        </div>

                        <!-- Priority List Items -->
                        <div id="priority-list-container" class="space-y-1.5 max-h-60 overflow-y-auto pr-1">
                            <p class="text-gray-500 text-center py-4 text-[11px]">No priority games configured. Miner will farm automatically based on campaign urgency.</p>
                        </div>
                    </div>
                </div>

                <!-- Card 4: Excluded Games Management -->
                <div class="bg-twitch-card border border-twitch-border rounded-xl p-5 space-y-4 shadow">
                    <div class="flex items-center justify-between border-b border-twitch-border pb-3">
                        <h3 class="text-sm font-bold text-white flex items-center space-x-2">
                            <span>🚫</span>
                            <span>Excluded Games (Blacklist)</span>
                        </h3>
                        <span id="exclude-count-badge" class="px-2 py-0.5 rounded-full text-[10px] bg-twitch-dark border border-twitch-border text-red-400 font-semibold">0 games</span>
                    </div>

                    <div class="space-y-3 text-xs">
                        <!-- Add Game Input with Autocomplete -->
                        <div class="flex space-x-2">
                            <input id="input-add-exclude" list="known-games-datalist" type="text" placeholder="Enter game name to exclude..." class="flex-1 px-3 py-2 bg-twitch-dark border border-twitch-border rounded-lg text-white text-xs focus:outline-none focus:border-twitch-purple transition">
                            <button onclick="addExcludeGame()" class="px-3.5 py-2 bg-red-900/50 hover:bg-red-800/60 text-red-300 border border-red-700/60 rounded-lg font-semibold transition active:scale-95 whitespace-nowrap">
                                + Exclude
                            </button>
                        </div>

                        <!-- Exclude Chips Tag Cloud -->
                        <div id="exclude-tags-container" class="flex flex-wrap gap-2 max-h-60 overflow-y-auto pr-1 pt-1">
                            <p class="text-gray-500 text-center w-full py-4 text-[11px]">No excluded games. All active games are eligible for drops.</p>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Datalist for autocomplete -->
            <datalist id="known-games-datalist"></datalist>

            <!-- Bottom Sticky Save Bar -->
            <div class="sticky bottom-4 z-30 bg-twitch-card border border-twitch-border rounded-xl p-4 shadow-2xl flex items-center justify-between">
                <div class="flex items-center space-x-2 text-xs text-gray-400">
                    <span class="w-2.5 h-2.5 rounded-full bg-emerald-400"></span>
                    <span>Changes take effect immediately on running miner</span>
                </div>
                <div class="flex space-x-3">
                    <button onclick="loadSettings()" class="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg text-xs font-semibold transition active:scale-95">
                        Reset / Reload
                    </button>
                    <button onclick="saveSettings()" class="px-5 py-2 bg-twitch-purple hover:bg-twitch-accent text-white rounded-lg text-xs font-bold transition shadow-lg shadow-twitch-purple/25 active:scale-95 flex items-center space-x-1.5">
                        <span>💾</span>
                        <span>Save Settings</span>
                    </button>
                </div>
            </div>
        </div>

        <!-- ================= TAB 4: LIVE LOGS ================= -->
        <div id="tab-logs" class="tab-pane hidden space-y-4">
            <div class="bg-twitch-card border border-twitch-border rounded-xl p-4 flex flex-col sm:flex-row items-center justify-between gap-3 shadow">
                <!-- Filter Pills -->
                <div class="flex items-center space-x-1.5 w-full sm:w-auto">
                    <span class="text-xs font-semibold text-gray-400 mr-1">Filter:</span>
                    <button onclick="setLogLevelFilter('ALL')" id="logfilter-all" class="px-2.5 py-1 rounded text-xs font-semibold bg-twitch-purple text-white transition">ALL</button>
                    <button onclick="setLogLevelFilter('INFO')" id="logfilter-info" class="px-2.5 py-1 rounded text-xs font-semibold bg-twitch-dark text-gray-400 hover:text-white transition">INFO</button>
                    <button onclick="setLogLevelFilter('WARNING')" id="logfilter-warning" class="px-2.5 py-1 rounded text-xs font-semibold bg-twitch-dark text-amber-400 hover:text-amber-300 transition">WARN</button>
                    <button onclick="setLogLevelFilter('ERROR')" id="logfilter-error" class="px-2.5 py-1 rounded text-xs font-semibold bg-twitch-dark text-red-400 hover:text-red-300 transition">ERROR</button>
                </div>

                <!-- Log Controls -->
                <div class="flex items-center space-x-3 text-xs w-full sm:w-auto justify-between sm:justify-end">
                    <label class="flex items-center space-x-2 cursor-pointer text-gray-300 select-none">
                        <input id="logs-autoscroll" type="checkbox" checked class="rounded bg-twitch-dark border-twitch-border text-twitch-purple focus:ring-0">
                        <span>Auto-scroll</span>
                    </label>
                    <button onclick="copyLogs()" class="px-3 py-1 bg-twitch-dark hover:bg-gray-800 text-gray-300 border border-twitch-border rounded font-semibold transition active:scale-95">
                        📋 Copy
                    </button>
                    <button onclick="clearLogs()" class="px-3 py-1 bg-twitch-dark hover:bg-gray-800 text-red-400 border border-twitch-border rounded font-semibold transition active:scale-95">
                        🗑️ Clear
                    </button>
                </div>
            </div>

            <!-- Terminal Output -->
            <div class="bg-[#09090b] border border-twitch-border rounded-xl p-4 font-mono text-xs overflow-hidden shadow-2xl relative">
                <div id="logs-console" class="h-[60vh] overflow-y-auto space-y-1 pr-2">
                    <div class="text-gray-500 py-6 text-center">Connecting to live log stream...</div>
                </div>
            </div>
        </div>
    </main>

    <!-- Toast Notification Container -->
    <div id="toast-container" class="fixed bottom-5 right-5 z-50 space-y-2 pointer-events-none flex flex-col items-end"></div>

    <!-- Footer -->
    <footer class="text-center py-4 text-xs text-gray-500 border-t border-twitch-border/40 mt-auto">
        Twitch Drops Miner Headless &bull; Control Center &bull; Auto-refreshing
    </footer>

    <!-- Frontend Single Page Application JavaScript -->
    <script>
        let currentTab = 'overview';
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
                btn.className = 'tab-btn px-3 py-1.5 rounded-md text-xs font-semibold transition text-gray-400 hover:text-white flex items-center space-x-1.5';
            });

            const activePane = document.getElementById(`tab-${tab}`);
            if (activePane) activePane.classList.remove('hidden');

            const activeBtn = document.getElementById(`tab-btn-${tab}`);
            if (activeBtn) activeBtn.className = 'tab-btn px-3 py-1.5 rounded-md text-xs font-semibold transition text-white bg-twitch-purple shadow-sm flex items-center space-x-1.5';

            if (tab === 'campaigns') fetchCampaigns();
            if (tab === 'settings') loadSettings();
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
                if (!res.ok) return;
                const data = await res.json();
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
                stateBadge.innerText = data.state;
                if (data.state === 'IDLE') {
                    stateBadge.className = 'px-2.5 py-1 rounded-md text-xs font-semibold bg-gray-800 text-gray-300 border border-gray-700 whitespace-nowrap';
                } else if (data.state === 'EXIT') {
                    stateBadge.className = 'px-2.5 py-1 rounded-md text-xs font-semibold bg-red-900/40 text-red-400 border border-red-800 whitespace-nowrap';
                } else {
                    stateBadge.className = 'px-2.5 py-1 rounded-md text-xs font-semibold bg-green-900/40 text-green-300 border border-green-800 whitespace-nowrap';
                }
            }

            const netBadge = document.getElementById('net-badge');
            if (netBadge && data.network) {
                if (data.network.healthy) {
                    netBadge.innerText = 'Twitch API: OK';
                    netBadge.className = 'px-2.5 py-1 rounded-md text-xs font-semibold bg-emerald-950/60 text-emerald-400 border border-emerald-800/60 whitespace-nowrap';
                } else {
                    netBadge.innerText = `Twitch API: Retrying (${data.network.consecutive_failures})`;
                    netBadge.className = 'px-2.5 py-1 rounded-md text-xs font-semibold bg-amber-950/80 text-amber-300 border border-amber-700 whitespace-nowrap animate-pulse';
                }
            }

            if (data.system && data.system.memory_mb) {
                const memBadge = document.getElementById('mem-badge');
                if (memBadge) memBadge.innerText = `RAM: ${data.system.memory_mb}MB`;
            }

            if (data.metrics && data.metrics.uptime_hours !== undefined) {
                const uptimeText = document.getElementById('uptime-text');
                if (uptimeText) uptimeText.innerText = `Uptime: ${data.metrics.uptime_hours}h`;
                const statClaimed = document.getElementById('stat-claimed');
                if (statClaimed) statClaimed.innerText = data.metrics.claimed_drops || 0;
                const statWatch = document.getElementById('stat-watch-time');
                if (statWatch) statWatch.innerText = `${data.metrics.watch_time_hours || 0}h`;
                const statRate = document.getElementById('stat-rate');
                if (statRate) statRate.innerText = `${data.metrics.success_rate || 100}%`;
            }

            const statMode = document.getElementById('stat-mode');
            if (statMode && data.priority_mode) statMode.innerText = data.priority_mode;

            const banner2fa = document.getElementById('banner-2fa');
            const containerScreenshot = document.getElementById('container-2fa-screenshot');
            if (banner2fa && data.two_factor_pending) {
                banner2fa.classList.remove('hidden');
                if (containerScreenshot) containerScreenshot.classList.remove('hidden');
                const img2fa = document.getElementById('img-2fa');
                if (img2fa) img2fa.src = `/api/2fa/screenshot?t=${Date.now()}`;
            } else if (banner2fa) {
                banner2fa.classList.add('hidden');
                if (containerScreenshot) containerScreenshot.classList.add('hidden');
            }

            const channelName = document.getElementById('channel-name');
            const gameName = document.getElementById('game-name');
            const streamLink = document.getElementById('stream-link');
            const channelAvatar = document.getElementById('channel-avatar');

            if (data.channel) {
                if (channelName) channelName.innerText = data.channel.name;
                if (gameName) gameName.innerText = data.channel.game || 'Active Stream';
                if (streamLink) {
                    streamLink.href = data.channel.url;
                    streamLink.classList.remove('hidden');
                }
                if (channelAvatar) channelAvatar.innerText = data.channel.name.charAt(0).toUpperCase();
            } else {
                if (channelName) channelName.innerText = 'Scanning for streams...';
                if (gameName) gameName.innerText = 'Waiting for active campaign';
                if (streamLink) streamLink.classList.add('hidden');
            }

            const dropName = document.getElementById('drop-name');
            const dropPercent = document.getElementById('drop-percent');
            const progressBar = document.getElementById('progress-bar');
            const dropMinutes = document.getElementById('drop-minutes');
            const dropEta = document.getElementById('drop-eta');
            const campaignDropsCount = document.getElementById('campaign-drops-count');

            if (data.drop) {
                if (dropName) dropName.innerText = data.drop.name;
                if (dropPercent) dropPercent.innerText = `${data.drop.progress_percent}%`;
                if (progressBar) progressBar.style.width = `${data.drop.progress_percent}%`;
                if (dropMinutes) dropMinutes.innerText = `${data.drop.current_minutes} / ${data.drop.required_minutes} min`;

                const remainingMin = data.drop.required_minutes - data.drop.current_minutes;
                if (dropEta) dropEta.innerText = remainingMin > 0 ? `ETA: ~${remainingMin}m` : 'Ready to claim';
                if (campaignDropsCount) campaignDropsCount.innerText = `Drops: ${data.drop.campaign_claimed}/${data.drop.campaign_total}`;
            } else {
                if (dropName) dropName.innerText = 'No active drop';
                if (dropPercent) dropPercent.innerText = '0%';
                if (progressBar) progressBar.style.width = '0%';
                if (dropMinutes) dropMinutes.innerText = '0 / 0 min';
                if (dropEta) dropEta.innerText = 'ETA: --';
            }

            renderOverviewCampaigns(data.campaigns || []);
        }

        function renderOverviewCampaigns(campaigns) {
            const container = document.getElementById('overview-campaigns-list');
            if (!container) return;
            const countBadge = document.getElementById('nav-campaigns-count');
            if (countBadge) countBadge.innerText = campaigns.length;

            if (!campaigns.length) {
                container.innerHTML = '<div class="col-span-full py-6 text-center text-xs text-gray-500">No active campaigns currently detected.</div>';
                return;
            }

            container.innerHTML = campaigns.slice(0, 6).map(c => `
                <div class="bg-twitch-dark/60 border border-twitch-border/70 rounded-lg p-3 space-y-2 hover:border-twitch-purple/40 transition">
                    <div class="flex items-start justify-between">
                        <div class="truncate">
                            <h4 class="font-bold text-white text-xs truncate">${c.game}</h4>
                            <p class="text-[11px] text-gray-400 truncate">${c.name}</p>
                        </div>
                        <span class="px-2 py-0.5 rounded text-[10px] font-semibold ${c.is_priority ? 'bg-twitch-purple/20 text-twitch-purple border border-twitch-purple/40' : 'bg-gray-800 text-gray-400'}">
                            ${c.is_priority ? `#${c.priority_rank} Priority` : c.time_left}
                        </span>
                    </div>
                    <div class="space-y-1">
                        <div class="flex justify-between text-[10px] text-gray-400">
                            <span>Progress: ${c.claimed}/${c.total} drops</span>
                            <span class="text-twitch-purple font-semibold">${c.progress_percent}%</span>
                        </div>
                        <div class="w-full bg-gray-800 h-1.5 rounded-full overflow-hidden">
                            <div class="bg-twitch-purple h-full rounded-full" style="width: ${c.progress_percent}%"></div>
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
                renderCampaignsTab();
            } catch (err) {
                console.error("Failed to load campaigns", err);
            }
        }

        function setCampaignFilter(f) {
            campaignFilter = f;
            ['all', 'linked', 'priority', 'excluded'].forEach(id => {
                const btn = document.getElementById(`cfilter-${id}`);
                if (btn) {
                    btn.className = id === f ? 
                        'px-2.5 py-1 rounded font-medium bg-twitch-purple text-white' : 
                        'px-2.5 py-1 rounded font-medium text-gray-400 hover:text-white';
                }
            });
            renderCampaignsTab();
        }

        function renderCampaignsTab() {
            const container = document.getElementById('campaigns-tab-grid');
            if (!container) return;
            const search = (document.getElementById('campaigns-search')?.value || '').toLowerCase();

            const filtered = rawCampaigns.filter(c => {
                const matchesSearch = c.game.toLowerCase().includes(search) || c.name.toLowerCase().includes(search);
                if (!matchesSearch) return false;
                if (campaignFilter === 'linked') return c.is_linked;
                if (campaignFilter === 'priority') return c.is_priority;
                if (campaignFilter === 'excluded') return c.is_excluded;
                return true;
            });

            if (!filtered.length) {
                container.innerHTML = '<div class="col-span-full py-12 text-center text-gray-500 text-xs">No matching campaigns found.</div>';
                return;
            }

            container.innerHTML = filtered.map(c => `
                <div class="bg-twitch-card border ${c.is_priority ? 'border-twitch-purple/60 shadow-lg shadow-twitch-purple/5' : c.is_excluded ? 'border-red-900/40 opacity-75' : 'border-twitch-border'} rounded-xl p-4 space-y-3.5 flex flex-col justify-between">
                    <div class="space-y-2">
                        <div class="flex items-start justify-between gap-2">
                            <div>
                                <span class="text-[10px] font-bold uppercase tracking-wider text-twitch-purple">${c.time_left}</span>
                                <h4 class="text-base font-extrabold text-white leading-tight">${c.game}</h4>
                            </div>
                            <span class="px-2 py-0.5 rounded text-[10px] font-semibold ${c.is_linked ? 'bg-green-950/60 text-green-300 border border-green-800/60' : 'bg-amber-950/60 text-amber-300 border border-amber-800/60'} whitespace-nowrap">
                                ${c.is_linked ? '🔗 Linked' : '⚠️ Unlinked'}
                            </span>
                        </div>
                        <p class="text-xs text-gray-400 font-medium line-clamp-1">${c.name}</p>

                        <div class="space-y-1.5 pt-1">
                            <span class="text-[10px] text-gray-400 font-semibold uppercase tracking-wider">Rewards (${c.claimed_drops}/${c.total_drops}):</span>
                            <div class="space-y-1 max-h-36 overflow-y-auto pr-1">
                                ${c.drops.map(d => `
                                    <div class="bg-twitch-dark/70 border border-twitch-border/60 rounded px-2.5 py-1.5 text-xs flex items-center justify-between">
                                        <div class="flex items-center space-x-2 truncate">
                                            <span>${d.is_claimed ? '✅' : d.progress_percent > 0 ? '⏳' : '🎁'}</span>
                                            <span class="text-gray-200 truncate ${d.is_claimed ? 'line-through text-gray-500' : ''}">${d.name}</span>
                                        </div>
                                        <span class="text-[10px] font-mono text-gray-400 ml-2 whitespace-nowrap">${d.current_minutes}/${d.required_minutes}m</span>
                                    </div>
                                `).join('')}
                            </div>
                        </div>
                    </div>

                    <div class="pt-3 border-t border-twitch-border/60 flex items-center justify-between gap-2">
                        <button onclick="quickActionPriority('${c.game.replace(/'/g, "\'")}')" class="flex-1 py-1.5 rounded-lg text-xs font-semibold transition active:scale-95 border ${c.is_priority ? 'bg-twitch-purple text-white border-twitch-purple' : 'bg-twitch-purple/10 text-twitch-purple border-twitch-purple/30 hover:bg-twitch-purple/20'}">
                            ${c.is_priority ? `⭐ In Priority (#${c.priority_rank})` : '+ Prioritize'}
                        </button>
                        <button onclick="quickActionExclude('${c.game.replace(/'/g, "\'")}')" class="px-3 py-1.5 rounded-lg text-xs font-semibold transition active:scale-95 border ${c.is_excluded ? 'bg-red-900/50 text-red-300 border-red-700/60' : 'bg-gray-800 text-gray-400 border-gray-700 hover:text-white'}">
                            ${c.is_excluded ? '🚫 Excluded' : 'Exclude'}
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
                const data = await res.json();
                if (res.ok) {
                    showToast(`Promoted "${game}" to top of Priority Queue!`);
                    fetchCampaigns();
                } else {
                    showToast(data.error || 'Failed to update priority', 'error');
                }
            } catch (err) {
                showToast(err.message, 'error');
            }
        }

        async function quickActionExclude(game) {
            try {
                const res = await fetch('/api/settings/exclude', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'add', game: game })
                });
                const data = await res.json();
                if (res.ok) {
                    showToast(`Added "${game}" to Excluded Games!`, 'warning');
                    fetchCampaigns();
                } else {
                    showToast(data.error || 'Failed to exclude game', 'error');
                }
            } catch (err) {
                showToast(err.message, 'error');
            }
        }

        async function loadSettings() {
            try {
                const res = await fetch('/api/settings');
                if (!res.ok) return;
                const data = await res.json();
                rawSettings = data.settings || {};
                rawOptions = data.options || {};
                isSettingsDirty = false;
                updateSettingsUI();
            } catch (err) {
                console.error("Failed to load settings", err);
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

            const datalist = document.getElementById('known-games-datalist');
            if (datalist && rawOptions.known_games) {
                datalist.innerHTML = rawOptions.known_games.map(g => `<option value="${g}"></option>`).join('');
            }

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
        }

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

            container.innerHTML = list.map((game, idx) => `
                <div class="bg-twitch-dark/70 border border-twitch-border/70 rounded-lg px-3 py-2 flex items-center justify-between text-xs hover:border-twitch-purple/40 transition">
                    <div class="flex items-center space-x-2.5 truncate">
                        <span class="font-bold text-twitch-purple font-mono text-[11px]">#${idx + 1}</span>
                        <span class="font-medium text-white truncate">${game}</span>
                    </div>
                    <div class="flex items-center space-x-1 ml-2">
                        <button onclick="movePriorityGame('${game.replace(/'/g, "\'")}', 'move_up')" class="p-1 hover:text-twitch-purple text-gray-400 transition" title="Move Up">▲</button>
                        <button onclick="movePriorityGame('${game.replace(/'/g, "\'")}', 'move_down')" class="p-1 hover:text-twitch-purple text-gray-400 transition" title="Move Down">▼</button>
                        <button onclick="movePriorityGame('${game.replace(/'/g, "\'")}', 'top')" class="p-1 hover:text-yellow-400 text-gray-400 transition text-[10px]" title="Promote to Top">⭐</button>
                        <button onclick="removePriorityGame('${game.replace(/'/g, "\'")}')" class="p-1 hover:text-red-400 text-gray-400 transition ml-1" title="Remove">✕</button>
                    </div>
                </div>
            `).join('');
        }

        function renderExcludeSettingsList() {
            const container = document.getElementById('exclude-tags-container');
            const countBadge = document.getElementById('exclude-count-badge');
            if (!container) return;

            const list = rawSettings.exclude || [];
            if (countBadge) countBadge.innerText = `${list.length} games`;

            if (!list.length) {
                container.innerHTML = '<p class="text-gray-500 text-center w-full py-6 text-[11px]">No excluded games configured.</p>';
                return;
            }

            container.innerHTML = list.map(game => `
                <span class="bg-red-950/40 border border-red-800/60 text-red-300 px-2.5 py-1 rounded-full text-xs flex items-center space-x-1.5 shadow-sm">
                    <span class="truncate max-w-[180px]">${game}</span>
                    <button onclick="removeExcludeGame('${game.replace(/'/g, "\'")}')" class="hover:text-white transition font-bold text-red-400 ml-1">✕</button>
                </span>
            `).join('');
        }

        async function addPriorityGame() {
            const input = document.getElementById('input-add-priority');
            const game = (input?.value || '').trim();
            if (!game) return;

            try {
                const res = await fetch('/api/settings/priority', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'add', game: game })
                });
                const data = await res.json();
                if (res.ok) {
                    rawSettings.priority = data.priority;
                    input.value = '';
                    renderPrioritySettingsList();
                    showToast(`Added "${game}" to Priority Queue`);
                } else {
                    showToast(data.error || 'Failed to add priority game', 'error');
                }
            } catch (err) {
                showToast(err.message, 'error');
            }
        }

        async function movePriorityGame(game, action) {
            try {
                const res = await fetch('/api/settings/priority', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: action, game: game })
                });
                const data = await res.json();
                if (res.ok) {
                    rawSettings.priority = data.priority;
                    renderPrioritySettingsList();
                }
            } catch (err) {
                showToast(err.message, 'error');
            }
        }

        async function removePriorityGame(game) {
            try {
                const res = await fetch('/api/settings/priority', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'remove', game: game })
                });
                const data = await res.json();
                if (res.ok) {
                    rawSettings.priority = data.priority;
                    renderPrioritySettingsList();
                    showToast(`Removed "${game}" from Priority Queue`);
                }
            } catch (err) {
                showToast(err.message, 'error');
            }
        }

        async function addExcludeGame() {
            const input = document.getElementById('input-add-exclude');
            const game = (input?.value || '').trim();
            if (!game) return;

            try {
                const res = await fetch('/api/settings/exclude', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'add', game: game })
                });
                const data = await res.json();
                if (res.ok) {
                    rawSettings.exclude = data.exclude;
                    input.value = '';
                    renderExcludeSettingsList();
                    showToast(`Excluded "${game}" from drops`, 'warning');
                } else {
                    showToast(data.error || 'Failed to exclude game', 'error');
                }
            } catch (err) {
                showToast(err.message, 'error');
            }
        }

        async function removeExcludeGame(game) {
            try {
                const res = await fetch('/api/settings/exclude', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'remove', game: game })
                });
                const data = await res.json();
                if (res.ok) {
                    rawSettings.exclude = data.exclude;
                    renderExcludeSettingsList();
                    showToast(`Removed "${game}" from Exclude List`);
                }
            } catch (err) {
                showToast(err.message, 'error');
            }
        }

        async function testDiscordWebhook() {
            try {
                const res = await fetch('/api/notifications/test', { method: 'POST' });
                const data = await res.json();
                if (res.ok) {
                    showToast('Discord test notification dispatched successfully!');
                } else {
                    showToast(data.error || data.message || 'Discord test failed', 'error');
                }
            } catch (err) {
                showToast(err.message, 'error');
            }
        }

        async function saveSettings() {
            const payload = {
                priority_mode: parseInt(document.getElementById('setting-priority-mode')?.value || '3'),
                stale_stream_timeout_minutes: parseInt(document.getElementById('setting-stale-timeout')?.value || '5'),
                maintenance_interval_minutes: parseInt(document.getElementById('setting-maint-interval')?.value || '20'),
                connection_quality: parseInt(document.getElementById('setting-conn-quality')?.value || '1'),
                available_drops_check: document.getElementById('setting-available-drops-check')?.checked,
                enable_badges_emotes: document.getElementById('setting-badges-emotes')?.checked,
                discord_notifications_enabled: document.getElementById('setting-discord-enabled')?.checked,
                discord_summary_interval_minutes: parseInt(document.getElementById('setting-summary-interval')?.value || '60'),
            };

            const webhookInput = document.getElementById('setting-webhook-url')?.value?.trim();
            if (webhookInput !== undefined && !webhookInput.includes('*****')) {
                payload.discord_webhook_url = webhookInput;
            }

            try {
                const res = await fetch('/api/settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                if (res.ok) {
                    showToast('Settings saved and applied successfully!');
                    clearSettingsDirty();
                    loadSettings();
                } else {
                    showToast(data.error || 'Failed to save settings', 'error');
                }
            } catch (err) {
                showToast(err.message, 'error');
            }
        }

        async function fetchLogs() {
            if (currentTab !== 'logs') return;
            try {
                const url = logFilter === 'ALL' ? '/api/logs?limit=250' : `/api/logs?level=${logFilter}&limit=250`;
                const res = await fetch(url);
                if (!res.ok) return;
                const data = await res.json();
                renderLogs(data.logs || []);
            } catch (err) {
                console.error("Logs fetch error", err);
            }
        }

        function setLogLevelFilter(level) {
            logFilter = level;
            ['ALL', 'INFO', 'WARNING', 'ERROR'].forEach(lvl => {
                const btn = document.getElementById(`logfilter-${lvl.toLowerCase()}`);
                if (btn) {
                    btn.className = lvl === level ?
                        'px-2.5 py-1 rounded text-xs font-semibold bg-twitch-purple text-white transition' :
                        'px-2.5 py-1 rounded text-xs font-semibold bg-twitch-dark text-gray-400 hover:text-white transition';
                }
            });
            fetchLogs();
        }

        function renderLogs(logs) {
            const consoleEl = document.getElementById('logs-console');
            if (!consoleEl) return;

            if (!logs.length) {
                consoleEl.innerHTML = '<div class="text-gray-500 py-6 text-center">No logs recorded in buffer for current filter.</div>';
                return;
            }

            const shouldScroll = document.getElementById('logs-autoscroll')?.checked;

            consoleEl.innerHTML = logs.map(l => {
                let colorClass = 'text-gray-300';
                if (l.level === 'WARNING') colorClass = 'text-amber-400';
                else if (l.level === 'ERROR') colorClass = 'text-rose-400 font-bold';
                else if (l.level === 'DEBUG') colorClass = 'text-gray-500';

                return `<div class="leading-relaxed hover:bg-white/[0.02] px-1 rounded">
                    <span class="text-gray-500 font-mono select-none">[${l.time_short || '..:..:..'}]</span>
                    <span class="font-bold text-[11px] ${colorClass}">[${l.level}]</span>
                    <span class="${colorClass}">${escapeHtml(l.message)}</span>
                </div>`;
            }).join('');

            if (shouldScroll) {
                consoleEl.scrollTop = consoleEl.scrollHeight;
            }
        }

        function escapeHtml(str) {
            return (str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        }

        function copyLogs() {
            const consoleEl = document.getElementById('logs-console');
            if (!consoleEl) return;
            navigator.clipboard.writeText(consoleEl.innerText).then(() => {
                showToast('All visible logs copied to clipboard!');
            }).catch(err => {
                showToast('Failed to copy logs', 'error');
            });
        }

        async function clearLogs() {
            try {
                await fetch('/api/logs/clear', { method: 'POST' });
                showToast('Log buffer cleared');
                fetchLogs();
            } catch (err) {
                showToast(err.message, 'error');
            }
        }

        async function triggerAction(actionName) {
            try {
                const res = await fetch(`/api/action/${actionName}`, { method: 'POST' });
                const data = await res.json();
                if (data.status === 'ok') {
                    showToast(data.message || 'Action executed successfully!');
                    fetchStatus();
                } else {
                    showToast(data.message || 'Action failed', 'error');
                }
            } catch (err) {
                showToast(err.message, 'error');
            }
        }

        async function submit2FA() {
            const input = document.getElementById('input-2fa');
            const code = (input?.value || '').trim();
            if (!code) return;

            try {
                const res = await fetch('/api/action/submit-2fa', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ code: code })
                });
                const data = await res.json();
                if (data.status === 'ok') {
                    showToast('2FA code submitted successfully!');
                    input.value = '';
                    fetchStatus();
                } else {
                    showToast(data.error || 'Failed to submit 2FA', 'error');
                }
            } catch (err) {
                showToast(err.message, 'error');
            }
        }

        fetchStatus();
        setInterval(fetchStatus, 3000);
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

        # 1. Channel Info
        watching_channel = self.twitch.watching_channel.get_with_default(None)
        if watching_channel is not None:
            channel_data = {
                "name": watching_channel.name,
                "game": watching_channel.game.name if watching_channel.game else None,
                "url": str(watching_channel.url),
            }

        # 2. Active Campaign and Drop
        active_campaign = self.twitch.get_active_campaign(watching_channel)
        if active_campaign is not None and active_campaign.active_drop is not None:
            drop = active_campaign.active_drop
            drop_data = {
                "name": drop.name,
                "rewards": drop.rewards_text(),
                "current_minutes": drop.current_minutes,
                "required_minutes": drop.required_minutes,
                "progress_percent": int(drop.progress * 100) if drop.progress else 0,
                "campaign_name": active_campaign.name,
                "campaign_claimed": active_campaign.claimed_drops,
                "campaign_total": active_campaign.total_drops,
            }

        # 3. Campaigns List
        priority_list = self.twitch.settings.priority
        priority_set = set(priority_list)
        for camp in sorted(self.twitch.inventory, key=lambda c: (
            0 if c.game.name in priority_set else 1,
            priority_list.index(c.game.name) if c.game.name in priority_set else 0,
            c.ends_at,
        )):
            hours_left = (camp.ends_at - now).total_seconds() / 3600
            time_left_str = f"{hours_left:.1f}h left" if hours_left > 0 else "Ending"
            rank = (priority_list.index(camp.game.name) + 1) if camp.game.name in priority_set else None
            campaigns_data.append({
                "game": camp.game.name,
                "name": camp.name,
                "claimed": camp.claimed_drops,
                "total": camp.total_drops,
                "progress_percent": int(camp.progress * 100),
                "is_priority": camp.game.name in priority_set,
                "priority_rank": rank,
                "time_left": time_left_str,
            })

        # 4. Metrics
        metrics_stats = {}
        if metrics := getattr(self.twitch, "metrics", None):
            metrics_stats = metrics.get_stats()

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
                "healthy": getattr(self.twitch, "is_network_healthy", True),
                "consecutive_failures": getattr(self.twitch, "_consecutive_gql_failures", 0),
            },
            "discord": {
                "enabled": getattr(self.twitch.settings, "discord_notifications_enabled", True),
                "has_webhook": bool(self.twitch.settings.discord_webhook_url.strip()),
                "interval_minutes": self.twitch.settings.discord_summary_interval_minutes,
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

        # Extract known games from active campaigns in inventory
        known_games: set[str] = set()
        if hasattr(self.twitch, "inventory") and self.twitch.inventory:
            for c in self.twitch.inventory:
                if getattr(c, "game", None) and getattr(c.game, "name", None):
                    known_games.add(c.game.name)

        priority_list = list(getattr(s, "priority", []))
        exclude_raw = getattr(s, "exclude", set())
        exclude_list = sorted(list(exclude_raw)) if isinstance(exclude_raw, (set, list)) else []
        known_games.update(priority_list)
        known_games.update(exclude_list)

        priority_mode = s.priority_mode
        mode_name = priority_mode.name if hasattr(priority_mode, "name") else str(priority_mode)

        settings_dict = {
            "priority_mode": mode_name,
            "connection_quality": getattr(s, "connection_quality", 1),
            "stale_stream_timeout_minutes": getattr(s, "stale_stream_timeout_minutes", 5),
            "maintenance_interval_minutes": getattr(s, "maintenance_interval_minutes", 20),
            "discord_webhook_url": masked_webhook,
            "has_discord_webhook": bool(webhook),
            "discord_notifications_enabled": getattr(s, "discord_notifications_enabled", True),
            "discord_summary_interval_minutes": getattr(s, "discord_summary_interval_minutes", 60),
            "logging_level": getattr(s, "logging_level", "INFO"),
            "enable_badges_emotes": getattr(s, "enable_badges_emotes", False),
            "available_drops_check": getattr(s, "available_drops_check", False),
            "auto_cookie_refresh": getattr(s, "auto_cookie_refresh", False),
            "priority": priority_list,
            "exclude": exclude_list,
            "web_port": getattr(s, "web_port", 8080),
            "has_web_password": bool(self.password),
            "has_discord_bot_token": bool(getattr(s, "discord_bot_token", "")),
            "discord_owner_id": getattr(s, "discord_owner_id", ""),
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
            "known_games": sorted(list(known_games)),
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

        inventory = getattr(self.twitch, "inventory", [])
        for camp in sorted(inventory, key=lambda c: (
            0 if (getattr(c, "game", None) and getattr(c.game, "name", "") in priority_set) else 1,
            priority_list.index(c.game.name) if (getattr(c, "game", None) and getattr(c.game, "name", "") in priority_set) else 0,
            getattr(c, "ends_at", now),
        )):
            game_name = camp.game.name if getattr(camp, "game", None) else "Unknown"
            is_prio = game_name in priority_set
            is_excl = game_name in exclude_set
            rank = (priority_list.index(game_name) + 1) if is_prio else None

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
                progress_val = getattr(drop, "progress", 0.0) or 0.0
                drops_list.append({
                    "id": getattr(drop, "id", ""),
                    "name": getattr(drop, "name", "Unnamed Drop"),
                    "required_minutes": getattr(drop, "required_minutes", 0),
                    "current_minutes": getattr(drop, "current_minutes", 0),
                    "is_claimed": getattr(drop, "is_claimed", False),
                    "progress_percent": int(progress_val * 100),
                })

            campaigns_list.append({
                "id": getattr(camp, "id", ""),
                "game": game_name,
                "name": getattr(camp, "name", game_name),
                "status": getattr(camp, "status", "ACTIVE"),
                "is_linked": getattr(camp, "linked", False),
                "starts_at": camp.starts_at.isoformat() if getattr(camp, "starts_at", None) else None,
                "ends_at": camp.ends_at.isoformat() if getattr(camp, "ends_at", None) else None,
                "time_left": time_left_str,
                "is_priority": is_prio,
                "priority_rank": rank,
                "is_excluded": is_excl,
                "claimed_drops": getattr(camp, "claimed_drops", 0),
                "total_drops": getattr(camp, "total_drops", len(drops_list)),
                "progress_percent": int((getattr(camp, "progress", 0.0) or 0.0) * 100),
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
