import asyncio
import json
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, scrolledtext, simpledialog

import spotipy
from spotipy.oauth2 import SpotifyPKCE
from telethon import TelegramClient, functions
from telethon.errors import AboutTooLongError, FloodWaitError

APP_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
CONFIG = APP_DIR / "config.json"
REDIRECT = "http://127.0.0.1:8888/callback"
POLL = 15  # секунд между проверками Spotify; bio меняется только при смене трека
DEFAULTS = {"api_id": "", "api_hash": "", "spotify_client_id": "", "prefix": "Слушает Spotify: ", "bio": ""}


def load_config():
    try:
        return DEFAULTS | json.loads(CONFIG.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULTS)


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


def build_bio(base, prefix, track, limit):
    """Первая строка — трек, дальше обычное bio. Обрезает под лимит Telegram."""
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
    """Убирает строку с треком, если она осталась после аварийного выхода."""
    first, _, rest = about.partition("\n")
    return rest if first.startswith((prefix, "♪ ")) else about


def current_track(sp):
    cur = sp.current_user_playing_track()
    if not cur or not cur.get("is_playing") or not cur.get("item"):
        return None  # пауза, реклама, подкаст
    item = cur["item"]
    artists = ", ".join(a["name"] for a in item["artists"])
    return f"{artists} - {item['name']}"


class Worker:
    """Фоновый поток: Spotify -> Telegram bio. С GUI общается только через методы App."""

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
            self.app.log(f"Ошибка: {e}")
        self.app.log("Остановлено")
        self.app.ui(self.app.on_stopped)

    async def _ask(self, prompt, secret=False):
        answer = await asyncio.to_thread(self.app.ask, prompt, secret)
        if not answer:
            raise RuntimeError("вход отменён")
        return answer

    async def _set_bio(self, tg, bio):
        try:
            await tg(functions.account.UpdateProfileRequest(about=bio))
        except FloodWaitError as e:
            self.app.log(f"Telegram просит подождать {e.seconds} c")
            await asyncio.sleep(e.seconds)
            return False
        except AboutTooLongError:
            self.app.log("Слишком длинно, ставлю обычное bio")
            await tg(functions.account.UpdateProfileRequest(about=self.cfg["bio"]))
            return True
        self.app.log("bio: " + bio.replace("\n", " | "))
        return True

    async def _main(self):
        self.loop, self.task = asyncio.get_running_loop(), asyncio.current_task()
        c = self.cfg
        sp = spotipy.Spotify(auth_manager=SpotifyPKCE(
            client_id=c["spotify_client_id"], redirect_uri=REDIRECT,
            scope="user-read-currently-playing", cache_path=str(APP_DIR / ".spotify_cache")))
        self.app.log("Вход в Spotify (при первом запуске откроется браузер)…")
        await asyncio.to_thread(current_track, sp)

        self.app.log("Вход в Telegram…")
        tg = TelegramClient(str(APP_DIR / "tg_session"), int(c["api_id"]), c["api_hash"])
        await tg.start(
            phone=lambda: self._ask("Номер телефона (в формате +7...)"),
            code_callback=lambda: self._ask("Код из Telegram"),
            password=lambda: self._ask("Облачный пароль (2FA)", secret=True))
        shown = None
        try:
            me = await tg.get_me()
            if not c["bio"]:
                full = await tg(functions.users.GetFullUserRequest("me"))
                c["bio"] = strip_track_line(full.full_user.about or "", c["prefix"])
                save_config(c)
                self.app.ui(lambda: self.app.set_bio_field(c["bio"]))
                self.app.log("Обычное bio взято из профиля")
            limit = 140 if me.premium else 70
            room = limit - len(c["bio"]) - 1
            self.app.log(f"Вошли как {me.first_name}. Лимит bio {limit}, под трек {room} символов")
            if room < 20:
                self.app.log("Мало места под трек — сократи обычное bio")
            while True:
                try:
                    track = await asyncio.to_thread(current_track, sp)
                except Exception as e:  # сеть/Spotify упал — оставляем как есть
                    self.app.log(f"Spotify: {e}")
                else:
                    bio = build_bio(c["bio"], c["prefix"], track, limit)
                    if bio != shown and await self._set_bio(tg, bio):
                        shown = bio
                await asyncio.sleep(POLL)
        finally:
            if shown not in (None, c["bio"]):
                await self._set_bio(tg, c["bio"])  # вернуть обычное bio
            await tg.disconnect()


class App:
    FIELDS = [
        ("api_id", "Telegram API ID", "https://my.telegram.org/apps"),
        ("api_hash", "Telegram API Hash", "https://my.telegram.org/apps"),
        ("spotify_client_id", "Spotify Client ID", "https://developer.spotify.com/dashboard"),
        ("prefix", "Текст перед треком", None),
    ]

    def __init__(self, root):
        self.root, self.q, self.worker = root, queue.Queue(), None
        root.title("Spotify → Telegram bio")
        root.resizable(False, False)
        cfg = load_config()

        self.vars = {}
        for row, (key, label, url) in enumerate(self.FIELDS):
            tk.Label(root, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=2)
            self.vars[key] = tk.StringVar(value=cfg[key])
            tk.Entry(root, textvariable=self.vars[key], width=48,
                     show="•" if key == "api_hash" else "").grid(row=row, column=1, padx=6, pady=2)
            if url:
                link = tk.Label(root, text="где взять?", fg="blue", cursor="hand2")
                link.grid(row=row, column=2, padx=6)
                link.bind("<Button-1>", lambda _, u=url: webbrowser.open(u))

        tk.Label(root, text="Redirect URI для Spotify").grid(row=4, column=0, sticky="w", padx=6)
        redirect = tk.Entry(root, width=48)
        redirect.insert(0, REDIRECT)
        redirect.config(state="readonly")
        redirect.grid(row=4, column=1, padx=6, pady=2)

        tk.Label(root, text="Обычное bio\n(пусто = взять из профиля)", justify="left").grid(
            row=5, column=0, sticky="nw", padx=6)
        self.bio = tk.Text(root, width=48, height=3, wrap="word")
        self.bio.insert("1.0", cfg["bio"])
        self.bio.grid(row=5, column=1, padx=6, pady=2)

        self.btn = tk.Button(root, text="Запустить", width=20, command=self.toggle)
        self.btn.grid(row=6, column=0, columnspan=3, pady=6)
        self.out = scrolledtext.ScrolledText(root, width=78, height=12, state="disabled")
        self.out.grid(row=7, column=0, columnspan=3, padx=6, pady=(0, 6))

        root.protocol("WM_DELETE_WINDOW", self.close)
        self._poll()

    # --- потокобезопасные методы (вызываются из Worker) ---
    def ui(self, fn):
        self.q.put(fn)

    def log(self, msg):
        self.ui(lambda: self._append(msg))

    def ask(self, prompt, secret=False):
        box, done = {}, threading.Event()

        def show():
            box["v"] = simpledialog.askstring("Вход в Telegram", prompt, parent=self.root,
                                              show="•" if secret else None)
            done.set()
        self.ui(show)
        done.wait()
        return box["v"]

    # --- только из GUI-потока ---
    def _poll(self):
        while not self.q.empty():
            self.q.get_nowait()()
        self.root.after(150, self._poll)

    def _append(self, msg):
        self.out.config(state="normal")
        self.out.insert("end", msg + "\n")
        self.out.see("end")
        self.out.config(state="disabled")

    def set_bio_field(self, text):
        self.bio.delete("1.0", "end")
        self.bio.insert("1.0", text)

    def on_stopped(self):
        self.btn.config(text="Запустить", state="normal")

    def toggle(self):
        if self.worker and self.worker.thread.is_alive():
            self.worker.stop()
            self.btn.config(text="Останавливаю…", state="disabled")
            return
        cfg = {k: v.get().strip() for k, v in self.vars.items()}
        cfg["prefix"] = self.vars["prefix"].get()  # пробел в конце префикса важен
        cfg["bio"] = self.bio.get("1.0", "end-1c").strip()
        if not (cfg["api_id"].isdigit() and cfg["api_hash"] and cfg["spotify_client_id"]):
            messagebox.showerror("Не хватает данных", "Заполни Telegram API ID, API Hash и Spotify Client ID")
            return
        save_config(cfg)
        self.worker = Worker(cfg, self)
        self.worker.thread.start()
        self.btn.config(text="Остановить")

    def close(self):
        if self.worker and self.worker.thread.is_alive():
            self.worker.stop()
            self.worker.thread.join(timeout=10)  # даём вернуть обычное bio
        self.root.destroy()


if __name__ == "__main__":
    if sys.argv[1:] == ["test"]:
        base = "ChatBot Developer | Founder @nzdiary_bot @CookieMerge_Bot"
        p = "Слушает Spotify: "
        assert build_bio(base, p, None, 140) == base
        assert build_bio(base, p, "Nervy - Зацепило", 140) == p + "Nervy - Зацепило\n" + base
        assert len(build_bio(base, p, "x" * 300, 140)) == 140
        assert len(build_bio(base, p, "Nervy - Зацепило", 70)) <= 70
        assert build_bio("", p, "Nervy - Зацепило", 70) == p + "Nervy - Зацепило"
        assert strip_track_line(p + "Nervy - Зацепило\n" + base, p) == base
        assert strip_track_line(base, p) == base
        print("ok")
    else:
        root = tk.Tk()
        App(root)
        root.mainloop()
