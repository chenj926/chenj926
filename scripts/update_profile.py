#!/usr/bin/env python3
"""Generate profile SVGs from GitHub's public data using only the standard library."""

from __future__ import annotations

import argparse
import calendar
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from html import escape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com"
PALETTES = {
    "dark": {"bg": "#0d1117", "panel": "#161b22", "border": "#30363d",
             "text": "#f0f6fc", "muted": "#919baa", "accent": "#67e8f9",
             "green": ["#161b22", "#0e4429", "#006d32", "#26a641", "#39d353"]},
    "light": {"bg": "#ffffff", "panel": "#f6f8fa", "border": "#d1d9e0",
              "text": "#1f2328", "muted": "#59636e", "accent": "#087e8b",
              "green": ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"]},
}
LANGUAGE_COLORS = {"Python": "#3572a5", "Java": "#b07219", "C": "#7d8b99",
                   "C++": "#f34b7d", "C#": "#178600", "TypeScript": "#3178c6",
                   "JavaScript": "#c7aa10", "HTML": "#e34c26", "CSS": "#8b65ce",
                   "Shell": "#5f963a", "R": "#198ce7", "MATLAB": "#e16737",
                   "Jupyter Notebook": "#da5b0b", "Other": "#84929f"}
SKIP_DIRS = {".ipynb_checkpoints", ".venv", "venv", "node_modules", "vendor",
             "third_party", "third-party", "dist", "build"}


def get(url: str) -> bytes:
    """Keep the Actions token on api.github.com; never send it to raw/HTML hosts."""
    headers = {"User-Agent": "chenj926-profile", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if urlparse(url).hostname == "api.github.com" and token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers=headers), timeout=40) as response:
                return response.read()
        except HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(2 ** attempt)
    raise RuntimeError(f"Could not fetch {url}")


def get_json(url: str):
    return json.loads(get(url))


class ContributionParser(HTMLParser):
    """Read the same dated cells and accessible tooltips as GitHub's public graph."""

    def __init__(self):
        super().__init__()
        self.cells = {}
        self.tooltips = {}
        self.active_tooltip = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("data-date") and attrs.get("data-level") is not None:
            self.cells[attrs["id"]] = {
                "date": attrs["data-date"], "level": int(attrs["data-level"])}
        if tag == "tool-tip" and attrs.get("for"):
            self.active_tooltip = attrs["for"]
            self.tooltips[self.active_tooltip] = ""

    def handle_data(self, text):
        if self.active_tooltip is not None:
            self.tooltips[self.active_tooltip] += text

    def handle_endtag(self, tag):
        if tag == "tool-tip":
            self.active_tooltip = None

    def days(self):
        result = []
        for cell_id, cell in self.cells.items():
            tooltip = self.tooltips.get(cell_id, "").strip()
            match = re.match(r"([\d,]+) contributions?\b", tooltip)
            if tooltip.startswith("No contributions"):
                count = 0
            elif match:
                count = int(match.group(1).replace(",", ""))
            else:
                raise ValueError(f"Missing contribution count for {cell['date']}")
            if not 0 <= cell["level"] <= 4 or (count == 0) != (cell["level"] == 0):
                raise ValueError(f"Invalid contribution cell for {cell['date']}")
            result.append({**cell, "count": count})
        result.sort(key=lambda row: row["date"])
        if not result or len({row["date"] for row in result}) != len(result):
            raise ValueError("Missing or duplicate contribution dates")
        return result


def contribution_days(username):
    # Omitting from/to preserves GitHub's native rolling one-year window.
    parser = ContributionParser()
    parser.feed(get(f"https://github.com/users/{quote(username)}/contributions").decode())
    days = parser.days()
    first, last = (date.fromisoformat(days[i]["date"]) for i in (0, -1))
    expected = (last - first).days + 1
    # GitHub's week-aligned calendar can span all 53 weeks (371 days).
    if len(days) != expected or not 364 <= expected <= 371:
        raise ValueError("GitHub did not return a complete one-year contribution calendar")
    if abs((date.today() - last).days) > 2:
        raise ValueError("GitHub returned a stale contribution calendar")
    return days


def notebook_source(notebook):
    """Count only UTF-8 source in code cells, attributed to the declared kernel."""
    metadata = notebook.get("metadata", {})
    kernel = metadata.get("language_info", {}).get("name") or metadata.get("kernelspec", {}).get("language")
    aliases = {"python": "Python", "python3": "Python", "ipython": "Python",
               "r": "R", "ir": "R", "julia": "Julia", "javascript": "JavaScript",
               "typescript": "TypeScript", "c++": "C++", "c": "C", "c#": "C#",
               "bash": "Shell", "sh": "Shell", "matlab": "MATLAB", "scala": "Scala"}
    language = aliases.get(str(kernel).lower(), str(kernel) if kernel else "Jupyter Notebook")
    cells = notebook.get("cells")
    if cells is None:  # Older nbformat 3 notebooks.
        cells = [cell for sheet in notebook.get("worksheets", []) for cell in sheet.get("cells", [])]
    sources = []
    for cell in cells:
        if cell.get("cell_type") == "code":
            source = cell.get("source", cell.get("input", ""))
            sources.append("".join(source) if isinstance(source, list) else source)
    byte_count = sum(len(source.encode("utf-8")) for source in sources)
    line_count = sum(sum(bool(line.strip()) for line in source.splitlines()) for source in sources)
    return language, byte_count, line_count


def public_repositories(config):
    repos = []
    for page in range(1, 100):
        batch = get_json(f"{API}/users/{quote(config['username'])}/repos?type=owner&per_page=100&page={page}")
        repos.extend(repo for repo in batch if not repo["fork"] and not repo["private"]
                     and not repo.get("disabled") and repo["name"] not in config["exclude_repositories"])
        if len(batch) < 100:
            return sorted(repos, key=lambda repo: repo["name"].lower())
    raise ValueError("Repository pagination did not finish")


def repository_languages(repo):
    base = f"{API}/repos/{repo['full_name']}"
    ordinary = get_json(f"{base}/languages")
    raw_notebook_bytes = ordinary.pop("Jupyter Notebook", 0)
    totals = Counter(ordinary)
    notebooks = []
    if raw_notebook_bytes:
        tree = get_json(f"{base}/git/trees/{quote(repo['default_branch'], safe='')}?recursive=1")
        if tree.get("truncated"):
            raise ValueError(f"Truncated notebook inventory in {repo['full_name']}")
        for entry in tree["tree"]:
            path = entry["path"]
            if entry["type"] != "blob" or not path.lower().endswith(".ipynb"):
                continue
            if SKIP_DIRS.intersection(Path(path).parts):
                continue
            # Raw files avoid the size limits of the GitHub Contents API.
            url = f"https://raw.githubusercontent.com/{repo['full_name']}/{quote(repo['default_branch'], safe='')}/{quote(path, safe='/')}"
            notebook = json.loads(get(url))
            language, byte_count, line_count = notebook_source(notebook)
            totals[language] += byte_count
            notebooks.append({"path": path, "language": language,
                              "code_bytes": byte_count, "nonblank_code_lines": line_count})
    return {"repository": repo["full_name"], "source_bytes": dict(totals),
            "github_notebook_bytes": raw_notebook_bytes, "notebooks": notebooks}


def collect(config):
    days = contribution_days(config["username"])
    repos = public_repositories(config)
    # A failed repository aborts the refresh; it never silently changes percentages.
    with ThreadPoolExecutor(max_workers=4) as pool:
        inventories = list(pool.map(repository_languages, repos))
    totals = Counter()
    for inventory in inventories:
        totals.update(inventory["source_bytes"])
    return {"username": config["username"], "as_of": days[-1]["date"],
            "scope": "Owned public non-fork repositories, default branches; configured repositories excluded.",
            "excluded_repositories": config["exclude_repositories"],
            "measurement": "GitHub Linguist source bytes, replacing raw notebook bytes with code-cell UTF-8 source attributed to the kernel language. Not personally authored lines or proficiency.",
            "contributions": days, "languages": dict(totals.most_common()), "repositories": inventories}


def text(x, y, content, fill, size=14, weight=400, extra=""):
    return f'<text x="{x}" y="{y}" fill="{fill}" font-size="{size}" font-weight="{weight}" {extra}>{escape(str(content))}</text>'


def svg_start(width, height, title, description, theme):
    p = PALETTES[theme]
    return [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
            f'<title id="title">{escape(title)}</title><desc id="desc">{escape(description)}</desc>',
            '<style>text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}</style>',
            f'<rect x="0.5" y="0.5" width="{width-1}" height="{height-1}" rx="16" fill="{p["bg"]}" stroke="{p["border"]}"/>']


def header_svg(theme, mobile=False):
    p = PALETTES[theme]
    purple = "#a78bfa" if theme == "dark" else "#7957ca"
    green = "#4ade80" if theme == "dark" else "#1a7f37"
    if mobile:
        out = svg_start(490, 246, "Jialuo (Eric) Chen", "AI research and software engineering. University of Toronto, Class of 2027.", theme)
        out += [text(25, 35, "RESEARCH / ENGINEERING / AI", p["accent"], 10, 650, 'letter-spacing="1.2"'),
                text(24, 91, "Jialuo (Eric) Chen", p["text"], 35, 750, 'letter-spacing="-1"'),
                text(25, 128, "AI research. Software engineering.", p["text"], 19, 500),
                text(25, 159, "University of Toronto  /  Class of 2027", p["muted"], 14),
                text(25, 195, "EMBODIED AI  /  LLM RESEARCH", p["accent"], 11, 650, 'letter-spacing=".5"'),
                text(25, 220, "BAYESIAN OPTIMIZATION", green, 11, 650, 'letter-spacing=".5"'),
                f'<path d="M403 177l24-14 24 14v29l-24 14-24-14z M403 177l24 14 24-14 M427 191v29" fill="{p["panel"]}" stroke="{p["accent"]}" stroke-width="1.3"/>']
        return "\n".join(out + ["</svg>\n"])
    out = svg_start(980, 274, "Jialuo (Eric) Chen", "AI research and software engineering. University of Toronto, Class of 2027.", theme)
    out += ['<defs><pattern id="grid" width="28" height="28" patternUnits="userSpaceOnUse"><path d="M28 0H0V28" fill="none" stroke="'+p["border"]+'" stroke-width="0.6"/></pattern><clipPath id="round"><rect x="1" y="1" width="978" height="272" rx="16"/></clipPath></defs>',
            '<style>@keyframes pulse{0%,100%{opacity:.35}50%{opacity:1}}@keyframes flow{to{stroke-dashoffset:-48}}.pulse{animation:pulse 4s ease-in-out infinite}.flow{animation:flow 4s linear infinite}@media(prefers-reduced-motion:reduce){.pulse,.flow{animation:none}}</style>',
            '<g clip-path="url(#round)"><rect x="617" y="0" width="363" height="274" fill="url(#grid)" opacity=".45"/></g>',
            text(38, 43, "RESEARCH / ENGINEERING / INTELLIGENT SYSTEMS", p["accent"], 11, 650, 'letter-spacing="1.5"'),
            text(36, 109, "Jialuo (Eric) Chen", p["text"], 46, 750, 'letter-spacing="-1.7"'),
            text(38, 148, "AI research. Software engineering.", p["text"], 23, 500),
            text(38, 180, "University of Toronto  /  Industrial Engineering  /  2027", p["muted"], 14),
            f'<rect x="38" y="214" width="146" height="28" rx="7" fill="{p["panel"]}" stroke="{p["border"]}"/>',
            text(111, 232, "EMBODIED AI", p["accent"], 10, 650, 'text-anchor="middle" letter-spacing="1"'),
            f'<rect x="197" y="214" width="138" height="28" rx="7" fill="{p["panel"]}" stroke="{p["border"]}"/>',
            text(266, 232, "LLM RESEARCH", purple, 10, 650, 'text-anchor="middle" letter-spacing="1"'),
            f'<rect x="348" y="214" width="173" height="28" rx="7" fill="{p["panel"]}" stroke="{p["border"]}"/>',
            text(434, 232, "BAYESIAN OPTIMIZATION", green, 10, 650, 'text-anchor="middle" letter-spacing=".5"')]
    nodes = [(690, 76), (744, 49), (842, 85), (905, 48), (690, 154), (753, 115), (840, 155), (913, 142), (747, 221), (892, 220)]
    edges = [(0, 1), (0, 5), (0, 4), (1, 2), (1, 5), (2, 3), (2, 5), (2, 6), (2, 7), (3, 7), (4, 5), (4, 8), (5, 6), (5, 8), (6, 7), (6, 8), (6, 9), (7, 9), (8, 9)]
    for a, b in edges:
        x1, y1 = nodes[a]
        x2, y2 = nodes[b]
        out.append(f'<path d="M{x1},{y1}L{x2},{y2}" fill="none" stroke="{p["border"]}" stroke-width="1.2"/>')
    for index in (2, 6, 12, 16):
        a, b = edges[index]
        x1, y1 = nodes[a]
        x2, y2 = nodes[b]
        out.append(f'<path class="flow" d="M{x1},{y1}L{x2},{y2}" fill="none" stroke="{p["accent"]}" stroke-width="1.2" stroke-dasharray="3 21" opacity=".65"/>')
    for i, (x, y) in enumerate(nodes):
        color = [p["accent"], purple, green][i % 3]
        out += [f'<circle class="pulse" cx="{x}" cy="{y}" r="10" fill="{color}" opacity=".16" style="animation-delay:{i*.3}s"/>',
                f'<circle cx="{x}" cy="{y}" r="3.5" fill="{color}"/>']
    out += [f'<path d="M773 115l36-21 36 21v43l-36 21-36-21z M773 115l36 21 36-21 M809 136v43" fill="{p["bg"]}" stroke="{p["accent"]}" stroke-width="1.5"/>',
            text(809, 198, "FROM MODELS TO SYSTEMS", p["muted"], 9, 600, 'text-anchor="middle" letter-spacing="1.2"')]
    return "\n".join(out + ["</svg>\n"])


def calendar_svg(data, theme, mobile=False):
    p = PALETTES[theme]
    days = data["contributions"]
    total = sum(day["count"] for day in days)
    active = sum(day["count"] > 0 for day in days)
    start, end = date.fromisoformat(days[0]["date"]), date.fromisoformat(days[-1]["date"])
    anchor = start - timedelta(days=(start.weekday() + 1) % 7)
    width = 490 if mobile else 980
    step, cell, left, top = (8, 6, 42, 116) if mobile else (16, 12, 69, 100)
    row_step = 14 if mobile else step
    out = svg_start(width, 266, "GitHub contributions", f"{total} contributions across {active} active days from {start} to {end}.", theme)
    out += [text(28, 37, "GitHub contributions", p["text"], 20, 650),
            text(28, 62, f"{total:,} contributions  /  {active} active days", p["accent"], 14, 600),
            text(28 if mobile else 950, 84 if mobile else 38, f"{start:%b %d, %Y} - {end:%b %d, %Y}", p["muted"], 12, extra='' if mobile else 'text-anchor="end"')]
    for weekday, label in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        out.append(text(10 if mobile else 28, top + weekday * row_step + 10, label, p["muted"], 9 if mobile else 11))
    labels = []
    previous_month = None
    for day in days:
        d = date.fromisoformat(day["date"])
        column = (d - anchor).days // 7
        row = (d.weekday() + 1) % 7
        x, y = left + column * step, top + row * row_step
        out.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{10 if mobile else cell}" rx="{2 if mobile else 3}" fill="{p["green"][day["level"]]}"><title>{day["date"]}: {day["count"]} contributions</title></rect>')
        if d.month != previous_month:
            if not labels or x - labels[-1][0] >= (22 if mobile else 32):
                labels.append((x, calendar.month_abbr[d.month]))
            previous_month = d.month
    for x, label in labels:
        out.append(text(x, top - 12, label, p["muted"], 9 if mobile else 11))
    if mobile:
        out.append(text(28, 238, "Commits, PRs, issues and reviews", p["muted"], 11))
        return "\n".join(out + ["</svg>\n"])
    out.append(text(28, 243, "GitHub activity includes commits, PRs, issues and reviews.", p["muted"], 11))
    out.append(text(814, 243, "Less", p["muted"], 10))
    for level, color in enumerate(p["green"]):
        out.append(f'<rect x="{845+level*15}" y="232" width="11" height="11" rx="2" fill="{color}"/>')
    out.append(text(927, 243, "More", p["muted"], 10))
    return "\n".join(out + ["</svg>\n"])


def language_svg(data, theme, maximum, mobile=False):
    p = PALETTES[theme]
    all_languages = sorted(data["languages"].items(), key=lambda item: (-item[1], item[0]))
    denominator = sum(amount for _, amount in all_languages)
    if not denominator:
        raise ValueError("No source bytes to chart")
    languages = all_languages[:maximum]
    if len(all_languages) > maximum:
        languages.append(("Other", sum(amount for _, amount in all_languages[maximum:])))
    rows = len(languages) if mobile else (len(languages) + 1) // 2
    height = 175 + 30 * rows
    width = 490 if mobile else 980
    bar_width = width - 56
    out = svg_start(width, height, "Languages by source code", "Default branches of owned public non-fork repositories. Notebook code is attributed to its kernel language; outputs and metadata are excluded.", theme)
    out += [text(28, 37, "Languages by source" if mobile else "Languages by source code", p["text"], 20, 650),
            text(28, 62, f"{len(data['repositories'])} public repositories  /  notebook code cells only", p["muted"], 12 if mobile else 13),
            text(width-28, 37, data['as_of'] if mobile else f"Updated {data['as_of']}", p["muted"], 10 if mobile else 11, extra='text-anchor="end"')]
    x = 28
    out.append(f'<defs><clipPath id="bar"><rect x="28" y="81" width="{bar_width}" height="15" rx="7.5"/></clipPath></defs><g clip-path="url(#bar)">')
    for language, amount in languages:
        segment_width = bar_width * amount / denominator
        color = LANGUAGE_COLORS.get(language, "#8b949e")
        out.append(f'<rect x="{x:.3f}" y="81" width="{segment_width+0.1:.3f}" height="15" fill="{color}"/>')
        x += segment_width
    out.append("</g>")
    for i, (language, amount) in enumerate(languages):
        x, y = (28, 127+i*30) if mobile else (28 + (i % 2) * 475, 127 + (i // 2) * 30)
        percentage = amount / denominator * 100
        label = f"{percentage:.2f}%" if percentage < 0.1 else f"{percentage:.1f}%"
        out += [f'<circle cx="{x+5}" cy="{y-5}" r="5" fill="{LANGUAGE_COLORS.get(language, "#8b949e")}"/>',
                text(x+19, y, language, p["text"], 14, 500),
                text(x+433, y, label, p["muted"], 14, extra='text-anchor="end"')]
    count = sum(len(repo["notebooks"]) for repo in data["repositories"])
    if mobile:
        out += [text(28, height-44, f"{count} notebooks: code counted by kernel language.", p["muted"], 12),
                text(28, height-24, "Outputs, Markdown and metadata excluded.", p["muted"], 12)]
        return "\n".join(out + ["</svg>\n"])
    out += [text(28, height-44, f"{count} notebooks parsed. Code counted by kernel language; outputs, Markdown and JSON metadata excluded.", p["muted"], 11),
            text(28, height-24, "Source-byte distribution across repositories; not an authorship or proficiency score.", p["muted"], 11)]
    return "\n".join(out + ["</svg>\n"])


def render(data, config):
    # Collect and validate everything before replacing the last successful snapshot.
    outputs = {}
    for theme in PALETTES:
        outputs[f"header-{theme}.svg"] = header_svg(theme)
        outputs[f"contributions-{theme}.svg"] = calendar_svg(data, theme)
        outputs[f"languages-{theme}.svg"] = language_svg(data, theme, config["max_languages"])
        outputs[f"header-mobile-{theme}.svg"] = header_svg(theme, mobile=True)
        outputs[f"contributions-mobile-{theme}.svg"] = calendar_svg(data, theme, mobile=True)
        outputs[f"languages-mobile-{theme}.svg"] = language_svg(data, theme, config["max_languages"], mobile=True)
    outputs["profile-data.json"] = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    assets = ROOT / "assets"
    assets.mkdir(exist_ok=True)
    for name, content in outputs.items():
        target = assets / name
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, help="Re-render an existing snapshot without network requests")
    args = parser.parse_args()
    config = json.loads((ROOT / "profile-config.json").read_text())
    data = json.loads(args.snapshot.read_text()) if args.snapshot else collect(config)
    render(data, config)
    total = sum(day["count"] for day in data["contributions"])
    print(f"Updated {config['username']}: {total} contributions; {len(data['repositories'])} public repositories.")
    print("Languages:", ", ".join(f"{name} {amount:,} B" for name, amount in data["languages"].items()))


if __name__ == "__main__":
    main()

