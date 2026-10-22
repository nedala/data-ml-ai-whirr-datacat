"""python -m bsp_harness  → uvicorn on 0.0.0.0:${HARNESS_PORT:-8096} (routes under /ai)."""
import logging
import os

import uvicorn


def main() -> None:
    logging.basicConfig(level=os.environ.get("HARNESS_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run("bsp_harness.app:app", host=os.environ.get("HARNESS_HOST", "0.0.0.0"),
                # proxy_headers off: identity.py needs the real TCP peer to trust X-Auth-User only from nginx
                port=int(os.environ.get("HARNESS_PORT", "8096")), proxy_headers=False,
                log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
