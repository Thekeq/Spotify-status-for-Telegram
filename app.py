import asyncio
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
from collections import deque
from pathlib import Path
from tkinter import messagebox

from PIL import Image, ImageDraw

import customtkinter as ctk
import pystray
import qrcode
import spotipy
from spotipy.oauth2 import SpotifyPKCE
from telethon import TelegramClient, functions
from telethon.errors import AboutTooLongError, FloodWaitError, PasswordHashInvalidError, SessionPasswordNeededError

if sys.platform == "win32":
    import winreg

VERSION = "1.1.1"  # bump together with the release tag
REPO = "Thekeq/Spotify-status-for-Telegram"
REPO_URL = f"https://github.com/{REPO}"
APP_NAME = "SpotifyStatusTelegram"
FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable if FROZEN else __file__).resolve().parent
# settings and sessions live outside the exe folder, so a new exe downloaded anywhere keeps them
DATA_DIR = Path(os.environ.get("APPDATA") or Path.home() / ".config") / "SpotifyStatus"
CONFIG = DATA_DIR / "config.json"
REDIRECT = "http://127.0.0.1:8888/callback"
INSTANCE_PORT = 48731  # localhost port the running copy listens on, so a second launch can find it
LOG_LIMIT = 100  # log entries kept in the window
POLL = 15  # seconds between Spotify checks; the bio only changes when the track changes
DEFAULTS = {"api_id": "", "api_hash": "", "spotify_client_id": "", "prefix": "Listening to Spotify: ", "bio": "",
            "star_dismissed": False}

GREEN, GREEN_HOVER = "#1DB954", "#1ED760"
RED, RED_HOVER = "#E5534B", "#F06A62"
MUTED = "#8A8A8A"

# Windows virtual key codes, so shortcuts work on any keyboard layout (Tk sees Cyrillic etc. keysyms)
EDIT_KEYS = {67: "<<Copy>>", 86: "<<Paste>>", 88: "<<Cut>>", 65: "<<SelectAll>>"}


def load_config():
    try:
        return DEFAULTS | json.loads(CONFIG.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULTS)


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


def prepare_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("config.json", "tg_session.session", ".spotify_cache"):  # v1.0 kept these next to the exe
        old, new = APP_DIR / name, DATA_DIR / name
        if old.exists() and not new.exists():
            shutil.copy2(old, new)
    if FROZEN:  # leftover from the last self-update
        try:
            Path(sys.executable).with_suffix(".old").unlink(missing_ok=True)
        except OSError:
            pass  # the previous copy is still exiting; next launch cleans it


def parse_version(tag):
    return tuple(int(x) for x in tag.lstrip("v").split("."))


def latest_release():
    """(tag, exe download URL or None) of the latest GitHub release."""
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.load(r)
    url = next((a["browser_download_url"] for a in data.get("assets", []) if a["name"].endswith(".exe")), None)
    return data["tag_name"], url


def install_update(url, exe):
    """Download the new exe and swap it in. Windows can't overwrite a running exe, but can rename it."""
    new, old = exe.with_suffix(".new"), exe.with_suffix(".old")
    urllib.request.urlretrieve(url, new)  # raises on incomplete download
    old.unlink(missing_ok=True)
    exe.rename(old)
    new.rename(exe)


def build_bio(base, prefix, track, limit):
    """First line is the track, then the normal bio. Truncated to Telegram's limit."""
    room = limit - len(base) - (1 if base else 0)
    if not track or room < 6:
        return base
    line = prefix + track
    if len(line) > room:
        line = "♪ " + track
    if len(line) > room:
        line = line[:room - 1] + "…"
    return f"{line}\n{base}" if base else line


def strip_track_line(about, prefix):
    """Removes a leftover track line (e.g. after a crash)."""
    first, _, rest = about.partition("\n")
    return rest if first.startswith((prefix, "♪ ")) else about


def eye_image(shown):
    """Eye icon for show/hide buttons; crossed out while the value is hidden."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = "#C8C8C8"
    draw.arc((4, 12, 60, 68), 200, 340, fill=color, width=5)  # upper lid
    draw.arc((4, -4, 60, 52), 20, 160, fill=color, width=5)  # lower lid
    draw.ellipse((22, 22, 42, 42), fill=color)  # pupil
    if not shown:
        draw.line((10, 54, 54, 10), fill=color, width=6)
    return img


def tray_image():
    """Green circle with equalizer bars."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, 62, 62), fill=GREEN)
    for x, h in ((17, 18), (29, 30), (41, 22)):
        draw.rounded_rectangle((x, 32 - h // 2, x + 6, 32 + h // 2), radius=3, fill="black")
    return img


def signal_running_instance():
    """True if a copy is already running; that copy is asked to show its window."""
    try:
        with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=1) as s:
            s.sendall(b"show")
            return s.recv(64) == APP_NAME.encode()  # not some other program on that port
    except OSError:
        return False


def current_track(sp):
    cur = sp.current_user_playing_track()
    if not cur or not cur.get("is_playing") or not cur.get("item"):
        return None  # paused, ad or podcast
    item = cur["item"]
    artists = ", ".join(a["name"] for a in item["artists"])
    return f"{artists} - {item['name']}"


# --- Windows autostart (HKCU Run key, no admin rights needed) ---
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --autostart'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else sys.executable}" "{Path(__file__).resolve()}" --autostart'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
        return True
    except FileNotFoundError:
        return False


def set_autostart(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


class Worker:
    """Background thread: Spotify -> Telegram bio. Talks to the GUI only through App's thread-safe methods."""

    def __init__(self, cfg, app):
        self.cfg, self.app = cfg, app
        self.loop = self.task = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def stop(self):
        if self.loop and self.task:
            self.loop.call_soon_threadsafe(self.task.cancel)

    def _run(self):
        try:
            asyncio.run(self._main())
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.app.log(f"Error: {e}")
        self.app.log("Stopped")
        self.app.ui(self.app.on_stopped)

    async def _ask(self, prompt, secret=False):
        answer = await asyncio.to_thread(self.app.ask, prompt, secret)
        if not answer:
            raise RuntimeError("login cancelled")
        return answer

    async def _login(self, tg):
        """QR login, like Telegram Desktop: no login code to wait for."""
        await tg.connect()
        if await tg.is_user_authorized():
            return
        self.app.log("Scan the QR code with Telegram on your phone")
        qr = await tg.qr_login()
        try:
            while True:
                self.app.ui(lambda url=qr.url: self.app.show_qr(url))
                try:
                    await qr.wait()
                    return
                except asyncio.TimeoutError:
                    await qr.recreate()  # token expires every ~30 s
        except SessionPasswordNeededError:
            pass
        finally:
            self.app.ui(self.app.hide_qr)
        while True:
            try:
                await tg.sign_in(password=await self._ask("Two-step verification password", secret=True))
                return
            except PasswordHashInvalidError:
                self.app.log("Wrong password, try again")

    async def _set_bio(self, tg, bio):
        try:
            await tg(functions.account.UpdateProfileRequest(about=bio))
        except FloodWaitError as e:
            self.app.log(f"Telegram rate limit, waiting {e.seconds}s")
            await asyncio.sleep(e.seconds)
            return False
        except AboutTooLongError:
            self.app.log("Bio too long, using the normal bio")
            await tg(functions.account.UpdateProfileRequest(about=self.cfg["bio"]))
            return True
        self.app.log("Bio updated:\n          " + bio.replace("\n", "\n          "))
        return True

    async def _main(self):
        self.loop, self.task = asyncio.get_running_loop(), asyncio.current_task()
        c = self.cfg
        cache = DATA_DIR / ".spotify_cache"
        sp = spotipy.Spotify(auth_manager=SpotifyPKCE(
            client_id=c["spotify_client_id"], redirect_uri=REDIRECT,
            scope="user-read-currently-playing", cache_path=str(cache)))
        if not cache.exists():
            self.app.log("Sign in to Spotify in the browser window…")
            await asyncio.to_thread(current_track, sp)

        self.app.log("Connecting to Telegram…")
        # retry forever: on Windows startup the network may not be up yet
        tg = TelegramClient(str(DATA_DIR / "tg_session"), int(c["api_id"]), c["api_hash"],
                            connection_retries=-1, retry_delay=5)
        shown = None
        try:
            await self._login(tg)
            me = await tg.get_me()
            if not c["bio"]:
                full = await tg(functions.users.GetFullUserRequest("me"))
                c["bio"] = strip_track_line(full.full_user.about or "", c["prefix"])
                self.app.ui(lambda bio=c["bio"]: self.app.remember_bio(bio))  # only the GUI thread saves config
                self.app.log("Normal bio taken from your profile")
            limit = 140 if me.premium else 70
            room = limit - len(c["bio"]) - 1
            self.app.log(f"Logged in as {me.first_name}. Bio limit {limit}, {room} chars left for the track")
            if room < 20:
                self.app.log("Not much room for the track: shorten your normal bio")
            self.app.ui(lambda: self.app.show_running(True))
            last = object()
            while True:
                try:
                    track = await asyncio.to_thread(current_track, sp)
                    if track != last:
                        last = track
                        self.app.ui(lambda t=track: self.app.show_track(t))
                    bio = build_bio(c["bio"], c["prefix"], track, limit)
                    if bio != shown and await self._set_bio(tg, bio):
                        shown = bio
                except Exception as e:  # network hiccup: keep going
                    self.app.log(f"Error: {e}")
                await asyncio.sleep(POLL)
        finally:
            if shown not in (None, c["bio"]):
                await self._set_bio(tg, c["bio"])  # restore the normal bio
            await tg.disconnect()


class Prompt(ctk.CTkToplevel):
    """Modal text input; CTkInputDialog can't hide passwords."""

    def __init__(self, master, text, secret):
        super().__init__(master)
        self.title("Telegram login")
        self.resizable(False, False)
        self.value = None
        ctk.CTkLabel(self, text=text, justify="left").pack(padx=24, pady=(20, 8), anchor="w")
        self.entry = ctk.CTkEntry(self, width=300, height=36, show="•" if secret else "")
        self.entry.pack(padx=24)
        self.entry.bind("<Return>", lambda _: self.ok())
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(padx=24, pady=20, fill="x")
        ctk.CTkButton(row, text="OK", width=140, fg_color=GREEN, hover_color=GREEN_HOVER,
                      text_color="black", command=self.ok).pack(side="right")
        ctk.CTkButton(row, text="Cancel", width=140, fg_color="transparent", border_width=1,
                      command=self.destroy).pack(side="left")
        self.transient(master)
        self.after(150, self._focus)  # CTkToplevel isn't ready for grab right away

    def _focus(self):
        self.lift()
        self.grab_set()
        self.entry.focus()

    def ok(self):
        self.value = self.entry.get().strip()
        self.destroy()


class QrWindow(ctk.CTkToplevel):
    def __init__(self, master, on_cancel):
        super().__init__(master)
        self.title("Telegram login")
        self.resizable(False, False)
        ctk.CTkLabel(self, text="Scan with your phone", font=ctk.CTkFont(size=17, weight="bold")).pack(
            padx=24, pady=(20, 4))
        ctk.CTkLabel(self, text="Telegram → Settings → Devices → Link Desktop Device",
                     text_color=MUTED).pack(padx=24)
        self.qr = ctk.CTkLabel(self, text="")
        self.qr.pack(padx=24, pady=16)
        ctk.CTkButton(self, text="Cancel", width=140, fg_color="transparent", border_width=1,
                      command=on_cancel).pack(pady=(0, 20))
        self.protocol("WM_DELETE_WINDOW", on_cancel)
        self.transient(master)
        self.after(150, self.lift)

    def set_url(self, url):
        img = qrcode.make(url, border=2).get_image().convert("RGB")
        self.image = ctk.CTkImage(light_image=img, dark_image=img, size=(260, 260))  # keep a reference
        self.qr.configure(image=self.image)


class App:
    def __init__(self, root, start=False, hidden=False):
        self.root, self.q, self.worker, self.qr_win, self.instance_srv = root, queue.Queue(), None, None, None
        self.cfg = load_config()
        self.vars = {}
        self.log_sizes = deque()  # line count of each log entry on screen
        root.title("Spotify Status for Telegram")
        root.geometry("920x650")
        root.minsize(860, 650)
        root.grid_columnconfigure(1, weight=1)
        root.grid_rowconfigure(0, weight=1)
        self.h1 = ctk.CTkFont(size=26, weight="bold")
        self.h2 = ctk.CTkFont(size=15, weight="bold")
        self.small = ctk.CTkFont(size=12)
        self.eye_icons = {shown: ctk.CTkImage(eye_image(shown), size=(18, 18)) for shown in (False, True)}

        # --- left: settings ---
        left = ctk.CTkFrame(root, fg_color="transparent", width=430)
        left.grid(row=0, column=0, sticky="ns", padx=(20, 10), pady=20)
        head = ctk.CTkFrame(left, fg_color="transparent")
        head.pack(fill="x", pady=(0, 14))
        ctk.CTkLabel(head, text="●", text_color=GREEN, font=self.h1).pack(side="left", padx=(2, 8))
        ctk.CTkLabel(head, text="Spotify Status", font=self.h1).pack(side="left")
        ctk.CTkLabel(head, text="for Telegram", text_color=MUTED, font=self.h2).pack(side="left", padx=8, pady=(6, 0))

        tg = self._card(left, "Telegram", "https://my.telegram.org/apps")
        tg.grid_columnconfigure(0, weight=1)
        tg.grid_columnconfigure(1, weight=2)
        self._entry(tg, "API ID", "api_id", 1, 0, secret=True)
        self._entry(tg, "API Hash", "api_hash", 1, 1, secret=True)

        sp = self._card(left, "Spotify", "https://developer.spotify.com/dashboard")
        self._entry(sp, "Client ID", "spotify_client_id", 1, 0, secret=True, colspan=2)
        redirect = ctk.CTkFrame(sp, fg_color="transparent")
        redirect.grid(row=3, column=0, columnspan=2, sticky="ew", padx=14, pady=(0, 12))
        ctk.CTkLabel(redirect, text="Redirect URI", text_color=MUTED, font=self.small).pack(side="left")
        ctk.CTkLabel(redirect, text=REDIRECT, font=self.small).pack(side="left", padx=8)
        self.copy_btn = ctk.CTkButton(redirect, text="Copy", width=60, height=24, font=self.small,
                                      fg_color="transparent", border_width=1, command=self._copy_redirect)
        self.copy_btn.pack(side="right")

        bio = self._card(left, "Bio")
        self._entry(bio, "Text before the track", "prefix", 1, 0, colspan=2)
        ctk.CTkLabel(bio, text="Your normal bio (leave empty to take it from your profile)",
                     text_color=MUTED, font=self.small).grid(row=3, column=0, columnspan=2, sticky="w", padx=14)
        self.bio = ctk.CTkTextbox(bio, height=64, wrap="word", border_width=2)
        self.bio.insert("1.0", self.cfg["bio"])
        self.bio.grid(row=4, column=0, columnspan=2, sticky="ew", padx=14, pady=(2, 14))

        # --- right: status, controls, log ---
        right = ctk.CTkFrame(root, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=(10, 20), pady=20)
        self.right = right
        self.status_card = status = ctk.CTkFrame(right, corner_radius=12)
        status.pack(fill="x")
        self.state = ctk.CTkLabel(status, text="● Stopped", text_color=MUTED, font=self.h2)
        self.state.pack(anchor="w", padx=18, pady=(14, 0))
        ctk.CTkLabel(status, text="NOW PLAYING", text_color=MUTED, font=self.small).pack(anchor="w", padx=18, pady=(10, 0))
        self.track = ctk.CTkLabel(status, text="—", font=ctk.CTkFont(size=20, weight="bold"),
                                  wraplength=400, justify="left")
        self.track.pack(anchor="w", padx=18, pady=(0, 16))

        self.btn = ctk.CTkButton(right, text="Start", height=46, corner_radius=23, font=self.h2,
                                 fg_color=GREEN, hover_color=GREEN_HOVER, text_color="black", command=self.toggle)
        self.btn.pack(fill="x", pady=(14, 10))
        opts = ctk.CTkFrame(right, fg_color="transparent")
        opts.pack(fill="x", pady=(0, 12))
        if sys.platform == "win32":
            self.autostart = ctk.CTkSwitch(opts, text="Launch with Windows", progress_color=GREEN,
                                           command=self._toggle_autostart)
            self.autostart.pack(side="left")
            if autostart_enabled():
                self.autostart.select()
                if FROZEN:  # refresh the path in case the exe was moved; a source run mustn't hijack it
                    set_autostart(True)
        self.update_link = ctk.CTkLabel(opts, text=f"v{VERSION} · Check for updates", text_color=MUTED,
                                        font=self.small, cursor="hand2")
        self.update_link.pack(side="right")
        self.update_link.bind("<Button-1>", lambda _: self.check_updates(manual=True))
        self.update_banner = None
        if not self.cfg["star_dismissed"]:
            self._banner("Like the app? Star it on GitHub ★", "Star", self._star, on_close=self._dismiss_star)

        ctk.CTkLabel(right, text="ACTIVITY", text_color=MUTED, font=self.small).pack(anchor="w")
        self.out = ctk.CTkTextbox(right, font=ctk.CTkFont(family="Consolas", size=12), state="disabled")
        self.out.pack(fill="both", expand=True, pady=(2, 0))

        self.menu = tk.Menu(root, tearoff=0)
        for label, event, keys in [("Cut", "<<Cut>>", "Ctrl+X"), ("Copy", "<<Copy>>", "Ctrl+C"),
                                   ("Paste", "<<Paste>>", "Ctrl+V"), ("Select all", "<<SelectAll>>", "Ctrl+A")]:
            self.menu.add_command(label=label, accelerator=keys,
                                  command=lambda e=event: self.menu_target.event_generate(e))
        root.bind_all("<Button-3>", self._context_menu)
        root.bind_all("<Control-KeyPress>", self._ctrl_key)

        # pystray runs in its own thread: hand its clicks to the GUI thread via the queue
        self.tray = pystray.Icon(APP_NAME, tray_image(), "Spotify Status", menu=pystray.Menu(
            pystray.MenuItem("Open", lambda: self.ui(self.show_window), default=True),
            pystray.MenuItem("Quit", lambda: self.ui(self.quit))))
        self.tray.run_detached()
        self.tray_hint_shown = False

        root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self._poll()
        if hidden:
            self.tray_hint_shown = True
            root.after(0, root.withdraw)
        if start:
            root.after(1000, self.toggle)
        root.after(2000, self.check_updates)

    def _card(self, parent, title, url=None):
        card = ctk.CTkFrame(parent, corner_radius=12)
        card.pack(fill="x", pady=(0, 12))
        card.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkLabel(card, text=title, font=self.h2).grid(row=0, column=0, sticky="w", padx=14, pady=(10, 0))
        if url:
            link = ctk.CTkLabel(card, text="Get keys ↗", text_color=GREEN, font=self.small, cursor="hand2")
            link.grid(row=0, column=1, sticky="e", padx=14, pady=(10, 0))
            link.bind("<Button-1>", lambda _: webbrowser.open(url))
        return card

    def _entry(self, parent, label, key, row, col, secret=False, colspan=1):
        ctk.CTkLabel(parent, text=label, text_color=MUTED, font=self.small).grid(
            row=row, column=col, columnspan=colspan, sticky="w", padx=14, pady=(4, 0))
        self.vars[key] = ctk.StringVar(value=self.cfg[key])
        if not secret:
            ctk.CTkEntry(parent, textvariable=self.vars[key], height=34).grid(
                row=row + 1, column=col, columnspan=colspan, sticky="ew", padx=14, pady=(0, 12))
            return
        # secrets start hidden (safe to screen-record); the eye button toggles them
        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.grid(row=row + 1, column=col, columnspan=colspan, sticky="ew", padx=14, pady=(0, 12))
        entry = ctk.CTkEntry(box, textvariable=self.vars[key], width=100, height=34, show="•")  # grows to fill
        entry.pack(side="left", fill="x", expand=True)

        def toggle():
            hidden = entry.cget("show") == "•"
            entry.configure(show="" if hidden else "•")
            eye.configure(image=self.eye_icons[hidden])
        eye = ctk.CTkButton(box, text="", image=self.eye_icons[False], width=34, height=34,
                            fg_color="transparent", hover_color="#3A3A3A", command=toggle)
        eye.pack(side="left", padx=(4, 0))

    def _banner(self, text, action_text, action, on_close=None):
        bar = ctk.CTkFrame(self.right, corner_radius=10, border_width=1, border_color=GREEN)
        bar.pack(fill="x", pady=(0, 10), before=self.status_card)
        ctk.CTkLabel(bar, text=text).pack(side="left", padx=(14, 6), pady=8)

        def close():
            bar.destroy()
            if on_close:
                on_close()
        ctk.CTkButton(bar, text="✕", width=28, height=28, fg_color="transparent", hover_color="#3A3A3A",
                      command=close).pack(side="right", padx=(0, 8))
        btn = ctk.CTkButton(bar, text=action_text, width=90, height=28, fg_color=GREEN, hover_color=GREEN_HOVER,
                            text_color="black", command=lambda: action(close))
        btn.pack(side="right", padx=4)
        return bar, btn

    def _star(self, close):
        webbrowser.open(REPO_URL)
        close()

    def _dismiss_star(self):
        self.cfg["star_dismissed"] = True
        save_config(self.cfg)

    def check_updates(self, manual=False):
        def work():
            try:
                tag, url = latest_release()
                newer = parse_version(tag) > parse_version(VERSION)
            except Exception as e:  # offline, rate limit, odd tag
                if manual:
                    self.log(f"Update check failed: {e}")
                return
            if newer:
                self.ui(lambda: self.show_update(tag, url))
            elif manual:
                self.ui(lambda: self.update_link.configure(text=f"v{VERSION} · Up to date ✓"))
        threading.Thread(target=work, daemon=True).start()

    def show_update(self, tag, url):
        if self.update_banner:
            return
        self.update_banner, self.update_btn = self._banner(
            f"Update {tag} is available", "Update", lambda _close: self.update_now(url))
        if self.root.state() == "withdrawn":
            self.tray.notify(f"Update {tag} is available. Open the app to install it.", "Spotify Status")

    def update_now(self, url):
        if not (FROZEN and url):  # running from source: just show the release
            webbrowser.open(f"{REPO_URL}/releases/latest")
            return
        self.update_btn.configure(text="Downloading…", state="disabled", width=120)

        def work():
            try:
                install_update(url, Path(sys.executable))
            except Exception as e:
                self.log(f"Update failed: {e}. Download it manually from GitHub.")
                self.ui(lambda: self.update_btn.configure(text="Update", state="normal", width=90))
                return
            was_running = bool(self.worker and self.worker.thread.is_alive())
            self.ui(lambda: self.quit(relaunch=["--start"] if was_running else []))
        threading.Thread(target=work, daemon=True).start()

    def _context_menu(self, event):
        if isinstance(event.widget, (tk.Entry, tk.Text)):
            self.menu_target = event.widget
            event.widget.focus_set()
            self.menu.tk_popup(event.x_root, event.y_root)

    def _ctrl_key(self, event):
        # Latin layouts are already handled by Tk's own bindings
        if event.keysym.lower() not in ("c", "v", "x", "a") and event.keycode in EDIT_KEYS \
                and isinstance(event.widget, (tk.Entry, tk.Text)):
            event.widget.event_generate(EDIT_KEYS[event.keycode])
            return "break"

    def _copy_redirect(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(REDIRECT)
        self.copy_btn.configure(text="Copied")
        self.root.after(1500, lambda: self.copy_btn.configure(text="Copy"))

    def _toggle_autostart(self):
        try:
            set_autostart(bool(self.autostart.get()))
        except OSError as e:
            messagebox.showerror("Autostart", f"Couldn't change autostart: {e}")

    # --- thread-safe (called from Worker) ---
    def ui(self, fn):
        self.q.put(fn)

    def log(self, msg):
        self.ui(lambda: self._append(f"{time.strftime('%H:%M:%S')}  {msg}"))

    def ask(self, prompt, secret=False):
        box, done = {}, threading.Event()

        def show():
            self.root.deiconify()
            dialog = Prompt(self.root, prompt, secret)
            self.root.wait_window(dialog)
            box["v"] = dialog.value
            done.set()
        self.ui(show)
        done.wait()
        return box["v"]

    # --- GUI thread only ---
    def _poll(self):
        while not self.q.empty():
            self.q.get_nowait()()
        self.root.after(150, self._poll)

    def _append(self, msg):
        self.out.configure(state="normal")
        self.out.insert("end", msg + "\n")
        self.log_sizes.append(msg.count("\n") + 1)  # entries can span lines ("Bio updated:")
        if len(self.log_sizes) > LOG_LIMIT:  # keep memory flat when running for weeks
            self.out.delete("1.0", f"{self.log_sizes.popleft() + 1}.0")
        self.out.see("end")
        self.out.configure(state="disabled")

    def show_qr(self, url):
        if not self.qr_win:
            self.root.deiconify()
            self.qr_win = QrWindow(self.root, on_cancel=self.toggle)  # toggle stops the worker
        self.qr_win.set_url(url)

    def hide_qr(self):
        if self.qr_win:
            self.qr_win.destroy()
            self.qr_win = None

    def remember_bio(self, text):
        self.bio.delete("1.0", "end")
        self.bio.insert("1.0", text)
        self.cfg["bio"] = text
        save_config(self.cfg)

    def show_running(self, running):
        self.state.configure(text="● Running" if running else "● Stopped", text_color=GREEN if running else MUTED)

    def show_track(self, track):
        self.track.configure(text=track or "—")
        self.tray.title = f"Spotify Status: {track}"[:120] if track else "Spotify Status"  # tray tooltip

    def on_stopped(self):
        self.show_running(False)
        self.show_track(None)
        self.btn.configure(text="Start", state="normal", fg_color=GREEN, hover_color=GREEN_HOVER)

    def toggle(self):
        if self.worker and self.worker.thread.is_alive():
            self.worker.stop()
            self.btn.configure(text="Stopping…", state="disabled")
            return
        form = {k: v.get().strip() for k, v in self.vars.items()}
        form["prefix"] = self.vars["prefix"].get()  # trailing space matters
        form["bio"] = self.bio.get("1.0", "end-1c").strip()
        if not (form["api_id"].isdigit() and form["api_hash"] and form["spotify_client_id"]):
            messagebox.showerror("Missing settings", "Fill in Telegram API ID, API Hash and Spotify Client ID")
            return
        self.cfg.update(form)
        save_config(self.cfg)
        self.worker = Worker(dict(self.cfg), self)
        self.worker.thread.start()
        self.btn.configure(text="Stop", fg_color=RED, hover_color=RED_HOVER)

    def listen_for_instances(self):
        # ponytail: two copies started in the same instant can both get past the check; fine for a desktop app
        srv = socket.socket()
        try:
            srv.bind(("127.0.0.1", INSTANCE_PORT))
        except OSError:
            return  # port taken by something else: just run without the single-instance check
        srv.listen()
        self.instance_srv = srv

        def serve():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return  # socket closed on quit
                with conn:
                    conn.settimeout(1)
                    try:
                        if conn.recv(16) == b"show":
                            conn.sendall(APP_NAME.encode())
                            self.ui(self.show_window)
                    except OSError:
                        pass
        threading.Thread(target=serve, daemon=True).start()

    def hide_to_tray(self):
        self.root.withdraw()
        if not self.tray_hint_shown:
            self.tray_hint_shown = True
            self.tray.notify("Still running in the tray. Right-click the icon to quit.", "Spotify Status")

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit(self, relaunch=None):
        """relaunch: args for starting the (just updated) exe again."""
        if self.worker and self.worker.thread.is_alive():
            self.worker.stop()
            self.worker.thread.join(timeout=10)  # let it restore the normal bio
        if self.instance_srv:
            self.instance_srv.close()  # otherwise the relaunched copy would just ask us to show the window
        if relaunch is not None:
            # a fresh onefile process must not reuse our temp folder, which is deleted when we exit
            subprocess.Popen([sys.executable, *relaunch], env=dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1"))
        self.tray.visible = False  # remove now, or a ghost icon stays until hovered
        self.tray.stop()
        self.root.destroy()


if __name__ == "__main__":
    if sys.argv[1:] == ["test"]:
        base = "Your bio"
        p = DEFAULTS["prefix"]
        assert build_bio(base, p, None, 140) == base
        assert build_bio(base, p, "Michael Jackson - Beat It", 140) == p + "Michael Jackson - Beat It\n" + base
        assert len(build_bio(base, p, "x" * 300, 140)) == 140
        assert len(build_bio(base, p, "Michael Jackson - Beat It", 70)) <= 70
        assert build_bio("", p, "Michael Jackson - Beat It", 70) == p + "Michael Jackson - Beat It"
        assert strip_track_line(p + "Michael Jackson - Beat It\n" + base, p) == base
        assert strip_track_line(base, p) == base
        assert parse_version("v1.10.0") > parse_version("v1.9.2") > parse_version("1.9")
        print("ok")
    elif not signal_running_instance():
        prepare_data_dir()
        ctk.set_appearance_mode("dark")
        root = ctk.CTk()
        autostart = "--autostart" in sys.argv
        App(root, start=autostart or "--start" in sys.argv, hidden=autostart).listen_for_instances()
        root.mainloop()
