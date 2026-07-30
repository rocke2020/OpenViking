# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import asyncio

import pytest

from openviking.storage.pathlock_lease import NativePathLockLease


class _FakeAsyncAGFS:
    def __init__(self) -> None:
        self.release_started = asyncio.Event()
        self.allow_release = asyncio.Event()
        self.release_calls = 0
        self.fail_next_release = False
        self.handoff_started = asyncio.Event()
        self.allow_handoff = asyncio.Event()
        self.handoff_calls = 0

    async def pathlock_release(self, lease_ref):
        self.release_calls += 1
        self.release_started.set()
        await self.allow_release.wait()
        if self.fail_next_release:
            self.fail_next_release = False
            raise RuntimeError("release failed")

    async def pathlock_handoff(self, lease_ref):
        self.handoff_calls += 1
        self.handoff_started.set()
        await self.allow_handoff.wait()


@pytest.mark.asyncio
async def test_concurrent_close_releases_native_lease_once():
    async_agfs = _FakeAsyncAGFS()
    lease = NativePathLockLease(async_agfs, {"lease_ref": "lease-1"})

    first_close = asyncio.create_task(lease.close())
    await async_agfs.release_started.wait()
    second_close = asyncio.create_task(lease.close())
    await asyncio.sleep(0)

    assert async_agfs.release_calls == 1

    async_agfs.allow_release.set()
    await asyncio.gather(first_close, second_close)

    assert lease.ref is None
    assert async_agfs.release_calls == 1


@pytest.mark.asyncio
async def test_failed_close_keeps_native_lease_for_retry():
    async_agfs = _FakeAsyncAGFS()
    async_agfs.fail_next_release = True
    async_agfs.allow_release.set()
    lease_ref = {"lease_ref": "lease-1"}
    lease = NativePathLockLease(async_agfs, lease_ref)

    with pytest.raises(RuntimeError, match="release failed"):
        await lease.close()

    assert lease.ref is lease_ref

    await lease.close()

    assert lease.ref is None
    assert async_agfs.release_calls == 2


@pytest.mark.asyncio
async def test_handoff_serializes_with_close_and_transfers_ownership_once():
    async_agfs = _FakeAsyncAGFS()
    lease = NativePathLockLease(async_agfs, {"lease_ref": "lease-1"})

    handoff = asyncio.create_task(lease.handoff())
    await async_agfs.handoff_started.wait()
    close = asyncio.create_task(lease.close())
    await asyncio.sleep(0)

    assert async_agfs.handoff_calls == 1
    assert async_agfs.release_calls == 0

    async_agfs.allow_handoff.set()
    await asyncio.gather(handoff, close)

    assert lease.ref is None
    assert async_agfs.handoff_calls == 1
    assert async_agfs.release_calls == 0
