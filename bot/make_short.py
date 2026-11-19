"""
AI Shorts Bot - makes one YouTube Short about AI, privacy and digital footprints.

Pipeline:
  1. Gemini writes topic + script + title + description + tags + stock-footage keywords
  2. Edge neural voice reads the script (free), with word timings for captions
  3. Pexels gives vertical stock clips (free)
  4. ffmpeg builds a 1080x1920 Short with burned-in captions
  5. The video is published as a GitHub release asset and sent to the Make webhook,
     which uploads it to YouTube.

Run locally without any keys:  python bot/make_short.py --dry-run
"""

import argparse
import asyncio
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
HISTORY = ROOT / "history.json"
W, H = 1080, 1920
VOICES = ["en-US-AndrewNeural", "en-US-ChristopherNeural", "en-US-GuyNeural", "en-US-AriaNeural"]
FONT = os.environ.get("CAPTION_FONT", "DejaVu Sans")

THEMES = [
    "what AI can already do that most people don't know",
    "how far AI could go in the next 5-10 years",
    "losing control: AI making decisions about people",
    "how much your phone and apps know about you",
    "your digital footprint and how it is built",
    "data brokers and who buys your personal data",
    "deepfakes and AI voice cloning",
    "face recognition and surveillance in public spaces",
    "how AI recommendation algorithms shape what you see",
    "simple steps to protect your privacy from AI",
    "AI at work: jobs, monitoring and automation",
    "smart home devices and what they record",
    "how AI chatbots use the data you type",
    "location tracking and what it reveals about you",
]


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def load_history():
    if HISTORY.exists():
        return json.loads(HISTORY.read_text())
    return []


# ---------------------------------------------------------------- 1. script
def gemini_script(history):
    key = os.environ["GEMINI_API_KEY"]
    recent = [h["title"] for h in history[-60:]]
    theme = random.choice(THEMES)
    prompt = f"""You write viral, factual YouTube Shorts for a channel about AI and privacy.
Theme for this Short: {theme}

Rules:
- 110-140 words of narration (about 45 seconds spoken).
- First sentence is a scroll-stopping hook (a surprising fact or a question).
- Plain spoken English, short sentences, no emojis, no stage directions, no hashtags in the script.
- Only state facts that are true and widely documented. No invented statistics, no fake quotes,
  no conspiracy claims. It is fine to say "experts warn" or "could" for future predictions.
- End with one practical tip or a thought-provoking line, then "Follow for more."
- Do NOT repeat any of these recent titles or their exact angle: {json.dumps(recent)}

Return JSON with exactly these keys:
"title": under 70 characters, curiosity-driven, no clickbait lies,
"script": the narration,
"description": 2-3 sentences summarising the Short,
"tags": list of 8-12 lowercase tags,
"hashtags": list of 3-5 hashtags starting with #, always include #Shorts,
"footage": list of 6 short English search terms for vertical stock video that match the script in order
  (e.g. "smartphone at night", "security camera", "data center servers")."""
    models = gemini_models(key)
    log(f"Gemini models to try: {models}")
    last = None
    errors = []
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            r = requests.post(
                url,
                headers={"x-goog-api-key": key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json", "temperature": 1.0},
                },
                timeout=90,
            )
            if r.status_code == 200:
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                data = json.loads(text)
                data["theme"] = theme
                log(f"Script written with {model}: {data['title']}")
                return data
            last = f"{model}: {r.status_code} {r.text[:300]}"
            log(f"Gemini {last}")
            if r.status_code in (500, 503) or (r.status_code == 429 and attempt == 0):
                time.sleep(15 * (attempt + 1))
                continue
            break
        errors.append(last)
    raise RuntimeError("Gemini failed on every model:\n" + "\n".join(errors))


def gemini_models(key):
    """Ask Google which models this key can use and pick the best 'flash' text models,
    so the bot keeps working when Google retires old model names."""
    preferred = os.environ.get("GEMINI_MODEL")
    found = []
    try:
        r = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": key}, params={"pageSize": 200}, timeout=30,
        )
        r.raise_for_status()
        for m in r.json().get("models", []):
            name = m["name"].split("/", 1)[-1]
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            if "flash" not in name or any(x in name for x in ("image", "tts", "live", "audio", "embed", "thinking")):
                continue
            found.append(name)
    except Exception as e:
        log(f"Could not list Gemini models: {e}")

    def rank(name):
        nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", name)] or [0]
        return (
            "preview" in name or "exp" in name,  # stable first
            "lite" in name,                      # full flash before lite
            -nums[0],                            # newest version first
            name,
        )

    found.sort(key=rank)
    ordered = ([preferred] if preferred else []) + found + ["gemini-flash-latest", "gemini-flash-lite-latest"]
    return list(dict.fromkeys(ordered))[:6]


# ---------------------------------------------------------------- 2. voice
async def _tts(text, voice, mp3_path):
    import edge_tts

    comm = edge_tts.Communicate(text, voice, rate="+8%", boundary="WordBoundary")
    words = []
    with open(mp3_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7
                words.append({"w": chunk["text"], "s": start, "e": start + chunk["duration"] / 1e7})
    return words


def make_voice(text, mp3_path):
    voice = random.choice(VOICES)
    log(f"Voice: {voice}")
    for attempt in range(3):
        try:
            return asyncio.run(_tts(text, voice, mp3_path))
        except Exception as e:  # network hiccups
            log(f"TTS retry {attempt + 1}: {e}")
            time.sleep(5)
    raise RuntimeError("Voice generation failed")


def duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


# ---------------------------------------------------------------- 3. footage
def download(url, path):
    with requests.get(url, stream=True, timeout=120) as dl:
        dl.raise_for_status()
        with open(path, "wb") as f:
            for part in dl.iter_content(1 << 20):
                f.write(part)


def pixabay_clips(queries, workdir, need_seconds):
    key = os.environ["PIXABAY_API_KEY"]
    clips, used = [], set()
    total = 0.0
    for q in queries + ["technology", "city night", "smartphone", "data", "computer code"]:
        if total >= need_seconds + 2:
            break
        r = requests.get(
            "https://pixabay.com/api/videos/",
            params={"key": key, "q": q[:100], "per_page": 20, "safesearch": "true"},
            timeout=30,
        )
        if r.status_code != 200:
            log(f"Pixabay '{q}' -> {r.status_code}")
            continue
        hits = [h for h in r.json().get("hits", []) if h["id"] not in used and h.get("duration", 0) >= 4]
        if not hits:
            continue
        # prefer vertical clips, then anything (landscape gets centre-cropped)
        vertical = [h for h in hits if h["videos"]["medium"].get("height", 0) > h["videos"]["medium"].get("width", 1)]
        h = random.choice((vertical or hits)[:6])
        used.add(h["id"])
        vids = h["videos"]
        src = next((vids[s]["url"] for s in ("large", "medium", "small") if vids.get(s, {}).get("url")), None)
        if not src:
            continue
        path = workdir / f"clip{len(clips)}.mp4"
        download(src, path)
        clips.append(path)
        total += min(h["duration"], 8)
        log(f"Clip '{q}' ({h['duration']}s)")
    if not clips:
        raise RuntimeError("No Pixabay clips found")
    return clips


def stock_clips(queries, workdir, need_seconds):
    if os.environ.get("PIXABAY_API_KEY"):
        return pixabay_clips(queries, workdir, need_seconds)
    return pexels_clips(queries, workdir, need_seconds)


def pexels_clips(queries, workdir, need_seconds):
    key = os.environ["PEXELS_API_KEY"]
    clips, used = [], set()
    total = 0.0
    for q in queries + ["technology abstract", "city at night", "people using phones"]:
        if total >= need_seconds + 2:
            break
        r = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": key},
            params={"query": q, "orientation": "portrait", "per_page": 10, "size": "medium"},
            timeout=30,
        )
        if r.status_code != 200:
            log(f"Pexels '{q}' -> {r.status_code}")
            continue
        vids = [v for v in r.json().get("videos", []) if v["id"] not in used and v["duration"] >= 4]
        if not vids:
            continue
        v = random.choice(vids[:5])
        used.add(v["id"])
        files = [f for f in v["video_files"] if f.get("height") and f["height"] >= f.get("width", 0)]
        files = sorted(files or v["video_files"], key=lambda f: abs((f.get("height") or 0) - 1920))
        path = workdir / f"clip{len(clips)}.mp4"
        with requests.get(files[0]["link"], stream=True, timeout=120) as dl:
            dl.raise_for_status()
            with open(path, "wb") as f:
                for part in dl.iter_content(1 << 20):
                    f.write(part)
        clips.append(path)
        total += min(v["duration"], 8)
        log(f"Clip '{q}' ({v['duration']}s)")
    if not clips:
        raise RuntimeError("No Pexels clips found")
    return clips


# ---------------------------------------------------------------- 4. edit
def ass_time(t):
    h, rem = divmod(max(t, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def write_captions(words, ass_path):
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{FONT},86,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,7,3,5,80,80,0,1
Style: Hi,{FONT},86,&H0000E5FF,&H0000E5FF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,7,3,5,80,80,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    group = 3
    for i in range(0, len(words), group):
        chunk = words[i:i + group]
        end_of_chunk = words[i + group]["s"] if i + group < len(words) else chunk[-1]["e"] + 0.3
        for j, w in enumerate(chunk):
            start = w["s"]
            end = chunk[j + 1]["s"] if j + 1 < len(chunk) else end_of_chunk
            parts = []
            for k, x in enumerate(chunk):
                word = re.sub(r"[{}\\]", "", x["w"]).upper()
                parts.append("{\\c&H0000E5FF&}" + word + "{\\c&H00FFFFFF&}" if k == j else word)
            lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Cap,,0,0,0,,{' '.join(parts)}")
    ass_path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


def ffmpeg(*args):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *map(str, args)]
    subprocess.run(cmd, check=True)


def build_video(clips, audio, ass_path, out_path, workdir):
    total = duration(audio) + 0.6
    per = max(total / len(clips), 2.5)
    parts = []
    t = 0.0
    i = 0
    while t < total:
        src = clips[i % len(clips)]
        seg = min(per, total - t) if total - t > 1 else per
        p = workdir / f"part{i}.mp4"
        ffmpeg(
            "-i", src, "-t", f"{seg:.2f}", "-an",
            "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=30,"
                   "eq=brightness=-0.08:saturation=1.1,setsar=1",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", p,
        )
        parts.append(p)
        t += duration(p)
        i += 1
    lst = workdir / "list.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
    joined = workdir / "joined.mp4"
    ffmpeg("-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", joined)
    ass_arg = ass_path.as_posix().replace(":", "\\:")
    ffmpeg(
        "-i", joined, "-i", audio,
        "-vf", f"ass='{ass_arg}'",
        "-map", "0:v", "-map", "1:a", "-t", f"{total:.2f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out_path,
    )
    log(f"Video ready: {out_path.name} ({duration(out_path):.1f}s)")


# ---------------------------------------------------------------- 5. publish
def publish(video_path, meta):
    repo = os.environ["GITHUB_REPOSITORY"]
    tag = "short-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    subprocess.run(
        ["gh", "release", "create", tag, str(video_path), "--title", meta["title"][:100],
         "--notes", "Auto-generated Short", "--repo", repo],
        check=True,
    )
    video_url = f"https://github.com/{repo}/releases/download/{tag}/{video_path.name}"
    hook = os.environ["MAKE_WEBHOOK_URL"]
    payload = {
        "title": meta["title"][:95],
        "description": meta["full_description"],
        "tags": ",".join(meta["tags"])[:480],
        "video_url": video_url,
        "file_name": video_path.name,
        "privacy": os.environ.get("YT_PRIVACY", "public"),
    }
    r = requests.post(hook, json=payload, timeout=60)
    r.raise_for_status()
    log(f"Sent to Make: {r.status_code}")
    return tag, video_url


def full_description(meta):
    tags = " ".join(meta.get("hashtags") or ["#Shorts"])
    if "#shorts" not in tags.lower():
        tags = "#Shorts " + tags
    return (
        f"{meta['description']}\n\n"
        "Follow for daily Shorts on AI, privacy and your digital footprint.\n\n"
        "Voiceover is AI-generated. Stock footage: Pixabay.\n\n"
        f"{tags}"
    )


# ---------------------------------------------------------------- dry run
def fake_inputs(workdir):
    meta = {
        "title": "Your phone knows where you sleep",
        "script": "Your phone knows where you sleep. Every night it records the same location for hours.",
        "description": "Test Short.",
        "tags": ["ai", "privacy"],
        "hashtags": ["#Shorts", "#AI", "#Privacy"],
        "footage": ["phone"],
        "theme": "test",
    }
    audio = workdir / "voice.m4a"
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=220:duration=6", "-c:a", "aac", audio)
    words = []
    for i, w in enumerate(meta["script"].split()):
        words.append({"w": w, "s": i * 0.38, "e": i * 0.38 + 0.34})
    clips = []
    for i, c in enumerate(["0x1b2a49", "0x3a0f4f", "0x0f4f3a"]):
        p = workdir / f"clip{i}.mp4"
        ffmpeg("-f", "lavfi", "-i", f"color=c={c}:s=720x1280:d=4", "-f", "lavfi", "-i",
               "testsrc2=s=720x1280:d=4", "-filter_complex", "[0][1]blend=all_opacity=0.25",
               "-c:v", "libx264", "-preset", "veryfast", p)
        clips.append(p)
    return meta, audio, words, clips


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no API keys; fake script/voice/footage")
    ap.add_argument("--no-publish", action="store_true", help="build the video but don't upload")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    work = OUT / stamp
    work.mkdir(parents=True, exist_ok=True)
    history = load_history()

    if args.dry_run:
        meta, audio, words, clips = fake_inputs(work)
    else:
        meta = gemini_script(history)
        audio = work / "voice.mp3"
        words = make_voice(meta["script"], audio)
        clips = stock_clips(meta.get("footage", []), work, duration(audio))

    ass = work / "captions.ass"
    write_captions(words, ass)
    video = OUT / f"short_{stamp}.mp4"
    build_video(clips, audio, ass, video, work)
    meta["full_description"] = full_description(meta)

    if args.dry_run or args.no_publish:
        log("Skipping publish.")
        return

    tag, url = publish(video, meta)
    history.append({
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "title": meta["title"], "theme": meta["theme"], "release": tag, "video_url": url,
    })
    HISTORY.write_text(json.dumps(history[-500:], indent=1))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FAILED: {e}")
        sys.exit(1)
