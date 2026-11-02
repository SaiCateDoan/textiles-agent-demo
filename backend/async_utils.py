"""Cancellation-safe boundaries around short native/database mutations."""
import asyncio


async def durable_call(fn, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(fn,*args,**kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def acquire_durable(acquire, release, *args):
    task = asyncio.create_task(asyncio.to_thread(acquire,*args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        value = await task
        if value:
            await durable_call(release,value)
        raise
