# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Governing: docs/openspec/specs/distribute/spec.md#req-0001, ADR-0008, SPEC-DIST, REQ-DOC-0100, REQ-DOC-0109, REQ-DOC-0110, REQ-DOC-0111, REQ-DOC-0112

"""Hermetic Markdown link and anchor validator for repository documentation."""

from collections.abc import Sequence
from pathlib import Path
import re
import tempfile
import unittest


_REPO_ROOT = Path(__file__).resolve().parents[2]

TARGET_DOCS: tuple[str, ...] = (
    "README.md",
    ".github/README.md",
    "docs/user-guides/cloud-run.md",
    "docs/user-guides/local-gvisor.md",
    "docker/README.md",
    "terraform/README.md",
    "packages/codemender/README.md",
    "packages/launcher/README.md",
    "packages/repohost/README.md",
)

_EXTERNAL_SCHEMES = ("http://", "https://", "mailto:")
_FENCED_CODE_RE = re.compile(
    r"(^|\n)[ \t>]*(`{3,}|~{3,})[^\n]*\n.*?\n[ \t>]*\2[ \t]*(?=\n|$)",
    re.DOTALL,
)
_INLINE_CODE_RE = re.compile(
    r"(`+)(?!`)((?:(?!\n[ \t]*\n).)+?)(?<!`)\1(?!`)",
    re.DOTALL,
)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_MD_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_HTML_HREF_RE = re.compile(
    r'<a\s+[^>]*href=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_HTML_ANCHOR_RE = re.compile(
    r'<a\s+[^>]*(?:id|name)=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)


def slugify_github_heading(heading: str) -> str:
  """Converts a Markdown heading string into its GitHub anchor slug."""
  text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", heading)
  text = re.sub(r"[`*_]", "", text)
  text = text.lower().strip()
  text = re.sub(r"[^a-z0-9_ -]", "", text)
  return text.replace(" ", "-")


def strip_ignored_regions(markdown_text: str) -> str:
  """Strips fenced code blocks, HTML comments, and inline code spans."""
  text = _FENCED_CODE_RE.sub("\n", markdown_text)
  text = _INLINE_CODE_RE.sub("CODE", text)
  text = _HTML_COMMENT_RE.sub("", text)
  return text


def collect_markdown_anchors(md_path: Path) -> set[str]:
  """Collects GitHub heading slugs and explicit HTML <a id/name> anchors."""
  raw = md_path.read_text(encoding="utf-8")
  without_fences = _FENCED_CODE_RE.sub("\n", raw)
  anchors: set[str] = set()

  for match in _HTML_ANCHOR_RE.finditer(without_fences):
    anchors.add(match.group(1))

  without_comments = _HTML_COMMENT_RE.sub("", without_fences)
  counts: dict[str, int] = {}
  for match in _HEADING_RE.finditer(without_comments):
    slug = slugify_github_heading(match.group(1))
    if not slug:
      continue
    seen = counts.get(slug, 0)
    counts[slug] = seen + 1
    if seen == 0:
      anchors.add(slug)
    else:
      anchors.add(f"{slug}-{seen}")

  return anchors


def extract_links(markdown_text: str) -> list[str]:
  """Extracts Markdown and HTML link targets from documentation text."""
  cleaned = strip_ignored_regions(markdown_text)
  targets: list[str] = []
  for match in _MD_LINK_RE.finditer(cleaned):
    raw_target = match.group(1).strip()
    if raw_target:
      targets.append(raw_target.split()[0])
  for match in _HTML_HREF_RE.finditer(cleaned):
    raw_target = match.group(1).strip()
    if raw_target:
      targets.append(raw_target)
  return targets


def validate_markdown_files(
    repo_root: Path,
    rel_paths: Sequence[str] = TARGET_DOCS,
) -> list[str]:
  """Validates relative links and anchors across target documentation files."""
  errors: list[str] = []
  anchor_cache: dict[Path, set[str]] = {}

  def _get_anchors(path: Path) -> set[str]:
    resolved = path.resolve()
    if resolved not in anchor_cache:
      anchor_cache[resolved] = collect_markdown_anchors(resolved)
    return anchor_cache[resolved]

  for rel_path in rel_paths:
    doc_path = repo_root / rel_path
    if not doc_path.is_file():
      errors.append(f"{rel_path}: required documentation file does not exist")
      continue

    raw_text = doc_path.read_text(encoding="utf-8")

    for target in extract_links(raw_text):
      if target.startswith(_EXTERNAL_SCHEMES):
        continue

      if "#" in target:
        path_part, anchor = target.split("#", 1)
      else:
        path_part, anchor = target, ""

      if path_part.startswith("/"):
        errors.append(
            f"{rel_path}: root-relative link {target!r} is not portable; "
            "use a relative path"
        )
        continue

      if path_part:
        target_path = (doc_path.parent / path_part).resolve()
      else:
        target_path = doc_path.resolve()

      if not target_path.is_relative_to(repo_root.resolve()):
        errors.append(
            f"{rel_path}: link target {target!r} escapes repository root"
        )
        continue

      if not target_path.exists():
        errors.append(
            f"{rel_path}: broken relative link target {target!r} "
            f"(resolved to {target_path})"
        )
        continue

      if anchor:
        if target_path.is_dir():
          readme_candidate = target_path / "README.md"
          if not readme_candidate.is_file():
            errors.append(
                f"{rel_path}: link {target!r} has anchor #{anchor} on "
                "directory without README.md"
            )
            continue
          anchor_file = readme_candidate
        else:
          anchor_file = target_path

        if anchor_file.suffix.lower() == ".md":
          valid_anchors = _get_anchors(anchor_file)
          if anchor not in valid_anchors:
            errors.append(
                f"{rel_path}: broken anchor #{anchor} in link {target!r} "
                f"(available in {anchor_file.relative_to(repo_root.resolve())}: "
                f"{sorted(valid_anchors)})"
            )

  return errors


def test_markdown_links_and_anchors() -> None:
  """Pytest entrypoint verifying all target documentation links and anchors."""
  errors = validate_markdown_files(_REPO_ROOT, TARGET_DOCS)
  assert not errors, "Documentation validation failed:\n" + "\n".join(errors)


class TestDocsLinks(unittest.TestCase):
  """Unit and integration tests for the documentation link and anchor validator."""

  def test_slugify_github_heading(self):
    self.assertEqual(
        slugify_github_heading(
            "Step 1 — Provision Google Cloud & Workload Identity Federation (Terraform)"
        ),
        "step-1--provision-google-cloud--workload-identity-federation-terraform",
    )
    self.assertEqual(
        slugify_github_heading(
            "Step 3 — Build & Publish the `cm-runner` Container Image"
        ),
        "step-3--build--publish-the-cm-runner-container-image",
    )
    self.assertEqual(
        slugify_github_heading("See [Guide](docs/guide.md) *Now* _Here_"),
        "see-guide-now-here",
    )

  def test_strip_ignored_regions_removes_code_blocks_and_inline_code(self):
    sample = """
# Active Heading

Check [valid link](existing.md) and [`code` label](code-label.md).
![Diagram](assets/diagram.svg)

`[inline code link](missing-inline.md)`

```bash
# [fenced code link](missing-fenced.md)
```
"""
    extracted = extract_links(sample)
    self.assertEqual(
        extracted, ["existing.md", "code-label.md", "assets/diagram.svg"]
    )

  def test_validator_ignores_external_urls_and_detects_broken_links_and_anchors(
      self,
  ):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      (root / "target.md").write_text(
          "# Target Heading\n\n<a id=\"custom-anchor\"></a>\n",
          encoding="utf-8",
      )
      (root / "valid.md").write_text(
          "# Valid Doc\n\n"
          "- [External HTTPS](https://example.com/docs#anchor)\n"
          "- [External HTTP](http://example.com)\n"
          "- [Mailto](mailto:security@example.com)\n"
          "- [Cross Heading](target.md#target-heading)\n"
          "- [Cross HTML Anchor](target.md#custom-anchor)\n"
          "- [Self Heading](#valid-doc)\n",
          encoding="utf-8",
      )
      self.assertEqual(validate_markdown_files(root, ("valid.md",)), [])

      (root / "broken.md").write_text(
          "# Broken Doc\n\n"
          "- [Missing File](nope.md)\n"
          "- [Missing Anchor](target.md#non-existent)\n"
          "- [Line Range Anchor](target.md#L10-L20)\n"
          "- [Root Relative](/target.md)\n"
          "- [Escapes Root](../outside.md)\n",
          encoding="utf-8",
      )
      errs = validate_markdown_files(root, ("broken.md",))
      self.assertEqual(len(errs), 5)
      self.assertTrue(any("nope.md" in e for e in errs))
      self.assertTrue(any("#non-existent" in e for e in errs))
      self.assertTrue(any("#L10-L20" in e for e in errs))
      self.assertTrue(any("root-relative link" in e for e in errs))
      self.assertTrue(any("escapes repository root" in e for e in errs))

  def test_repository_markdown_links_and_anchors(self):
    test_markdown_links_and_anchors()


if __name__ == "__main__":
  unittest.main()
