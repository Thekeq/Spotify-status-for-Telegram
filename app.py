import asyncio
import json
import queue
import sys
import threading
import time
import webbrowser
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk
import spotipy
from spotipy.oauth2 import SpotifyPKCE
from telethon import TelegramClient, functions
from telethon.errors import (AboutTooLongError, FloodWaitError, PasswordHashInvalidError,
                             PhoneCodeInvalidError, SessionPasswordNeededError)

if sys.platform == "win32":
    import winreg

APP_NAME = "SpotifyStatusTelegram"
APP_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
CONFIG = APP_DIR / "config.json"
REDIRECT = "http://127.0.0.1:8888/callback"
POLL = 15  # seconds between Spotify checks; the bio only changes when the track changes
DEFAULTS = {"api_id": "", "api_hash": "", "spotify_client_id": "", "prefix": "Listening to Spotify: ", "bio": ""}

GREEN, GREEN_HOVER = "#1DB954", "#1ED760"
RED, RED_HOVER = "#E5534B", "#F06A62"
MUTED = "#8A8A8A"

CODE_HINTS = {
    "SentCodeTypeApp": "Code sent to your Telegram app: open the chat with the official \"Telegram\" "
                       "account on any device where you're logged in (it's not an SMS)",
    "SentCodeTypeSms": "Code sent by SMS",
    "SentCodeTypeCall": "You'll get a phone call with the code",
    "SentCodeTypeFlashCall": "You'll get a call: the code is the caller's phone number",
    "SentCodeTypeMissedCall": "You'll get a missed call: the code is the last digits of the caller's number",
    "SentCodeTypeFragmentSms": "Code sent to your Fragment number",
}


def load_config():
    try:
        return DEFAULTS | json.loads(CONFIG.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULTS)


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


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
        await tg.connect()
        if await tg.is_user_authorized():
            return
        phone = await self._ask("Phone number with country code\n(+<country code> <number>)")
        phone = "+" + "".join(ch for ch in phone if ch.isdigit())
        sent = await tg.send_code_request(phone)
        kind = type(sent.type).__name__
        self.app.log(CODE_HINTS.get(kind, f"Code sent ({kind})"))
        while True:
            try:
                await tg.sign_in(phone, await self._ask("Login code from Telegram"))
                return
            except PhoneCodeInvalidError:
                self.app.log("Wrong code, try again")
            except SessionPasswordNeededError:
                break
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
        self.app.log("Bio: " + bio.replace("\n", " | "))
        return True

    async def _main(self):
        self.loop, self.task = asyncio.get_running_loop(), asyncio.current_task()
        c = self.cfg
        cache = APP_DIR / ".spotify_cache"
        sp = spotipy.Spotify(auth_manager=SpotifyPKCE(
            client_id=c["spotify_client_id"], redirect_uri=REDIRECT,
            scope="user-read-currently-playing", cache_path=str(cache)))
        if not cache.exists():
            self.app.log("Sign in to Spotify in the browser window…")
            await asyncio.to_thread(current_track, sp)

        self.app.log("Connecting to Telegram…")
        # retry forever: on Windows startup the network may not be up yet
        tg = TelegramClient(str(APP_DIR / "tg_session"), int(c["api_id"]), c["api_hash"],
                            connection_retries=-1, retry_delay=5)
        shown = None
        try:
            await self._login(tg)
            me = await tg.get_me()
            if not c["bio"]:
                full = await tg(functions.users.GetFullUserRequest("me"))
                c["bio"] = strip_track_line(full.full_user.about or "", c["prefix"])
                save_config(c)
                self.app.ui(lambda: self.app.set_bio_field(c["bio"]))
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


class App:
    def __init__(self, root, autostart=False):
        self.root, self.q, self.worker = root, queue.Queue(), None
        self.cfg = load_config()
        self.vars = {}
        root.title("Spotify Status for Telegram")
        root.geometry("920x650")
        root.minsize(860, 650)
        root.grid_columnconfigure(1, weight=1)
        root.grid_rowconfigure(0, weight=1)
        self.h1 = ctk.CTkFont(size=26, weight="bold")
        self.h2 = ctk.CTkFont(size=15, weight="bold")
        self.small = ctk.CTkFont(size=12)

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
        self._entry(tg, "API ID", "api_id", 1, 0)
        self._entry(tg, "API Hash", "api_hash", 1, 1, secret=True)

        sp = self._card(left, "Spotify", "https://developer.spotify.com/dashboard")
        self._entry(sp, "Client ID", "spotify_client_id", 1, 0, colspan=2)
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
        status = ctk.CTkFrame(right, corner_radius=12)
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
        if sys.platform == "win32":
            self.autostart = ctk.CTkSwitch(right, text="Launch with Windows", progress_color=GREEN,
                                           command=self._toggle_autostart)
            self.autostart.pack(anchor="w", pady=(0, 12))
            if autostart_enabled():
                self.autostart.select()
                set_autostart(True)  # refresh the path in case the app was moved

        ctk.CTkLabel(right, text="ACTIVITY", text_color=MUTED, font=self.small).pack(anchor="w")
        self.out = ctk.CTkTextbox(right, font=ctk.CTkFont(family="Consolas", size=12), state="disabled")
        self.out.pack(fill="both", expand=True, pady=(2, 0))

        root.protocol("WM_DELETE_WINDOW", self.close)
        self._poll()
        if autostart:
            root.after(0, root.iconify)
            root.after(1000, self.toggle)

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
        ctk.CTkEntry(parent, textvariable=self.vars[key], height=34, show="•" if secret else "").grid(
            row=row + 1, column=col, columnspan=colspan, sticky="ew", padx=14, pady=(0, 12))

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
        self.out.see("end")
        self.out.configure(state="disabled")

    def set_bio_field(self, text):
        self.bio.delete("1.0", "end")
        self.bio.insert("1.0", text)

    def show_running(self, running):
        self.state.configure(text="● Running" if running else "● Stopped", text_color=GREEN if running else MUTED)

    def show_track(self, track):
        self.track.configure(text=track or "—")

    def on_stopped(self):
        self.show_running(False)
        self.show_track(None)
        self.btn.configure(text="Start", state="normal", fg_color=GREEN, hover_color=GREEN_HOVER)

    def toggle(self):
        if self.worker and self.worker.thread.is_alive():
            self.worker.stop()
            self.btn.configure(text="Stopping…", state="disabled")
            return
        cfg = {k: v.get().strip() for k, v in self.vars.items()}
        cfg["prefix"] = self.vars["prefix"].get()  # trailing space matters
        cfg["bio"] = self.bio.get("1.0", "end-1c").strip()
        if not (cfg["api_id"].isdigit() and cfg["api_hash"] and cfg["spotify_client_id"]):
            messagebox.showerror("Missing settings", "Fill in Telegram API ID, API Hash and Spotify Client ID")
            return
        save_config(cfg)
        self.worker = Worker(cfg, self)
        self.worker.thread.start()
        self.btn.configure(text="Stop", fg_color=RED, hover_color=RED_HOVER)

    def close(self):
        if self.worker and self.worker.thread.is_alive():
            self.worker.stop()
            self.worker.thread.join(timeout=10)  # let it restore the normal bio
        self.root.destroy()


if __name__ == "__main__":
    if sys.argv[1:] == ["test"]:
        base = "ChatBot Developer | Founder @nzdiary_bot @CookieMerge_Bot"
        p = DEFAULTS["prefix"]
        assert build_bio(base, p, None, 140) == base
        assert build_bio(base, p, "Nervy - Зацепило", 140) == p + "Nervy - Зацепило\n" + base
        assert len(build_bio(base, p, "x" * 300, 140)) == 140
        assert len(build_bio(base, p, "Nervy - Зацепило", 70)) <= 70
        assert build_bio("", p, "Nervy - Зацепило", 70) == p + "Nervy - Зацепило"
        assert strip_track_line(p + "Nervy - Зацепило\n" + base, p) == base
        assert strip_track_line(base, p) == base
        print("ok")
    else:
        ctk.set_appearance_mode("dark")
        root = ctk.CTk()
        App(root, autostart="--autostart" in sys.argv)
        root.mainloop()
