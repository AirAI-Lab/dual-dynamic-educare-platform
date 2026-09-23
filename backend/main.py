"""平台启动入口。"""
from __future__ import annotations

import argparse

from .web import run


def main() -> None:
    parser = argparse.ArgumentParser(description="双动赋能双驱育心教学评估平台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    run(args.host, args.port)


if __name__ == "__main__":
    main()
