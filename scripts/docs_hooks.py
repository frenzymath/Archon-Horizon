"""MkDocs hooks: reuse root guides and link source files to the exact build ref."""

from pathlib import Path
import os
import posixpath
import re

from mkdocs.structure.files import File


ROOT = Path(__file__).resolve().parent.parent
GENERATED = {"overview.md": "README.md", "contributing.md": "CONTRIBUTING.md"}


def on_files(files, config):
    """Expose root guides without maintaining a second documentation copy."""
    for target, source in GENERATED.items():
        files.append(File.generated(config, target, content=(ROOT / source).read_text()))
    return files


def on_page_markdown(markdown, page, config, files):
    """Keep repository-relative links usable in both source Markdown and Pages."""
    source = GENERATED.get(page.file.src_uri, f"docs/{page.file.src_uri}")
    ref = os.environ.get("HORIZON_DOCS_REF", "main")
    repository = f"https://github.com/frenzymath/Archon-Horizon/blob/{ref}/"

    def replace(match):
        target = match.group(1)
        if re.match(r"(?:[a-zA-Z][a-zA-Z0-9+.-]*:|/|#|\?)", target):
            return match.group(0)
        path, separator, fragment = target.partition("#")
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
        if resolved == "CONTRIBUTING.md":
            site_path = "contributing.md"
        elif resolved == "README.md":
            site_path = "overview.md"
        elif resolved.startswith("docs/"):
            site_path = resolved[5:]
        else:
            return f"]({repository}{resolved}{separator}{fragment})"
        relative = posixpath.relpath(site_path, posixpath.dirname(page.file.src_uri) or ".")
        return f"]({relative}{separator}{fragment})"

    def image_source(match):
        # Raw HTML images bypass MkDocs' Markdown link rewriting. Root guides
        # need paths relative to the generated page URL, including its directory.
        target = match.group(2)
        if re.match(r"(?:[a-zA-Z][a-zA-Z0-9+.-]*:|/|#|\?)", target):
            return match.group(0)
        path, separator, fragment = target.partition("#")
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
        if not resolved.startswith("docs/"):
            return match.group(0)
        relative = posixpath.relpath(resolved[5:], posixpath.dirname(page.url) or ".")
        return match.group(1) + relative + separator + fragment + match.group(3)

    markdown = re.sub(r"\]\(([^\s)]+)\)", replace, markdown)
    return re.sub(r"(<img\b[^>]*?\bsrc=[\"'])([^\"']+)([\"'])", image_source, markdown, flags=re.I)
