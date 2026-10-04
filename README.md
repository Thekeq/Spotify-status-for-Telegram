# Spotify → Telegram bio

Показывает трек, который сейчас играет в Spotify, первой строкой в описании (bio) Telegram:

```
Слушает Spotify: Nervy - Зацепило
ChatBot Developer | Founder @nzdiary_bot
```

Музыка на паузе или программа остановлена — возвращается обычное bio.

## Установка

Скачай `SpotifyBio.exe` из [Releases](../../releases) и положи в отдельную папку (там будут храниться настройки и сессия).

Или из исходников:

```
pip install -r requirements.txt
python app.py
```

## Настройка (один раз)

**Telegram API ID и Hash**
1. Зайди на https://my.telegram.org → *API development tools*.
2. Создай приложение (название любое), скопируй `api_id` и `api_hash`.

**Spotify Client ID**
1. Зайди на https://developer.spotify.com/dashboard → *Create app*.
2. В *Redirect URIs* добавь `http://127.0.0.1:8888/callback`, в *APIs used* отметь *Web API*.
3. Скопируй *Client ID* (Client Secret не нужен).

Вставь всё в окно программы → **Запустить**.
При первом запуске откроется браузер для входа в Spotify, потом программа спросит номер телефона и код из Telegram.

Поле «Обычное bio» можно оставить пустым — оно возьмётся из профиля.

## Ограничения

- Лимит bio: 70 символов, с Telegram Premium — 140. Длинные названия обрезаются.
- Таймер трека не показывается: Telegram не даёт менять профиль чаще.
- Работает, пока открыта программа.

## Безопасность

`config.json` и `tg_session.session` лежат рядом с программой. **Файл сессии даёт полный доступ к твоему Telegram-аккаунту** — никому его не передавай. Программа ничего никуда не отправляет, кроме API Telegram и Spotify.
