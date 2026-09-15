"""Launcher for BANALL bot."""
import runpy
import termux_env
termux_env.bootstrap()
runpy.run_path("bot.py", run_name="__main__")
