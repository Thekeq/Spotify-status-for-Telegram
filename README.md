# Spotify Status for Telegram

Shows the track you're playing on Spotify as the first line of your Telegram bio, Discord-style:

```
Listening to Spotify: Michael Jackson - Beat It
Your bio
```

When the music is paused or the app is closed, your normal bio comes back.

![screenshot](docs/screenshot.png)

## Install

Download `SpotifyStatus.exe` from [Releases](../../releases) and run it. The app checks for updates on every start and can update itself in one click; your settings are kept.

Or run from source (Python 3.10+ with Tk):

```
pip install -r requirements.txt
python app.py
```

## Setup (once)

**Telegram API ID and Hash**
1. Go to https://my.telegram.org → *API development tools*.
2. Create an app (any name), copy `api_id` and `api_hash`.

**Spotify Client ID**
1. Go to https://developer.spotify.com/dashboard → *Create app*.
2. Add `http://127.0.0.1:8888/callback` to *Redirect URIs* and tick *Web API*.
3. Copy the *Client ID* (no secret needed).

Paste everything into the app and press **Start**.
On the first run a browser window opens to sign in to Spotify, then a QR code appears: scan it with Telegram on your phone (*Settings → Devices → Link Desktop Device*). If you use two-step verification, the app asks for that password too. No login code needed.

Leave "Your normal bio" empty to take it from your profile. Closing the window keeps the app running in the system tray (right-click the tray icon → *Quit* to exit). Turn on **Launch with Windows** to start it in the tray at login.

## Limits

- Bio is limited to 70 characters (140 with Telegram Premium). Long titles are cut.
- No track timer: Telegram rate-limits profile changes.
- Works only while the app is running.
- Windows SmartScreen may warn about the unsigned exe: *More info → Run anyway*.

## Security

Settings (`config.json`) and the login session (`tg_session.session`) are stored in `%APPDATA%\SpotifyStatus`. **The session file gives full access to your Telegram account**: never share it. The app talks only to the Telegram and Spotify APIs, plus GitHub to check for updates.

## Uninstall

1. Turn off **Launch with Windows**, so the app is removed from Windows startup.
2. Right-click the tray icon → **Quit**. Your normal bio is restored.
3. Delete `SpotifyStatus.exe` and the `%APPDATA%\SpotifyStatus` folder.
4. Optional: end the session in Telegram (*Settings → Devices*) and delete the apps you created on my.telegram.org and developer.spotify.com.

## Privacy

The app only talks to:

- **Telegram**, to update your bio;
- **Spotify**, to read the track you're playing;
- **GitHub**, to check for new versions (no information about you is sent).

No analytics, no telemetry. Your keys and login sessions stay on your computer.

## Code signing policy

Free code signing provided by [SignPath.io](https://about.signpath.io/), certificate by [SignPath Foundation](https://signpath.org/).

- Committers and reviewers: [Thekeq](https://github.com/Thekeq)
- Approvers: [Thekeq](https://github.com/Thekeq)

See [Privacy](#privacy) for what the app sends over the network.

## License

[MIT](LICENSE)
