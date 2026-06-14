"""
ComfyUI-WorkerKeeper - Background service that kills idle comfy-env isolation workers.
mxToolkit-style single-file node module.

Two-layer architecture:
  1. on_prompt_handler (FAST)  — kills envs with ZERO nodes in the prompt JSON
  2. stage_node_execution (ACCURATE) — kills envs whose nodes are ALL cached / not in execution plan
"""

import logging
import os

log = logging.getLogger("workerkeeper")

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

# Wildcard type for trigger passthrough
ANYPE = "*"

_mapping_cache = None


def _extract_env_dir(cls):
    func_name = getattr(cls, "FUNCTION", None)
    if not func_name:
        return None
    func = getattr(cls, func_name, None)
    if func is None:
        return None
    try:
        freevars = func.__code__.co_freevars
        closure = func.__closure__
        if closure and "ed" in freevars:
            return closure[freevars.index("ed")].cell_contents
    except Exception:
        pass
    return None


def _build_env_mapping():
    import nodes as comfy_nodes
    mapping = {}
    for class_type, cls in comfy_nodes.NODE_CLASS_MAPPINGS.items():
        if not getattr(cls, "_comfy_env_isolated", False):
            continue
        env_dir = _extract_env_dir(cls)
        if env_dir is not None:
            mapping[class_type] = str(env_dir)
    unique = set(mapping.values())
    log.info("WorkerKeeper: mapped %d isolated node types across %d env(s)", len(mapping), len(unique))
    return mapping


def _alive_workers(worker_pool=None):
    if worker_pool is None:
        try:
            from comfy_env.isolation.wrap import _WORKER_POOL as worker_pool
        except ImportError:
            return
    for env_key, (worker, gen) in list(worker_pool.items()):
        if not getattr(worker, "_shutdown", False):
            yield env_key, worker, gen


def _shutdown_worker(env_key, worker, worker_pool, patcher_pool):
    log.info("WorkerKeeper: killing idle worker %s (env=%s)", worker.name, env_key)
    proc = getattr(worker, "_process", None)
    temp_dir = getattr(worker, "_temp_dir", None)
    if proc and proc.poll() is None:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
    if temp_dir:
        try:
            import shutil
            shutil.rmtree(str(temp_dir), ignore_errors=True)
        except Exception:
            pass
    worker_pool.pop(env_key, None)
    patcher_pool.pop(env_key, None)


def _short_name(path_str):
    name = path_str.replace("\\", "/").rstrip("/").split("/")[-1]
    if name.endswith("-nodes"):
        name = name[:-6]
    return name


def _get_env_names():
    names = set()
    global _mapping_cache

    if _mapping_cache is None or not _mapping_cache:
        import nodes as comfy_nodes
        log.warning(
            "WorkerKeeper: method1 NODE_CLASS_MAPPINGS has %d entries",
            len(comfy_nodes.NODE_CLASS_MAPPINGS),
        )
        if comfy_nodes.NODE_CLASS_MAPPINGS:
            try:
                _mapping_cache = _build_env_mapping()
            except Exception:
                _mapping_cache = {}
    if _mapping_cache:
        log.warning(
            "WorkerKeeper: method1 mapping has %d entries: %s",
            len(_mapping_cache), list(_mapping_cache.keys())[:5],
        )
        for env_dir in _mapping_cache.values():
            names.add(_short_name(env_dir))

    try:
        from comfy_env.isolation.wrap import _WORKER_POOL as pool
        log.warning("WorkerKeeper: method2 worker pool has %d entries", len(pool))
        for env_key in pool:
            names.add(_short_name(env_key))
    except Exception as e:
        log.warning("WorkerKeeper: method2 failed: %s", e)

    try:
        from pathlib import Path
        localappdata = os.environ.get("LOCALAPPDATA", "<NOT SET>")
        workspace = Path(localappdata) / "Programs" / "comfy-env"
        log.warning("WorkerKeeper: method3 scanning %s", workspace)
        for root in (workspace / "envs", workspace / ".pixi" / "envs"):
            if root.is_dir():
                log.warning("WorkerKeeper: method3 found dir %s", root)
                for child in sorted(root.iterdir()):
                    if child.is_dir():
                        short = _short_name(child.name)
                        log.warning("WorkerKeeper: method3 found env %s", short)
                        names.add(short)
            else:
                log.warning("WorkerKeeper: method3 dir NOT FOUND %s", root)
    except Exception as e:
        log.warning("WorkerKeeper: method3 failed: %s", e)

    names.discard("default")
    log.warning("WorkerKeeper: discovered envs: %s", sorted(names))
    return sorted(names)


def _kill_envs_by_name(target_names):
    try:
        from comfy_env.isolation.wrap import _WORKER_POOL, _WORKER_PATCHERS
    except ImportError:
        return
    for env_key, (worker, gen) in list(_WORKER_POOL.items()):
        if getattr(worker, "_shutdown", False):
            continue
        for short in target_names:
            if (short + "-nodes") in env_key:
                _shutdown_worker(env_key, worker, _WORKER_POOL, _WORKER_PATCHERS)
                log.info("WorkerKeeper (manual): killed %s via node trigger", short)
                break


# Layer 1 - FAST PATH
def _on_prompt_handler(json_data):
    prompt = json_data.get("prompt", {})
    all_ct = set()
    for node_data in prompt.values():
        ct = node_data.get("class_type") if isinstance(node_data, dict) else None
        if ct:
            all_ct.add(ct)
    if not all_ct:
        return json_data
    try:
        from comfy_env.isolation.wrap import _WORKER_POOL as worker_pool
        from comfy_env.isolation.wrap import _WORKER_PATCHERS as patcher_pool
    except ImportError:
        return json_data
    global _mapping_cache
    if _mapping_cache is None:
        _mapping_cache = _build_env_mapping()
    if not _mapping_cache:
        return json_data
    needed = set()
    for ct in all_ct:
        ek = _mapping_cache.get(ct)
        if ek:
            needed.add(ek)
    killed = 0
    for env_key, worker, _gen in _alive_workers(worker_pool):
        if env_key not in needed:
            _shutdown_worker(env_key, worker, worker_pool, patcher_pool)
            killed += 1
    if killed:
        log.info("WorkerKeeper (fast): killed %d idle worker(s)", killed)
    return json_data


# Layer 2 - ACCURATE PATH
_WK_PATCHED = False


def _install_execution_hook():
    global _WK_PATCHED
    if _WK_PATCHED:
        return
    import comfy_execution.graph as _graph
    original = _graph.ExecutionList.stage_node_execution

    async def _wk_stage(self):
        if not hasattr(self, "_wk_checked"):
            self._wk_checked = True
            needed_ct = set()
            for node_id in list(self.pendingNodes.keys()):
                try:
                    node = self.dynprompt.get_node(node_id)
                    ct = node.get("class_type")
                    if ct:
                        needed_ct.add(ct)
                except Exception:
                    pass
            if not needed_ct:
                return await original(self)
            try:
                from comfy_env.isolation.wrap import _WORKER_POOL as worker_pool
                from comfy_env.isolation.wrap import _WORKER_PATCHERS as patcher_pool
            except ImportError:
                return await original(self)
            global _mapping_cache
            if _mapping_cache is None:
                _mapping_cache = _build_env_mapping()
            if not _mapping_cache:
                return await original(self)
            needed = set()
            for ct in needed_ct:
                ek = _mapping_cache.get(ct)
                if ek:
                    needed.add(ek)
            killed = 0
            for env_key, worker, _gen in _alive_workers(worker_pool):
                if env_key not in needed:
                    _shutdown_worker(env_key, worker, worker_pool, patcher_pool)
                    killed += 1
            if killed:
                log.info("WorkerKeeper (exact): killed %d worker(s) via execution list", killed)
        return await original(self)

    _graph.ExecutionList.stage_node_execution = _wk_stage
    _WK_PATCHED = True
    log.info("WorkerKeeper: ExecutionList hook installed")


# Manual Kill Node
class WorkerKeeperManualKill:
    @classmethod
    def INPUT_TYPES(cls):
        envs = _get_env_names()
        required = {"trigger": (ANYPE,)}
        for name in envs:
            required[name] = ("BOOLEAN", {"default": False})
        return {"required": required}

    RETURN_TYPES = (ANYPE,)
    RETURN_NAMES = ("trigger",)
    FUNCTION = "execute"
    OUTPUT_NODE = True
    CATEGORY = "WorkerKeeper"

    def execute(self, trigger=None, **kwargs):
        wanted = []
        for key, val in kwargs.items():
            if key == "trigger":
                continue
            if isinstance(val, str):
                val = val.lower() == "true"
            if val:
                wanted.append(key)
        if wanted:
            _kill_envs_by_name(wanted)
        return (trigger,)


NODE_CLASS_MAPPINGS["WorkerKeeperManualKill"] = WorkerKeeperManualKill
NODE_DISPLAY_NAME_MAPPINGS["WorkerKeeperManualKill"] = "Worker Keeper \u2014 Manual Kill"


# Registration
_web = None
try:
    import server
    from aiohttp import web as _web
    instance = server.PromptServer.instance
    if instance is not None:
        instance.add_on_prompt_handler(_on_prompt_handler)
        _install_execution_hook()
        instance.routes.get("/workerkeeper/status")(_route_status)
        instance.routes.post("/workerkeeper/kill_all")(_route_kill_all)
        log.info("WorkerKeeper: registered (fast=on_prompt, accurate=ExecutionList) + /workerkeeper/* routes")
    else:
        log.warning("WorkerKeeper: PromptServer.instance is None")
except ImportError:
    log.warning("WorkerKeeper: server module not available")
except Exception as e:
    log.warning("WorkerKeeper: registration failed: %s", e)


# API routes
async def _route_status(_request):
    try:
        import comfy_env.isolation.wrap as _wrap
        workers = {}
        for env_key, (worker, gen) in _wrap._WORKER_POOL.items():
            workers[env_key] = {"alive": worker.is_alive(), "generation": gen, "name": worker.name}
        return _web.json_response({"workers": workers, "count": len(workers)})
    except Exception as e:
        return _web.json_response({"error": str(e)}, status=500)


async def _route_kill_all(_request):
    try:
        import comfy_env.isolation.wrap as _wrap
        count = len(_wrap._WORKER_POOL)
        for env_key, (worker, gen) in list(_wrap._WORKER_POOL.items()):
            try:
                worker.shutdown()
            except Exception:
                pass
        _wrap._WORKER_POOL.clear()
        _wrap._WORKER_PATCHERS.clear()
        log.info("WorkerKeeper: killed all %d workers (manual)", count)
        return _web.json_response({"killed": count})
    except Exception as e:
        return _web.json_response({"error": str(e)}, status=500)
