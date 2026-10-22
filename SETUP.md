# Setup Guide

## Quick Start

1. **Fork/Clone** this repo.
2. Install dependencies: `pip install -r requirements.txt`
3. Copy `.env.example` → `.env` and fill in your values.
4. Run: `python main.py`

## Heroku Deploy

1. Create a new Heroku app.
2. Add buildpacks: `heroku/python` and `heroku-community/ffmpeg`.
3. Set Config Vars from `.env.example`.
4. Deploy.

## Termux (Android)

Run `bash start-termux.sh`.
