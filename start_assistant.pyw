#! python3.14
import ctypes
from pathlib import Path
from runpy import run_path
from traceback import format_exc


if __name__ == "__main__":
    try:
        run_path(str(Path(__file__).with_name("assistant.py")), run_name="__main__")
    except Exception:
        details = format_exc()
        ctypes.windll.user32.MessageBoxW(None, details, "选调助手启动失败", 0x10)
