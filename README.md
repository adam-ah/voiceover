# Voiceover

Replace the narration in a video or audio file while keeping the original timing. Voiceover transcribes the full recording first. It uses Whisper’s word timestamps to split at phrase boundaries and lines up the replacement with the original audio. The tool assembles the result at 48 kHz. WAV preserves the decoded input duration to the sample, and MP3 or M4A uses the corresponding compressed format. Short takes leave silence. Long takes are sped up to fit, so timing errors do not accumulate.

## Run on this machine

```sh
cd /mnt/f/AI/voiceover
./start.sh "Tutorial.mp4"
```

This creates `Tutorial.out.mp4` beside the input. Supported formats are MP4, MOV, MKV, WebM, WAV, MP3, and M4A. The launcher keeps the original extension. Pass `--voice af_bella` for a different voice or `--overwrite` to replace an existing output in unattended mode.

For video input, the tool transcribes the first audio track and replaces the original audio with the new narration. It copies the video and subtitle streams to the output. If the narration shares its track with music or sound effects, those sounds are replaced along with the voice.

The launcher prints a local review link. Open it in a browser. Each phrase has a scrollable text box, its original time range, and a frame from the start of the phrase for video inputs. Text changes, start and end times, and deletions save automatically after a short pause. Use **Undo delete** to restore a phrase during the current review. Keep phrases in chronological order without overlaps. Wait for **Saved** before closing the tab. Choose **Render dub** to create the output. If a dub exists, **Replace dubbed file** updates it after a successful render.

Edits are saved in `Tutorial.transcript.json` beside the source and reused on the next review. The terminal keeps the server running. Press **Ctrl+C** to stop it when you’re done.

You can also run transcription and rendering separately:

```sh
cd /mnt/f/AI/voiceover
source ~/.local/share/voiceover/venv/bin/activate
python voiceover.py transcribe "Tutorial.mp4" narration.json
python voiceover.py render narration.json "Tutorial.out.mp4" --voice af_heart
```

Open `narration.json` between commands. Fix misheard words and remove fillers from `text`. Set a clip's text to an empty string to leave its slot silent. Times are seconds from the start of the input. Keep the times as recorded, or split a clip at a specific cue when a word must line up with an on-screen action. The tool fits each phrase to its slot. Words inside that phrase can move.

To skip review and render immediately:

```sh
./start.sh narration.wav --no-review --voice af_bella
```

Keep the source video at the path recorded in the transcript so the renderer can copy its picture. To use the new narration in a video editor, render to WAV and place it at the start of the original media.

## Settings

- `--min-gap 0.8` sets the pause between recognized words that starts a new phrase. A sentence ending can split at a shorter pause. Raise the threshold for longer phrases.
- `--silence-db -38` sets the pause threshold in dBFS. Move it toward zero if background noise prevents splits.
- `--stt-model small.en` uses the default English Whisper checkpoint. Try `--stt-model large-v3` if the transcript needs too many corrections. It needs a larger download and more compute. `--prompt 'Applio, Kokoro'` gives Whisper spelling hints.
- `--voice af_heart` and `--voice af_bella` select US English Kokoro voices. First use downloads the model weights.
- `--overwrite` updates an existing output or transcript. Without it, the tool preserves existing files.

## Set up on another Linux machine

Install FFmpeg and eSpeak NG through your system package manager. With Python 3.11 or 3.12:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

The built-in models default to CPU. `--stt-device cuda` and `--tts-device cuda` are available with compatible CUDA libraries and Python dependencies. [Whisper CUDA setup](https://github.com/SYSTRAN/faster-whisper#gpu) · [Kokoro voices](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md)

## Use a different speech engine

Custom engines need only Python's standard library and FFmpeg for the surrounding workflow:

- `--stt-command 'python my_stt.py'` appends a path to each input WAV. The command must print the transcript to stdout and send logs to stderr.
- `--tts-command 'python my_tts.py'` appends a UTF-8 text file path and a destination WAV path. The command must write generated audio there, at any sample rate.

Custom STT receives audio chunks split at quiet gaps because its text output has no timestamps. Custom TTS runs once per nonempty transcript line. A nonzero exit stops the job and preserves any previous output.
