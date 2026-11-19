"""
config.py - reads your secret settings from the .env file.

Why: tokens are like passwords. Keeping them in .env (not in the code)
means you can share or upload your code without leaking them.
"""
import os

from dotenv import load_dotenv

load_dotenv()  # reads the .env file and puts its values into the environment

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v25.0")

# SIMULATE=true -> replies are NOT sent to Meta; they are saved locally so
# simulate.py can show them in your terminal. Use this while WhatsApp is locked.
SIMULATE = os.getenv("SIMULATE", "false").lower() == "true"

# Stop early with a clear message if something is missing,
# instead of failing later with a confusing error.
# (In simulate mode we don't talk to Meta, so tokens are not needed.)
if not SIMULATE:
    _required = {
        "WHATSAPP_TOKEN": WHATSAPP_TOKEN,
        "PHONE_NUMBER_ID": PHONE_NUMBER_ID,
        "VERIFY_TOKEN": VERIFY_TOKEN,
    }
    _missing = [name for name, value in _required.items() if not value]
    if _missing:
        raise RuntimeError(f"Missing in .env file: {', '.join(_missing)}")
