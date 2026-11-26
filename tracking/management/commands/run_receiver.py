import asyncio
import logging

from django.conf import settings
from django.core.management.base import BaseCommand

from tracking.receiver import Receiver


class Command(BaseCommand):
    help = "Run the Teltonika (Codec 8 / 8E) asyncio TCP receiver."

    def add_arguments(self, parser):
        parser.add_argument("--host", default=settings.FLEET["RECEIVER_BIND_HOST"])
        parser.add_argument("--port", type=int, default=settings.FLEET["RECEIVER_PORT"])
        parser.add_argument("--name", default="receiver")

    def handle(self, *args, **opts):
        logging.getLogger("fleet").setLevel(logging.INFO)
        rx = Receiver(opts["host"], opts["port"], opts["name"])
        self.stdout.write(self.style.SUCCESS(f"Starting receiver on {opts['host']}:{opts['port']} (Ctrl+C to stop)"))
        try:
            asyncio.run(rx.serve_forever())
        except KeyboardInterrupt:
            self.stdout.write("Receiver stopped.")
