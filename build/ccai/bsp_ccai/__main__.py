"""python -m bsp_ccai  →  uvicorn on :8100 (routes served natively under /ccai)."""

import os

import uvicorn


def main() -> None:
    uvicorn.run("bsp_ccai.app:app", host="0.0.0.0", port=int(os.environ.get("CCAI_PORT", "8100")),
                proxy_headers=True, forwarded_allow_ips="*", log_level=os.environ.get("CCAI_LOG_LEVEL", "info"))


if __name__ == "__main__":
    main()
