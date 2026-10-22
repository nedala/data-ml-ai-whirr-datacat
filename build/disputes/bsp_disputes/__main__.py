"""python -m bsp_disputes [poll|submit [mode]|serve]"""
import json
import sys


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "poll":
        from .db import init
        from .pipeline import poll
        init()
        print(json.dumps(poll(), indent=2, default=str))
    elif cmd == "submit":
        from .submit import run
        print(json.dumps(run(mode=sys.argv[2] if len(sys.argv) > 2 else None, concurrency=None), indent=2, default=str))
    else:
        import uvicorn
        uvicorn.run("bsp_disputes.app:app", host="0.0.0.0", port=8099, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
