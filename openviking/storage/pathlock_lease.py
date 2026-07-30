# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Serialized ownership for native RAGFS path-lock leases."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional


class NativePathLockLease:
    """Keep one native owned lease live until release or handoff succeeds."""

    def __init__(
        self,
        async_agfs: Any,
        lease_ref: Optional[Dict[str, Any]],
        *,
        owned: bool = True,
    ) -> None:
        self._async_agfs = async_agfs
        self._lease_ref = lease_ref
        self._owned = owned
        self._state_lock = asyncio.Lock()

    @property
    def ref(self) -> Optional[Dict[str, Any]]:
        return self._lease_ref

    @property
    def active(self) -> bool:
        return self._lease_ref is not None

    async def as_borrowed(self) -> "NativePathLockLease":
        lease_ref = self._lease_ref
        if lease_ref is None:
            return NativePathLockLease(self._async_agfs, None, owned=False)
        borrowed = await self._async_agfs.pathlock_as_borrowed(lease_ref)
        return NativePathLockLease(self._async_agfs, borrowed, owned=False)

    async def to_handoff(self) -> Optional[Dict[str, Any]]:
        lease_ref = self._lease_ref
        if lease_ref is None:
            return None
        return await self._async_agfs.pathlock_to_handoff(lease_ref)

    async def close(self) -> None:
        async with self._state_lock:
            lease_ref = self._lease_ref
            if lease_ref is None or not self._owned:
                return
            await self._async_agfs.pathlock_release(lease_ref)
            self._lease_ref = None

    async def handoff(self) -> None:
        async with self._state_lock:
            lease_ref = self._lease_ref
            if lease_ref is None or not self._owned:
                return
            await self._async_agfs.pathlock_handoff(lease_ref)
            self._lease_ref = None
