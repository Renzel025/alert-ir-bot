"""Print the last 20 messages in ALERT_CHAT_ID with their sender ids, so you can
copy the MonitorFlow bot's id into ALERT_BOT_SENDER_ID.

    python -m scripts.find_sender
"""
from app import alert_parser, config, lark_client

for item in lark_client.list_messages(config.ALERT_CHAT_ID, 20):
    sender = item.get("sender") or {}
    text = alert_parser.extract_text(item.get("msg_type", ""), (item.get("body") or {}).get("content", ""))
    print(f"{item.get('message_id')}  sender={sender.get('id')} ({sender.get('sender_type')})  {text[:70]!r}")
