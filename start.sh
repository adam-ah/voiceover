#!/usr/bin/env bash
set -euo pipefail

if [[ $# -eq 0 || ${1-} == --help ]]; then
    printf 'Usage: %s INPUT.{wav,mp3,m4a,mp4,mov,mkv,webm} [dub options]\n' "${0##*/}"
    printf 'Opens a local transcript review before writing INPUT.out.EXT. Use --no-review to render immediately.\n'
    if [[ $# -eq 0 ]]; then
        exit 2
    fi
    exit 0
fi

tool_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
input=$1
shift
output=${input%.*}.out.${input##*.}

python=${VOICEOVER_PYTHON:-"$tool_dir/.venv/bin/python"}
if [[ -z ${VOICEOVER_PYTHON:-} && ! -x $python ]]; then
    python=${XDG_DATA_HOME:-"$HOME/.local/share"}/voiceover/venv/bin/python
    if [[ ! -x $python ]]; then
        python=python3
    fi
fi

exec "$python" "$tool_dir/voiceover.py" dub "$@" -- "$input" "$output"
