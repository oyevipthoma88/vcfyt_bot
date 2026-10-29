"""Minimal ffprobe replacement built on top of ``ffmpeg -i``.

Installed as ``.bin/ffprobe`` only when no real ffprobe exists.  Supports:
  * ``ffprobe -h full``                      (pytgcalls option discovery)
  * ``ffprobe ... -of json -i <src>``        (pytgcalls check_stream)
  * ``ffprobe ... -show_entries format=duration -of default=nw=1:nk=1 <src>``
"""
import json
import re
import subprocess
import sys

PROBE_OPTS = ("-v", "-show_entries", "-show_format", "-show_streams", "-of",
              "-print_format", "-select_streams", "-i", "-headers",
              "-user_agent", "-rw_timeout", "-timeout", "-analyzeduration",
              "-probesize", "-loglevel", "-hide_banner")
NO_VALUE = {"-show_format", "-show_streams", "-hide_banner"}
FFMPEG_INPUT_OPTS = {"-headers", "-user_agent", "-rw_timeout", "-timeout",
                     "-analyzeduration", "-probesize", "-ss", "-f",
                     "-reconnect", "-reconnect_streamed", "-reconnect_delay_max"}


def _help() -> int:
    for o in PROBE_OPTS + tuple(sorted(FFMPEG_INPUT_OPTS)):
        print(f"{o} <value>  option")
    return 0


def _parse(argv):
    src, out_fmt, entries, extra = None, "default", "", []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in NO_VALUE:
            i += 1
            continue
        if a.startswith("-") and i + 1 < len(argv):
            val = argv[i + 1]
            if a == "-i":
                src = val
            elif a in ("-of", "-print_format"):
                out_fmt = val
            elif a == "-show_entries":
                entries = val
            elif a in FFMPEG_INPUT_OPTS:
                extra += [a, val]
            i += 2
            continue
        src = a
        i += 1
    return src, out_fmt, entries, extra


def _probe(src, extra):
    try:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", *extra, "-i", src],
                           capture_output=True, text=True, timeout=20)
        err = p.stderr
    except Exception as e:
        err = str(e)
    streams, fmt = [], {}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        h, mi, s = m.groups()
        fmt["duration"] = f"{int(h) * 3600 + int(mi) * 60 + float(s):.6f}"
    for line in err.splitlines():
        sm = re.search(r"Stream #\S+.*?:\s*(Audio|Video):\s*([A-Za-z0-9_]+)", line)
        if not sm:
            continue
        kind, codec = sm.group(1).lower(), sm.group(2)
        st = {"codec_type": kind, "codec_name": codec}
        if kind == "video":
            wh = re.search(r"\b(\d{2,5})x(\d{2,5})\b", line)
            if wh:
                st["width"], st["height"] = int(wh.group(1)), int(wh.group(2))
        streams.append(st)
    return streams, fmt, err


def main(argv) -> int:
    if "-h" in argv or "-version" in argv:
        return _help()
    src, out_fmt, entries, extra = _parse(argv)
    if not src:
        sys.stderr.write("ffprobe-shim: no input\n")
        return 1
    streams, fmt, err = _probe(src, extra)
    if "No such file" in err:
        sys.stderr.write(f"{src}: No such file or directory\n")
    if out_fmt.startswith("json"):
        print(json.dumps({"streams": streams, "format": fmt}))
    elif "duration" in entries:
        if "duration" in fmt:
            print(fmt["duration"])
    else:
        for st in streams:
            print(st.get("codec_type", ""))
    return 0 if (streams or fmt) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
