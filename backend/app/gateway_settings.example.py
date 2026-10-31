"""Template for the default LLM. Copy to gateway_settings.py and fill in your own values.

    copy backend\\app\\gateway_settings.example.py backend\\app\\gateway_settings.py

gateway_settings.py is git-ignored. Any OpenAI-compatible server works, because the
app posts to URL + "/v1/chat/completions". GATEWAY_URL, GATEWAY_TOKEN and GATEWAY_MODEL
environment variables override these values.
"""
URL = "https://api.openai.com"
TOKEN = ""
MODEL = "gpt-4o-mini"
