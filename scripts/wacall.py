#!/usr/bin/env python3
"""wacall — WA-call-claw CLI for outgoing WhatsApp calls.

Places a one-way outbound call: dial a number, wait for the peer to answer,
play an audio announcement (WAV 16-bit PCM, or MP3/other — auto-converted
via ffmpeg), then hang up automatically.

Usage:
  wacall call <phone> --audio <file.wav> [--delay <sec>] [--bridge <url>]
  wacall allowlist                     # show configured allowlist (via /api/call reject hint)

Examples:
  wacall call 6281234567890 --audio pesan.wav
  wacall call 6281234567890 --audio pesan.wav --delay 3

Exit codes:
  0  call accepted (or help/version)
  1  call rejected (validation, rate limit, allowlist)
  2  connection/usage error
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

DEFAULT_BRIDGE = "http://127.0.0.1:9090"
PHONE_RE = re.compile(r"^[1-9][0-9]{6,14}$")
TMP_DIR = "/opt/wa-call-claw/TMP"


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def ensure_pcm_wav(path):
    """Return a 16-bit PCM WAV path for `path`.

    RIFF/WAVE files are used as-is; anything else (MP3, OGG, M4A, ...) is
    converted via ffmpeg to 48 kHz mono s16le WAV so the bridge can play it.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError as e:
        eprint(f"error: cannot read audio file {path}: {e}")
        raise SystemExit(1)

    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return path

    out_dir = TMP_DIR if os.path.isdir(TMP_DIR) else tempfile.gettempdir()
    fd, out = tempfile.mkstemp(suffix=".wav", prefix="wacall_", dir=out_dir)
    os.close(fd)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", path,
             "-ar", "48000", "-ac", "1", "-sample_fmt", "s16", out],
            check=True, capture_output=True, text=True,
        )
    except FileNotFoundError:
        eprint("error: ffmpeg not found — install it or provide a 16-bit PCM WAV file")
        raise SystemExit(1)
    except subprocess.CalledProcessError as e:
        eprint(f"error: ffmpeg conversion failed: {e.stderr.strip()}")
        try:
            os.unlink(out)
        except OSError:
            pass
        raise SystemExit(1)
    return out


def post_json(url, payload):
    """POST a JSON payload, returning (status_code, parsed_json)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"status": "rejected", "reason": body.strip() or f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        eprint(f"error: cannot reach bridge at {url}: {e.reason}")
        raise SystemExit(2)


def cmd_call(args):
    phone = args.phone
    if not PHONE_RE.match(phone):
        eprint(f"error: invalid phone format {phone!r} (need E.164 without +, 7-15 digits)")
        return 1

    audio = args.audio
    if not os.path.isfile(audio):
        eprint(f"error: audio file not found: {audio}")
        return 1

    converted = False
    audio = ensure_pcm_wav(audio)
    if audio != args.audio:
        converted = True

    bridge = args.bridge.rstrip("/")
    payload = {
        "type": "outgoing_call",
        "phone": phone,
        "audio": audio,
        "delay_ms": args.delay * 1000 if args.delay else 0,
    }

    status, ack = post_json(f"{bridge}/api/call", payload)
    if ack.get("status") == "accepted":
        print(f"call accepted: id={ack.get('call_id')} phone={phone}")
        print(f"  audio: {audio}" + (" (converted from MP3 via ffmpeg)" if converted else ""))
        print(f"  delay: {args.delay}s" if args.delay else "  delay: (config default)")
        print("  event: will be written to the spool when the call ends")
        return 0

    reason = ack.get("reason", f"HTTP {status}")
    print(f"call rejected: {reason}")
    if "allowlist" in reason:
        print("hint: add the number to outgoing.allowlist in config.yaml")
    elif "rate limit" in reason:
        print("hint: wait for the hourly window to reset, or raise outgoing.max_calls_per_hour")
    return 1


def build_parser():
    ap = argparse.ArgumentParser(
        prog="wacall",
        description="Place one-way outgoing WhatsApp calls (dial → play WAV → hang up).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples:")[1] if "Examples:" in __doc__ else None,
    )
    ap.add_argument("--bridge", default=DEFAULT_BRIDGE, help=f"bridge base URL (default: {DEFAULT_BRIDGE})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_call = sub.add_parser("call", help="place an outgoing call")
    p_call.add_argument("phone", help="target phone, E.164 without + (e.g. 6281234567890)")
    p_call.add_argument("--audio", required=True, help="audio file to play (16-bit PCM WAV, or MP3/OGG/M4A — auto-converted via ffmpeg)")
    p_call.add_argument("--delay", type=int, default=0, help="silence before playback in seconds (0 = config default)")
    p_call.add_argument("--bridge", default=None, help="bridge base URL (default: %(default)s)")
    p_call.set_defaults(fn=cmd_call)

    return ap


def main():
    args = build_parser().parse_args()
    if getattr(args, "bridge", None) is None:
        args.bridge = DEFAULT_BRIDGE
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        eprint("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
