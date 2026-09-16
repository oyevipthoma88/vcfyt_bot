# TERMUX SETUP

**First time:**

```
pkg update -y && pkg install -y python git
git clone https://github.com/oyevipthoma88/Banall-.git
cd Banall-
pip install -r requirements.txt
python run.py
```

Pehli baar chalane par wizard puchhega:
- `API_ID`, `API_HASH` (my.telegram.org)
- `BOT_TOKEN` (@BotFather)
- `OWNER_ID`, `OWNER_USERNAME`, `TUTORIAL_URL` (optional)

Values `.env` me save ho jati hain.

**Baad me:**

```
cd ~/Banall- && python run.py
```

**Update:**

```
cd ~/Banall- && git pull && python run.py
```

## Group me kaise setup kare

1. Bot ko group me **add** karo.
2. Group settings -> Administrators -> bot ko **admin** banao.
3. **Ban Users** permission ON rakho (baaki off bhi chalega).
4. Koi bhi admin (ban rights waala) `/banall` bheje. Bas.

## Commands

- `/banall` — sab non-admin members + bots ban
- `/kickall` — sab non-admin members + bots kick
- `/stop` — abort
- `/alive` — status
