"""HTTP ingestion smoke/load test. Uses the running parser and embedding model.

Creates clearly marked synthetic CSVs, never calls DeepSeek, removes its own
documents unless --keep is passed. Not a claim of 100k-document capacity.
"""
import argparse
import asyncio
import json
import math
import time
from pathlib import Path
from uuid import uuid4

import httpx


def percentile(values, fraction):
    return round(sorted(values)[max(0,math.ceil(len(values)*fraction)-1)],2) if values else None


async def run(args):
    created, ack, ready, health = [],[],[],[]
    counters = {"backpressure_retries":0,"health_errors":0}
    prefix = "LOAD-SYNTHETIC-"+uuid4().hex[:8]
    report = {"synthetic":True,"model_mode":"real server embedding; no chat/vision calls","documents":args.documents,"concurrency":args.concurrency,"rows_per_document":args.rows}
    stopped = asyncio.Event()
    started = time.monotonic()
    async with httpx.AsyncClient(base_url=args.url,timeout=30,trust_env=False,limits=httpx.Limits(max_connections=args.concurrency+8)) as client:
        async def probe():
            while not stopped.is_set():
                stamp=time.monotonic()
                try:
                    response=await client.get('/api/health')
                    response.raise_for_status()
                    health.append((time.monotonic()-stamp)*1000)
                except httpx.HTTPError:
                    counters['health_errors']+=1
                await asyncio.sleep(.1)
        monitor=asyncio.create_task(probe())
        semaphore=asyncio.Semaphore(args.concurrency)
        async def submit(i):
            async with semaphore:
                body='批次,机台,克重_g_m2,不良率_pct\n'+''.join(f'{prefix}-{i}-{j},定型{j%3},{140+j%12},{(j%9)/10}\n' for j in range(args.rows))
                key=prefix+'-'+str(i)
                accepted_at=None
                for attempt in range(12):
                    tick=time.monotonic()
                    response=await client.post('/api/documents',headers={'Idempotency-Key':key},files={'file':(key+'.csv',body.encode(),'text/csv')})
                    if response.status_code in {429,503}:
                        counters['backpressure_retries']+=1
                        await asyncio.sleep(min(30,float(response.headers.get('Retry-After','2'))))
                        continue
                    response.raise_for_status()
                    doc=response.json();created.append(doc['id'])
                    accepted_at=time.monotonic()
                    ack.append((accepted_at-tick)*1000)
                    break
                if accepted_at is None:
                    raise AssertionError('Upload retry budget exhausted')
                return doc['id'],accepted_at
        try:
            uploads=await asyncio.wait_for(asyncio.gather(*(submit(i) for i in range(args.documents))),args.timeout)
            async def wait_document(doc_id,stamp):
                deadline=started+args.timeout
                while time.monotonic()<deadline:
                    response=await client.get('/api/documents/'+doc_id)
                    response.raise_for_status();doc=response.json()
                    if doc['status']=='error':
                        raise AssertionError(doc['error'])
                    if doc['status']=='ready':
                        assert doc['chunks']==args.rows
                        ready.append((time.monotonic()-stamp)*1000)
                        return
                    await asyncio.sleep(.4)
                raise TimeoutError('Documents did not become ready before timeout')
            await asyncio.gather(*(wait_document(*item) for item in uploads))
            report.update(passed=True,ready=len(ready),elapsed_seconds=round(time.monotonic()-started,2),
                upload_ack_p95_ms=percentile(ack,.95),accepted_to_observed_ready_p95_ms=percentile(ready,.95),
                health_p95_ms=percentile(health,.95),health_max_ms=max(health) if health else None,**counters)
        except BaseException as exc:
            report.update(passed=False,error=type(exc).__name__,ready=len(ready),**counters)
            raise
        finally:
            stopped.set();await monitor
            if not args.keep:
                pending=[]
                for doc_id in created:
                    response=await client.delete('/api/documents/'+doc_id)
                    if response.status_code not in {200,404}:
                        pending.append(doc_id)
                report['cleanup_pending_document_ids']=pending
            report['health_samples']=len(health)
            output=Path(args.report);output.parent.mkdir(parents=True,exist_ok=True)
            output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
            print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:8000')
    parser.add_argument('--documents',type=int,default=40)
    parser.add_argument('--concurrency',type=int,default=12)
    parser.add_argument('--rows',type=int,default=32)
    parser.add_argument('--timeout',type=float,default=240)
    parser.add_argument('--report',default='artifacts/ingestion-load.json')
    parser.add_argument('--keep',action='store_true')
    args=parser.parse_args()
    if not (1<=args.documents<=10000 and 1<=args.concurrency<=64 and 1<=args.rows<=20000):
        parser.error('documents 1..10000, concurrency 1..64, rows 1..20000')
    asyncio.run(run(args))
