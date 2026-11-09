import json
import os
import sys
from http.server import BaseHTTPRequestHandler

# refinderbot.py лежит в корне репозитория
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import telebot  # noqa: E402
import refinderbot  # noqa: E402  (регистрирует все обработчики)

WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET')


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if WEBHOOK_SECRET and self.headers.get('X-Telegram-Bot-Api-Secret-Token') != WEBHOOK_SECRET:
            self.send_response(403)
            self.end_headers()
            return
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode('utf-8')
        try:
            update = telebot.types.Update.de_json(json.loads(body))
            refinderbot.bot.process_new_updates([update])
        except Exception as e:  # не даём Telegram бесконечно повторять битый update
            refinderbot.logging.error(f'Ошибка обработки update: {e}')
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'ok')

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'REFinder bot is running')
