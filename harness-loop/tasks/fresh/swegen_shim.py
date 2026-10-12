"""Run SWE-gen's CLI with its model choices rebound from the environment.

Usage: python3 swegen_shim.py create --repo OWNER/NAME --pr N [swegen create options]

Upstream SWE-gen hardwires two things we replace:
- gpt-5.5 for the instruction writer and the verdict synthesis. The model names are
  default arguments bound at import time, so changing the module constant alone does
  nothing; the shim rewrites the functions' __defaults__/__kwdefaults__ too.
  With OPENAI_BASE_URL pointing at Gemini's OpenAI-compatible endpoint, the `openai`
  SDK sends those calls to Gemini instead (model from SWEGEN_LLM_MODEL).
- claude-opus-4-8 for the Claude Code session that writes the Dockerfile and test.sh.
  That one is a literal `ClaudeAgentOptions(model="claude-opus-4-8")`, so the shim
  wraps ClaudeAgentOptions in that module (model from SWEGEN_CLAUDE_MODEL). It also
  wraps that module's `query` to print each session's total_cost_usd to stderr, which
  SWE-gen does not report and build.sh uses as the task's measured cost. With
  SWEGEN_CLAUDE_MAX_BUDGET_USD set, the same wrapper passes it as the session's
  max_budget_usd, so the Agent SDK ends a session that would cost more.

Every place patched is listed in PATCH_TARGETS. If a SWE-gen upgrade moves one, the
shim fails loudly ("nothing to patch") instead of silently calling the old model.
"""
import importlib
import inspect
import os
import sys
from importlib.metadata import entry_points

# What the shim patches. Checked against abundant-ai/SWE-gen src/swegen as of 2026-07-13;
# fix here if the installed version differs.
#   (module, value SWE-gen ships, env var holding our replacement, wrap OPTIONS_CLASS?)
PATCH_TARGETS = (
    # MODEL_NAME = "gpt-5.5"; evaluate_and_generate_task(..., model: str = MODEL_NAME)
    ("swegen.create.task_instruction", "gpt-5.5", "SWEGEN_LLM_MODEL", False),
    # VERDICT_MODEL = "gpt-5.5"; verdict functions default model=VERDICT_MODEL
    ("swegen.analyze.classifier", "gpt-5.5", "SWEGEN_LLM_MODEL", False),
    # `from ...classifier import VERDICT_MODEL` plus a typer.Option(VERDICT_MODEL) default
    ("swegen.cli", "gpt-5.5", "SWEGEN_LLM_MODEL", False),
    # ClaudeAgentOptions(..., model="claude-opus-4-8") inside the build-session runner
    ("swegen.create.claude_code_runner", "claude-opus-4-8", "SWEGEN_CLAUDE_MODEL", True),
)
# Module settings read at call time, raised for Gemini: its thinking tokens count toward
# max_completion_tokens (4096 left 150 for the answer and failed the parse), and a
# thinking model can need longer than 90 s.   (module, name, value SWE-gen ships, ours)
SETTING_TARGETS = (
    ("swegen.create.task_instruction", "MAX_COMPLETION_TOKENS", 4096, 32768),
    ("swegen.create.task_instruction", "OPENAI_API_TIMEOUT", 90.0, 600.0),
)
OPTIONS_CLASS = "ClaudeAgentOptions"  # callable whose literal model= kwarg is rewritten
QUERY_FUNC = "query"                  # claude_agent_sdk.query as imported there; cost logged
BUDGET_ENV = "SWEGEN_CLAUDE_MAX_BUDGET_USD"  # optional per-session cap, passed as max_budget_usd
LOG_TAG_ENV = "SWEGEN_LOG_TAG"  # random per run; tags the cost-report lines
ENTRY_POINT = "swegen"                # console_scripts name of SWE-gen's CLI

# Env the build needs: calls must go to Gemini and Bedrock, never OpenAI or a personal login.
REQUIRED_ENV = ("SWEGEN_LLM_MODEL", "SWEGEN_CLAUDE_MODEL", "OPENAI_BASE_URL", "OPENAI_API_KEY")
FORBIDDEN_ENV = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")
FORBIDDEN_PREFIXES = ("AWS_ADMIN_",)  # admin AWS keys must never reach the build session


class ShimError(RuntimeError):
    pass


def check_env(env):
    missing = [k for k in REQUIRED_ENV if not env.get(k)]
    if missing:
        raise ShimError(f"missing env: {', '.join(missing)}")
    if env.get("CLAUDE_CODE_USE_BEDROCK") != "1":
        raise ShimError("CLAUDE_CODE_USE_BEDROCK must be 1 (the build session runs on Bedrock)")
    present = [k for k in FORBIDDEN_ENV if env.get(k)]
    present += sorted(k for k in env if k.startswith(FORBIDDEN_PREFIXES))
    if present:
        raise ShimError(f"unset {', '.join(present)}: the build session must not use a direct key or login")


def _swap(value, old, new):
    """Return (replacement, changed) for one default value."""
    if value == old and isinstance(value, str):
        return new, True
    # typer.Option(...) / click-style holders keep the real default on `.default`
    if getattr(value, "default", None) == old and not isinstance(value, str):
        value.default = new
        return value, True
    return value, False


def _patch_function(fn, old, new):
    n = 0
    if fn.__defaults__:
        swapped = [_swap(v, old, new) for v in fn.__defaults__]
        n += sum(c for _, c in swapped)
        fn.__defaults__ = tuple(v for v, _ in swapped)
    if fn.__kwdefaults__:
        for k, v in fn.__kwdefaults__.items():
            fn.__kwdefaults__[k], changed = _swap(v, old, new)
            n += changed
    return n


def _functions(module):
    """Functions defined in module, including methods of its classes."""
    for obj in vars(module).values():
        if inspect.isfunction(obj) and obj.__module__ == module.__name__:
            yield obj
        elif inspect.isclass(obj) and obj.__module__ == module.__name__:
            for attr in vars(obj).values():
                fn = getattr(attr, "__func__", attr)  # staticmethod / classmethod
                if inspect.isfunction(fn):
                    yield fn


def _wrap_options(cls, old, new, max_budget_usd=None):
    def options(*args, **kwargs):
        if kwargs.get("model") == old:
            kwargs["model"] = new
            if max_budget_usd is not None:  # ours, or a lower one SWE-gen already set
                given = kwargs.get("max_budget_usd")
                kwargs["max_budget_usd"] = max_budget_usd if given is None else min(given, max_budget_usd)
        return cls(*args, **kwargs)
    return options


def _max_budget(env, cls):
    """The per-session USD cap from SWEGEN_CLAUDE_MAX_BUDGET_USD, or None if unset."""
    raw = env.get(BUDGET_ENV)
    if not raw:
        return None
    try:
        usd = float(raw)
    except ValueError:
        raise ShimError(f"{BUDGET_ENV} must be a number, got {raw!r}") from None
    if usd <= 0:
        raise ShimError(f"{BUDGET_ENV} must be above 0, got {raw!r}")
    try:
        params = inspect.signature(cls).parameters
    except (TypeError, ValueError):
        params = {}
    if "max_budget_usd" not in params:
        raise ShimError(f"{OPTIONS_CLASS} takes no max_budget_usd: upgrade claude-agent-sdk or unset {BUDGET_ENV}")
    return usd


def _report(what, env=os.environ):
    """One cost-report line, tagged with SWEGEN_LOG_TAG so the driver can tell it from
    repo or transcript text that merely looks like one."""
    tag = env.get(LOG_TAG_ENV, "")
    print(f"swegen_shim{f'[{tag}]' if tag else ''}: {what}", file=sys.stderr, flush=True)


def _wrap_query(query):
    async def logged(*args, **kwargs):
        _report("session_start")  # a start with no cost after it means the session was cut off
        async for message in query(*args, **kwargs):
            cost = getattr(message, "total_cost_usd", None)
            if cost is not None:
                _report(f"total_cost_usd: {cost}")
            yield message
    return logged


def apply_settings(targets=SETTING_TARGETS):
    for name, attr, old, new in targets:
        module = importlib.import_module(name)
        if getattr(module, attr, None) != old:
            raise ShimError(f"{name}.{attr} is no longer {old!r}: update SETTING_TARGETS")
        setattr(module, attr, new)


def apply(targets=PATCH_TARGETS, env=os.environ):
    """Import each target module and rebind `old` to env[var]. Returns patch counts.

    Raises ShimError if an env var is unset, or a module no longer mentions `old` or
    has nothing to patch (SWE-gen moved it: update PATCH_TARGETS).
    """
    counts = {}
    # Import everything first: a module that copies a constant at import (cli.py's
    # `from ...classifier import VERDICT_MODEL`) must copy the old value, then be patched.
    modules = {name: importlib.import_module(name) for name, *_ in targets}
    for name, old, var, wrap in targets:
        new = env.get(var)
        if not new:
            raise ShimError(f"{var} is not set")
        module = modules[name]
        src = inspect.getsource(module)
        imported = any(isinstance(v, str) and v == old for v in vars(module).values())
        if f'"{old}"' not in src and f"'{old}'" not in src and not imported:
            raise ShimError(f"{name} no longer mentions {old!r}: update PATCH_TARGETS")
        n = 0
        for attr, value in list(vars(module).items()):
            if isinstance(value, str) and value == old:
                setattr(module, attr, new)
                n += 1
        n += sum(_patch_function(fn, old, new) for fn in _functions(module))
        if wrap:
            options = getattr(module, OPTIONS_CLASS, None)
            if options is None:
                raise ShimError(f"{name} has no {OPTIONS_CLASS}: update PATCH_TARGETS")
            setattr(module, OPTIONS_CLASS, _wrap_options(options, old, new, _max_budget(env, options)))
            query = getattr(module, QUERY_FUNC, None)
            if query is None:
                raise ShimError(f"{name} has no {QUERY_FUNC}: update PATCH_TARGETS")
            setattr(module, QUERY_FUNC, _wrap_query(query))
            n += 1
        if n == 0:
            raise ShimError(f"nothing to patch in {name} for {old!r}: update PATCH_TARGETS")
        counts[name] = n
    return counts


def load_cli(name=ENTRY_POINT):
    found = entry_points(group="console_scripts", name=name)
    if not found:
        raise ShimError(f"{name} is not installed (install it from tasks/fresh/requirements.txt)")
    return next(iter(found)).load()


def main(argv=None, env=os.environ, targets=PATCH_TARGETS, entry=ENTRY_POINT):
    argv = sys.argv[1:] if argv is None else argv
    try:
        check_env(env)
        cli = load_cli(entry)
        counts = apply(targets, env)
        if targets is PATCH_TARGETS:
            apply_settings()
    except ShimError as e:
        sys.exit(f"swegen_shim: {e}")
    print("swegen_shim: patched " + ", ".join(f"{m} x{n}" for m, n in counts.items()), file=sys.stderr)
    sys.argv = [entry, *argv]
    return cli()


if __name__ == "__main__":
    main()
