"""Print non-secret diagnostics and download the local Chinese embedding model."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.config import settings
from backend.retrieval import VectorIndex
from openai import AsyncOpenAI


async def main():
    settings.textile_data_dir.mkdir(parents=True, exist_ok=True)
    index = VectorIndex(settings.textile_data_dir, settings.embedding_model, settings.onnx_provider)
    try:
        v = await asyncio.to_thread(index.embed, ["纯棉坯布的克重与染整工艺"])
        print(json.dumps({"embedding": "ok", "dimension": len(v[0]), "model": settings.embedding_model}))
    except Exception as exc:
        print("Embedding check failed:", type(exc).__name__, str(exc)[:250])
        raise
    if not settings.deepseek_api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")
    async with AsyncOpenAI(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url, timeout=60, max_retries=0) as client:
        try:
            r = await client.responses.create(model=settings.deepseek_model,input="请只回答：连接成功",max_output_tokens=20,reasoning={"effort":"none"})
            print(json.dumps({"deepseek":"ok","api":"responses","model":r.model,"response":r.output_text},ensure_ascii=False))
        except Exception as exc:
            print(json.dumps({"deepseek": "failed", "error_type": type(exc).__name__, "status": getattr(exc, "status_code", None)}))
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
