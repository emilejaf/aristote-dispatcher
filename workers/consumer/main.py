import asyncio
import logging
import signal

from probes import prober
from rpc_server import rpc_server
from settings import settings
from metrics import wait_for_metrics


shutdown_signal = asyncio.Event()


async def main_consumer():

    await wait_for_metrics()

    await rpc_server.connect()

    # Consumer is running until shutdown signal is received
    # Until then, all action occurs in the on_message_callback
    # of the RPCServer class
    await shutdown_signal.wait()

    logging.debug("Closing RPC connection...")
    await rpc_server.close()
    logging.info("RPC disconnected")


if __name__ == "__main__":
    logging.info("Starting consumer")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown():
        logging.info("Shutting down consumer...")
        shutdown_signal.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown)

    if settings.USE_PROBES:
        loop.run_until_complete(prober.setup())

    try:
        loop.run_until_complete(main_consumer())
    except Exception as e:
        logging.fatal(f"Consumer fatal error: {e}")
        raise
    finally:
        if settings.USE_PROBES:
            loop.run_until_complete(prober.cleanup())
        loop.close()
