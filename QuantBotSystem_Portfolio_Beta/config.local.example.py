# File name: config.local.example.py
# Documents local-only settings that must not be committed.
# The root version selector normally points Portfolio Beta at
# QuantBotSystem_Beta/config.local.py. Admin shows the effective private path.

BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
ALLOWED_CHAT_IDS = {"YOUR_CHAT_ID"}

# Exact MSCI ACWI (MIWD00000PUS) benchmark source. Choose at most one.
# With neither setting, Admin -> Refresh Benchmark reads:
# QuantBotSystem_Portfolio_Beta/data/imports/msci_acwi.csv
# MSCI_ACWI_SOURCE_PATH = r"C:\path\to\legally_obtained_msci_acwi.csv"
# MSCI_ACWI_SOURCE_URL = "https://your-authorized-endpoint.example/acwi.csv"
# MSCI_ACWI_SOURCE_HEADERS = {"Authorization": "Bearer YOUR_LOCAL_SECRET"}
