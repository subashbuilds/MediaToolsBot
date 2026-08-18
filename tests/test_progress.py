import asyncio

from app.utils.progress import ProgressReporter


def test_progress_reporter_final_update_and_speed():
    seen = []

    async def cb(text):
        seen.append(text)

    async def run():
        p = ProgressReporter(cb, interval=60)
        await p.update(50, 100, "📥 Test", force=True)
        await p.finish(100, "✅ Done")

    asyncio.run(run())
    assert len(seen) == 2
    assert "50.0%" in seen[0]
    assert "100.0%" in seen[1]
    assert "ETA" not in seen[1]
    assert "⚡" in seen[0]
