from __future__ import annotations

from .gcax_parse import MLTBankBase
from .gcax_metadata import GCAXMetadataMixin
from .gcax_edit import GCAXEditMixin
from .gcax_validation import GCAXValidationMixin
from .gcax_render import GCAXRenderMixin


class MLTBank(
    GCAXRenderMixin,
    GCAXValidationMixin,
    GCAXEditMixin,
    GCAXMetadataMixin,
    MLTBankBase,
):
    """Public gcax soundbank class composed from focused implementation layers."""

    pass


__all__ = ["MLTBank"]
