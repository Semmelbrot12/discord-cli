#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
NEXUS TUI - Enterprise Discord Client
Forked from https://github.com/fourjr/discord-cli
-------------------------------------
Version: 3.0.0-Persistent
"""

import asyncio
import json
import logging
import random
import re
import sys
import shutil
import tempfile
import os
from argparse import ArgumentParser
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

# --- Third-Party Dependencies ---
import discord
from curl_cffi.requests import AsyncSession
import aiohttp # Used for direct image fetching for Chafa
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text
from rich.panel import Panel

from textual import work, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical, VerticalScroll, Horizontal
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Footer,
    Input,
    Label,
    ListItem,
    ListView,
    OptionList,
    Static,
    Switch,
    TabbedContent,
    TabPane,
    TextArea
)
from textual.widgets.option_list import Option

# --- SYSTEM CONSTANTS ---

APP_NAME = "Nexus TUI"
CONFIG_DIR = Path.home() / ".config" / "nexus-tui"
LOG_FILE = CONFIG_DIR / "nexus.debug.log"
CACHE_DIR = CONFIG_DIR / "cache"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "discord/1.0.9015 Chrome/108.0.5359.215 Electron/22.3.14 Safari/537.36"
)

# --- LOGGING ---

CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s : %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8')]
)
logger = logging.getLogger("NexusCore")

# --- UTILITIES ---

class LRUCache:
    def __init__(self, capacity: int = 1000):
        self.capacity = capacity
        self.cache = OrderedDict()
        self.lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        async with self.lock:
            if key not in self.cache: return None
            self.cache.move_to_end(key)
            return self.cache[key]

    async def put(self, key: str, value: Any):
        async with self.lock:
            if key in self.cache: self.cache.move_to_end(key)
            self.cache[key] = value
            if len(self.cache) > self.capacity: self.cache.popitem(last=False)

async def render_image_ansi(url: str, width: int = 60) -> str:
    """Downloads an image and converts it to ANSI art using Chafa."""
    if not shutil.which("chafa"):
        return "[Image: 'chafa' not installed]"
    
    try:
        # We use aiohttp here for quick fetching
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200: return "[Image Error]"
                data = await resp.read()
        
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        
        # -f symbols: use block characters, -c full: full color
        proc = await asyncio.create_subprocess_exec(
            "chafa", "-f", "symbols", "-c", "full", f"--size={width}x20", tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        os.unlink(tmp_path)
        return "\n" + stdout.decode("utf-8")
    except Exception:
        return "[Image Render Failed]"

@dataclass
class AppConfig:
    token: str = ""
    theme: str = "dark"
    streamer_mode: bool = False
    show_embeds: bool = True
    show_attachments: bool = True
    max_history: int = 100
    auto_login: bool = False

    @classmethod
    def load(cls) -> 'AppConfig':
        config_path = CONFIG_DIR / "config.json"
        if not config_path.exists(): return cls()
        try:
            with open(config_path, 'r') as f: return cls(**json.load(f))
        except Exception: return cls()

    def save(self):
        with open(CONFIG_DIR / "config.json", 'w') as f:
            json.dump(self.__dict__, f, indent=4)

# --- NETWORK LAYER (Original TrafficController) ---

class TrafficController:
    def __init__(self, concurrency: int = 6):
        self.queue = asyncio.Queue()
        self.concurrency = concurrency
        self.session: Optional[AsyncSession] = None
        self.active = False
        self.workers: List[asyncio.Task] = []
        self.cache = LRUCache(capacity=5000)
        self.semaphore = asyncio.Semaphore(10) 

    async def startup(self):
        self.active = True
        self.session = AsyncSession(
            impersonate="chrome110",
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Referer": "https://discord.com/"
            }
        )
        self.workers = [asyncio.create_task(self._worker(i)) for i in range(self.concurrency)]

    async def shutdown(self):
        self.active = False
        if self.session: self.session.close()
        for task in self.workers: task.cancel()

    async def ingest(self, message: discord.Message):
        """Pre-fetches content to warm up cache."""
        targets = []
        if message.author.avatar: targets.append(str(message.author.avatar.url))
        for att in message.attachments: targets.append(str(att.url))
        emojis = re.findall(r'<a?:[a-zA-Z0-9_]+:([0-9]+)>', message.content)
        for eid in emojis:
            targets.append(f"https://cdn.discordapp.com/emojis/{eid}.webp?size=96&quality=lossless")
        for url in targets:
            if await self.cache.get(url): continue
            await self.cache.put(url, True)
            await self.queue.put(url)

    async def _worker(self, worker_id: int):
        while self.active:
            try:
                url = await self.queue.get()
                async with self.semaphore:
                    await asyncio.sleep(random.uniform(0.05, 0.5))
                    if self.session: await self.session.get(url)
                self.queue.task_done()
            except asyncio.CancelledError: break
            except Exception: pass

# --- UI COMPONENTS ---

class ServerIcon(ListItem):
    def __init__(self, guild: discord.Guild):
        super().__init__()
        self.guild = guild
        clean_name = "".join([w[0] for w in guild.name.split() if w])
        self.initials = clean_name[:6] if len(clean_name) > 0 else guild.name[:6]

    def compose(self) -> ComposeResult:
        yield Label(self.initials, classes="server-bubble")

class DMItem(ListItem):
    """Sidebar item for Direct Messages."""
    def __init__(self, channel: Any):
        super().__init__()
        self.channel = channel
        self.is_group = isinstance(channel, discord.GroupChannel)
        
        if self.is_group:
            self.label = channel.name or "Group Chat"
            self.icon = "👥"
        else:
            self.label = channel.recipient.display_name
            self.icon = "👤"

    def compose(self) -> ComposeResult:
        yield Label(f"{self.icon} {self.label}", classes="channel-label")

class ChannelNode(ListItem):
    def __init__(self, channel: Any, is_category: bool = False):
        super().__init__()
        self.channel_ref = channel
        self.is_category = is_category
        self.channel_name = channel.name

    def compose(self) -> ComposeResult:
        if self.is_category:
            yield Label(f"v {self.channel_name.upper()}", classes="category-label")
        else:
            icon = "🔊" if isinstance(self.channel_ref, discord.VoiceChannel) else "#"
            classes = "channel-label"
            if isinstance(self.channel_ref, discord.VoiceChannel): classes += " voice"
            yield Label(f"{icon} {self.channel_name}", classes=classes)

class MemberItem(ListItem):
    def __init__(self, member: discord.Member):
        super().__init__()
        self.member = member
        self.color_hex = "#dbdee1"
        if member.color.value != 0:
            self.color_hex = f"#{member.color.value:06x}"
        status_map = {"online": ("●", "green"), "idle": ("🌙", "yellow"), "dnd": ("⛔", "red"), "offline": ("○", "grey")}
        self.icon, self.status_color = status_map.get(str(member.status), ("○", "grey"))

    def compose(self) -> ComposeResult:
        text = Text()
        text.append(self.icon + " ", style=self.status_color)
        text.append(self.member.display_name, style=self.color_hex)
        if self.member.bot: text.append(" [BOT]", style="bold blue")
        yield Label(text)

class MessageRenderWidget(Static):
    """Renders a single message, including text, code blocks, embeds, and images."""
    class Selected(Message):
        def __init__(self, msg_obj: discord.Message):
            self.msg_obj = msg_obj
            super().__init__()

    def __init__(self, message: discord.Message, is_mentioned: bool, client_user_id: int):
        super().__init__()
        self.msg = message
        self.is_mentioned = is_mentioned
        self.add_class("message-container")
        if is_mentioned: self.add_class("mentioned")

    async def on_mount(self):
        # Header
        if self.msg.reference and self.msg.reference.resolved and isinstance(self.msg.reference.resolved, discord.Message):
            reply_author = self.msg.reference.resolved.author.display_name
            self.mount(Label(f"  ┌─ Replying to {reply_author}", classes="reply-header"))

        timestamp = self.msg.created_at.strftime("%H:%M")
        author_color = "white"
        if isinstance(self.msg.author, discord.Member) and self.msg.author.color.value != 0:
            author_color = f"#{self.msg.author.color.value:06x}"

        header_text = Text()
        header_text.append(f"{timestamp} ", style="dim")
        header_text.append(self.msg.author.display_name, style=f"bold {author_color}")
        self.mount(Label(header_text, classes="msg-header"))

        # Content
        raw_content = self.msg.clean_content
        if "```" in raw_content:
            parts = raw_content.split("```")
            for i, part in enumerate(parts):
                if i % 2 == 1: 
                    lines = part.split('\n')
                    lang = lines[0].strip() if lines[0].strip() else "text"
                    code = "\n".join(lines[1:]) if len(lines) > 1 else part
                    self.mount(Static(Syntax(code, lang, theme="monokai", line_numbers=False, word_wrap=True), classes="code-block"))
                elif part.strip():
                    self.mount(Label(part))
        else:
            processed = re.sub(r'<a?:([a-zA-Z0-9_]+):[0-9]+>', r':\1:', raw_content)
            if processed.strip():
                self.mount(Static(Markdown(processed), classes="markdown-body"))

        # Attachments / Images
        for att in self.msg.attachments:
            if att.content_type and att.content_type.startswith("image/"):
                # Async render image
                ansi = await render_image_ansi(att.url)
                self.mount(Label(Text.from_ansi(ansi)))
            else:
                self.mount(Label(f"📎 {att.filename} ({att.content_type})", classes="attachment-link"))

        # Embeds
        for embed in self.msg.embeds:
            if embed.title or embed.description:
                panel = Panel(Text(embed.description or "", style="white"), title=f"[bold]{embed.title or ''}[/]", border_style="blue", padding=(0, 1))
                self.mount(Static(panel, classes="embed-panel"))

    def on_click(self):
        self.post_message(self.Selected(self.msg))

# --- INTERACTIVE BARS ---

class TopActionBar(Static):
    """Top bar for chat area with Title and Action Buttons."""
    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Label("# -", id="chat-title")
            yield Label(" ", classes="spacer")
            yield Button("🔍 Search", id="btn-search", classes="icon-btn")
            yield Button("👥 Members", id="btn-members", classes="icon-btn")

class UserControlPanel(Static):
    """Bottom left panel for User Info and Settings."""
    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="user-info-text"):
                yield Label("Connecting...", id="uc-username")
                yield Label("Online", id="uc-status")
            yield Button("⚙️", id="btn-settings", classes="settings-btn")

class ReplyStatus(Static):
    def compose(self) -> ComposeResult:
        yield Label("", id="reply-target-text")
        yield Label("ESC to Cancel", classes="reply-hint")

# --- SCREENS ---

class LoginScreen(Screen):
    CSS = """
    LoginScreen { align: center middle; background: #2b2d31; }
    #login-box { width: 60; height: auto; background: #313338; border: solid #5865f2; padding: 2; }
    Input { margin-bottom: 1; }
    Button { width: 100%; margin-top: 1; }
    """
    def compose(self) -> ComposeResult:
        with Vertical(id="login-box"):
            yield Label("[bold]Nexus TUI Login[/]", style="text-align: center")
            yield Label("Enter User Token:", classes="label")
            yield Input(placeholder="Token...", password=True, id="token-input")
            yield Horizontal(Label("Remember me?  "), Switch(value=True, id="save-switch"))
            yield Button("Login", variant="primary", id="btn-login")

    @on(Button.Pressed, "#btn-login")
    def action_login(self):
        token = self.query_one("#token-input").value
        save = self.query_one("#save-switch").value
        if len(token) > 10:
            self.dismiss((token, save))

class MessageActionModal(ModalScreen):
    """Context Menu for Messages (Edit/Delete)."""
    CSS = """
    MessageActionModal { align: center middle; background: rgba(0,0,0,0.7); }
    #action-list { width: 40; height: auto; background: #1e1f22; border: white; }
    """
    def __init__(self, is_author: bool):
        super().__init__()
        self.is_author = is_author

    def compose(self) -> ComposeResult:
        options = [Option("Reply", id="reply"), Option("Copy ID", id="copy")]
        if self.is_author:
            options.append(Option("Edit", id="edit"))
            options.append(Option("Delete", id="delete"))
        
        with Vertical(id="action-list"):
            yield Label("Message Actions", style="bold center")
            yield OptionList(*options, id="opt-list")

    @on(OptionList.OptionSelected)
    def on_select(self, event: OptionList.OptionSelected):
        self.dismiss(event.option.id)

class EditInputModal(ModalScreen):
    """Modal for editing messages."""
    CSS = """
    EditInputModal { align: center middle; background: rgba(0,0,0,0.8); }
    #edit-box { width: 80%; height: 60%; background: #313338; border: blue; }
    TextArea { height: 1fr; background: #1e1f22; }
    """
    def __init__(self, original_text: str):
        super().__init__()
        self.original_text = original_text

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-box"):
            yield Label("Edit Message", style="bold")
            yield TextArea(self.original_text, id="modal-text")
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("Save", variant="primary", id="save")

    @on(Button.Pressed)
    def on_btn(self, event: Button.Pressed):
        if event.button.id == "save":
            self.dismiss(self.query_one(TextArea).text)
        else:
            self.dismiss(None)

class CommandPalette(ModalScreen):
    CSS = """
    CommandPalette { align: center top; background: rgba(0,0,0,0.6); }
    #palette-box { width: 60; height: 30; background: #1e1f22; margin-top: 2; }
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
    CSS = """
    SettingsScreen { align: center middle; background: rgba(0,0,0,0.8); }
    #settings-window { width: 60; height: auto; background: #313338; padding: 2; }
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
            yield Button("Save & Close", variant="primary", id="save")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "save":
            self.config.save()
            self.dismiss()

# --- CORE ---

class DiscordGateway(discord.Client):
    def __init__(self, app_ref: 'NexusApp'):
        self.app = app_ref
        kwargs = {"chunk_guilds_at_startup": False, "status": discord.Status.online}
        # Discord.py-self usually doesn't need intents manually set like standard d.py
        # but we keep it for compatibility with the original script structure
        if hasattr(discord, 'Intents'):
            intents = discord.Intents.default()
            try:
                intents.members = True 
                intents.presences = True
            except AttributeError: pass
            kwargs["intents"] = intents
        super().__init__(**kwargs)

    async def on_connect(self):
        self.app.call_later(self.app.notify, "Handshaking...", title="Network")

    async def on_ready(self):
        self.app.call_later(self.app.on_gateway_ready)

    async def on_message(self, message: discord.Message):
        self.app.call_later(self.app.dispatch_message, message)

class NexusApp(App):
    CSS = """
    /* GLOBAL */
    Screen { layout: horizontal; background: #313338; color: #dbdee1; }
    
    /* LAYOUT GRID */
    #col-sidebar { width: 32; background: #2b2d31; height: 100%; border-right: solid #1e1f22; }
    #col-chat { width: 1fr; height: 100%; background: #313338; layout: vertical; }
    #col-members { dock: right; width: 22; background: #2b2d31; height: 100%; border-left: solid #1e1f22; }
    
    /* SERVER/DM TABS */
    TabbedContent { height: 1fr; }
    TabPane { padding: 0; }
    
    /* SERVER RAIL */
    .server-bubble { 
        width: 100%; height: 3; 
        background: #313338; 
        content-align: center middle; 
        margin: 0; border: solid #1e1f22;
    }
    ListItem.--highlight .server-bubble { background: #5865f2; color: white; }
    
    /* SIDEBAR */
    #sidebar-header { height: 3; padding: 1; border-bottom: solid #1e1f22; text-style: bold; content-align: center middle; }
    #channel-list, #dm-list, #server-list { height: 1fr; }
    
    /* USER CONTROL PANEL (Bottom Left) */
    UserControlPanel { 
        height: 4; background: #232428; 
        border-top: solid #1e1f22; 
        padding: 0 1; 
    }
    #user-info-text { width: 1fr; content-align: left middle; }
    #uc-username { text-style: bold; }
    #uc-status { color: #949ba4; }
    .settings-btn { 
        min-width: 4; width: 4; background: transparent; border: none; 
    }
    .settings-btn:hover { background: #3f4147; }

    /* CHANNELS */
    .category-label { color: #949ba4; text-style: bold; padding-left: 1; margin-top: 1; }
    .channel-label { color: #949ba4; padding-left: 2; }
    .channel-label.voice { color: #5e646e; }
    ListItem.--highlight .channel-label { color: #f2f3f5; background: #3f4147; }
    
    /* CHAT AREA - HEADER */
    TopActionBar { 
        height: 3; padding: 0 1; 
        border-bottom: solid #26272d; 
        background: #313338; 
    }
    #chat-title { content-align: left middle; text-style: bold; height: 100%; }
    .spacer { width: 1fr; }
    .icon-btn { 
        background: transparent; border: none; color: #b5bac1; 
        min-width: 8; height: 100%; 
    }
    .icon-btn:hover { color: white; background: #3f4147; }

    #message-feed { height: 1fr; }
    #welcome-msg { text-align: center; width: 100%; padding-top: 2; color: #72767d; }
    
    /* INPUT AREA - UPDATED FOR MULTILINE */
    #input-area { height: auto; margin: 1; background: #383a40; padding: 1; }
    #reply-status { height: 1; background: #2b2d31; color: #b9bbbe; padding: 0 1; display: none; }
    #reply-status.visible { display: block; }
    .reply-hint { text-align: right; color: #72767d; }
    TextArea { height: 4; background: #2b2d31; border: none; }
    #btn-send { width: 100%; margin-top: 1; background: #5865f2; color: white; }
    
    /* MISC */
    Toast { background: #5865f2; color: white; }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit"),
        Binding("ctrl+k", "action_palette", "Jump"),
        Binding("ctrl+m", "action_toggle_members", "Members"),
        Binding("escape", "action_esc", "Back"),
        Binding("tab", "focus_next", "Next"),
    ]

    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self.client = DiscordGateway(self)
        self.traffic = TrafficController(concurrency=5)
        self.active_guild: Optional[discord.Guild] = None
        self.active_channel: Optional[Any] = None
        self.reply_msg: Optional[discord.Message] = None
        self.typing_task: Optional[asyncio.Task] = None
        self.search_index: List[Dict] = []

    async def on_mount(self):
        # 1. Check Login
        if not self.config.token:
            res = await self.push_screen_wait(LoginScreen())
            if not res:
                self.exit()
                return
            token, save = res
            self.config.token = token
            self.config.auto_login = save
            if save: self.config.save()
        
        # 2. Start Services
        await self.traffic.startup()
        asyncio.create_task(self.boot_gateway())

    async def on_unmount(self):
        await self.traffic.shutdown()
        if self.client:
            await self.client.close()

    async def boot_gateway(self):
        try:
            await self.client.start(self.config.token)
        except Exception as e:
            self.notify(f"Login Failed: {e}", severity="error", timeout=20)
            self.config.token = "" # Clear invalid token
            self.config.save()

    def compose(self) -> ComposeResult:
        # LEFT: Sidebar (Servers + DMs + Channels)
        with Vertical(id="col-sidebar"):
            yield Label("Nexus TUI", id="sidebar-header")
            with TabbedContent(initial="tab-servers"):
                # Tab 1: Servers & Channels
                with TabPane("Servers", id="tab-servers"):
                    with Horizontal(style="height: 1fr;"):
                         # Thin rail for servers
                        yield ListView(id="server-list", style="width: 8; margin-right: 1;") 
                        # List for channels
                        yield ListView(id="channel-list", style="width: 1fr;")
                # Tab 2: DMs
                with TabPane("DMs", id="tab-dms"):
                    yield ListView(id="dm-list")
            
            # User Control Panel at bottom of sidebar
            yield UserControlPanel()

        # CENTER: Chat
        with Vertical(id="col-chat"):
            # Interactive Top Bar
            yield TopActionBar()
            with VerticalScroll(id="message-feed"):
                yield Label("Welcome to Nexus.\nWaiting for Gateway...", id="welcome-msg")
            # Multi-line Input Area
            with Vertical(id="input-area"):
                yield ReplyStatus(id="reply-status")
                yield TextArea(id="main-input")
                yield Button("Send", id="btn-send")

        # RIGHT: Members
        with VerticalScroll(id="col-members"):
            yield ListView(id="member-list")
        yield Footer()

    def on_gateway_ready(self):
        self.notify("Gateway Connected", title="System")
        
        # Populate Servers
        sl = self.query_one("#server-list", ListView)
        sl.clear()
        self.search_index = []
        for guild in self.client.guilds:
            sl.append(ServerIcon(guild))
            self.search_index.append({"label": f"Server: {guild.name}", "id": f"g:{guild.id}"})
            for c in guild.text_channels:
                self.search_index.append({"label": f"#{c.name} ({guild.name})", "id": f"c:{c.id}"})
        
        # Populate DMs
        self.refresh_dms()
        
        self.query_one("#welcome-msg", Label).update("Select a Server or DM.")
        
        # Update User Control Panel
        uc = self.query_one(UserControlPanel)
        uc.query_one("#uc-username", Label).update(self.client.user.name)
        uc.query_one("#uc-status", Label).update(f"#{self.client.user.discriminator}")

    def refresh_dms(self):
        dl = self.query_one("#dm-list", ListView)
        dl.clear()
        for pm in self.client.private_channels:
            dl.append(DMItem(pm))
            name = pm.name if isinstance(pm, discord.GroupChannel) else pm.recipient.name
            self.search_index.append({"label": f"DM: {name}", "id": f"d:{pm.id}"})

    # --- EVENT HANDLERS (BUTTONS) ---
    
    @on(Button.Pressed)
    async def on_button_pressed(self, event: Button.Pressed):
        bid = event.button.id
        if bid == "btn-settings":
            self.action_settings()
        elif bid == "btn-search":
            self.action_palette()
        elif bid == "btn-members":
            self.action_toggle_members()
        elif bid == "btn-send":
            await self.action_submit_message()

    def on_list_view_selected(self, event: ListView.Selected):
        item = event.item
        if isinstance(item, ServerIcon):
            self.load_guild(item.guild)
        elif isinstance(item, ChannelNode):
            if not item.is_category:
                self.load_channel(item.channel_ref)
        elif isinstance(item, DMItem):
            self.load_channel(item.channel)

    def on_message_render_widget_selected(self, event: MessageRenderWidget.Selected):
        # NEW: Show Context Menu instead of immediate reply
        self.show_message_context_menu(event.msg_obj)

    def show_message_context_menu(self, message: discord.Message):
        is_author = (message.author.id == self.client.user.id)
        
        def handler(action_id):
            if action_id == "reply":
                self.initiate_reply(message)
            elif action_id == "delete":
                asyncio.create_task(message.delete())
                self.notify("Message deleted.")
            elif action_id == "edit":
                self.initiate_edit(message)
            elif action_id == "copy":
                self.notify(f"ID: {message.id}")
                
        self.push_screen(MessageActionModal(is_author), handler)

    def initiate_edit(self, message: discord.Message):
        def edit_handler(new_content):
            if new_content and new_content != message.content:
                asyncio.create_task(message.edit(content=new_content))
                self.notify("Message edited.")
        self.push_screen(EditInputModal(message.content), edit_handler)

    def action_esc(self):
        if self.reply_msg:
            self.cancel_reply()
        else:
            self.query_one("#message-feed").focus()

    def action_palette(self):
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
            elif type_ == "d":
                channel = self.client.get_channel(id_)
                if channel: self.load_channel(channel)
        self.push_screen(CommandPalette(self.search_index), handle_palette)

    def action_settings(self):
        self.push_screen(SettingsScreen(self.config))

    def action_toggle_members(self):
        mem = self.query_one("#col-members")
        mem.display = not mem.display

    def load_guild(self, guild: discord.Guild):
        self.active_guild = guild
        self.query_one("#sidebar-header", Label).update(guild.name)
        cl = self.query_one("#channel-list", ListView)
        cl.clear()
        
        # Ensure we are on the server tab
        self.query_one(TabbedContent).active = "tab-servers"

        by_category = {}
        no_category = []
        for channel in guild.channels:
            if isinstance(channel, discord.CategoryChannel):
                by_category[channel.id] = {"obj": channel, "channels": []}
            elif channel.category_id:
                if channel.category_id not in by_category: continue 
                by_category[channel.category_id]["channels"].append(channel)
            else:
                no_category.append(channel)
        for c in sorted(no_category, key=lambda x: x.position):
            if isinstance(c, (discord.TextChannel, discord.VoiceChannel)):
                cl.append(ChannelNode(c))
        sorted_cats = sorted([v["obj"] for v in by_category.values()], key=lambda x: x.position)
        for cat in sorted_cats:
            cl.append(ChannelNode(cat, is_category=True))
            for c in sorted(by_category[cat.id]["channels"], key=lambda x: x.position):
                if isinstance(c, (discord.TextChannel, discord.VoiceChannel)):
                    cl.append(ChannelNode(c))
        self.populate_members(guild.members)
        cl.focus()

    @work
    async def populate_members(self, members: List[discord.Member]):
        ml = self.query_one("#member-list", ListView)
        ml.clear()
        def sort_key(m):
            return {"online": 0, "streaming": 1, "idle": 2, "dnd": 3, "offline": 4}.get(str(m.status), 5)
        
        m_list = sorted(members, key=sort_key)
        count = 0
        for m in m_list:
            if str(m.status) == "offline": continue
            ml.append(MemberItem(m))
            count += 1
            if count > 200: break

    @work
    async def load_channel(self, channel: Any):
        # Handle DM vs Guild Channel
        self.active_channel = channel
        self.cancel_reply()
        
        name = ""
        if isinstance(channel, discord.DMChannel):
            name = f"@{channel.recipient.name}"
            # Populate members with just the two people
            self.populate_members([channel.recipient, self.client.user])
        elif isinstance(channel, discord.GroupChannel):
            name = channel.name or "Group Chat"
            self.populate_members(channel.recipients + [self.client.user])
        else:
            name = f"#{channel.name}"

        # Update Top Bar
        self.query_one("#chat-title", Label).update(name)
        
        # Unlock Input
        # inp = self.query_one("#main-input") # Now it's a TextArea
        # inp.focus() 

        # Load History
        feed = self.query_one("#message-feed", VerticalScroll)
        await feed.remove_children()
        try:
            msgs = [m async for m in channel.history(limit=self.config.max_history)]
            for m in reversed(msgs):
                await self.render_message(m, scroll=False)
            feed.scroll_end(animate=False)
        except discord.Forbidden:
            self.notify("Access Denied", severity="error")

    def dispatch_message(self, message: discord.Message):
        asyncio.create_task(self.traffic.ingest(message))
        if self.active_channel and message.channel.id == self.active_channel.id:
            asyncio.create_task(self.render_message(message, scroll=True))

    async def render_message(self, message: discord.Message, scroll: bool):
        feed = self.query_one("#message-feed", VerticalScroll)
        # Trim history if needed
        if len(feed.children) > self.config.max_history + 20:
            await feed.children[0].remove()
        
        is_mentioned = self.client.user in message.mentions
        widget = MessageRenderWidget(message, is_mentioned, self.client.user.id)
        await feed.mount(widget)
        if scroll:
            feed.scroll_end()

    def initiate_reply(self, message: discord.Message):
        self.reply_msg = message
        status = self.query_one("#reply-status")
        status.query_one("#reply-target-text", Label).update(f"Replying to: {message.author.display_name}")
        status.add_class("visible")
        self.query_one("#main-input").focus()

    def cancel_reply(self):
        self.reply_msg = None
        self.query_one("#reply-status").remove_class("visible")

    async def action_submit_message(self):
        ta = self.query_one("#main-input", TextArea)
        val = ta.text.strip()
        if not val or not self.active_channel: return
        
        ta.text = "" # Clear input
        try:
            if self.reply_msg:
                await self.reply_msg.reply(val)
                self.cancel_reply()
            else:
                await self.active_channel.send(val)
        except Exception as e:
            self.notify(f"Send Failed: {e}", severity="error")

def main():
    parser = ArgumentParser(description="Nexus TUI - Enterprise Discord Client")
    # Kept argument for backwards compatibility
    parser.add_argument("-t", "--token", help="Auth Token", default=None)
    args = parser.parse_args()
    
    cfg = AppConfig.load()
    if args.token:
        cfg.token = args.token
    
    app = NexusApp(cfg)
    app.run()

if __name__ == "__main__":
    main()
