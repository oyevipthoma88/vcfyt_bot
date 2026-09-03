# Android Chrome → VPS → Telegram VC Live Mic

This mode sends microphone PCM from Android Chrome to the VPS. The bot's logged-in userbot reads that stream, applies the live-microphone DSP chain, and publishes it into a Telegram voice chat. The voice appears from the userbot account, not from the personal Telegram Web account.

## 1. VPS package setup

```bash
sudo apt update
sudo apt install -y python3-venv nginx certbot python3-certbot-nginx ffmpeg
cd /opt
sudo git clone https://github.com/oyevipthoma88/vcfyt_bot.git vcfyt_bot
sudo chown -R "$USER":"$USER" /opt/vcfyt_bot
cd /opt/vcfyt_bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

If the bot is already installed on the VPS, update it with `git pull` instead of cloning a second copy. Keep one process using the userbot session.

## 2. Environment variables

Add these variables to the bot's existing environment file. Do not commit the token.

```env
MIC_RELAY_ENABLED=true
MIC_RELAY_FIFO=/tmp/apex_live_mic.pcm
MIC_RELAY_BIND=127.0.0.1
MIC_RELAY_PORT=8765
MIC_RELAY_TOKEN=generate-a-long-random-secret-here
MIC_DSP=true
```

Generate a token with:

```bash
openssl rand -hex 32
```

Keep `MIC_RELAY_TOKEN` identical in the bot service and relay service.

## 3. Start the relay service

Create `/etc/systemd/system/apex-mic-relay.service`:

```ini
[Unit]
Description=Apex VC Fyt Android microphone relay
After=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/vcfyt_bot
EnvironmentFile=/opt/vcfyt_bot/.env
ExecStart=/opt/vcfyt_bot/.venv/bin/python /opt/vcfyt_bot/live_relay.py
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=false

[Install]
WantedBy=multi-user.target
```

Change `User=ubuntu` and paths if the bot runs under another Linux user. Then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now apex-mic-relay
sudo journalctl -u apex-mic-relay -f
```

## 4. HTTPS reverse proxy

Browser microphone access requires HTTPS. Point a domain such as `mic.example.com` to the VPS IP, then create `/etc/nginx/sites-available/apex-mic`:

```nginx
server {
    listen 80;
    server_name mic.example.com;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600;
    }
}
```

Enable TLS:

```bash
sudo ln -s /etc/nginx/sites-available/apex-mic /etc/nginx/sites-enabled/apex-mic
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d mic.example.com
```

The private page URL is:

```text
https://mic.example.com/mic?token=YOUR_MIC_RELAY_TOKEN
```

Do not post this URL publicly. Anyone possessing the token could send audio to the relay.

## 5. Telegram use sequence

1. Ensure the bot's logged-in userbot account has **Manage Voice Chats** permission.
2. In Telegram Web, start or join the target voice chat.
3. Keep the personal Telegram Web microphone muted to avoid duplicate audio.
4. In the same Android Chrome, open the private `/mic?token=...` URL.
5. Tap **Start Live Mic** and allow microphone permission.
6. In Telegram, send `/mic on` to the bot or use the VC microphone control.
7. Speak into the Android phone. The userbot account publishes the processed voice.
8. Stop with `/mic off` if that command exists in the deployed branch, or leave/stop the mic stream from the page and stop the userbot mic stream.

## 6. Troubleshooting

Use these commands:

```bash
systemctl status apex-mic-relay
journalctl -u apex-mic-relay -n 100 --no-pager
systemctl status apex-vcfyt-bot
ls -l /tmp/apex_live_mic.pcm
```

If `/mic on` waits, open the Android page and press **Start Live Mic** after the bot has started the mic stream. If the browser reports a microphone error, use HTTPS and grant Chrome microphone permission. If the voice is duplicated, mute Telegram Web's own microphone. If the voice is still quiet, verify `MIC_DSP=true` and that the userbot account is the participant being volume-boosted.

## Security

Use HTTPS, a long random token, and a reverse proxy. Do not bind the relay publicly unless necessary. Rotate the token if the URL is shared. The relay is intentionally limited to one FIFO and should not be exposed without authentication.
