import asyncio
import subprocess

import httpx
import pytest
import respx

from job_finder import runtime_status
from job_finder.career_lens import CareerLens


def test_image_version_takes_priority_over_git(tmp_path):
    (tmp_path / 'VERSION').write_text('a' * 40 + '\n')
    assert runtime_status.read_version(tmp_path) == {'commit': 'a' * 40, 'local': False, 'modified': False}


def test_local_version_and_changes(tmp_path):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(tmp_path), *args], text=True).strip()
    git('init', '-q')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '--allow-empty', '-qm', 'Initial')
    commit = git('rev-parse', 'HEAD')
    assert runtime_status.read_version(tmp_path) == {'commit': commit, 'local': True, 'modified': False}
    (tmp_path / 'new.txt').write_text('new')
    assert runtime_status.read_version(tmp_path)['modified'] is True


def test_missing_version_is_unknown(tmp_path):
    assert runtime_status.read_version(tmp_path)['commit'] is None


@pytest.mark.parametrize('code,body,expected', [
    (200, {'status': 'ok'}, 'online'), (200, {'other': 'ok'}, 'error'),
    (503, {}, 'error'), (401, {}, 'access_denied'), (403, {}, 'access_denied'),
    (302, {}, 'error'),
])
@respx.mock
async def test_career_lens_health(code, body, expected):
    respx.get('https://wh.test/healthz').respond(code, json=body)
    wh = CareerLens('https://wh.test', 'secret')
    try:
        monitor = runtime_status.CareerLensStatus(wh)
        await monitor.check()
        assert monitor.snapshot['state'] == expected
        assert monitor.snapshot['checked_at'] is not None
    finally:
        await wh.aclose()


@respx.mock
async def test_health_recovers_after_timeout():
    route = respx.get('https://wh.test/healthz').mock(side_effect=httpx.ConnectTimeout('timeout'))
    wh = CareerLens('https://wh.test', 'secret')
    try:
        monitor = runtime_status.CareerLensStatus(wh)
        await monitor.check()
        assert monitor.snapshot['state'] == 'unreachable'
        route.respond(200, json={'status': 'ok'})
        await monitor.check()
        assert monitor.snapshot['state'] == 'online'
    finally:
        await wh.aclose()


async def test_unconfigured_health_does_not_make_requests():
    wh = CareerLens('', '')
    try:
        monitor = runtime_status.CareerLensStatus(wh)
        await monitor.check()
        assert monitor.snapshot['state'] == 'not_configured'
    finally:
        await wh.aclose()


@respx.mock
async def test_monitor_rechecks_and_can_be_cancelled():
    route = respx.get('https://wh.test/healthz').respond(503)
    wh = CareerLens('https://wh.test', 'secret')
    monitor = runtime_status.CareerLensStatus(wh)
    task = asyncio.create_task(monitor.run(interval=0.01))
    try:
        async with asyncio.timeout(1):
            while monitor.snapshot['state'] != 'error':
                await asyncio.sleep(0.001)
            route.respond(200, json={'status': 'ok'})
            while monitor.snapshot['state'] != 'online':
                await asyncio.sleep(0.001)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await wh.aclose()
