"""Blueprint schema, storage, validation, and variable interpolation.

Blueprints are stored as JSON files in ~/.supreme-claudemander/blueprints/{name}.json.
"""

import json
import pathlib
import re
from typing import Any

from loguru import logger

from .config import AppConfig

# Variable name pattern: [a-zA-Z_][a-zA-Z0-9_]*
_VAR_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# Pattern to find $variable references (but not $$)
_VAR_REF_RE = re.compile(r"(?<!\$)\$([a-zA-Z_][a-zA-Z0-9_]*)")

# Allowed blueprint name pattern (same as canvas names)
_BLUEPRINT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# Valid step actions
VALID_ACTIONS = frozenset(
    {
        "get_main_profile",
        "discover_containers",
        "start_container",
        "open_terminal",
        "open_claude_terminal",
        "open_widget",
        "for_each",
    }
)

# Default timeouts per action type (seconds)
DEFAULT_TIMEOUTS = {
    "get_main_profile": 10,
    "discover_containers": 30,
    "start_container": 120,
    "open_terminal": 30,
    "open_claude_terminal": 60,
    "open_widget": 30,
    "for_each": 300,
}

# Valid provenance values for parameters
VALID_PROVENANCES = frozenset({"user", "canvas", "static"})

# Valid parameter types
VALID_PARAM_TYPES = frozenset({"string", "int", "list"})

# Numeric fields that must not contain $variable references
_NUMERIC_FIELDS = frozenset({"x", "y", "w", "h", "cols", "rows", "timeout"})


def blueprints_dir(app_config: AppConfig) -> pathlib.Path:
    """Return the blueprints directory path, creating it if needed."""
    bp_dir = app_config.config_dir / "blueprints"
    bp_dir.mkdir(parents=True, exist_ok=True)
    return bp_dir


def _valid_blueprint_name(name: str) -> bool:
    """Return True if name is a safe blueprint filename."""
    return bool(name) and bool(_BLUEPRINT_NAME_RE.match(name))


# ── CRUD ──────────────────────────────────────────────────


def list_blueprints(app_config: AppConfig) -> list[str]:
    """Return sorted list of saved blueprint names (without .json extension)."""
    bp_dir = blueprints_dir(app_config)
    names = sorted(p.stem for p in bp_dir.glob("*.json") if p.is_file())
    logger.debug("Listed {} blueprint(s)", len(names))
    return names


def read_blueprint(app_config: AppConfig, name: str) -> dict | None:
    """Read a blueprint by name. Returns None if not found."""
    if not _valid_blueprint_name(name):
        logger.warning("Invalid blueprint name: {!r}", name)
        return None
    path = blueprints_dir(app_config) / f"{name}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        logger.debug("Loaded blueprint '{}' from {}", name, path)
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read blueprint '{}': {}", name, exc)
        return None


def write_blueprint(app_config: AppConfig, name: str, data: dict) -> bool:
    """Write a blueprint to disk. Returns True on success."""
    if not _valid_blueprint_name(name):
        logger.warning("Invalid blueprint name: {!r}", name)
        return False
    bp_dir = blueprints_dir(app_config)
    path = bp_dir / f"{name}.json"
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info("Wrote blueprint '{}' to {}", name, path)
        return True
    except OSError as exc:
        logger.error("Failed to write blueprint '{}': {}", name, exc)
        return False


def delete_blueprint(app_config: AppConfig, name: str) -> bool:
    """Delete a blueprint. Returns True if it existed and was deleted."""
    if not _valid_blueprint_name(name):
        return False
    path = blueprints_dir(app_config) / f"{name}.json"
    if path.exists():
        path.unlink()
        logger.info("Deleted blueprint '{}'", name)
        return True
    return False


# ── Variable interpolation ────────────────────────────────


def interpolate_string(value: str, variables: dict[str, Any]) -> str:
    """Interpolate $variable references in a string value.

    Rules:
    - $variable is replaced with the variable's string value
    - $$ is replaced with a literal $
    - Unresolved variables raise KeyError
    """
    # First, replace $$ with a placeholder
    PLACEHOLDER = "\x00DOLLAR\x00"
    result = value.replace("$$", PLACEHOLDER)

    # Replace $variable references
    def _replace(match):
        var_name = match.group(1)
        if var_name not in variables:
            raise KeyError(f"Unresolvable variable: ${var_name}")
        return str(variables[var_name])

    result = _VAR_REF_RE.sub(_replace, result)

    # Restore literal $
    result = result.replace(PLACEHOLDER, "$")
    return result


def interpolate_value(value: Any, variables: dict[str, Any], field_name: str = "") -> Any:
    """Interpolate variables in a value, recursing into dicts and lists.

    Raises ValueError if a numeric field contains a $variable reference.
    """
    if isinstance(value, str):
        # Check for $variable in numeric fields
        if field_name in _NUMERIC_FIELDS and _VAR_REF_RE.search(value.replace("$$", "")):
            raise ValueError(f"$variable reference in numeric field '{field_name}': {value!r}")
        return interpolate_string(value, variables)
    elif isinstance(value, dict):
        return {k: interpolate_value(v, variables, field_name=k) for k, v in value.items()}
    elif isinstance(value, list):
        return [interpolate_value(item, variables, field_name=field_name) for item in value]
    return value


def find_variable_refs(value: Any) -> set[str]:
    """Find all $variable references in a value (recursing into dicts/lists)."""
    refs = set()
    if isinstance(value, str):
        # Ignore $$ escaped dollars
        cleaned = value.replace("$$", "")
        refs.update(_VAR_REF_RE.findall(cleaned))
    elif isinstance(value, dict):
        for v in value.values():
            refs.update(find_variable_refs(v))
    elif isinstance(value, list):
        for item in value:
            refs.update(find_variable_refs(item))
    return refs


# ── Validation ────────────────────────────────────────────


def validate_blueprint(blueprint: dict, context: dict | None = None) -> dict:
    """Validate a blueprint definition and optionally resolve variables.

    Args:
        blueprint: The blueprint definition dict
        context: Optional dict of canvas-context and user-supplied parameter values

    Returns:
        {
            "valid": True/False,
            "errors": [...],  # list of error strings
            "resolved_steps": [...],  # steps with variables resolved (if valid)
            "parameters": {...},  # resolved parameter values
        }
    """
    errors = []
    context = context or {}

    # Validate top-level fields
    name = blueprint.get("name")
    if not name or not isinstance(name, str):
        errors.append("Blueprint must have a non-empty 'name' string")

    if not isinstance(blueprint.get("steps", []), list):
        errors.append("'steps' must be a list")
        return {"valid": False, "errors": errors, "resolved_steps": [], "parameters": {}}

    steps = blueprint.get("steps", [])
    if not steps:
        errors.append("Blueprint must have at least one step")

    # Validate parameters
    parameters = blueprint.get("parameters", [])
    if not isinstance(parameters, list):
        errors.append("'parameters' must be a list")
        parameters = []

    resolved_params = {}
    for param in parameters:
        pname = param.get("name")
        if not pname or not _VAR_NAME_RE.match(pname):
            errors.append(f"Invalid parameter name: {pname!r}")
            continue

        provenance = param.get("provenance", "static")
        if provenance not in VALID_PROVENANCES:
            errors.append(f"Parameter '{pname}': invalid provenance '{provenance}'")

        ptype = param.get("type", "string")
        if ptype not in VALID_PARAM_TYPES:
            errors.append(f"Parameter '{pname}': invalid type '{ptype}'")

        # Resolve value from context or default
        if pname in context:
            resolved_params[pname] = context[pname]
        elif "default" in param:
            resolved_params[pname] = param["default"]
        elif provenance == "user":
            errors.append(f"User parameter '{pname}' not provided and has no default")
        elif provenance == "canvas":
            errors.append(f"Canvas parameter '{pname}' not provided by canvas context")

    # Validate steps
    available_vars = set(resolved_params.keys())
    resolved_steps = []

    for i, step in enumerate(steps):
        step_errors = _validate_step(step, i, available_vars)
        errors.extend(step_errors)

        # Track output variable
        out = step.get("out")
        if out:
            if not _VAR_NAME_RE.match(out):
                errors.append(f"Step {i}: invalid output variable name '{out}'")
            else:
                available_vars.add(out)

        # Try to resolve step values if no errors so far.
        # Use resolved_params plus placeholder values for output variables
        # from prior steps (since we don't know actual values at validation time).
        if not errors:
            try:
                resolve_vars = dict(resolved_params)
                for var in available_vars:
                    if var not in resolve_vars:
                        resolve_vars[var] = f"<{var}>"  # placeholder
                resolved = _resolve_step(step, resolve_vars)
                resolved_steps.append(resolved)
            except (KeyError, ValueError) as exc:
                errors.append(f"Step {i}: {exc}")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "resolved_steps": resolved_steps if not errors else [],
        "parameters": resolved_params,
    }


def _validate_step(step: dict, index: int, available_vars: set[str]) -> list[str]:
    """Validate a single step, returning a list of error strings."""
    errors = []

    action = step.get("action")
    if not action:
        errors.append(f"Step {index}: missing 'action' field")
        return errors

    if action not in VALID_ACTIONS:
        errors.append(f"Step {index}: unknown action '{action}'")
        return errors

    # Check for $variable in numeric fields
    for field in _NUMERIC_FIELDS:
        val = step.get(field)
        if isinstance(val, str) and _VAR_REF_RE.search(val.replace("$$", "")):
            errors.append(f"Step {index}: $variable in numeric field '{field}': {val!r}")

    # Check variable references are resolvable
    # Skip 'out' and 'action' fields
    for key, val in step.items():
        if key in ("action", "out", "steps"):
            continue
        refs = find_variable_refs(val)
        for ref in refs:
            if ref not in available_vars:
                errors.append(f"Step {index}: unresolvable variable '${ref}' in field '{key}'")

    # Validate for_each sub-steps
    if action == "for_each":
        sub_steps = step.get("steps", [])
        if not isinstance(sub_steps, list) or not sub_steps:
            errors.append(f"Step {index}: for_each must have a non-empty 'steps' list")
        else:
            # The for_each item variable is bound by the loop
            item_var = step.get("item_var", "item")
            loop_vars = available_vars | {item_var}
            for j, sub_step in enumerate(sub_steps):
                sub_errors = _validate_step(sub_step, f"{index}.{j}", loop_vars)
                errors.extend(sub_errors)
                # Sub-step outputs also become available within the loop
                sub_out = sub_step.get("out")
                if sub_out and _VAR_NAME_RE.match(sub_out):
                    loop_vars.add(sub_out)

    return errors


def _resolve_step(step: dict, variables: dict[str, Any]) -> dict:
    """Resolve variable references in a step dict."""
    resolved = {}
    for key, val in step.items():
        if key in ("action", "out", "steps"):
            resolved[key] = val
        else:
            resolved[key] = interpolate_value(val, variables, field_name=key)

    # Resolve sub-steps for for_each
    if step.get("action") == "for_each" and "steps" in step:
        # Sub-steps are not fully resolvable at validate time (need loop variable)
        resolved["steps"] = step["steps"]

    return resolved
