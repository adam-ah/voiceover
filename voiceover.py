#!/usr/bin/env python3
"""Replace narration while preserving each phrase's position on the timeline."""

import argparse
from contextlib import contextmanager
from functools import lru_cache
from hashlib import sha256
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import mimetypes
from pathlib import Path
import re
from secrets import token_urlsafe
import shlex
import shutil
from string import Template
import subprocess
import sys
import tempfile
from threading import Lock
from urllib.parse import parse_qs, quote
import wave

RATE = 48_000
SAMPLE_BYTES = 2
AUDIO_ENCODINGS = {
    ".wav": (),
    ".mp3": ("-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3"),
    ".m4a": ("-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-f", "ipod"),
}
VIDEO_ENCODINGS = {
    ".mp4": ("-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-f", "mp4"),
    ".mov": ("-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-f", "mov"),
    ".mkv": ("-c:a", "aac", "-b:a", "192k", "-f", "matroska"),
    ".webm": ("-c:a", "libopus", "-b:a", "96k", "-f", "webm"),
}
MEDIA_FORMATS = ", ".join(ext[1:].upper() for ext in (*AUDIO_ENCODINGS, *VIDEO_ENCODINGS))
SILENCE_EVENT = re.compile(r"silence_(start|end):\s*(-?[\d.]+(?:e[+-]?\d+)?)", re.I)


def run(command):
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise ValueError(f"{command[0]} exited with status {result.returncode}. {detail}")
    return result


def ffmpeg(*args):
    return run(["ffmpeg", "-nostdin", "-hide_banner", "-y", *map(str, args)])


def decode_audio(source, destination):
    # Keep the first audio stream aligned to the video timeline, including a delayed audio start.
    is_video = source.suffix.lower() in VIDEO_ENCODINGS
    timestamps = ("-copyts", "-start_at_zero") if is_video else ()
    resample = ("-af", f"aresample={RATE}:async=1:first_pts=0") if is_video else ()
    ffmpeg("-loglevel", "error", *timestamps, "-i", source, "-map", "0:a:0",
           *resample, "-ac", 1, "-ar", RATE, "-c:a", "pcm_s16le", destination)


def voice_regions(path, frames, threshold, gap):
    result = ffmpeg("-i", path, "-af", f"silencedetect=n={threshold}dB:d={gap}",
                    "-f", "null", "-")
    position = 0
    regions = []
    for kind, timestamp in SILENCE_EVENT.findall(result.stderr.decode(errors="replace")):
        boundary = min(frames, max(0, round(float(timestamp) * RATE)))
        if kind == "start":
            if position is not None and boundary > position:
                regions.append((position, boundary))
            position = None
        else:
            position = boundary
    if position is not None and position < frames:
        regions.append((position, frames))
    return regions


def write_pcm(path, pcm):
    with wave.open(str(path), "wb") as output:
        output.setparams((1, SAMPLE_BYTES, RATE, 0, "NONE", "not compressed"))
        output.writeframes(pcm)


def write_silence(output, frames):
    block = bytes(RATE * SAMPLE_BYTES)
    while frames:
        count = min(frames, RATE)
        output.writeframesraw(block[:count * SAMPLE_BYTES])
        frames -= count


@contextmanager
def staged_output(path, overwrite):
    if path.exists() and not overwrite:
        raise ValueError(f"{path} already exists. Use --overwrite to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".voiceover-", dir=path.parent) as directory:
        staged = Path(directory) / path.name
        yield staged
        if not overwrite and path.exists():
            raise ValueError(f"{path} was created by another run.")
        if overwrite:
            staged.replace(path)
        else:
            path.hardlink_to(staged)


@lru_cache(maxsize=1)
def whisper(model, device):
    from faster_whisper import WhisperModel

    return WhisperModel(model, device=device, compute_type="int8" if device == "cpu" else "float16")


def whisper_phrases(path, args):
    segments, _ = whisper(args.stt_model, args.stt_device).transcribe(
        str(path), language=args.language, beam_size=5, initial_prompt=args.prompt,
        word_timestamps=True, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 650, "speech_pad_ms": 120},
    )
    words = [word for segment in segments for word in segment.words or () if word.word.strip()]
    if not words:
        return []

    phrases = []
    first = 0
    onset = words[0].start
    for index in range(1, len(words)):
        previous, current = words[index - 1:index + 1]
        gap = current.start - previous.end
        sentence_end = previous.word.rstrip(' "’”\')').endswith((".", "!", "?"))
        if gap < args.min_gap and not (sentence_end and gap >= 0.15):
            continue

        # A conjunction before a long pause reads more naturally with the clause it introduces.
        trailing = previous.word.strip().lower()
        cut = index - 1 if trailing in {"and", "but", "or", "then"} and index - first > 1 else index
        phrases.append((onset, words[cut - 1].end, "".join(word.word for word in words[first:cut]).strip()))
        first = cut
        onset = current.start

    phrases.append((onset, words[-1].end, "".join(word.word for word in words[first:]).strip()))
    return phrases


def timed_clips(path, source, frames, args):
    if args.stt_command:
        chunk = path.with_name("chunk.wav")
        regions = voice_regions(path, frames, args.silence_db, args.min_gap)
        for number, (start, end) in enumerate(regions, 1):
            print(f"Transcribing {number}/{len(regions)} at {start / RATE:.3f}s", file=sys.stderr)
            source.setpos(start)
            write_pcm(chunk, source.readframes(end - start))
            text = run([*shlex.split(args.stt_command), str(chunk)]).stdout.decode().strip()
            yield start, end, text
        return

    print("Transcribing the full recording before splitting into phrases.", file=sys.stderr)
    phrases = whisper_phrases(path, args)
    regions = voice_regions(path, frames, args.silence_db, 0.35)
    onsets = [start for start, _ in regions]
    slots = []
    for onset, finish, text in phrases:
        start = max(0, min(frames - 1, round(onset * RATE)))
        end = min(frames, max(start + 1, round((finish + 0.12) * RATE)))
        nearby = min(onsets, key=lambda candidate: abs(candidate - start), default=start)
        if abs(nearby - start) <= round(RATE * 0.60) and nearby < end:
            start = nearby
        if slots and start <= slots[-1][0]:
            previous_start, previous_end, previous_text = slots[-1]
            slots[-1] = (previous_start, max(previous_end, end), f"{previous_text} {text}")
        else:
            slots.append((start, end, text))

    for index, (start, end, text) in enumerate(slots):
        if index + 1 < len(slots):
            end = min(end, slots[index + 1][0])
        yield start, end, text


@lru_cache(maxsize=1)
def kokoro(voice, device):
    from kokoro import KPipeline

    return KPipeline(lang_code=voice[0], repo_id="hexgrad/Kokoro-82M", device=device)


def synthesize(text, path, args):
    path.unlink(missing_ok=True)
    if args.tts_command:
        text_file = path.with_suffix(".txt")
        text_file.write_text(text, encoding="utf-8")
        run([*shlex.split(args.tts_command), str(text_file), str(path)])
        return
    import soundfile

    with soundfile.SoundFile(path, mode="w", samplerate=24_000, channels=1) as output:
        for _, _, audio in kokoro(args.voice, args.tts_device)(text, voice=args.voice):
            output.write(audio.detach().cpu().numpy())


def make_transcript(args):
    suffix = args.source.suffix.lower()
    if not args.source.is_file() or (suffix not in AUDIO_ENCODINGS and suffix not in VIDEO_ENCODINGS):
        raise ValueError(f"Input must be an existing media file ({MEDIA_FORMATS}).")
    if args.source.resolve() == args.plan.resolve():
        raise ValueError("The transcript path must differ from the input audio.")
    with staged_output(args.plan, args.overwrite) as staged, tempfile.TemporaryDirectory() as directory:
        pcm = Path(directory) / "source.wav"
        decode_audio(args.source, pcm)
        with wave.open(str(pcm), "rb") as source:
            frames = source.getnframes()
            if not frames:
                raise ValueError("The input audio is empty.")
            clips = [
                {"start": round(start / RATE, 6), "end": round(end / RATE, 6), "text": text}
                for start, end, text in timed_clips(pcm, source, frames, args)
            ]
        plan = {
            "source": str(args.source.resolve()),
            "duration": round(frames / RATE, 6),
            "clips": clips,
        }
        staged.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.stage == "dub":
        print(f"Transcribed {len(clips)} clips.", file=sys.stderr)
    else:
        print(f"{len(clips)} clips in {args.plan}. Edit the text, then render.")


def sample_number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Timing values must be finite, nonnegative numbers in seconds.")
    return round(value * RATE)


def read_plan(path):
    return validate_plan(json.loads(path.read_text(encoding="utf-8")))


def validate_plan(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get("clips"), list):
        raise ValueError("Expected a transcript JSON object with a clips list.")
    frames = sample_number(plan.get("duration"))
    if not frames:
        raise ValueError("The transcript duration must be positive.")
    clips = []
    previous_end = 0
    for number, clip in enumerate(plan["clips"], 1):
        if not isinstance(clip, dict) or not isinstance(clip.get("text"), str):
            raise ValueError(f"Clip {number} needs a text string.")
        start = sample_number(clip.get("start"))
        end = sample_number(clip.get("end"))
        if not previous_end <= start < end <= frames:
            raise ValueError(f"Clip {number} overlaps, is out of order, or falls outside the timeline.")
        clips.append((start, end, clip["text"].strip()))
        previous_end = end
    return plan.get("source"), frames, clips


def tempo_filter(speed):
    filters = []
    while speed > 2:
        filters.append("atempo=2")
        speed /= 2
    filters.append(f"atempo={speed:.8f}")
    return ",".join(filters)


def clip_audio(raw, scratch, slot):
    normalized = scratch / "normalized.wav"
    decode_audio(raw, normalized)
    with wave.open(str(normalized), "rb") as audio:
        regions = voice_regions(normalized, audio.getnframes(), -48, 0.05)
        if not regions:
            raise ValueError("The voice engine returned silent or empty audio.")
        start, end = regions[0][0], regions[-1][1]
        audio.setpos(start)
        pcm = audio.readframes(end - start)
    if end - start <= slot:
        return pcm, 1.0

    trimmed = scratch / "trimmed.wav"
    fitted = scratch / "fitted.wav"
    write_pcm(trimmed, pcm)
    # Leave a small margin for atempo's windowed processing, then pad in the timeline.
    margin = min(round(RATE * 0.04), slot // 20)
    speed = (end - start) / (slot - margin)
    for _ in range(3):
        ffmpeg("-loglevel", "error", "-i", trimmed, "-af", tempo_filter(speed), fitted)
        with wave.open(str(fitted), "rb") as audio:
            length = audio.getnframes()
            if 0 < length <= slot:
                return audio.readframes(length), speed
        if not length:
            break
        speed *= length / (slot - margin) * 1.02
    raise ValueError("Generated speech could not fit this slot. Shorten the text or extend its timing.")


def render(args):
    source, frames, clips = read_plan(args.plan)
    protected = {args.plan.resolve()}
    if isinstance(source, str):
        protected.add(Path(source).resolve())
    suffix = args.output.suffix.lower()
    is_video = suffix in VIDEO_ENCODINGS
    encoding = VIDEO_ENCODINGS[suffix] if is_video else AUDIO_ENCODINGS.get(suffix)
    if args.output.resolve() in protected or encoding is None:
        raise ValueError(f"Choose a new output path ({MEDIA_FORMATS}).")
    if is_video and (not isinstance(source, str) or Path(source).suffix.lower() not in VIDEO_ENCODINGS
                     or not Path(source).is_file()):
        raise ValueError("Video output needs the original video at the transcript's source path.")
    with staged_output(args.output, args.overwrite) as staged, tempfile.TemporaryDirectory() as directory:
        scratch = Path(directory)
        raw = scratch / "generated.wav"
        mix = scratch / "mix.wav" if encoding else staged
        with wave.open(str(mix), "wb") as output:
            output.setparams((1, SAMPLE_BYTES, RATE, 0, "NONE", "not compressed"))
            cursor = 0
            for number, (start, end, text) in enumerate(clips, 1):
                if not text:
                    continue
                print(f"Rendering {number}/{len(clips)} at {start / RATE:.3f}s", file=sys.stderr)
                try:
                    synthesize(text, raw, args)
                    pcm, speed = clip_audio(raw, scratch, end - start)
                except Exception as error:
                    raise ValueError(f"Clip {number} at {start / RATE:.3f}s: {error}") from error
                if speed > 1.35:
                    print(f"  sped up {speed:.2f}× to fit; shorten this line if it sounds rushed",
                          file=sys.stderr)
                write_silence(output, start - cursor)
                output.writeframesraw(pcm)
                cursor = start + len(pcm) // SAMPLE_BYTES
            write_silence(output, frames - cursor)
        if is_video:
            ffmpeg("-loglevel", "error", "-i", source, "-i", mix,
                   "-map", "0:V", "-map", "1:a:0", "-map", "0:s?",
                   "-map_metadata", "0", "-map_chapters", "0", "-c:v", "copy", "-c:s", "copy",
                   "-avoid_negative_ts", "disabled", *encoding, staged)
        elif encoding:
            ffmpeg("-loglevel", "error", "-i", mix, *encoding, staged)
    print(f"{args.output} · {frames / RATE:.3f}s of mono audio at {RATE} Hz")


def format_time(seconds):
    total_ms = round(seconds * 1000)
    minutes, rest = divmod(total_ms, 60_000)
    seconds, milliseconds = divmod(rest, 1000)
    return f"{minutes:02}:{seconds:02}.{milliseconds:03}"


def review_page(template, plan, revision, output, has_video, notice, is_error):
    cards = []
    for index, clip in enumerate(plan["clips"]):
        start = format_time(clip["start"])
        end = format_time(clip["end"])
        image = (
            f'<button class="thumbnail" type="button" aria-label="Preview phrase {index + 1}">'
            f'<img src="frame/{sample_number(clip["start"])}.jpg" alt="" loading="lazy"></button>'
            if has_video else ""
        )
        cards.append(
            f'<article class="clip{" with-image" if image else ""}">{image}'
            f'<div class="clip-editor"><div class="time-bar">'
            f'<label>Start <input class="start" type="text" inputmode="decimal" '
            f'value="{start}" data-seconds="{clip["start"]}"></label>'
            f'<label>End <input class="end" type="text" inputmode="decimal" '
            f'value="{end}" data-seconds="{clip["end"]}"></label>'
            f'<button class="delete" type="button" aria-label="Delete phrase {index + 1}">Delete</button></div>'
            f'<textarea aria-label="Text for phrase {index + 1}" spellcheck="true">'
            f'{escape(clip["text"])}</textarea></div></article>'
        )
    preview = ""
    if has_video:
        preview = (
            '<aside class="preview" aria-label="Frame preview"><div class="preview-title">'
            '<h2>Frame preview</h2><span class="preview-time"></span></div>'
            '<div class="preview-media"><img alt="Video frame at the selected phrase start" hidden>'
            '<p class="preview-placeholder">Select a phrase to preview its frame.</p></div></aside>'
        )
    message = ""
    if notice:
        message = f'<p class="notice{" error" if is_error else ""}" role="status">{escape(notice)}</p>'
    exists = output.is_file()
    download = '<a class="secondary" href="download">Download dub</a>' if exists else ""
    return template.substitute(
        filename=escape(Path(plan["source"]).name),
        notice=message,
        revision=revision,
        render_action="replace" if exists else "render",
        render_label="Render video" if has_video else "Render audio",
        download=download,
        duration=format_time(plan["duration"]),
        clips="\n".join(cards),
        preview=preview,
    )


def review_and_render(args):
    if not args.source.is_file():
        raise ValueError(f"Input does not exist: {args.source}")
    args.plan = args.source.with_suffix(".transcript.json")
    if not args.plan.exists():
        make_transcript(argparse.Namespace(**(vars(args) | {"overwrite": False})))
    source, frames, _ = read_plan(args.plan)
    if source != str(args.source.resolve()):
        raise ValueError(f"{args.plan} belongs to a different input. Move it aside before reviewing.")

    assets = Path(__file__).parent
    template = Template((assets / "review.html").read_text(encoding="utf-8"))
    script = (assets / "review.js").read_bytes()
    base = f"/{token_urlsafe(24)}/"
    has_video = args.source.suffix.lower() in VIDEO_ENCODINGS
    save_lock = Lock()
    status = {"notice": "", "is_error": False}

    @lru_cache(maxsize=32)
    def thumbnail(sample):
        start = sample / RATE
        return ffmpeg("-loglevel", "error", "-ss", f"{start:.6f}", "-threads", 1,
                      "-i", args.source, "-map", "0:V:0", "-frames:v", 1,
                      "-filter_threads", 1, "-vf", "scale='min(960,iw)':-2", "-threads", 1,
                      "-c:v", "mjpeg", "-q:v", 4, "-f", "image2pipe", "-").stdout

    class ReviewHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send_data(self, data, content_type, code=200):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def page(self):
            contents = args.plan.read_bytes()
            revision = sha256(contents).hexdigest()
            page = review_page(template, json.loads(contents), revision, args.output, has_video, **status)
            self.send_data(page.encode("utf-8"), "text/html; charset=utf-8")

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if self.path == base:
                self.page()
                return
            if self.path == base + "review.js":
                self.send_data(script, "text/javascript; charset=utf-8")
                return
            frame = re.fullmatch(re.escape(base) + r"frame/(\d+)\.jpg", self.path)
            if has_video and frame and len(frame[1]) < 14 and int(frame[1]) < frames:
                try:
                    image = thumbnail(int(frame[1]))
                    if image:
                        self.send_data(image, "image/jpeg")
                        return
                except ValueError:
                    pass
            if self.path == base + "download" and args.output.is_file():
                with args.output.open("rb") as media:
                    self.send_response(200)
                    self.send_header("Content-Type", mimetypes.guess_type(str(args.output))[0] or "application/octet-stream")
                    self.send_header("Content-Length", str(media.seek(0, 2)))
                    filename = quote(args.output.name, safe="")
                    self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{filename}")
                    self.end_headers()
                    if self.command != "HEAD":
                        media.seek(0)
                        shutil.copyfileobj(media, self.wfile)
                return
            self.send_error(404)

        def send_json(self, payload, code=200):
            self.send_data(json.dumps(payload).encode("utf-8"), "application/json", code)

        def do_POST(self):
            wants_json = self.headers.get("Accept") == "application/json"

            def reject(code, message):
                if wants_json:
                    self.send_json({"error": message}, code)
                else:
                    self.send_error(code, message)

            if self.path != base:
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 8_000_000:
                    reject(400, "Invalid form size")
                    return
                form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            except (UnicodeError, ValueError):
                reject(400, "Invalid form")
                return
            if not save_lock.acquire(blocking=False):
                reject(409, "Another save or render is in progress.")
                return
            try:
                contents = args.plan.read_bytes()
                if form.get("revision") != [sha256(contents).hexdigest()]:
                    reject(409, "The transcript changed. Copy your edits and reload before saving.")
                    return
                plan = json.loads(contents)
                action = form.get("action", [""])[0]
                if action not in {"save", "render", "replace"} or len(form.get("clips", [])) != 1:
                    reject(400, "Incomplete transcript.")
                    return
                try:
                    plan["clips"] = json.loads(form["clips"][0])
                    validate_plan(plan)
                except (ValueError, OverflowError) as error:
                    reject(400, str(error))
                    return
                updated = (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                if updated != contents:
                    with staged_output(args.plan, overwrite=True) as edited:
                        edited.write_bytes(updated)
                status.update(notice="", is_error=False)
                if wants_json and action == "save":
                    self.send_json({"revision": sha256(updated).hexdigest()})
                    return
                if action != "save":
                    overwrite = args.overwrite or action == "replace"
                    render(argparse.Namespace(**(vars(args) | {"overwrite": overwrite})))
                    status.update(notice=f"Rendered {args.output.name}")
            except Exception as error:
                status.update(notice=str(error), is_error=True)
                if wants_json:
                    reject(500, str(error))
                    return
            finally:
                save_lock.release()
            self.send_response(303)
            self.send_header("Location", base)
            self.end_headers()

    with ThreadingHTTPServer(("127.0.0.1", 0), ReviewHandler) as server:
        print(f"Open the review page: http://127.0.0.1:{server.server_port}{base}", flush=True)
        print("Press Ctrl+C to stop the review server.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


def transcription_options(parser):
    parser.add_argument("--min-gap", type=float, default=0.8, help="minimum pause between words in seconds")
    parser.add_argument("--silence-db", type=float, default=-38, help="silence threshold in dBFS")
    parser.add_argument("--stt-model", default="small.en", help="faster-whisper name or model folder")
    parser.add_argument("--stt-device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--language", default="en")
    parser.add_argument("--prompt", help="optional spelling hints for Whisper")
    parser.add_argument("--stt-command", help="custom program receiving a WAV path and printing text")


def synthesis_options(parser):
    parser.add_argument("--voice", default="af_heart", help="Kokoro voice name")
    parser.add_argument("--tts-device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--tts-command", help="custom program receiving a text file and output WAV path")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="stage", required=True)

    transcript = commands.add_parser("transcribe", help="transcribe and write editable timed phrases")
    transcript.add_argument("source", type=Path)
    transcript.add_argument("plan", type=Path)
    transcription_options(transcript)

    synthesis = commands.add_parser("render", help="synthesize the edited transcript into timed audio")
    synthesis.add_argument("plan", type=Path)
    synthesis.add_argument("output", type=Path)
    synthesis_options(synthesis)

    dub = commands.add_parser("dub", help="transcribe, review in a browser, and render")
    dub.add_argument("source", type=Path)
    dub.add_argument("output", type=Path)
    review = dub.add_mutually_exclusive_group()
    review.add_argument("--review", dest="review", action="store_true", default=True,
                        help="review the transcript in a browser before rendering (default)")
    review.add_argument("--no-review", dest="review", action="store_false",
                        help="render immediately without opening a review server")
    transcription_options(dub)
    synthesis_options(dub)

    for command in (transcript, synthesis, dub):
        command.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if not shutil.which("ffmpeg"):
        parser.exit(1, "Error: ffmpeg is required and must be on PATH.\n")
    try:
        if args.stage in ("transcribe", "dub"):
            if not (0 < args.min_gap < math.inf and -120 <= args.silence_db < 0):
                raise ValueError("Use a positive --min-gap and --silence-db between -120 and 0.")
        if args.stage == "transcribe":
            make_transcript(args)
        elif args.stage == "render":
            render(args)
        elif args.review:
            review_and_render(args)
        else:
            with tempfile.TemporaryDirectory() as directory:
                args.plan = Path(directory) / "transcript.json"
                make_transcript(args)
                render(args)
    except Exception as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
