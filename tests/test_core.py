from pathlib import Path
import asyncio
import subprocess
import tempfile

from app.services.process import ffprobe_json
from app.utils.files import human_size, safe_filename
from app.utils.progress import bar


def test_safe_filename_and_size():
    assert safe_filename('../bad<>name?.mkv') == 'bad__name_.mkv'
    assert human_size(1024) == '1.00 KiB'
    assert bar(50, 10) == '█████░░░░░'


def test_ffmpeg_media_pipeline():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / 'sample.mkv'
        # Two-second test file: video + two audio streams.
        subprocess.run([
            'ffmpeg','-hide_banner','-loglevel','error','-y',
            '-f','lavfi','-i','testsrc=size=320x180:rate=10:duration=2',
            '-f','lavfi','-i','sine=frequency=440:duration=2',
            '-f','lavfi','-i','sine=frequency=880:duration=2',
            '-map','0:v','-map','1:a','-map','2:a','-c:v','libx264','-preset','ultrafast','-t','2',
            '-c:a','aac',str(src)
        ], check=True)
        info = asyncio.run(ffprobe_json(src))
        assert len(info['streams']) == 3
        assert {s['codec_type'] for s in info['streams']} == {'video','audio'}

        no_audio = root / 'no_audio.mkv'
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(src),'-map','0:v','-c','copy',str(no_audio)], check=True)
        info2 = asyncio.run(ffprobe_json(no_audio))
        assert all(s['codec_type'] == 'video' for s in info2['streams'])

        extracted = root / 'audio.m4a'
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(src),'-map','0:1','-c','copy',str(extracted)], check=True)
        info3 = asyncio.run(ffprobe_json(extracted))
        assert info3['streams'][0]['codec_type'] == 'audio'

        trimmed = root / 'trim.mkv'
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-ss','0.5','-i',str(src),'-t','0.5','-map','0','-c','copy',str(trimmed)], check=True)
        assert trimmed.exists() and trimmed.stat().st_size > 0
