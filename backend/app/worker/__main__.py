"""Worker entry point: ``python -m app.worker``."""

from __future__ import annotations

import asyncio
import signal

from app.container import Container
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.worker.handlers import HANDLERS
from app.worker.runner import Worker


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    container = Container(settings)
    await container.graph.ensure_schema()
    await container.vectors.ensure_collection()
    _ = container.embeddings  # load the model once, before taking jobs
    worker = Worker(container, HANDLERS)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:  # Windows: rely on KeyboardInterrupt
            pass
    try:
        await worker.run()
    finally:
        await container.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
