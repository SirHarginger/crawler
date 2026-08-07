from __future__ import annotations

import pytest

from screening_crawler.datasets import load_adapter


def test_load_adapter_imports_registered_dataset() -> None:
    adapter = load_adapter("un_sc_consolidated")
    assert adapter.metadata.key == "un_sc_consolidated"


def test_load_adapter_rejects_unknown_key() -> None:
    with pytest.raises(ValueError, match="no dataset package registered"):
        load_adapter("not_a_real_dataset")
