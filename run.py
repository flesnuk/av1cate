"""
av1cate — Entry point.

Starts the FastAPI server using uvicorn.
Usage:
    python run.py [--host HOST] [--port PORT] [--reload]
"""
import sys
import uvicorn

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Start the av1cate API server.")
    parser.add_argument("--host",   default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port",   default=8000, type=int, help="Port (default: 8000)")
    parser.add_argument("--reload", action="store_true",    help="Enable auto-reload (dev mode)")
    args = parser.parse_args()

    print(f"  ▶  av1cate API  →  http://{args.host}:{args.port}")
    uvicorn.run(
        "api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
