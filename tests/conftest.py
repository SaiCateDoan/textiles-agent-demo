import hashlib
import os
import tempfile
from pathlib import Path

import pytest

# Never touch the running demo's data when importing the application in tests.
os.environ["TEXTILE_DATA_DIR"] = tempfile.mkdtemp(prefix="textile-test-app-")

from backend.retrieval import VectorIndex
from backend.store import Store, now, uid
from backend.ingest import extract, split_blocks


class LocalTestIndex(VectorIndex):
    """Deterministic vectors only in unit tests. LanceDB I/O remains real."""
    def embed(self, texts, query=False):
        import numpy as np
        out=[]
        for text in texts:
            v=np.zeros(512, dtype=float)
            for ch in text:
                v[int(hashlib.md5(ch.encode()).hexdigest()[:8],16)%512]+=1
            v/=np.linalg.norm(v) or 1
            out.append(v.tolist())
        return out


@pytest.fixture
def storage(tmp_path):
    s=Store(tmp_path/'data')
    idx=LocalTestIndex(tmp_path/'data','unit-test')
    return s,idx


def load_doc(storage, path, name=None):
    s,idx=storage
    doc_id=uid()
    s.execute("INSERT INTO documents(id,name,suffix,size,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (doc_id,name or path.name,path.suffix,path.stat().st_size,'ready',now(),now()))
    extraction=extract(path,path.suffix)
    blocks=split_blocks(extraction.blocks)
    for b in blocks:
        b.update(id=uid(),revision=1)
    from backend.datasets import stage_datasets,write_datasets
    datasets=stage_datasets(s,doc_id,1,extraction,blocks)
    idx.replace(doc_id,blocks,1)
    s.replace_chunks(doc_id,blocks)
    with s.connect() as connection:
        write_datasets(connection,doc_id,1,datasets)
    s.update_doc(doc_id,chunks=len(blocks))
    (s.root/'uploads'/f'{doc_id}{path.suffix}').write_bytes(path.read_bytes())
    return doc_id


@pytest.fixture
def sample_root():
    return Path(__file__).resolve().parents[1]/'samples'
