import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_HEADER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_VERSION = re.compile(r"^version:\s*(\S+)\s*$", re.MULTILINE)
DOCUMENT_PLACEHOLDER = "{{document_text}}"
DOCUMENT_END_MARKER = "DOCUMENT>>>"


@dataclass(frozen=True, slots=True)
class Prompt:
    name: str
    version: str
    template: str

    def render(self, document_text: str) -> str:
        # A document must not be able to close its own delimiter and pose as instructions.
        safe_text = document_text.replace(DOCUMENT_END_MARKER, "DOCUMENT >>>")
        # str.replace, not format: document text is full of braces that must stay literal.
        return self.template.replace(DOCUMENT_PLACEHOLDER, safe_text)


def _find_prompts_dir() -> Path:
    # /prompts lives at the repository root in development, and at /app/prompts in the image.
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "prompts"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("The prompts directory was not found.")


@lru_cache
def load_prompt(name: str, prompts_dir: Path | None = None) -> Prompt:
    path = (prompts_dir or _find_prompts_dir()) / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    header = _HEADER.match(text)
    version = _VERSION.search(header[1]) if header else None
    if header is None or version is None:
        raise ValueError(f"Prompt {name} has no version header.")
    template = text[header.end() :]
    if DOCUMENT_PLACEHOLDER not in template:
        raise ValueError(f"Prompt {name} has no {DOCUMENT_PLACEHOLDER} placeholder.")
    return Prompt(name=name, version=version[1], template=template)
