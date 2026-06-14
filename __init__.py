"""
ComfyUI-WorkerKeeper — Background service that kills idle comfy-env isolation workers.
"""

from .workerkeeper import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
