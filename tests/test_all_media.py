import subprocess
from pathlib import Path
from app.services import ffmpeg


def make_media(path: Path):
    subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y","-f","lavfi","-i","testsrc=size=320x180:rate=10","-f","lavfi","-i","sine=frequency=1000:sample_rate=48000","-t","3","-c:v","libx264","-c:a","aac",str(path)],check=True)


def test_more_media_functions(tmp_path):
    src=tmp_path/'in.mkv'; make_media(src)
    ffmpeg.trim(src,tmp_path/'trim.mkv','00:00:00','00:00:01')
    ffmpeg.convert_video(src,tmp_path/'out.mp4','mp4')
    ffmpeg.convert_video(src,tmp_path/'out2.mkv','mkv')
    ffmpeg.sample(src,tmp_path/'sample.mkv',1)
    ffmpeg.audio_filter(src,tmp_path/'bass.m4a','bass=g=2')
    ffmpeg.screenshots(src,tmp_path/'shots',3)
    ffmpeg.split_video(src,tmp_path/'split',1)
    ffmpeg.optimize(src,tmp_path/'opt.mkv')
    for p in [tmp_path/'trim.mkv',tmp_path/'out.mp4',tmp_path/'out2.mkv',tmp_path/'sample.mkv',tmp_path/'bass.m4a',tmp_path/'opt.mkv']:
        assert p.exists() and p.stat().st_size>0
    assert len(list((tmp_path/'shots').glob('*.jpg')))==3
    assert len(list((tmp_path/'split').glob('*.mkv')))>=2
