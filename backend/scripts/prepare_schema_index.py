from __future__ import annotations

import json

from app.config import settings
from app.model_client import ModelClient
from app.retrieval import SchemaIndex


if __name__ == "__main__":
    index = SchemaIndex(ModelClient(settings), settings)
    index.ensure_built()
    print(json.dumps(index.status(), ensure_ascii=False, indent=2))
