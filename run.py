"""Start the local server and open the scan page in your browser.

    python run.py                 # scan page at http://127.0.0.1:8765/
    python run.py --demo          # open the review demo (/demo) instead
    python run.py --rules-only    # never call the AI judge, even with an API key in .env
    python run.py --port 9000 --no-browser

Stop with Ctrl+C. The AI judge is off unless .env holds ANTHROPIC_API_KEY (see .env.example and README.md).
"""
import argparse
import os
import socket
import threading
import time
import webbrowser


def port_free(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((host, port)) != 0


def pick_port(host, wanted):
    if port_free(host, wanted):
        return wanted
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def open_when_ready(host, port, url, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if not port_free(host, port):
            webbrowser.open(url)
            return
        time.sleep(0.3)


def main():
    ap = argparse.ArgumentParser(description="Start the compliance checking tool and open it in the browser.")
    ap.add_argument("--port", type=int, default=8765, help="port to listen on (another free one is used if taken)")
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on (default: this computer only)")
    ap.add_argument("--demo", action="store_true", help="open the review demo page instead of the scan page")
    ap.add_argument("--rules-only", action="store_true", help="run the rules only, never the AI judge")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    args = ap.parse_args()

    root = os.path.dirname(os.path.abspath(__file__))
    try:  # ANTHROPIC_API_KEY and other settings from .env
        from dotenv import load_dotenv
        load_dotenv(os.path.join(root, ".env"))
    except ImportError:
        pass
    if args.rules_only:
        os.environ["LLM_JUDGE"] = "off"
    from app.llm_judge import status
    mode = status()["message"]

    port = pick_port(args.host, args.port)
    url = f"http://{args.host}:{port}/" + ("demo" if args.demo else "")
    print(f"Compliance checking tool: {url}  Stop with Ctrl+C.", flush=True)
    print(mode, flush=True)
    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(args.host, port, url), daemon=True).start()

    import uvicorn
    uvicorn.run("app.server:app", host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
