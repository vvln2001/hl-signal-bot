#!/usr/bin/env python3
"""Print the chat id(s) of whoever messaged your bot.

1. Send any message to your bot in Telegram (or add it to a group and send a message there).
2. TELEGRAM_BOT_TOKEN=xxx python get_chat_id.py     (or put the token in .env)
Standard library only.
"""
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def token() -> str:
    t = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    env = os.path.join(HERE, ".env")
    if not t and os.path.exists(env):
        for line in open(env, encoding="utf-8"):
            if line.strip().startswith("TELEGRAM_BOT_TOKEN="):
                t = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not t and len(sys.argv) > 1:
        t = sys.argv[1]
    return t


def main() -> None:
    t = token()
    if not t:
        sys.exit("缺少 TELEGRAM_BOT_TOKEN（环境变量、.env 或命令行参数）")
    with urllib.request.urlopen(f"https://api.telegram.org/bot{t}/getUpdates", timeout=20) as r:
        data = json.load(r)
    if not data.get("ok"):
        sys.exit(f"Telegram 返回错误: {data}")
    seen = {}
    for u in data.get("result", []):
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            seen[chat["id"]] = (chat.get("type"), chat.get("title") or chat.get("username") or chat.get("first_name"))
    if not seen:
        print("没有找到消息。请先在 Telegram 里给你的机器人发一条消息（例如 /start），再运行本脚本。")
        return
    for cid, (typ, name) in seen.items():
        print(f"chat_id = {cid}   ({typ}: {name})")
    print("\n把它设为环境变量 TELEGRAM_CHAT_ID")


if __name__ == "__main__":
    main()
