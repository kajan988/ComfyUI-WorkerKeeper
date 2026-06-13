# ComfyUI-WorkerKeeper

**One optional node. Two hooks. Clean RAM & VRAM.**

WorkerKeeper is a background service for ComfyUI that automatically kills idle comfy-env isolation subprocesses. It requires zero workflow changes — install and forget. An optional manual override node is also provided.

## The Problem

Custom node packs using `comfy-env` (GeometryPack, GaussianPack, SAM3, MoGe2, Sharp, PanoPack, DepthAnythingV3, etc.) spawn **persistent Python subprocesses** (~2 GB each) for dependency isolation. These subprocesses:

- Live forever once created — ComfyUI never kills them
- Occupy 2+ GB of system RAM each
- Reserve ~200 MB–2 GB of GPU VRAM (model weights loaded in the subprocess)
- Multiply: 7 packs → **14 GB RAM + VRAM wasted** when unused

**Before WorkerKeeper**: switching from a GeometryPack workflow to a standard image-to-image workflow leaves all 7 subprocesses alive, burning 14 GB for nothing.

## How It Works — Two-Layer Architecture

WorkerKeeper uses **two independent detection layers**, neither of which modifies any ComfyUI core file or any comfy-env environment file:

### Layer 1 — `on_prompt_handler` (Fast Path)

Triggered synchronously **before** prompt execution begins. Scans the prompt JSON for node `class_type` values. If an isolation environment has **zero** nodes in the prompt, its worker is killed immediately.

**Cost**: ~0.05 ms when no kill is needed; ~50 ms per killed worker.

### Layer 2 — `ExecutionList.stage_node_execution` (Accurate Path)

A zero-invasiveness monkey-patch on `ExecutionList.stage_node_execution` (one method replacement — no ComfyUI internals are modified, no files are patched on disk). Runs **after** ComfyUI's cache resolution, so `self.pendingNodes` contains only the nodes that will actually execute. Kills workers whose environment appears nowhere in the real execution plan.

**Cost**: ~0.05 ms when no kill is needed; ~50 ms per killed worker.

### What We Do NOT Touch

| Component | Modified? |
|-----------|-----------|
| `ComfyUI_CORE/execution.py` | ❌ No |
| `ComfyUI_CORE/server.py` | ❌ No |
| `comfy_execution/graph.py` | ❌ No (runtime class patching only) |
| Pixi environments (`comfy-env/envs/*`) | ❌ No |
| comfy-env package | ❌ No |
| Any node's `__init__.py` | ❌ No |
| Any workflow JSON | ❌ No |

The proxy class env_dir mapping is extracted via **Python closure introspection** (reading `func.__code__.co_freevars` + `__closure__`) — no files read, no configs parsed.

## Scenario Behavior Table

| Scenario | What Happens | Workers Killed | Workers Kept |
|----------|-------------|----------------|--------------|
| **Full workflow with isolation nodes** (e.g., GeomPackRemesh → PreviewMesh) | Layer 1 sees GeomPack* in prompt. Layer 2 confirms they're in pendingNodes. | None | geometrypack |
| **Full workflow without isolation nodes** (e.g., KSampler → VAEDecode → SaveImage) | Layer 1 sees zero isolation class_types. Fast path kills everything. | sharp, moge2, geometrypack, gaussianpack, sam3, panopack, depthanythingv3 | None |
| **Partial execution on non-isolation nodes** (run only KSampler in a mixed workflow) | Layer 1 sees all class_types (full prompt). Layer 2 checks pendingNodes — finds only ComfyUI native nodes. | All 7 envs | None |
| **Partial execution on isolation subgraph** (run only GeomPackRemesh) | Layer 1 sees GeomPack* in prompt. Layer 2 confirms they're in pendingNodes. | sharp, moge2, sam3, etc. | geometrypack |
| **All isolation nodes muted or bypassed** | Layer 1 sees class_types. Layer 2 sees them NOT in pendingNodes (ComfyUI removes bypassed nodes from the execution graph). | All 7 envs | None |
| **One env muted, one active** (GeomPackRemesh active, SharpPredict muted) | Layer 1 sees both. Layer 2 sees only GeomPackRemesh in pendingNodes. | sharp | geometrypack |
| **All nodes cached (re-run identical prompt)** | Layer 1 sees class_types. Layer 2 finds pendingNodes empty (everything cached). All envs without cached-only nodes in the prompt are killed. | Varies | — |
| **Workflow switch** (GeometryPack → Sharp → standard → GeometryPack) | Each prompt re-evaluates. Workers killed/created on demand. | See rows above | — |

## Visual Flow

```
User clicks "Queue Prompt"
          │
          ▼
┌─────────────────────────────────────┐
│  Layer 1: on_prompt_handler         │  ◄── registered via server.PromptServer.instance
│                                     │       .add_on_prompt_handler()
│  Scan prompt JSON for all           │
│  class_type values                  │
│                                     │
│  Any isolation env has 0 nodes      │
│  in the full prompt?                │
│         │ YES                       │
│         ├──► Kill that env's worker │
│         │    (fast: ~50ms)          │
│         └──► Continue               │
└─────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────┐
│  ComfyUI resolves caches:           │
│  builds ExecutionList with          │
│  add_node() for each output         │
│                                     │
│  pendingNodes now contains ONLY     │
│  nodes that MUST execute            │
│  (cached nodes are excluded)        │
└─────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────┐
│  Layer 2: stage_node_execution      │  ◄── monkey-patch on
│                                     │       ExecutionList (runtime only)
│  (first call only)                  │
│                                     │
│  Scan pendingNodes for isolation    │
│  class_types                        │
│                                     │
│  Any live worker's env has 0        │
│  matches in pendingNodes?           │
│         │ YES                       │
│         ├──► Kill that env's worker │
│         │    (~50ms, silent kill)   │
│         └──► Continue               │
└─────────────────────────────────────┘
          │
          ▼
     Execution begins ───► Nodes run normally, workers created on demand
```

## What "Silent Kill" Means

When WorkerKeeper terminates an idle subprocess, it uses `proc.kill()` + `proc.wait()` directly **instead** of the comfy-env graceful shutdown sequence. This avoids the "RuntimeError: Worker process died" / "Subprocess worker die" messages that appear in ComfyUI logs when the socket-based shutdown handshake is interrupted. The process is terminated instantly, and its temp directory is cleaned up immediately.

No log noise. No tracebacks.

## Manual API Endpoints

Two HTTP routes are registered on the ComfyUI server:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/workerkeeper/status` | GET | Returns JSON with all live workers and their state |
| `/workerkeeper/kill_all` | POST | Kills all isolation workers immediately |

Example:
```bash
curl http://localhost:8188/workerkeeper/status
# → {"workers": {"C:\\...geometrypack-nodes": {"alive": true, ...}}, "count": 1}

curl -X POST http://localhost:8188/workerkeeper/kill_all
# → {"killed": 1}
```

## Manual Kill Node (Optional)

In addition to the automated background service, WorkerKeeper provides a **workflow node** for manual control:

### Worker Keeper — Manual Kill

| Category | Value |
|----------|-------|
| **Class** | `WorkerKeeperManualKill` |
| **Category** | `WorkerKeeper` |
| **Input** | `trigger` (any type, passed through unchanged) |
| **Output** | `trigger` (same value as input) |
| **Widgets** | One BOOLEAN toggle per discovered environment |

![Node Preview](https://via.placeholder.com/300x200/1a1a2e/e0e0e0?text=Worker+Keeper+Node)

The node automatically detects all installed comfy-env environments at startup by:
1. Scanning proxy class closures (comfy-env's `_comfy_env_isolated` classes)
2. Reading the live `_WORKER_POOL` (already-running workers)
3. Scanning the pixi filesystem (`comfy-env/envs/*`)

For each detected environment, a toggle switch appears on the node. Set it to **ON** (True) and connect any trigger input — when the node executes, the selected environment's subprocess is killed. The trigger value passes through to the output unchanged, so the node can be inserted anywhere in a workflow without affecting data flow.

### Usage Example

```
LoadImage ──► WorkerKeeperManualKill ──► PreviewImage
                   │
             [geometrypack] ◄── True → kills geometrypack worker
             [sharp]        ◄── False → leaves sharp worker alive
             [sam3]         ◄── False → leaves sam3 worker alive
```

All toggles default to **OFF** (False). Only explicitly enabled environments are killed when the node triggers. Environments with OFF toggles remain under the control of the automatic background service (Layers 1 & 2).

## Benchmark: Impact on System

### WorkerKeeper Itself

| Operation | Frequency | CPU Time | Blocks Execution? |
|-----------|-----------|----------|-------------------|
| Module init + hook registration | Once at startup | < 0.01 ms | No |
| Build env mapping (closure scan) | Once (lazy, first prompt) | < 1 ms | No |
| Layer 1 scan (no kill) | Every prompt | ~0.05 ms | Yes (negligible) |
| Layer 2 scan (no kill) | Every prompt | ~0.05 ms | No (async) |
| Kill one worker | Per unused env | ~50 ms | Varies |

### Memory Savings per Environment

| Environment | RAM Freed | VRAM Freed (typical) |
|-------------|-----------|---------------------|
| geometrypack-nodes | ~2000 MB | ~200-500 MB |
| gaussianpack-nodes | ~2000 MB | ~100-300 MB |
| sam3-nodes | ~2000 MB | ~500-1500 MB (model) |
| sharp-nodes | ~2000 MB | ~500-1000 MB (model) |
| moge2-nodes | ~2000 MB | ~300-800 MB (model) |
| panopack-nodes | ~2000 MB | ~200-500 MB |
| depthanythingv3-nodes | ~2000 MB | ~300-800 MB (model) |

**Total potential savings**: 2.5+ GB system RAM + 2-5+ GB GPU VRAM.

## Installation

### Via Git URL (ComfyUI Manager)
1. Open ComfyUI Manager
2. "Install via Git URL"
3. Enter `https://github.com/PozzettiAndrea/ComfyUI-WorkerKeeper.git`

### Manual
```bash
cd ComfyUI/custom_nodes
git clone https://github.com/PozzettiAndrea/ComfyUI-WorkerKeeper.git
```

**No dependencies.** WorkerKeeper only uses Python stdlib + the already-installed `comfy-env`, `server`, and `comfy_execution` modules that ship with ComfyUI.
