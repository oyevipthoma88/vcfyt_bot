#!/data/data/com.termux/files/usr/bin/bash
set -e
pkg update -y
pkg install -y python git
pip install --upgrade pip
pip install -r requirements.txt
python run.py
