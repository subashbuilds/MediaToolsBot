import asyncio
import subprocess
import tempfile
from pathlib import Path

from app.services.media import (
    audio_convert, audio_filter, convert_video, generate_sample, optimize_video,
    remove_audio, screenshot, screenshots, split_video, trim, video_to_audio,
)


def test_media_services_end_to_end():
    async def run():
        with tempfile.TemporaryDirectory() as td:
            r=Path(td); src=r/'src.mkv'
            subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi','-i','testsrc=size=320x180:rate=10:duration=3','-f','lavfi','-i','sine=frequency=440:duration=3','-map','0:v','-map','1:a','-c:v','libx264','-preset','ultrafast','-c:a','aac',str(src)],check=True)
            cancel=asyncio.Event()
            for fn,args,out in [
                (remove_audio,(src,r/'noaudio.mkv',cancel),r/'noaudio.mkv'),
                (trim,(src,'0.5','1',r/'trim.mkv',cancel),r/'trim.mkv'),
                (optimize_video,(src,r/'opt.mkv',23,'veryfast',cancel),r/'opt.mkv'),
                (convert_video,(src,r/'out.mp4','mp4',cancel),r/'out.mp4'),
                (video_to_audio,(src,r/'audio.mp3','mp3',cancel),r/'audio.mp3'),
                (audio_convert,(r/'audio.mp3',r/'audio2.mp3','libmp3lame','128k',cancel),r/'audio2.mp3'),
                (audio_filter,(r/'audio.mp3',r/'filtered.mp3','volume=0.8','libmp3lame',cancel),r/'filtered.mp3'),
                (screenshot,(src,r/'shot.jpg','1',cancel),r/'shot.jpg'),
                (generate_sample,(src,r/'sample.mp4',1,cancel),r/'sample.mp4'),
            ]:
                code,_,err=await fn(*args)
                assert code==0, err
                assert out.exists() and out.stat().st_size>0
            shot_dir=r/'shots'; files=await screenshots(src,shot_dir,3,cancel); assert len(files)==3
            split_dir=r/'split'; code,_,err=await split_video(src,split_dir,1,cancel); assert code==0,err; assert len(list(split_dir.glob('*')))>=2
    asyncio.run(run())


def test_zip_archive_extract_streaming_and_traversal_protection():
    import zipfile
    from app.services.media import archive_extract

    async def run():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / 'sample.zip'
            payload = b'x' * (2 * 1024 * 1024)
            with zipfile.ZipFile(src, 'w', compression=zipfile.ZIP_STORED) as zf:
                zf.writestr('nested/file.bin', payload)
            out = root / 'out'
            code, _, err = await archive_extract(src, out, asyncio.Event())
            assert code == 0, err
            assert (out / 'nested' / 'file.bin').read_bytes() == payload

            bad = root / 'bad.zip'
            with zipfile.ZipFile(bad, 'w') as zf:
                zf.writestr('../../escape.txt', b'no')
            try:
                await archive_extract(bad, root / 'bad-out', asyncio.Event())
            except RuntimeError as exc:
                assert 'Unsafe archive member' in str(exc)
            else:
                raise AssertionError('path traversal archive was not rejected')

    asyncio.run(run())
