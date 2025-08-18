#!/usr/bin/env python3
"""
Secure PPO Loader - No Pickle Execution
Replaces all PPO.load() calls in the Swarm system.
"""

import io
import json
import zipfile
from pathlib import Path
from typing import Any, Mapping

import torch as th
from stable_baselines3 import PPO

# Activation mapping
_ACT_MAP = {
    "relu": th.nn.ReLU,
    "tanh": th.nn.Tanh,
    "elu": th.nn.ELU,
    "leakyrelu": th.nn.LeakyReLU,
    "silu": th.nn.SiLU,
    "gelu": th.nn.GELU,
    "mish": th.nn.Mish,
    "selu": th.nn.SELU,
    "celu": th.nn.CELU,
}

SAFE_META_FILENAME = "safe_policy_meta.json"


def _parse_activation(name: str) -> type[th.nn.Module]:
    """Parse activation function name to PyTorch class."""
    key = name.strip().split(".")[-1].lower()
    return _ACT_MAP.get(key, th.nn.ReLU)


def _choose_policy_class_from_env(env) -> str:
    """Choose policy class based on environment observation space."""
    try:
        from gymnasium import spaces
        return "MultiInputPolicy" if isinstance(env.observation_space, spaces.Dict) else "MlpPolicy"
    except:
        return "MlpPolicy"


def _extract_policy_state_dict(raw_obj: Any) -> Mapping[str, th.Tensor]:
    """Extract policy state dict from loaded PyTorch object."""
    if isinstance(raw_obj, Mapping):
        if "mlp_extractor.policy_net.0.weight" in raw_obj or "action_net.weight" in raw_obj or "log_std" in raw_obj:
            return raw_obj
        if "policy" in raw_obj and isinstance(raw_obj["policy"], Mapping):
            return raw_obj["policy"]
    raise RuntimeError("Could not interpret loaded object as policy state_dict.")


def secure_load_ppo(model_path: Path, *, env, device: str = "cpu") -> PPO:
    """
    Secure replacement for PPO.load() - requires JSON metadata.
    
    Args:
        model_path: Path to model ZIP
        env: Environment for policy initialization  
        device: Device to load on
        
    Returns:
        PPO model loaded securely
        
    Raises:
        FileNotFoundError: Missing required files
        RuntimeError: PyTorch doesn't support weights_only
    """
    # Check PyTorch version
    try:
        from inspect import signature
        if "weights_only" not in signature(th.load).parameters:
            raise RuntimeError("PyTorch version doesn't support weights_only=True")
    except:
        raise RuntimeError("Cannot verify PyTorch weights_only support")

    with zipfile.ZipFile(str(model_path), "r") as zf:
        names = set(zf.namelist())

        # Require both files
        if SAFE_META_FILENAME not in names:
            raise FileNotFoundError(f"Missing {SAFE_META_FILENAME} - model not compatible with secure loading")
        if "policy.pth" not in names:
            raise FileNotFoundError("Missing policy.pth")

        # Read JSON metadata
        with zf.open(SAFE_META_FILENAME, "r") as f:
            meta = json.loads(f.read().decode("utf-8"))

        act_name: str = meta["activation_fn"]
        net_arch: Any = meta["net_arch"]
        use_sde: bool = bool(meta["use_sde"])

        # Load weights safely
        with zf.open("policy.pth", "r") as f:
            raw = f.read()

    # Load tensors with weights_only=True (no pickle execution)
    obj = th.load(io.BytesIO(raw), map_location=device, weights_only=True)
    state_dict = _extract_policy_state_dict(obj)

    # Create fresh PPO
    policy_class = _choose_policy_class_from_env(env)
    policy_kwargs = {"activation_fn": _parse_activation(act_name), "net_arch": net_arch}
    model = PPO(policy_class, env, device=device, policy_kwargs=policy_kwargs, use_sde=use_sde)

    # Load weights strictly
    incompat = model.policy.load_state_dict(state_dict, strict=True)
    if getattr(incompat, "missing_keys", []) or getattr(incompat, "unexpected_keys", []):
        raise RuntimeError(f"State dict mismatch: missing={getattr(incompat, 'missing_keys', [])}, unexpected={getattr(incompat, 'unexpected_keys', [])}")

    if getattr(model, "use_sde", False):
        model.policy.reset_noise()

    return model