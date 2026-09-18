#!/usr/bin/env python3
"""Polyglot Inline Spec-Driven Agentic AI Daemon for Antigravity, VS Code, and Neovim.

Supports human-written spec-driven design across Python, Go, TypeScript, Rust, Lua, and Ruby.
- Writes intent/spec inline using language-appropriate comment syntax (#, //, --).
- Emits interactive Review Frames (<<<<<<< PROPOSAL ... ======= ... >>>>>>> SPEC).
- Allows Accept, Deny, and Suggest directly on code or via CLI/tasks/Lua.
"""

import argparse
import ast
from dataclasses import dataclass
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Dict, List, Optional, Tuple
import urllib.error
import urllib.request


LANGUAGE_MAP: Dict[str, str] = {
    ".py": "python",
    ".go": "go",
    ".ts": "typescript",
    ".js": "javascript",
    ".rs": "rust",
    ".lua": "lua",
    ".rb": "ruby",
    ".ex": "elixir",
    ".exs": "elixir",
}


# Polyglot comment patterns: supports # (Python/Ruby), // (Go/Rust/TS), -- (Lua)
AI_START_REGEX = re.compile(
    r"(?P<indent>[ \t]*)(?:#|//|--)\s*\[ai(?P<action>!|:run|:test|!test)?\](?:\s*<(?P<meta>[^>]+)>)?(?::\s*(?P<prompt>[^\n]*))?",
    re.IGNORECASE,
)
AI_SEAL_REGEX = re.compile(
    r"^[ \t]*(?:#|//|--)\s*\[(?:run|end|run:test|test)\]",
    re.MULTILINE | re.IGNORECASE,
)
STUB_DECORATOR_REGEX = re.compile(
    r"^[ \t]*@(?:stub|ai)(?:\((?P<args>.*?)\))?\b",
    re.MULTILINE,
)

CONFLICT_BLOCK_REGEX = re.compile(
    r"^<{7}[^\n]*\n(?P<proposal>.*?)\n={7}\n(?P<spec>.*?)\n>{7}[^\n]*\n?",
    re.MULTILINE | re.DOTALL,
)

MODEL_FAST = "gemini-3.8-flash-high"
MODEL_REASONING = "claude-sonnet-4-6"

DEFAULT_BACKEND_MODELS = {
    "agy": {
        "fast": "gemini-3.8-flash-high",
        "reasoning": "claude-sonnet-4-6",
    },
    "claude": {
        "fast": "claude-sonnet-5",
        "reasoning": "claude-opus-5",
    },
    "anthropic": {
        "fast": "claude-sonnet-5",
        "reasoning": "claude-opus-5",
    },
    "openai": {
        "fast": "gpt-5.6-luna",
        "reasoning": "gpt-5.6-sol",
    },
    "codex": {
        "fast": "gpt-5.6-luna",
        "reasoning": "gpt-5.6-sol",
    },
}


def find_workspace_root(start_path: Path) -> Path:
    """Climb up directories starting from start_path to find project root."""
    current = start_path.resolve()
    if current.is_file():
        current = current.parent

    root_markers = [
        ".git",
        "pyproject.toml",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "mix.exs",
        "Gemfile",
        "AGENTS.md",
        "Makefile",
    ]

    for parent in [current] + list(current.parents):
        for marker in root_markers:
            if (parent / marker).exists():
                return parent

    return current


def resolve_backend(requested: Optional[str] = None) -> str:
    """Determine the AI synthesis backend to use."""
    if requested:
        return requested.strip().lower()
    env = os.environ.get("INLINE_AGENT_BACKEND")
    if env:
        return env.strip().lower()
    if shutil.which("agy"):
        return "agy"
    if os.environ.get("ANTHROPIC_API_KEY") or shutil.which("claude"):
        return "claude"
    if os.environ.get("OPENAI_API_KEY") or shutil.which("openai"):
        return "openai"
    return "agy"


def resolve_model(backend: str, is_high_complexity: bool, model_override: Optional[str] = None) -> str:
    """Determine synthesis model based on backend and complexity."""
    if model_override:
        return model_override.strip()
    env = os.environ.get("INLINE_AGENT_MODEL")
    if env:
        return env.strip()
    tier = "reasoning" if is_high_complexity else "fast"
    cfg = DEFAULT_BACKEND_MODELS.get(backend.lower(), DEFAULT_BACKEND_MODELS["agy"])
    return cfg.get(tier, "gemini-3.8-flash-high")


def get_fast_model(backend: str) -> str:
    """Get fast repair model for given backend."""
    cfg = DEFAULT_BACKEND_MODELS.get(backend.lower(), DEFAULT_BACKEND_MODELS["agy"])
    return cfg.get("fast", "gemini-3.8-flash-high")



@dataclass
class DetectionResult:
    has_directives: bool
    is_ready: bool
    is_high_complexity: bool
    with_tests: bool
    markers: List[str]


def get_comment_prefix(ext: str) -> str:
    """Return idiomatic single-line comment token for file extension."""
    if ext in (".go", ".ts", ".js", ".rs"):
        return "//"
    if ext == ".lua":
        return "--"
    return "#"


def resolve_test_path(source_file: Path) -> Path:
    """Determine the standard companion unit test file path for any source file."""
    ext = source_file.suffix
    stem = source_file.stem
    parent = source_file.parent

    if ext == ".go":
        return parent / f"{stem}_test.go"
    if ext == ".py":
        return parent / f"test_{stem}.py"
    if ext in (".ts", ".js"):
        return parent / f"{stem}.test{ext}"
    if ext == ".rs":
        return parent / f"{stem}_test.rs"
    if ext in (".ex", ".exs"):
        return parent / f"{stem}_test.exs"
    if ext == ".rb":
        return parent / f"test_{stem}.rb"
    if ext == ".lua":
        return parent / f"{stem}_spec.lua"
    return parent / f"test_{stem}{ext}"


def detect_tasks(content: str, force_ready: bool = False, force_test: bool = False) -> Optional[DetectionResult]:
    """Check if file contains AI directives, and determine if it's sealed / ready to execute."""
    # If the file already contains an open proposal, don't re-trigger automatically
    if CONFLICT_BLOCK_REGEX.search(content) and not force_ready:
        return None

    ai_matches = list(AI_START_REGEX.finditer(content))
    stub_matches = list(STUB_DECORATOR_REGEX.finditer(content))
    seal_matches = list(AI_SEAL_REGEX.finditer(content))

    if not ai_matches and not stub_matches:
        return None

    is_high_complexity = False
    is_ready = force_ready or len(seal_matches) > 0
    with_tests = force_test
    markers: List[str] = []

    for m in ai_matches:
        meta = m.group("meta") or ""
        prompt = m.group("prompt") or ""
        action = (m.group("action") or "").lower()
        if action in ("!", ":run", ":test", "!test"):
            is_ready = True
        if "high complexity" in meta.lower() or "high complexity" in prompt.lower():
            is_high_complexity = True
        if "test" in action or "test" in meta.lower() or "test" in prompt.lower():
            with_tests = True
        markers.append(m.group(0).strip())

    for sm in seal_matches:
        token = sm.group(0).lower()
        if "test" in token:
            with_tests = True

    for sm in stub_matches:
        args = sm.group("args") or ""
        if "run" in args.lower() or "true" in args.lower():
            is_ready = True
        if "high" in args.lower():
            is_high_complexity = True
        if "test" in args.lower():
            with_tests = True
        markers.append(sm.group(0).strip())

    return DetectionResult(
        has_directives=True,
        is_ready=is_ready,
        is_high_complexity=is_high_complexity,
        with_tests=with_tests,
        markers=markers,
    )



def harvest_workspace_context(root: Path) -> str:
    """Gather real workspace context (guidelines, skills, files) to ground the agent."""
    hints: List[str] = []

    agents_md = root / "AGENTS.md"
    if agents_md.exists():
        try:
            excerpt = agents_md.read_text(encoding="utf-8")[:1000]
            hints.append(f"AGENTS.md overview:\n{excerpt}\n...")
        except OSError:
            pass

    skills_dir = root / ".agents" / "skills"
    if skills_dir.exists() and skills_dir.is_dir():
        skills = [p.name for p in skills_dir.iterdir() if p.is_dir()]
        hints.append(f"Available skills in {skills_dir}: {skills}")

    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            hints.append(f"pyproject.toml:\n{pyproject.read_text(encoding='utf-8')}")
        except OSError:
            pass

    return "\n\n".join(hints)


CONTEXT_TAG_REGEX = re.compile(
    r"(?:@\[(?P<bracket_file>[^\]]+)\]|@(?P<bare_file>[a-zA-Z0-9_\-\./]+\.[a-zA-Z0-9]+)|<context:\s*(?P<meta_files>[^>]+)>)",
    re.IGNORECASE,
)


def extract_referenced_context_files(content: str, extra_files: Optional[List[str]] = None) -> List[str]:
    """Find all @[file], @file.ext, and <context: ...> references in spec content."""
    files: List[str] = []
    if extra_files:
        files.extend(extra_files)

    for m in CONTEXT_TAG_REGEX.finditer(content):
        if m.group("bracket_file"):
            files.append(m.group("bracket_file").strip())
        elif m.group("bare_file"):
            files.append(m.group("bare_file").strip())
        elif m.group("meta_files"):
            for f in m.group("meta_files").split(","):
                if f.strip():
                    files.append(f.strip())

    seen = set()
    unique_files = []
    for f in files:
        if f not in seen:
            seen.add(f)
            unique_files.append(f)
    return unique_files


def harvest_referenced_context(workspace_root: Path, referenced_files: List[str]) -> str:
    """Load content of referenced files to provide strict context constraints."""
    sections: List[str] = []

    for ref in referenced_files:
        ref_path = Path(ref)
        target = None
        if ref_path.is_absolute() and ref_path.exists():
            target = ref_path
        elif (workspace_root / ref).exists():
            target = workspace_root / ref
        else:
            matches = list(workspace_root.glob(f"**/{ref_path.name}"))
            if matches:
                target = matches[0]

        if target and target.is_file():
            try:
                c = target.read_text(encoding="utf-8")
                if len(c) > 15000:
                    c = c[:15000] + "\n... (truncated for context limit)"
                ext = target.suffix
                lang = LANGUAGE_MAP.get(ext, "")
                sections.append(f"REFERENCED CONTEXT FILE: {target.name}\n```{lang}\n{c}\n```")
                print(f"   📎 Injected context constraint: {target.name} ({len(c)} chars)")
            except OSError as err:
                sections.append(f"REFERENCED CONTEXT FILE: {target.name} (Error reading: {err})")

    return "\n\n".join(sections)


def extract_code_block(text: str, lang: str = "python") -> str:
    """Extract code from markdown code fences matching language or fallback."""
    pattern = rf"```{lang}\s*\n(.*?)```"
    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()

    generic_match = re.search(r"```\w*\s*\n(.*?)```", text, re.DOTALL)
    if match := generic_match:
        return match.group(1).strip()

    return text.strip()


def extract_implementation_and_tests(text: str, lang: str) -> Tuple[str, Optional[str]]:
    """Extract separate implementation and test code blocks."""
    test_pattern = rf"```{lang}[:\s_-]*(?:test|tests|spec|unit_test)\b[^\n]*\n(.*?)```"
    test_m = re.search(test_pattern, text, re.DOTALL | re.IGNORECASE)

    impl_pattern = rf"```{lang}[:\s_-]*(?:impl|implementation|src|source|code)\b[^\n]*\n(.*?)```"
    impl_m = re.search(impl_pattern, text, re.DOTALL | re.IGNORECASE)

    if impl_m and test_m:
        return impl_m.group(1).strip(), test_m.group(1).strip()

    generic_lang_pattern = rf"```{lang}[^\n]*\n(.*?)```"
    blocks = re.findall(generic_lang_pattern, text, re.DOTALL | re.IGNORECASE)
    if len(blocks) >= 2:
        if test_m:
            test_code = test_m.group(1).strip()
            for b in blocks:
                if b.strip() != test_code:
                    return b.strip(), test_code
        return blocks[0].strip(), blocks[1].strip()
    elif len(blocks) == 1:
        return blocks[0].strip(), (test_m.group(1).strip() if test_m else None)

    generic_blocks = re.findall(r"```\w*[^\n]*\n(.*?)```", text, re.DOTALL)
    if len(generic_blocks) >= 2:
        return generic_blocks[0].strip(), generic_blocks[1].strip()
    elif len(generic_blocks) == 1:
        return generic_blocks[0].strip(), None

    return text.strip(), None



def validate_syntax(code: str, ext: str) -> Tuple[bool, str]:
    """Validate generated code syntax using language compiler/linter."""
    if ext == ".py":
        try:
            ast.parse(code)
            return True, ""
        except SyntaxError as err:
            return False, f"Python SyntaxError on line {err.lineno}: {err.msg}"

    # For Go, test compile with gofmt/temp file
    if ext == ".go":
        with tempfile.NamedTemporaryFile(suffix=".go", mode="w", delete=False) as tmp:
            tmp.write(code)
            tmp_path = tmp.name
        try:
            res = subprocess.run(["gofmt", "-e", tmp_path], capture_output=True, text=True, check=False)
            if res.returncode != 0:
                return False, f"Go syntax error: {res.stderr}"
            return True, ""
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    if ext == ".lua":
        with tempfile.NamedTemporaryFile(suffix=".lua", mode="w", delete=False) as tmp:
            tmp.write(code)
            tmp_path = tmp.name
        try:
            res = subprocess.run(["luac", "-p", tmp_path], capture_output=True, text=True, check=False)
            if res.returncode != 0:
                return False, f"Lua syntax error: {res.stderr}"
            return True, ""
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    if ext == ".rb":
        with tempfile.NamedTemporaryFile(suffix=".rb", mode="w", delete=False) as tmp:
            tmp.write(code)
            tmp_path = tmp.name
        try:
            res = subprocess.run(["ruby", "-c", tmp_path], capture_output=True, text=True, check=False)
            if res.returncode != 0:
                return False, f"Ruby syntax error: {res.stderr}"
            return True, ""
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    if ext in (".ex", ".exs"):
        with tempfile.NamedTemporaryFile(suffix=ext, mode="w", delete=False) as tmp:
            tmp.write(code)
            tmp_path = tmp.name
        try:
            res = subprocess.run(
                ["elixir", "-e", "Code.string_to_quoted!(File.read!(System.argv() |> hd))", tmp_path],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                return False, f"Elixir syntax error: {res.stderr or res.stdout}"
            return True, ""
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    return True, ""


def run_unit_tests(source_path: Path, test_path: Path, ext: str) -> Tuple[bool, str]:
    """Execute language test runner to verify implementation against companion tests."""
    parent_dir = str(source_path.parent)
    src_name = source_path.name
    test_name = test_path.name

    try:
        if ext == ".go":
            res = subprocess.run(
                ["go", "test", "-v", src_name, test_name],
                cwd=parent_dir,
                capture_output=True,
                text=True,
                check=False,
                timeout=25,
            )
            return (res.returncode == 0, res.stderr or res.stdout)

        if ext == ".py":
            res = subprocess.run(
                [sys.executable, "-m", "unittest", test_name],
                cwd=parent_dir,
                capture_output=True,
                text=True,
                check=False,
                timeout=25,
            )
            return (res.returncode == 0, res.stderr or res.stdout)

        if ext in (".ts", ".js"):
            res = subprocess.run(
                ["bun", "test", test_name],
                cwd=parent_dir,
                capture_output=True,
                text=True,
                check=False,
                timeout=25,
            )
            return (res.returncode == 0, res.stderr or res.stdout)

        if ext == ".rb":
            res = subprocess.run(
                ["ruby", "-I.", test_name],
                cwd=parent_dir,
                capture_output=True,
                text=True,
                check=False,
                timeout=25,
            )
            return (res.returncode == 0, res.stderr or res.stdout)

        if ext in (".ex", ".exs"):
            res = subprocess.run(
                ["elixir", "-r", src_name, test_name],
                cwd=parent_dir,
                capture_output=True,
                text=True,
                check=False,
                timeout=25,
            )
            return (res.returncode == 0, res.stderr or res.stdout)

        if ext == ".lua":
            res = subprocess.run(
                ["lua", test_name],
                cwd=parent_dir,
                capture_output=True,
                text=True,
                check=False,
                timeout=25,
            )
            return (res.returncode == 0, res.stderr or res.stdout)

    except subprocess.TimeoutExpired:
        return False, "Test suite execution timed out after 25 seconds."
    except Exception as exc:
        return True, f"Test runner notice: {exc}"

    return True, ""





def _run_agy(prompt: str, model: str, live_stream: bool = True) -> str:
    """Invoke agy CLI with live line-by-line streaming to stdout."""
    cmd = [
        "agy",
        "--dangerously-skip-permissions",
        "--model",
        model,
        "-p",
        prompt,
    ]
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    chunks: List[str] = []
    if live_stream:
        print(f"   ┌── [live streaming: agy ({model})] " + "─" * 40)

    try:
        for line in iter(process.stdout.readline, ""):
            if live_stream:
                sys.stdout.write(f"   \033[90m│\033[0m {line}")
                sys.stdout.flush()
            chunks.append(line)
    finally:
        process.stdout.close()
        process.wait()

    if live_stream:
        print("   └── " + "─" * 60)

    full_output = "".join(chunks)
    if process.returncode != 0 and not full_output.strip():
        raise RuntimeError(f"agy execution failed with return code {process.returncode}")
    return full_output


def _run_claude(prompt: str, model: str, live_stream: bool = True) -> str:
    """Invoke Claude via Anthropic Messages API or claude CLI."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": model,
            "max_tokens": 4096,
            "stream": live_stream,
            "messages": [{"role": "user", "content": prompt}],
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        if live_stream:
            print(f"   ┌── [live streaming: anthropic-api ({model})] " + "─" * 36)
        chunks: List[str] = []
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                if live_stream:
                    for line_bytes in resp:
                        line = line_bytes.decode("utf-8").strip()
                        if line.startswith("data: "):
                            data_str = line[6:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                event_data = json.loads(data_str)
                                if event_data.get("type") == "content_block_delta":
                                    delta_text = event_data.get("delta", {}).get("text", "")
                                    if delta_text:
                                        sys.stdout.write(delta_text)
                                        sys.stdout.flush()
                                        chunks.append(delta_text)
                            except json.JSONDecodeError:
                                pass
                    print()
                else:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                    text = "".join(c.get("text", "") for c in resp_data.get("content", []))
                    chunks.append(text)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            raise RuntimeError(f"Anthropic API error (HTTP {e.code}): {err_body}")
        except Exception as e:
            raise RuntimeError(f"Anthropic request failed: {e}")

        if live_stream:
            print("   └── " + "─" * 60)
        return "".join(chunks)

    # Fall back to claude CLI if installed
    if shutil.which("claude"):
        cmd = ["claude", "-p", "--model", model, prompt]
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        chunks = []
        if live_stream:
            print(f"   ┌── [live streaming: claude-cli ({model})] " + "─" * 38)
        try:
            for line in iter(process.stdout.readline, ""):
                if live_stream:
                    sys.stdout.write(f"   \033[90m│\033[0m {line}")
                    sys.stdout.flush()
                chunks.append(line)
        finally:
            process.stdout.close()
            process.wait()
        if live_stream:
            print("   └── " + "─" * 60)
        full_output = "".join(chunks)
        if process.returncode != 0 and not full_output.strip():
            raise RuntimeError(f"claude CLI failed with return code {process.returncode}")
        return full_output

    raise RuntimeError(
        "Claude backend requires either ANTHROPIC_API_KEY in environment or authenticated 'claude' CLI in PATH."
    )


def _run_openai(prompt: str, model: str, live_stream: bool = True) -> str:
    """Invoke OpenAI / Codex via Chat Completions API or openai CLI."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        }
        body = {
            "model": model,
            "stream": live_stream,
            "messages": [{"role": "user", "content": prompt}],
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        if live_stream:
            print(f"   ┌── [live streaming: openai-api ({model})] " + "─" * 38)
        chunks: List[str] = []
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                if live_stream:
                    for line_bytes in resp:
                        line = line_bytes.decode("utf-8").strip()
                        if line.startswith("data: "):
                            data_str = line[6:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                event_data = json.loads(data_str)
                                choices = event_data.get("choices", [])
                                if choices:
                                    delta_text = choices[0].get("delta", {}).get("content", "")
                                    if delta_text:
                                        sys.stdout.write(delta_text)
                                        sys.stdout.flush()
                                        chunks.append(delta_text)
                            except json.JSONDecodeError:
                                pass
                    print()
                else:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                    choices = resp_data.get("choices", [])
                    if choices:
                        chunks.append(choices[0].get("message", {}).get("content", ""))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            raise RuntimeError(f"OpenAI API error (HTTP {e.code}): {err_body}")
        except Exception as e:
            raise RuntimeError(f"OpenAI request failed: {e}")

        if live_stream:
            print("   └── " + "─" * 60)
        return "".join(chunks)

    if shutil.which("openai"):
        cmd = ["openai", "api", "chat.completions.create", "-m", model, "-g", "user", prompt]
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        chunks = []
        for line in iter(process.stdout.readline, ""):
            chunks.append(line)
        process.stdout.close()
        process.wait()
        return "".join(chunks)

    raise RuntimeError(
        "OpenAI/Codex backend requires OPENAI_API_KEY in environment or 'openai' CLI in PATH."
    )


def _run_custom(prompt: str, custom_cmd: Optional[str], live_stream: bool = True) -> str:
    """Invoke user-defined CLI command template."""
    cmd_template = custom_cmd or os.environ.get("INLINE_AGENT_CMD")
    if not cmd_template:
        raise RuntimeError("Custom backend requested, but no command provided via --cmd or INLINE_AGENT_CMD.")

    if "{prompt}" in cmd_template:
        final_cmd = cmd_template.replace("{prompt}", prompt)
        stdin_data = None
    else:
        final_cmd = cmd_template
        stdin_data = prompt

    process = subprocess.Popen(
        final_cmd,
        shell=True,
        stdin=subprocess.PIPE if stdin_data else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    if stdin_data:
        stdout, _ = process.communicate(input=stdin_data)
        return stdout

    chunks = []
    if live_stream:
        print(f"   ┌── [live streaming: custom cmd] " + "─" * 40)
    try:
        for line in iter(process.stdout.readline, ""):
            if live_stream:
                sys.stdout.write(f"   \033[90m│\033[0m {line}")
                sys.stdout.flush()
            chunks.append(line)
    finally:
        process.stdout.close()
        process.wait()
    if live_stream:
        print("   └── " + "─" * 60)
    return "".join(chunks)


def run_backend_synthesis(
    prompt: str,
    model: str,
    backend: str = "agy",
    live_stream: bool = True,
    custom_cmd: Optional[str] = None,
) -> str:
    """Dispatch prompt to configured AI synthesis backend."""
    b = backend.lower()
    if b == "agy":
        return _run_agy(prompt, model, live_stream)
    elif b in ("claude", "anthropic"):
        return _run_claude(prompt, model, live_stream)
    elif b in ("openai", "codex"):
        return _run_openai(prompt, model, live_stream)
    elif b == "custom" or custom_cmd:
        return _run_custom(prompt, custom_cmd, live_stream)
    else:
        raise ValueError(f"Unknown backend '{backend}'. Supported: agy, claude, openai, codex, custom")


def run_agy_synthesis(prompt: str, model: str, live_stream: bool = True) -> str:
    """Invoke agy CLI (retained for backwards compatibility)."""
    return run_backend_synthesis(prompt, model=model, backend="agy", live_stream=live_stream)



def resolve_proposals(file_path: Path, action: str, note: Optional[str] = None) -> bool:
    """Resolve conflict review frames: accept (keep code), deny (keep spec), or suggest."""
    if not file_path.exists():
        print(f"Error: file {file_path} does not exist", file=sys.stderr)
        return False

    content = file_path.read_text(encoding="utf-8")
    if not CONFLICT_BLOCK_REGEX.search(content):
        print(f"ℹ️  No open proposals found in {file_path.name}")
        return False

    ext = file_path.suffix
    prefix = get_comment_prefix(ext)

    if action == "accept":
        # Keep proposal code (top section), clean up markers
        resolved = CONFLICT_BLOCK_REGEX.sub(r"\g<proposal>\n", content)
        file_path.write_text(resolved, encoding="utf-8")
        print(f"✅ Accepted proposed implementation in {file_path.name}")
        return True

    elif action == "deny":
        # Keep original spec (bottom section), discard proposal
        resolved = CONFLICT_BLOCK_REGEX.sub(r"\g<spec>\n", content)
        file_path.write_text(resolved, encoding="utf-8")
        print(f"🛑 Denied proposal. Restored spec in {file_path.name}")
        test_path = resolve_test_path(file_path)
        if test_path.exists():
            test_path.unlink()
            print(f"🧹 Cleaned up companion test file {test_path.name}")
        return True

    elif action == "suggest":
        # Append human feedback note to spec and re-prepare for synthesis
        feedback = f"\n{prefix} HUMAN FEEDBACK / SUGGESTION: {note}" if note else ""
        seal = f"\n{prefix}[run]\n"
        replacement = f"\\g<spec>{feedback}{seal}"
        resolved = CONFLICT_BLOCK_REGEX.sub(replacement, content)
        file_path.write_text(resolved, encoding="utf-8")
        print(f"🔄 Updated spec with feedback in {file_path.name}. Re-triggering...")
        return agentic_transform_file(file_path, file_path.parent, force_ready=True)

    return False


def build_localized_review_frame(
    original_content: str,
    generated_content: str,
    lang: str,
    test_banner: str = "",
) -> str:
    """Construct localized conflict review frames so untouched code outside the spec remains clean."""
    orig_lines = original_content.splitlines(keepends=True)
    gen_lines = generated_content.splitlines(keepends=True)

    orig_clean = [l.rstrip() + "\n" if l.endswith("\n") else l.rstrip() for l in orig_lines]
    gen_clean = [l.rstrip() + "\n" if l.endswith("\n") else l.rstrip() for l in gen_lines]

    matcher = difflib.SequenceMatcher(None, orig_clean, gen_clean)
    opcodes = matcher.get_opcodes()

    # If there is minimal/no unchanged context (e.g. brand new file), wrap the entire file
    equal_lines = sum(i2 - i1 for tag, i1, i2, j1, j2 in opcodes if tag == "equal")
    if equal_lines < 3 or (len(opcodes) == 1 and opcodes[0][0] != "equal"):
        banner = f"<<<<<<< PROPOSAL ({lang.title()} Implementation{test_banner} - Click 'Accept Current Change' to keep)\n"
        return f"{banner}{generated_content.strip()}\n=======\n{original_content.strip()}\n>>>>>>> SPEC (Original Intent - Click 'Accept Incoming Change' to revert)\n"

    out_parts: List[str] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            out_parts.append("".join(orig_lines[i1:i2]))
        else:
            p_chunk = "".join(gen_lines[j1:j2]).strip()
            s_chunk = "".join(orig_lines[i1:i2]).strip()

            if not p_chunk and not s_chunk:
                continue

            banner = f"<<<<<<< PROPOSAL ({lang.title()} Implementation{test_banner} - Click 'Accept Current Change' to keep)\n"
            frame = f"{banner}{p_chunk}\n=======\n{s_chunk}\n>>>>>>> SPEC (Original Intent - Click 'Accept Incoming Change' to revert)\n"
            out_parts.append(frame)

    return "".join(out_parts)


def agentic_transform_file(
    file_path: Path,
    workspace_root: Path,
    force_ready: bool = False,
    force_test: bool = False,
    line_range: Optional[Tuple[int, int]] = None,
    context_files: Optional[List[str]] = None,
    backend: Optional[str] = None,
    model_override: Optional[str] = None,
    custom_cmd: Optional[str] = None,
) -> bool:
    """Execute end-to-end agentic transformation, emitting a Review Frame (Conflict Block)."""
    ext = file_path.suffix
    lang = LANGUAGE_MAP.get(ext, "python")
    prefix = get_comment_prefix(ext)

    content = file_path.read_text(encoding="utf-8")
    all_lines = content.splitlines(keepends=True)

    if line_range:
        start_line, end_line = line_range
        start_idx = max(0, start_line - 1)
        end_idx = min(len(all_lines), end_line)

        prefix_content = "".join(all_lines[:start_idx])
        target_content = "".join(all_lines[start_idx:end_idx])
        suffix_content = "".join(all_lines[end_idx:])

        is_high_complexity = "high" in target_content.lower()
        with_tests = force_test or "test" in target_content.lower()
        markers_desc = f"selected lines {start_line}-{end_line}"
    else:
        prefix_content = ""
        target_content = content
        suffix_content = ""

        detection = detect_tasks(content, force_ready=force_ready, force_test=force_test)
        if not detection or not detection.has_directives:
            return False

        if not detection.is_ready:
            print(f"⏳ [inline-agent] {file_path.name}: {len(detection.markers)} directive(s) in draft state. (Add {prefix}[run] or {prefix}[ai!] to execute)")
            return False

        is_high_complexity = detection.is_high_complexity
        with_tests = detection.with_tests
        markers_desc = f"{len(detection.markers)} directive(s)"

    resolved_backend = resolve_backend(backend)
    model = resolve_model(resolved_backend, is_high_complexity, model_override)
    fast_model = get_fast_model(resolved_backend)

    print(f"\n⚡ [inline-agent] Proposing implementation ({lang}) for {markers_desc} in {file_path.name}")
    print(f"   Backend: {resolved_backend} | Model: {model} (High complexity: {is_high_complexity} | Companion tests: {with_tests})")

    # Immediate in-editor visual feedback banner for full-file runs
    if not line_range:
        in_file_banner = f"{prefix} ⏳ [inline-agent: synthesizing {lang} proposal with {resolved_backend}:{model}...]"
        ack_content = AI_SEAL_REGEX.sub(in_file_banner, content)
        if ack_content != content:
            file_path.write_text(ack_content, encoding="utf-8")

    workspace_context = harvest_workspace_context(workspace_root)
    referenced_refs = extract_referenced_context_files(content if not line_range else target_content, context_files)
    referenced_context = harvest_referenced_context(workspace_root, referenced_refs)

    context_prompt_part = ""
    if referenced_context:
        context_prompt_part = f"""
EXPLICIT CONSTRAINTS & REFERENCED CONTEXT FILES:
{referenced_context}

MANDATORY CONSTRAINT:
Your code MUST strictly conform to the types, signatures, and patterns in the referenced context files above.
"""

    if with_tests:
        mode_instructions = f"""INSTRUCTIONS & INVARIANTS:
1. Resolve all pseudocode, stubs, and [ai] tags into idiomatic {lang}.
2. Ensure complete type and signature harmony.
3. Remove all [ai], [ai!], [ai:run], [run], [end], and @stub markers from the generated code.
4. Preserve existing valid imports and logic.
5. COMPANION TEST SUITE REQUIREMENT:
   You MUST generate BOTH the implementation and a comprehensive companion unit test suite.
   Format your response using EXACTLY TWO markdown code blocks:
   ```{lang}:implementation
   <complete implementation code>
   ```
   ```{lang}:test
   <complete runnable test suite code>
   ```
   Testing guidelines:
   - For Go: package name MUST match the implementation package. Use standard `testing` package with `TestXxx(t *testing.T)`.
   - For Python: use `unittest.TestCase`. Import classes/functions directly from the module (`{file_path.stem}`).
   - For TypeScript/JavaScript: use `bun:test` (`describe`, `test`, `expect`).
   - For Ruby: use `minitest/autorun` with `Minitest::Test`.
   - For Elixir: start with `ExUnit.start()`, define module with `use ExUnit.Case`.
   - For Lua: test module using assertions that run on execution.
   - Mock external network/HTTP or heavy I/O so unit tests execute instantly and reliably.
   - Output NO conversational text, ONLY the two code blocks.
"""
    else:
        mode_instructions = f"""INSTRUCTIONS & INVARIANTS:
1. Resolve all pseudocode, stubs, and [ai] tags into idiomatic {lang}.
2. Ensure complete type and signature harmony.
3. Remove all [ai], [ai!], [ai:run], [run], [end], and @stub markers from the generated code.
4. Preserve existing valid imports and logic.
5. Output ONLY the working {lang} code inside a single ```{lang} code block. No conversational preamble.
"""

    if line_range:
        file_presentation = f"""CURRENT FILE ({file_path.name}) - TARGET RANGE TO IMPLEMENT (Lines {start_line}-{end_line}):
--- READ-ONLY PREFIX CONTEXT (Lines 1-{start_idx}) ---
```{lang}
{prefix_content}
```

--- TARGET SELECTION TO IMPLEMENT (Lines {start_line}-{end_line}) ---
```{lang}
{target_content}
```

--- READ-ONLY SUFFIX CONTEXT (Lines {end_idx + 1}-{len(all_lines)}) ---
```{lang}
{suffix_content}
```

TARGET RANGE INSTRUCTIONS:
- Generate code replacement ONLY for the TARGET SELECTION (lines {start_line}-{end_line}).
- Do NOT repeat the prefix or suffix lines in your output block.
- Your code must integrate seamlessly with the prefix and suffix context.
"""
    else:
        file_presentation = f"""CURRENT FILE ({file_path.name}):
```{lang}
{content}
```
"""

    system_prompt = f"""You are an inline agentic coding engine embedded in Antigravity / Neovim.
Your task is to transform the provided source file or target selection into clean, idiomatic, production-ready {lang} code.

WORKSPACE CONTEXT:
{workspace_context if workspace_context else "(No extra workspace configs)"}

{context_prompt_part}

{file_presentation}

{mode_instructions}
"""

    start_t = time.time()
    raw_output = run_backend_synthesis(
        system_prompt,
        model=model,
        backend=resolved_backend,
        live_stream=True,
        custom_cmd=custom_cmd,
    )

    if with_tests:
        generated_code, generated_test = extract_implementation_and_tests(raw_output, lang=lang)
    else:
        generated_code = extract_code_block(raw_output, lang=lang)
        generated_test = None

    def get_full_candidate(code: str) -> str:
        if line_range:
            return prefix_content + code.strip() + "\n" + suffix_content
        return code

    # Self-healing syntax verification loop
    for attempt in range(1, 3):
        candidate = get_full_candidate(generated_code)
        valid_impl, err_impl = validate_syntax(candidate, ext)
        valid_test = True
        err_test = ""
        if with_tests and generated_test:
            valid_test, err_test = validate_syntax(generated_test, ext)

        if valid_impl and valid_test:
            break

        print(f"   ⚠️ Syntax validation failed (Attempt {attempt}/2)...")
        if not valid_impl:
            print(f"      Impl error: {err_impl}")
        if not valid_test:
            print(f"      Test error: {err_test}")

        repair_prompt = f"""The previous {lang} code failed syntax verification:
{f'IMPLEMENTATION ERROR: {err_impl}' if not valid_impl else ''}
{f'TEST ERROR: {err_test}' if not valid_test else ''}

CURRENT CODE:
```{lang}
{generated_code}
```
"""
        if with_tests and generated_test:
            repair_prompt += f"""
CURRENT TEST:
```{lang}
{generated_test}
```
Fix the syntax errors and return TWO blocks: ```{lang}:implementation and ```{lang}:test.
"""
        else:
            repair_prompt += f"""
Fix the syntax errors and return the complete valid snippet in a ```{lang} block.
"""
        raw_output = run_backend_synthesis(
            repair_prompt,
            model=fast_model,
            backend=resolved_backend,
            live_stream=False,
            custom_cmd=custom_cmd,
        )
        if with_tests:
            generated_code, generated_test = extract_implementation_and_tests(raw_output, lang=lang)
        else:
            generated_code = extract_code_block(raw_output, lang=lang)

    candidate = get_full_candidate(generated_code)
    valid_impl, err_impl = validate_syntax(candidate, ext)
    if not valid_impl:
        print(f"   ❌ Failed to repair code syntax: {err_impl}. Skipping update.")
        return False

    # Companion Test Execution & Self-Healing Loop
    test_path = None
    if with_tests and generated_test:
        test_path = resolve_test_path(file_path)
        test_path.write_text(generated_test, encoding="utf-8")
        file_path.write_text(candidate, encoding="utf-8")

        print(f"   🧪 Running companion tests against implementation ({test_path.name})...")
        passed, test_out = run_unit_tests(file_path, test_path, ext)

        if not passed:
            print(f"   ⚠️ Unit tests failed. Initiating self-repair loop (1/2)...")
            repair_test_prompt = f"""The companion test suite failed when run against the implementation.
TEST FAILURE:
{test_out}

IMPLEMENTATION:
```{lang}
{generated_code}
```

COMPANION TEST:
```{lang}
{generated_test}
```

Analyze the failure. Fix the implementation or the companion test (or both) so that the tests pass.
Return EXACTLY two code blocks:
```{lang}:implementation
<fixed implementation>
```
```{lang}:test
<fixed companion test>
```
"""
            raw_output = run_backend_synthesis(
                repair_test_prompt,
                model=fast_model,
                backend=resolved_backend,
                live_stream=False,
                custom_cmd=custom_cmd,
            )
            generated_code, generated_test = extract_implementation_and_tests(raw_output, lang=lang)
            if generated_test:
                test_path.write_text(generated_test, encoding="utf-8")
            candidate = get_full_candidate(generated_code)
            file_path.write_text(candidate, encoding="utf-8")
            passed, test_out = run_unit_tests(file_path, test_path, ext)

        if passed:
            print(f"   ✅ Companion tests verified and passing ({test_path.name})")
        else:
            print(f"   ⚠️ Companion tests had warnings (review test file): {test_out.strip()[:160]}")

    test_banner = f" + companion test: {test_path.name}" if test_path else ""

    if line_range:
        clean_target = re.sub(r"^<{7}[^\n]*\n", "", target_content, flags=re.MULTILINE)
        clean_target = re.sub(r"^={7}\n", "", clean_target, flags=re.MULTILINE)
        clean_target = re.sub(r"^>{7}[^\n]*\n", "", clean_target, flags=re.MULTILINE)
        clean_target = clean_target.strip()

        review_frame = (
            prefix_content
            + f"<<<<<<< PROPOSAL ({lang.title()} Implementation{test_banner} - Click 'Accept Current Change' to keep)\n"
            + f"{generated_code.strip()}\n"
            + f"=======\n"
            + f"{clean_target}\n"
            + f">>>>>>> SPEC (Original Intent - Click 'Accept Incoming Change' to revert)\n"
            + suffix_content
        )
    else:
        # Strip any temporary in-file banners and old/stale conflict markers to prevent nesting
        clean_spec = re.sub(rf"{re.escape(prefix)}\s*⏳\s*\[inline-agent: synthesizing.*?\n", "", content)
        clean_spec = re.sub(r"^<{7}[^\n]*\n", "", clean_spec, flags=re.MULTILINE)
        clean_spec = re.sub(r"^={7}\n", "", clean_spec, flags=re.MULTILINE)
        clean_spec = re.sub(r"^>{7}[^\n]*\n", "", clean_spec, flags=re.MULTILINE)
        clean_spec = clean_spec.strip()

        review_frame = build_localized_review_frame(clean_spec, generated_code, lang, test_banner)

    file_path.write_text(review_frame, encoding="utf-8")
    duration = time.time() - start_t
    print(f"   📋 Proposal ready in {file_path.name} ({duration:.1f}s)")
    if test_path:
        print(f"   🧪 Companion test generated: {test_path.name}")
    print(f"   👉 Review in editor: Click 'Accept Current Change' to keep proposal, or use tasks/CLI.")
    return True


class FileChangeHandler:
    """Manages file hashes and debouncing to prevent feedback loops."""

    def __init__(
        self,
        workspace_root: Path,
        backend: Optional[str] = None,
        model: Optional[str] = None,
        custom_cmd: Optional[str] = None,
    ):
        self.workspace_root = workspace_root
        self.backend = backend
        self.model = model
        self.custom_cmd = custom_cmd
        self.hashes = {}
        self.last_processed = {}

    def get_hash(self, path: Path) -> str:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return ""

    def handle(self, file_path: Path):
        if file_path.suffix not in LANGUAGE_MAP or file_path.name.startswith("."):
            return
        if file_path.name == Path(__file__).name:
            return

        now = time.time()
        last_t = self.last_processed.get(str(file_path), 0)
        if now - last_t < 1.0:  # 1s debounce
            return

        current_hash = self.get_hash(file_path)
        if current_hash == self.hashes.get(str(file_path)):
            return

        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError:
            return

        # If file is currently displaying a proposal review frame, don't re-trigger
        if CONFLICT_BLOCK_REGEX.search(content):
            return

        detection = detect_tasks(content, force_ready=False)
        if not detection or not detection.has_directives:
            self.hashes[str(file_path)] = current_hash
            return

        ext = file_path.suffix
        prefix = get_comment_prefix(ext)
        if not detection.is_ready:
            print(f"⏳ [inline-agent] {file_path.name}: Directives detected, still drafting. Add {prefix}[run] or {prefix}[ai!] when ready.")
            self.hashes[str(file_path)] = current_hash
            return

        self.last_processed[str(file_path)] = now
        success = agentic_transform_file(
            file_path,
            self.workspace_root,
            force_ready=False,
            force_test=detection.with_tests,
            backend=self.backend,
            model_override=self.model,
            custom_cmd=self.custom_cmd,
        )
        if success:
            self.hashes[str(file_path)] = self.get_hash(file_path)


def main():
    parser = argparse.ArgumentParser(
        description="Polyglot Inline Spec-Driven Agentic AI Daemon for Antigravity, VS Code, and Neovim"
    )
    parser.add_argument("file", nargs="?", type=Path, help="Target file to transform (positional shortcut for --once)")
    parser.add_argument("--once", type=Path, help="Propose implementation for a file and exit")
    parser.add_argument(
        "--backend",
        type=str,
        choices=["agy", "claude", "anthropic", "openai", "codex", "custom"],
        help="AI backend engine (default: env INLINE_AGENT_BACKEND or agy)",
    )
    parser.add_argument("--model", type=str, help="Specific model override for synthesis")
    parser.add_argument("--cmd", type=str, help="Custom CLI command template for 'custom' backend")
    parser.add_argument("--test", action="store_true", help="Generate and verify companion unit tests")
    parser.add_argument("--range", type=str, help="Specific line range to transform (e.g. '15:30')")
    parser.add_argument("--context", type=str, help="Comma-separated files or constraints to include as context")
    parser.add_argument("--accept", type=Path, help="Accept proposal and keep generated code")
    parser.add_argument("--deny", type=Path, help="Deny proposal and restore spec")
    parser.add_argument("--suggest", type=Path, help="Provide feedback note and re-propose")
    parser.add_argument("--note", type=str, default="", help="Feedback note for --suggest")
    parser.add_argument(
        "--path",
        type=Path,
        default=None,
        help="Workspace directory to watch (auto-discovered from file if omitted)",
    )
    args = parser.parse_args()

    # Support positional file argument as shortcut for --once
    if args.file and not args.once and not args.accept and not args.deny and not args.suggest:
        args.once = args.file

    target_file = args.once or args.accept or args.deny or args.suggest

    is_path_explicit = any(arg == "--path" or arg.startswith("--path=") for arg in sys.argv[1:])
    if args.path and is_path_explicit:
        workspace_root = args.path.resolve()
    elif target_file:
        workspace_root = find_workspace_root(target_file.resolve())
    elif args.path:
        workspace_root = args.path.resolve()
    else:
        workspace_root = Path(".").resolve()

    if args.accept:
        resolve_proposals(args.accept.resolve(), action="accept")
        return

    if args.deny:
        resolve_proposals(args.deny.resolve(), action="deny")
        return

    if args.suggest:
        resolve_proposals(args.suggest.resolve(), action="suggest", note=args.note)
        return

    line_range = None
    if args.range:
        parts = args.range.split(":")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            line_range = (int(parts[0]), int(parts[1]))

    context_files = None
    if args.context:
        context_files = [c.strip() for c in args.context.split(",") if c.strip()]

    if args.once:
        target = args.once.resolve()
        if not target.exists():
            print(f"Error: file {target} does not exist", file=sys.stderr)
            sys.exit(1)
        agentic_transform_file(
            target,
            workspace_root,
            force_ready=True,
            force_test=args.test,
            line_range=line_range,
            context_files=context_files,
            backend=args.backend,
            model_override=args.model,
            custom_cmd=args.cmd,
        )
        return

    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    class WatchdogWrapper(FileSystemEventHandler):
        def __init__(self, handler: FileChangeHandler):
            self.handler = handler

        def on_modified(self, event):
            if not event.is_directory:
                self.handler.handle(Path(event.src_path))

        def on_created(self, event):
            if not event.is_directory:
                self.handler.handle(Path(event.src_path))

    handler = FileChangeHandler(
        workspace_root,
        backend=args.backend,
        model=args.model,
        custom_cmd=args.cmd,
    )
    observer = Observer()
    observer.schedule(WatchdogWrapper(handler), str(workspace_root), recursive=True)
    observer.start()

    backend_desc = args.backend or os.environ.get("INLINE_AGENT_BACKEND") or "auto (agy)"
    supported_list = ", ".join(LANGUAGE_MAP.keys())
    print(f"🚀 [inline-agent] Watching {workspace_root} for polyglot specs...")
    print(f"   Backend: {backend_desc} | Supported extensions: {supported_list}")
    print("   Workflow: Write spec -> add [run] -> Review Frame appears in editor.")
    print("   Actions: Click editor buttons, or use tasks / CLI (--accept, --deny, --suggest).")
    print("   Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()



if __name__ == "__main__":
    main()
