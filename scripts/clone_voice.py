#!/usr/bin/env python3
"""
Enroll a voice for CosyVoice v3.5-plus TTS.

CosyVoice v3.5-plus has NO system voices — you must clone/design one first.
This script:
  1. Records (or accepts) a 10-30 second clean speech WAV (16kHz/24kHz mono)
  2. Uploads it to a public URL (uses 0x0.st — free, no auth)
  3. Calls Bailian VoiceEnrollmentService to register it
  4. Polls until ready, returns the voice_id

Usage:
  uv run python scripts/clone_voice.py path/to/reference.wav
  uv run python scripts/clone_voice.py --record 10   # record 10s via Mac
  uv run python scripts/clone_voice.py --say "请讲一下您的车牌号"  # generate via Mac `say`

After getting voice_id, set in .env:
  BAILIAN_TTS_VOICE=<voice_id>
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

# Allow running as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import get_settings  # noqa: E402


def record_audio_via_say(text: str, out_path: Path) -> Path:
    """Use Mac's `say` to generate a clean reference audio."""
    aiff = out_path.with_suffix(".aiff")
    print(f"🔊 Generating audio via `say -v Tingting`: {text!r}")
    subprocess.run(["say", "-v", "Tingting", "-o", str(aiff), text], check=True)
    # Convert AIFF → WAV 16kHz mono s16le
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(aiff),
        "-ar", "16000", "-ac", "1", "-f", "wav",
        str(out_path),
    ], check=True)
    aiff.unlink(missing_ok=True)
    print(f"💾 Wrote {out_path} ({out_path.stat().st_size} bytes)")
    return out_path


def record_audio_via_mic(seconds: int, out_path: Path) -> Path:
    """Record from the default mic using `rec` (sox). Falls back to `arecord`."""
    if subprocess.run(["which", "rec"], capture_output=True).returncode == 0:
        print(f"🎤 Recording {seconds}s from default mic via `rec`...")
        subprocess.run([
            "rec", "-q", "-r", "16000", "-c", "1", out_path,
            "trim", "0", str(seconds),
        ], check=True)
    else:
        raise RuntimeError("No `rec` (sox) found. Install with `brew install sox` or pass an audio file.")
    print(f"💾 Wrote {out_path} ({out_path.stat().st_size} bytes)")
    return out_path


def upload_to_public_url(local_path: Path) -> str:
    """Return a publicly-fetchable URL for the audio file.

    Order:
      1. Reuse the existing cloudflared tunnel if available (zero upload,
         reaches Bailian in ~1-2 s from inside China via TCP route).
         Bailian's enrollment worker is in Aliyun Beijing which has a
         fast peer to the local cloudflared tunnel.
      2. Fall back to 0x0.st (slow from China; only used in CI / dev).
    """
    tunnel_url = os.environ.get("CLONE_AUDIO_URL", "").strip()
    if not tunnel_url:
        cf = Path("/tmp/cloudflared.url")
        if cf.exists():
            base = cf.read_text().strip()
            if base:
                tunnel_url = f"{base}/api/clone/audio"

    if tunnel_url:
        # Trust the tunnel URL without HEAD-checking (HEAD often fails on
        # CDN-cached responses, but GET works). We try a 1-byte GET to
        # confirm reachability with a 10 s budget.
        try:
            r = subprocess.run(
                ["curl", "-fsS", "-m", "10", "--range", "0-3", tunnel_url],
                capture_output=True, timeout=15,
            )
            if r.returncode == 0 and r.stdout[:4] == b"RIFF":
                print(f"🔗 Using tunnel URL: {tunnel_url} (verified)")
                return tunnel_url
            print(f"⚠️  Tunnel URL returned code {r.returncode} or non-RIFF body; falling back to 0x0.st")
        except Exception as e:
            print(f"⚠️  Tunnel URL check failed: {e}; falling back to 0x0.st")

    print(f"☁️  Uploading {local_path.name} to 0x0.st...")
    proc = subprocess.run(
        ["curl", "-fsSL", "-F", f"file=@{local_path}", "https://0x0.st"],
        capture_output=True, text=True, check=True,
    )
    url = proc.stdout.strip()
    if not url.startswith("http"):
        raise RuntimeError(f"Upload failed: {url!r}")
    print(f"🔗 Public URL: {url}")
    return url


async def enroll_voice(audio_url: str, prefix: str = "user") -> str:
    """Call Bailian VoiceEnrollmentService and poll for the voice_id.

    `audio_url` may be either:
      - a `https://...` URL Bailian can fetch, or
      - an `oss://<bucket>/<key>` URI returned by `dashscope.OssUtils.upload`.
    The `oss://` form is the most reliable path because it bypasses the
    public-URL reachability check entirely (Bailian can always read its
    own OSS buckets).
    """
    import httpx

    settings = get_settings()
    headers = {
        "Authorization": f"Bearer {settings.dashscope_api_key}",
        "Content-Type": "application/json",
    }
    base = "https://dashscope.aliyuncs.com/api/v1/services/audio/voice"

    # Step 1: create the enrollment
    print("📝 Creating voice enrollment (target=cosyvoice-v3.5-plus)...")
    # NB: per Aliyun docs, the `model` field for the enrollment service
    # must be the literal string "voice-enrollment" (NOT the target TTS
    # model). `target_model` carries the actual TTS model name. Setting
    # `model` to a TTS model name returns HTTP 400 with code
    # "InvalidParameter" and message "url error, please check url!" —
    # the URL itself is fine; the request is just routed to the wrong
    # service and rejected.
    payload = {
        "model": "voice-enrollment",
        "target_model": "cosyvoice-v3.5-plus",
        "prefix": prefix,
        "url": audio_url,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{base}/enrollment", headers=headers, json=payload)
        if r.status_code != 200:
            raise RuntimeError(f"Enrollment HTTP {r.status_code}: {r.text[:500]}")
        body = r.json()
    voice_id = body.get("output", {}).get("voice_id") or body.get("voice_id")
    if not voice_id:
        raise RuntimeError(f"No voice_id in response: {body!r}")
    print(f"🆔 voice_id: {voice_id}")
    print("⏳ Polling for ready (typically 30-60s)...")

    # Step 2: poll until status=OK
    for i in range(60):  # up to 5 minutes
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{base}/enrollment/{voice_id}",
                headers=headers,
            )
            if r.status_code != 200:
                await asyncio.sleep(5)
                continue
            body = r.json()
        status = (body.get("output") or {}).get("status") or body.get("status")
        print(f"   [{i+1}/60] status = {status}")
        if status == "OK":
            print(f"\n✅ voice ready: {voice_id}")
            print(f"\nNext: add to your .env:")
            print(f"  BAILIAN_TTS_VOICE={voice_id}")
            return voice_id
        if status in ("FAILED", "ERROR"):
            raise RuntimeError(f"Voice enrollment failed: {body!r}")
        await asyncio.sleep(5)
    raise RuntimeError("Voice enrollment did not complete within 5 minutes")


def upload_via_dashscope_oss(local_path: Path) -> str:
    """Upload a local file to Aliyun OSS via the DashScope SDK, return
    a public HTTPS URL. Bailian's voice enrollment service fetches the
    audio from the URL, so we need the HTTPS form (not the `oss://`
    internal URI) — the `oss://` form is rejected with "url error".
    """
    import dashscope
    from dashscope.utils.oss_utils import OssUtils

    settings = get_settings()
    dashscope.api_key = settings.dashscope_api_key
    print(f"☁️  Uploading {local_path.name} to DashScope OSS (purpose=voice-enrollment)...")
    uri = OssUtils.upload(
        model="voice-enrollment",
        file_path=str(local_path),
        api_key=settings.dashscope_api_key,
    )
    print(f"🔗 Internal URI: {uri}")
    # Convert oss://<bucket>/<key> → https://<bucket>.<region>.aliyuncs.com/<key>
    # DashScope uploads land in the `dashscope-instant` bucket in Beijing
    # (oss-cn-beijing). For other regions the path may need a different
    # endpoint; if 404, try oss-cn-hangzhou, oss-cn-shanghai, etc.
    if uri.startswith("oss://"):
        without_scheme = uri[len("oss://"):]
        bucket, _, key = without_scheme.partition("/")
        # Try the most common region first; iterate if 404
        for region in ("oss-cn-beijing", "oss-cn-hangzhou", "oss-cn-shanghai"):
            url = f"https://{bucket}.{region}.aliyuncs.com/{key}"
            # Verify the URL serves the file
            rc = subprocess.run(
                ["curl", "-fsSI", "-m", "10", url],
                capture_output=True, text=True,
            )
            if rc.returncode == 0 and "200" in rc.stdout:
                print(f"🔗 Public URL: {url}")
                return url
        # Fall back to the Beijing URL even if we couldn't verify (some
        # OSS buckets have signed-URL requirements — Bailian's fetcher
        # has its own credentials).
        url = f"https://{bucket}.oss-cn-beijing.aliyuncs.com/{key}"
        print(f"🔗 Public URL (unverified): {url}")
        return url
    return uri


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("audio", nargs="?", help="Path to reference audio file (WAV/MP3)")
    ap.add_argument("--record", type=int, metavar="SECS", help="Record SECS seconds from default mic")
    ap.add_argument("--say", metavar="TEXT", help="Generate audio using Mac's `say` command (Tingting voice)")
    ap.add_argument("--prefix", default="user", help="Voice prefix (default: user)")
    args = ap.parse_args()

    if not (args.audio or args.record or args.say):
        ap.error("Provide an audio file, --record SECS, or --say TEXT")

    out_path = Path("/tmp/voice_clone_ref.wav")

    if args.say:
        record_audio_via_say(args.say, out_path)
    elif args.record:
        record_audio_via_mic(args.record, out_path)
    else:
        out_path = Path(args.audio).expanduser().resolve()
        if not out_path.exists():
            print(f"❌ File not found: {out_path}", file=sys.stderr)
            sys.exit(1)

    url = upload_via_dashscope_oss(out_path)
    voice_id = await enroll_voice(url, prefix=args.prefix)
    print(f"\n🎉 DONE. voice_id = {voice_id}")


if __name__ == "__main__":
    asyncio.run(main())
