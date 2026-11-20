# Demo clip

`run_demo.py` starts the fixture site on `127.0.0.1:8765` and `python -m ie_mcp`, then
makes the same MCP tool calls every run: `ie_goto` to
`demo.htm`, two `ie_fill`, an async `ie_click` on Send, `ie_dialog` to accept the page's
`confirm`, and `ie_network` to show the POST the proxy captured.

```bash
python demo/run_demo.py           # just run it (opens a visible IE-mode window)
```

## Recording

Needs ffmpeg and an unlocked desktop. Run it in a classic console so the script can set
its own font and position:

```powershell
$env:FFMPEG = "C:\path\to\ffmpeg.exe"   # if ffmpeg is not on PATH
conhost.exe python demo\run_demo.py --record raw.mkv
```

Only the region holding the two windows is recorded (near-lossless H.264). Keep the raw
file out of git.

## Encoding

The raw take starts on the start screen, and the picture first changes about 1 s in. It
ends back on the same screen. Find the cut points with:

```bash
ffmpeg -i raw.mkv -vf "scale=480:-1,select='gt(scene,0.002)',showinfo" -f null - 2>&1 | grep -o "pts_time:[0-9.]*"
```

The window that has focus differs between the first and the last frame (the dialog leaves
the browser active), so the loop seam is a 0.4 s crossfade between two frames of the start
screen. With cuts at 0.3 s and 10.95 s:

```bash
LOOP="[0:v]trim=start=0.3:end=10.95,setpts=PTS-STARTPTS,split[x][y];[x]trim=start=0.4,setpts=PTS-STARTPTS[a];[y]trim=end=0.4,setpts=PTS-STARTPTS[b];[a][b]xfade=transition=fade:duration=0.4:offset=9.85"

# MP4: H.264, yuv420p, no audio, moov atom first
ffmpeg -i raw.mkv -filter_complex "$LOOP,scale=1200:-2:flags=lanczos,format=yuv420p" \
  -c:v libx264 -preset veryslow -crf 20 -tune stillimage -an -movflags +faststart demo.mp4

# GIF: 800 px, 12 fps, one palette for the whole clip
ffmpeg -i raw.mkv -filter_complex "$LOOP,fps=12,scale=800:-1:flags=lanczos,split[c][d];[c]palettegen=max_colors=128:stats_mode=diff[p];[d][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" demo.gif
```

`offset` is the clip length (`end - start`) minus 0.4 s twice.
