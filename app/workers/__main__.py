"""Entrypoint for `python -m app.workers`."""

import asyncio

from app.workers import main

if __name__ == "__main__":
    asyncio.run(main())