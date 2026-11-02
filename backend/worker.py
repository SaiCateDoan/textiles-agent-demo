"""Standalone single-host ingestion service: uv run python -m backend.worker."""
import asyncio
import logging

from openai import AsyncOpenAI

from .config import settings
from .multimodal import DeepSeekVision
from .pipeline import IngestionRunner
from .retrieval import VectorIndex
from .store import Store


async def serve():
    store = Store(settings.textile_data_dir)
    index = VectorIndex(settings.textile_data_dir,settings.embedding_model,settings.onnx_provider)
    client = AsyncOpenAI(api_key=settings.deepseek_api_key or "not-configured",base_url=settings.deepseek_base_url,timeout=settings.vision_timeout_seconds,max_retries=0)
    worker_config = settings.model_copy(update={"ingestion_enabled":True})
    runner = IngestionRunner(store,index,worker_config,lambda:DeepSeekVision(client,settings.deepseek_vision_model or settings.deepseek_model))
    runner.start()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.stop()
        await client.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
