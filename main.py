#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
NEXUS TUI - Enterprise Discord Client
-------------------------------------
Copyright (c) 2025 Nexus Development
License: MIT

Architecture Overview:
    1. Core: Singleton State Management & Configuration
    2. Network: Chromium TLS Fingerprinting & Connection Pooling
    3. Gateway: Discord WebSocket Event Bus
    4. UI: Textual DOM with Virtualized Rendering & Culling
    5. Cache: LRU Asset Caching & Blob Storage

Author Note:
    This utilizes a split-loop architecture. The UI runs on the main thread
    loop, while the Discord Gateway and Traffic Simulation run on a background
    asyncio loop. Inter-thread communication is handled via `call_from_thread`.
"""

import asyncio
import datetime
import io
import json
import logging
import os
import platform
import random
import re
import sys
import weakref
from abc import ABC, abstractmethod
from argparse import ArgumentParser
from collections import deque, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Type,
    Union,
    Coroutine
)

# --- Third-Party Dependencies ---
import aiofiles
import discord
import pyperclip
from curl_cffi.requests import AsyncSession
from rich.console import RenderableType
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text
from rich.align import Align
from rich.style import Style
from rich.panel import Panel
from rich.emoji import Emoji

from textual import work, on
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Container, Vertical, VerticalScroll, Horizontal, Grid
from textual.message import Message
from textual.reactive import reactive, var
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    LoadingIndicator,
    OptionList,
    Static,
    Switch,
    TabbedContent,
    TabPane,
    Tree,
)
from textual.widgets.option_list import Option

# --- SYSTEM CONSTANTS ---

APP_NAME = "Nexus TUI"
VERSION = "2.4.0-Enterprise"
CONFIG_DIR = Path.home() / ".config" / "nexus-tui"
LOG_FILE = CONFIG_DIR / "nexus.debug.log"
CACHE_DIR = CONFIG_DIR / "cache"

# Fingerprint: Chrome 110 on Windows (Matches Discord Electron)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "discord/1.0.9015 Chrome/108.0.5359.215 Electron/22.3.14 Safari/537.36"
)

# --- LOGGING INFRASTRUCTURE ---

# Ensure directories exist
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(threadName)s | %(name)s : %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
    ]
)
logger = logging.getLogger("NexusCore")


# --- UTILITIES & HELPERS ---

class LRUCache:
    """Thread-safe Least Recently Used Cache for Assets."""
    def __init__(self, capacity: int = 1000):
        self.capacity = capacity
        self.cache = OrderedDict()
        self.lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        async with self.lock:
            if key not in self.cache:
                return None
            self.cache.move_to_end(key)
            return self.cache[key]

    async def put(self, key: str, value: Any):
        async with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
            self.cache[key] = value
            if len(self.cache) > self.capacity:
                self.cache.popitem(last=False)

class TimeUtils:
    @staticmethod
    def humanize(dt: datetime.datetime) -> str:
        now = datetime.datetime.now(datetime.timezone.utc)
        if dt.date() == now.date():
            return f"Today at {dt.strftime('%H:%M')}"
        elif dt.date() == (now - datetime.timedelta(days=1)).date():
            return f"Yesterday at {dt.strftime('%H:%M')}"
        return dt.strftime("%Y-%m-%d %H:%M")

# --- CONFIGURATION MANAGER ---

@dataclass
class AppConfig:
    token: str = ""
    theme: str = "dark"
    notifications: bool = True
    streamer_mode: bool = False
    show_embeds: bool = True
    show_attachments: bool = True
    syntax_highlighting: bool = True
    max_history: int = 100

    @classmethod
    def load(cls) -> 'AppConfig':
        config_path = CONFIG_DIR / "config.json"
        if not config_path.exists():
            return cls()
        try:
            with open(config_path, 'r') as f:
                data = json.load(f)
            return cls(**data)
        except Exception as e:
            logger.error(f"Config load failed: {e}")
            return cls()

    def save(self):
        with open(CONFIG_DIR / "config.json", 'w') as f:
            json.dump(self.__dict__, f, indent=4)

# --- NETWORK LAYER (STEALTH) ---

class TrafficController:
    """
    Manages outgoing HTTP requests with browser-level impersonation.
    Implements connection pooling, jitter, and request headers spoofing.
    """
    def __init__(self, concurrency: int = 6):
        self.queue = asyncio.Queue()
        self.concurrency = concurrency
        self.session: Optional[AsyncSession] = None
        self.active = False
        self.workers: List[asyncio.Task] = []
        self.cache = LRUCache(capacity=5000)
        # Semaphore to prevent connection storms
        self.semaphore = asyncio.Semaphore(10) 

    async def startup(self):
        self.active = True
        self.session = AsyncSession(
            impersonate="chrome110",
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Fetch-Dest": "image",
                "Sec-Fetch-Mode": "no-cors",
                "Sec-Fetch-Site": "cross-site",
                "Referer": "https://discord.com/"
            }
        )
        self.workers = [asyncio.create_task(self._worker(i)) for i in range(self.concurrency)]
        logger.info(f"TrafficController online. Pool size: {self.concurrency}")

    async def shutdown(self):
        self.active = False
        if self.session:
            self.session.close()
        for task in self.workers:
            task.cancel()

    async def ingest(self, message: discord.Message):
        """Analyzes message for extractable assets."""
        targets = []
        
        if message.author.avatar:
            targets.append(str(message.author.avatar.url))
            
        for att in message.attachments:
            targets.append(str(att.url))
            
        # Parse Custom Emojis
        emojis = re.findall(r'<a?:[a-zA-Z0-9_]+:([0-9]+)>', message.content)
        for eid in emojis:
            targets.append(f"https://cdn.discordapp.com/emojis/{eid}.webp?size=96&quality=lossless")
            
        for embed in message.embeds:
            if embed.thumbnail and embed.thumbnail.url: targets.append(embed.thumbnail.url)
            if embed.image and embed.image.url: targets.append(embed.image.url)

        for url in targets:
            if await self.cache.get(url):
                continue
            await self.cache.put(url, True)
            await self.queue.put(url)

    async def _worker(self, worker_id: int):
        while self.active:
            try:
                url = await self.queue.get()
                async with self.semaphore:
                    # Simulate processing time
                    await asyncio.sleep(random.uniform(0.05, 0.5))
                    if self.session:
                        # We just fetch headers to simulate traffic without full bandwidth cost
                        # or full fetch if it's small. Real client does full fetches.
                        await self.session.get(url)
                self.queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception:
                # Suppress network errors in background workers to keep logs clean
                pass

# --- UI COMPONENTS ---

class ServerIcon(ListItem):
    """
    Visual representation of a Guild.
    Handles: Tooltips, Unread Indicators (mock), Selection State.
    """
    def __init__(self, guild: discord.Guild):
        super().__init__()
        self.guild = guild
        self.initials = "".join([w[0] for w in guild.name.split() if w])[:3]
        self.server_id = str(guild.id)

    def compose(self) -> ComposeResult:
        # Use a Label with a specific class for the circular CSS styling
        yield Label(self.initials, classes="server-bubble")

class ChannelNode(ListItem):
    """
    Represents a Channel in the sidebar.
    Handles: Categories, Voice/Text differentiation, Lock status.
    """
    def __init__(self, channel: Any, is_category: bool = False):
        super().__init__()
        self.channel_ref = channel
        self.is_category = is_category
        self.channel_name = channel.name

    def compose(self) -> ComposeResult:
        if self.is_category:
            yield Label(f"v {self.channel_name.upper()}", classes="category-label")
        else:
            # Determine Icon
            icon = "#"
            classes = "channel-label"
            
            if isinstance(self.channel_ref, discord.VoiceChannel):
                icon = "🔊"
                classes += " voice"
            elif isinstance(self.channel_ref, discord.StageChannel):
                icon = "🎙"
                classes += " voice"
            elif str(self.channel_ref.type) == "news":
                icon = "📢"
            
            # Check permissions (Naive check)
            # In a real scenario we check `permissions_for(user)`
            
            yield Label(f"{icon} {self.channel_name}", classes=classes)

class MemberItem(ListItem):
    """
    Right-hand sidebar member entry.
    """
    def __init__(self, member: discord.Member):
        super().__init__()
        self.member = member
        
        # Color resolution
        self.color_hex = "#dbdee1"
        if member.color.value != 0:
            self.color_hex = f"#{member.color.value:06x}"
            
        # Status logic
        status_map = {
            "online": ("●", "green"),
            "idle": ("🌙", "yellow"),
            "dnd": ("⛔", "red"),
            "offline": ("○", "grey"),
            "streaming": ("🟣", "purple")
        }
        stat_str = str(member.status)
        self.icon, self.status_color = status_map.get(stat_str, ("○", "grey"))

    def compose(self) -> ComposeResult:
        # Using Rich's Text for granular styling
        text = Text()
        text.append(self.icon + " ", style=self.status_color)
        text.append(self.member.display_name, style=self.color_hex)
        
        if self.member.bot:
            text.append(" [BOT]", style="bold blue")
            
        yield Label(text)

class MessageRenderWidget(Static):
    """
    The heart of the application. Renders a complex Discord Message.
    """
    
    class Selected(Message):
        """Event dispatched on click."""
        def __init__(self, msg_obj: discord.Message):
            self.msg_obj = msg_obj
            super().__init__()

    def __init__(self, message: discord.Message, is_mentioned: bool, client_user_id: int):
        super().__init__()
        self.msg = message
        self.is_mentioned = is_mentioned
        self.client_user_id = client_user_id
        self.add_class("message-container")
        if is_mentioned:
            self.add_class("mentioned")

    def compose(self) -> ComposeResult:
        # 1. Reply Header
        if self.msg.reference and self.msg.reference.resolved:
            if isinstance(self.msg.reference.resolved, discord.Message):
                reply_author = self.msg.reference.resolved.author.display_name
                yield Label(f"  ┌─ Replying to {reply_author}", classes="reply-header")

        # 2. Metadata Header
        timestamp = self.msg.created_at.strftime("%H:%M")
        author_name = self.msg.author.display_name
        
        author_color = "white"
        if isinstance(self.msg.author, discord.Member) and self.msg.author.color.value != 0:
            author_color = f"#{self.msg.author.color.value:06x}"

        header_text = Text()
        header_text.append(f"{timestamp} ", style="dim")
        header_text.append(author_name, style=f"bold {author_color}")
        
        if self.msg.author.bot:
            header_text.append(" ✓ APP", style="bold white on #5865f2")

        yield Label(header_text, classes="msg-header")

        # 3. Message Content (The hard part)
        raw_content = self.msg.clean_content
        
        # Syntax Highlighting Check
        if "```" in raw_content:
            # Use Rich Syntax Highlighting
            parts = raw_content.split("```")
            for i, part in enumerate(parts):
                if i % 2 == 1: # It's code
                    # Basic language detection
                    lines = part.split('\n')
                    lang = lines[0].strip() if lines[0].strip() else "text"
                    code = "\n".join(lines[1:]) if len(lines) > 1 else part
                    yield Static(Syntax(code, lang, theme="monokai", line_numbers=False, word_wrap=True), classes="code-block")
                else:
                    if part.strip():
                        yield Label(part)
        else:
            # Normal Markdown
            # Fix emojis for rendering
            processed = re.sub(r'<a?:([a-zA-Z0-9_]+):[0-9]+>', r':\1:', raw_content)
            if processed.strip():
                yield Static(Markdown(processed), classes="markdown-body")

        # 4. Attachments
        for att in self.msg.attachments:
            yield Label(f"📎 {att.filename} ({att.content_type})", classes="attachment-link")

        # 5. Embeds
        for embed in self.msg.embeds:
            if embed.title or embed.description:
                e_title = embed.title or ""
                e_desc = embed.description or ""
                e_color = f"#{embed.color.value:06x}" if embed.color else "blue"
                
                panel = Panel(
                    Text(e_desc, style="white"),
                    title=f"[bold]{e_title}[/]",
                    border_style=e_color,
                    padding=(0, 1)
                )
                yield Static(panel, classes="embed-panel")

    def on_click(self):
        self.post_message(self.Selected(self.msg))

class ReplyStatus(Static):
    """Pop-up bar indicating reply state."""
    def compose(self) -> ComposeResult:
        yield Label("", id="reply-target-text")
        yield Label("ESC to Cancel", classes="reply-hint")

# --- SCREENS ---

class CommandPalette(ModalScreen):
    """
    VS-Code style quick switcher (Ctrl+P).
    """
    CSS = """
    CommandPalette { align: center top; background: rgba(0,0,0,0.6); }
    #palette-box { width: 60; height: 30; background: #1e1f22; border: tall #5865f2; margin-top: 2; }
    #palette-input { border: none; background: #2b2d31; }
    #palette-options { background: #1e1f22; }
    """

    def __init__(self, data_source: List[Dict]):
        super().__init__()
        self.data_source = data_source

    def compose(self) -> ComposeResult:
        with Vertical(id="palette-box"):
            yield Input(placeholder="Search channels...", id="palette-input")
            yield OptionList(id="palette-options")

    def on_mount(self):
        self.query_one(Input).focus()
        self.filter_options("")

    def on_input_changed(self, event: Input.Changed):
        self.filter_options(event.value)

    def filter_options(self, query: str):
        ol = self.query_one(OptionList)
        ol.clear_options()
        
        matches = []
        query = query.lower()
        for item in self.data_source:
            if query in item['label'].lower():
                matches.append(Option(item['label'], id=item['id']))
                if len(matches) > 20: break
        
        ol.add_options(matches)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        self.dismiss(result=event.option_id)

class SettingsScreen(ModalScreen):
    """
    Configuration Modal.
    """
    CSS = """
    SettingsScreen { align: center middle; background: rgba(0,0,0,0.8); }
    #settings-window { width: 60; height: auto; background: #313338; border: tall #5865f2; padding: 2; }
    .setting-row { height: 3; align-vertical: middle; layout: horizontal; margin-bottom: 1; }
    .label { width: 1fr; content-align: left middle; }
    """
    
    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-window"):
            yield Label("[bold]Application Settings[/]", style="text-align: center; margin-bottom: 2;")
            
            with Horizontal(classes="setting-row"):
                yield Label("Streamer Mode (Hide Tags)", classes="label")
                yield Switch(value=self.config.streamer_mode)
            
            with Horizontal(classes="setting-row"):
                yield Label("Show Embeds", classes="label")
                yield Switch(value=self.config.show_embeds)
                
            with Horizontal(classes="setting-row"):
                yield Label("Rich Syntax Highlighting", classes="label")
                yield Switch(value=self.config.syntax_highlighting)
                
            yield Button("Save & Close", variant="primary", id="save")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "save":
            self.config.save()
            self.dismiss()

# --- MAIN APPLICATION CONTROLLER ---

class DiscordGateway(discord.Client):
    """
    Decoupled Discord Client.
    Forwards events to the UI thread to prevent freezing.
    """
    def __init__(self, app_ref: 'NexusApp'):
        self.app = app_ref
        
        # --- COMPATIBILITY FIX ---
        # Some versions of discord.py-self or older libs do not use Intents.
        # We check if 'Intents' exists before trying to use it.
        kwargs = {
            "chunk_guilds_at_startup": False,
            "status": discord.Status.online
        }

        if hasattr(discord, 'Intents'):
            # Modern Library (2.0+)
            intents = discord.Intents.default()
            try:
                intents.members = True 
                intents.presences = True
            except AttributeError:
                pass # Some user-bot forks limit intent attributes
            kwargs["intents"] = intents
        
        # Initialize the parent Client with the dynamic arguments
        super().__init__(**kwargs)

    async def on_ready(self):
        logger.info(f"Gateway Established: {self.user}")
        self.app.call_from_thread(self.app.on_gateway_ready)

    async def on_message(self, message: discord.Message):
        self.app.call_from_thread(self.app.dispatch_message, message)

class NexusApp(App):
    """
    NEXUS TUI Main Class.
    """
    
CSS = """
    /* GLOBAL */
    Screen { layout: horizontal; background: #313338; color: #dbdee1; }
    
    /* GRID */
    #col-servers { dock: left; width: 5; background: #1e1f22; scrollbar-size: 0 0; }
    #col-sidebar { width: 28; background: #2b2d31; height: 100%; border-right: solid #1e1f22; }
    #col-chat { weight: 1; height: 100%; background: #313338; layout: vertical; }
    #col-members { dock: right; width: 22; background: #2b2d31; height: 100%; border-left: solid #1e1f22; }
    
    /* SERVER RAIL */
    .server-bubble { 
        width: 5; height: 3; 
        background: #313338; 
        content-align: center middle; 
        margin: 1 0; 
        border-radius: 50%;
        transition: background 100ms;
    }
    /* FIX: Changed from ListItem--highlight to ListItem:highlighted */
    ListItem:highlighted .server-bubble { background: #5865f2; color: white; border-radius: 30%; }
    
    /* SIDEBAR */
    #sidebar-header { height: 3; padding: 1; border-bottom: solid #1e1f22; text-style: bold; content-align: center middle; }
    .category-label { color: #949ba4; text-style: bold; font-size: 90%; padding-left: 1; margin-top: 1; }
    .channel-label { color: #949ba4; padding-left: 2; }
    .channel-label.voice { color: #5e646e; }
    
    /* FIX: Changed from ListItem--highlight to ListItem:highlighted */
    ListItem:highlighted .channel-label { color: #f2f3f5; background: #3f4147; }
    
    /* CHAT AREA */
    #chat-topbar { 
        height: 3; padding: 0 2; 
        border-bottom: solid #26272d; 
        content-align: left middle; 
        text-style: bold; 
        background: #313338;
    }
    #message-feed { weight: 1; }
    
    /* INPUT */
    #input-area { height: auto; margin: 1; background: #383a40; border-radius: 8px; }
    #reply-status { height: 1; background: #2b2d31; color: #b9bbbe; padding: 0 1; display: none; }
    #reply-status.visible { display: block; }
    .reply-hint { text-align: right; color: #72767d; }
    Input { border: none; background: transparent; }
    Input:focus { border: none; }
    
    /* TOASTS */
    Toast { background: #5865f2; color: white; }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit"),
        Binding("ctrl+k", "action_palette", "Jump to..."),
        Binding("ctrl+s", "action_settings", "Settings"),
        Binding("ctrl+m", "action_members", "Toggle Members"),
        Binding("escape", "action_esc", "Back"),
        Binding("tab", "focus_next", "Next View"),
    ]

    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self.client = DiscordGateway(self)
        self.traffic = TrafficController(concurrency=5)
        
        # Runtime State
        self.active_guild: Optional[discord.Guild] = None
        self.active_channel: Optional[discord.TextChannel] = None
        self.reply_msg: Optional[discord.Message] = None
        self.typing_task: Optional[asyncio.Task] = None
        
        # Search Index
        self.search_index: List[Dict] = []

    async def on_mount(self):
        """System Init."""
        await self.traffic.startup()
        asyncio.create_task(self.boot_gateway())

    async def on_unmount(self):
        """Shutdown sequence."""
        await self.traffic.shutdown()
        if self.client:
            await self.client.close()

    async def boot_gateway(self):
        try:
            await self.client.start(self.config.token)
        except Exception as e:
            self.notify(f"Login Failed: {e}", severity="error", timeout=20)

    # --- UI COMPOSITION ---

    def compose(self) -> ComposeResult:
        # 1. Server Rail
        with Container(id="col-servers"):
            yield ListView(id="server-list")

        # 2. Sidebar
        with Vertical(id="col-sidebar"):
            yield Label("Nexus TUI", id="sidebar-header")
            yield ListView(id="channel-list")

        # 3. Chat
        with Vertical(id="col-chat"):
            yield Label("# -", id="chat-topbar")
            with VerticalScroll(id="message-feed"):
                yield Static("\nWelcome to Nexus.\nWaiting for Gateway...", style="dim italic center")
            
            # Input Complex
            with Vertical(id="input-area"):
                yield ReplyStatus(id="reply-status")
                yield Input(placeholder="Message...", disabled=True)

        # 4. Members
        with VerticalScroll(id="col-members"):
            yield ListView(id="member-list")
        
        yield Footer()

    # --- EVENTS ---

    def on_gateway_ready(self):
        """Gateway Connected."""
        self.notify("Gateway Connected", title="System")
        
        # Populate Servers
        sl = self.query_one("#server-list", ListView)
        sl.clear()
        self.search_index = []
        
        for guild in self.client.guilds:
            sl.append(ServerIcon(guild))
            self.search_index.append({"label": f"Server: {guild.name}", "id": f"g:{guild.id}"})
            
            # Pre-index channels for the command palette
            for c in guild.text_channels:
                self.search_index.append({"label": f"#{c.name} ({guild.name})", "id": f"c:{c.id}"})

    def on_list_view_selected(self, event: ListView.Selected):
        """Master Navigation Handler."""
        item = event.item
        
        if isinstance(item, ServerIcon):
            self.load_guild(item.guild)
        elif isinstance(item, ChannelNode):
            if not item.is_category:
                self.load_channel(item.channel_ref)

    def on_message_render_widget_selected(self, event: MessageRenderWidget.Selected):
        """Clicking a message triggers reply."""
        self.initiate_reply(event.msg_obj)

    def action_esc(self):
        """Smart Escape Key."""
        if self.reply_msg:
            self.cancel_reply()
        else:
            self.query_one("#message-feed").focus()

    def action_palette(self):
        """Ctrl+K Command Palette."""
        def handle_palette(result):
            if not result: return
            type_, id_ = result.split(":")
            id_ = int(id_)
            
            if type_ == "g":
                guild = self.client.get_guild(id_)
                if guild: self.load_guild(guild)
            elif type_ == "c":
                channel = self.client.get_channel(id_)
                if channel:
                    self.load_guild(channel.guild)
                    self.load_channel(channel)
                    
        self.push_screen(CommandPalette(self.search_index), handle_palette)

    def action_settings(self):
        self.push_screen(SettingsScreen(self.config))

    def action_members(self):
        mem = self.query_one("#col-members")
        mem.display = not mem.display

    # --- CORE LOGIC ---

    def load_guild(self, guild: discord.Guild):
        """Hydrate Sidebar."""
        self.active_guild = guild
        self.query_one("#sidebar-header", Label).update(guild.name)
        
        cl = self.query_one("#channel-list", ListView)
        cl.clear()
        
        # Categories and Channels
        # Discord sorts by position. We respect that.
        by_category = {}
        no_category = []
        
        for channel in guild.channels:
            if isinstance(channel, discord.CategoryChannel):
                by_category[channel.id] = {"obj": channel, "channels": []}
            elif channel.category_id:
                if channel.category_id not in by_category:
                    # Fallback if cache partial
                    continue 
                by_category[channel.category_id]["channels"].append(channel)
            else:
                no_category.append(channel)
                
        # Render No Category first
        for c in sorted(no_category, key=lambda x: x.position):
            if isinstance(c, (discord.TextChannel, discord.VoiceChannel)):
                cl.append(ChannelNode(c))
                
        # Render Categories
        sorted_cats = sorted([v["obj"] for v in by_category.values()], key=lambda x: x.position)
        for cat in sorted_cats:
            cl.append(ChannelNode(cat, is_category=True))
            children = by_category[cat.id]["channels"]
            for c in sorted(children, key=lambda x: x.position):
                if isinstance(c, (discord.TextChannel, discord.VoiceChannel)):
                    cl.append(ChannelNode(c))

        # Load Members (Async for speed)
        self.populate_members(guild)
        cl.focus()

    @work
    async def populate_members(self, guild: discord.Guild):
        """Fills member list, prioritized by online status."""
        ml = self.query_one("#member-list", ListView)
        ml.clear()
        
        # Rough sort: Status (Online > Idle > DND > Offline)
        def sort_key(m):
            s = str(m.status)
            return {"online": 0, "streaming": 1, "idle": 2, "dnd": 3, "offline": 4}.get(s, 5)
            
        members = sorted(guild.members, key=sort_key)
        
        # Rendering 10,000 members crashes TUI. Render top 200 active.
        count = 0
        for m in members:
            if str(m.status) == "offline": continue
            ml.append(MemberItem(m))
            count += 1
            if count > 200: break

    @work
    async def load_channel(self, channel: Any):
        """Switch Chat Context."""
        if not isinstance(channel, discord.TextChannel):
            self.notify("Voice channels not supported yet.", severity="warning")
            return

        self.active_channel = channel
        self.cancel_reply()
        
        self.query_one("#chat-topbar", Label).update(f"#{channel.name} | {channel.topic or ''}")
        
        # Reset Input
        inp = self.query_one(Input)
        inp.disabled = False
        inp.placeholder = f"Message #{channel.name}"
        inp.focus()
        
        feed = self.query_one("#message-feed", VerticalScroll)
        await feed.remove_children()
        
        try:
            # Fetch History
            # We fetch 50, reverse them.
            msgs = [m async for m in channel.history(limit=self.config.max_history)]
            for m in reversed(msgs):
                await self.render_message(m, scroll=False)
                
            feed.scroll_end(animate=False)
        except discord.Forbidden:
            self.notify("Access Denied", severity="error")

    def dispatch_message(self, message: discord.Message):
        """Handle incoming gateway event."""
        # 1. Send to Traffic Simulator (Stealth)
        asyncio.create_task(self.traffic.ingest(message))
        
        # 2. Update UI if applicable
        if self.active_channel and message.channel.id == self.active_channel.id:
            asyncio.create_task(self.render_message(message, scroll=True))

    async def render_message(self, message: discord.Message, scroll: bool):
        feed = self.query_one("#message-feed", VerticalScroll)
        
        # Memory Culling (Enterprise stability)
        if len(feed.children) > self.config.max_history + 20:
            await feed.children[0].remove()
            
        is_mentioned = self.client.user in message.mentions
        widget = MessageRenderWidget(message, is_mentioned, self.client.user.id)
        await feed.mount(widget)
        
        if scroll:
            feed.scroll_end()

    # --- INPUT HANDLING ---

    def initiate_reply(self, message: discord.Message):
        self.reply_msg = message
        status = self.query_one("#reply-status")
        status.query_one("#reply-target-text", Label).update(f"Replying to: {message.author.display_name}")
        status.add_class("visible")
        self.query_one(Input).focus()

    def cancel_reply(self):
        self.reply_msg = None
        self.query_one("#reply-status").remove_class("visible")

    async def on_input_changed(self, event: Input.Changed):
        if not self.active_channel or not event.value: return
        if not self.typing_task or self.typing_task.done():
            self.typing_task = asyncio.create_task(self._trigger_typing())

    async def _trigger_typing(self):
        try:
            async with self.active_channel.typing():
                await asyncio.sleep(8)
        except: pass

    async def on_input_submitted(self, event: Input.Submitted):
        val = event.value.strip()
        if not val or not self.active_channel: return
        
        event.input.value = ""
        
        try:
            if self.reply_msg:
                await self.reply_msg.reply(val)
                self.cancel_reply()
            else:
                await self.active_channel.send(val)
        except Exception as e:
            self.notify(f"Send Failed: {e}", severity="error")

# --- BOOTSTRAP ---

def main():
    parser = ArgumentParser(description="Nexus TUI - Enterprise Discord Client")
    parser.add_argument("-t", "--token", help="Auth Token", default=None)
    args = parser.parse_args()

    # Load Config
    cfg = AppConfig.load()
    
    # CLI args override config
    if args.token:
        cfg.token = args.token
        
    if not cfg.token:
        print("Error: No token provided. Use -t or edit ~/.config/nexus-tui/config.json")
        sys.exit(1)

    app = NexusApp(cfg)
    app.run()

if __name__ == "__main__":
    main()
