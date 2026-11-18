# AI Shorts Bot

Posts 5 YouTube Shorts a day about AI, privacy and digital footprints — fully automatic.

**How it works:** GitHub runs the bot on a schedule → Gemini writes the script → a free neural
voice reads it → Pexels supplies stock clips → ffmpeg edits a vertical Short with captions →
the video is sent to a Make scenario that uploads it to YouTube.

## Secrets (Settings → Secrets and variables → Actions)
| Name | Where it comes from |
|---|---|
| `GEMINI_API_KEY` | aistudio.google.com → Get API key |
| `PEXELS_API_KEY` | pexels.com/api |
| `MAKE_WEBHOOK_URL` | the Custom webhook in the Make scenario |

## Schedule
Times are in `.github/workflows/shorts.yml` (UTC). To post one right now:
Actions → **Post AI Short** → **Run workflow**.

## Test without keys
```
pip install -r requirements.txt
python bot/make_short.py --dry-run
```

Keep this repository **public** so Make can download the finished videos (your keys stay hidden in Secrets).
