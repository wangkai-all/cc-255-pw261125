#!/usr/bin/env python3
"""启动入口：python run.py [port]"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server.app import serve  # noqa: E402

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    serve(port=port)
