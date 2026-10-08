"""Keep test artifacts on the external disk without recursive deletion."""
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


@contextmanager
def artifact_directory():
    path = Path(__file__).resolve().parents[1] / "data" / "test_artifacts" / uuid4().hex
    path.mkdir(parents=True, exist_ok=False)
    yield str(path)
