#!/usr/bin/env python3
"""Render the profile README's activity card as a self-contained SVG.

Runs daily in GitHub Actions (.github/workflows/stats.yml) and publishes to the
`output` branch, so the README never depends on a third-party card service that
can rate limit. Standard library only, and it only reads public data.

    GITHUB_TOKEN=... python3 scripts/stats.py --login srikar0805 --out dist/stats.svg
"""
import argparse
import datetime as dt
import json
import math
import os
import sys
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

# 100 public repos is plenty for this profile; paginate if that ever changes.
QUERY = """
query($login: String!) {
  user(login: $login) {
    login
    followers { totalCount }
    repositories(first: 100, ownerAffiliations: OWNER, privacy: PUBLIC, isFork: false) {
      totalCount
      nodes {
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
    contributionsCollection {
      totalCommitContributions
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date weekday contributionCount } }
      }
    }
  }
}
"""

# Byte counts for these say more about generated output than about the code
# written: exported notebooks, data-heavy HTML dashboards, stylesheets.
EXCLUDED_LANGUAGES = {"HTML", "CSS", "SCSS", "Jupyter Notebook", "EJS", "Handlebars"}

W, H = 1200, 440
CYAN, VIOLET, TEAL_DIM = "#62f0dc", "#9d86ff", "#1b5e58"
INK, MUTED, BODY = "#eef3f8", "#66758a", "#a9b6c4"
SANS = "'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace"


def fetch(login: str, token: str) -> dict:
    request = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": QUERY, "variables": {"login": login}}).encode(),
        headers={"Authorization": f"bearer {token}", "Content-Type": "application/json", "User-Agent": "profile-stats"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.load(response)
    user = (body.get("data") or {}).get("user")
    if body.get("errors") or not user:
        sys.exit(f"GitHub API error: {body.get('errors') or 'user not found'}")
    return user


def streaks(days: list[dict]) -> tuple[int, int]:
    counts = [d["contributionCount"] for d in sorted(days, key=lambda d: d["date"])]
    longest = run = 0
    for count in counts:
        run = run + 1 if count else 0
        longest = max(longest, run)
    current, i = 0, len(counts) - 1
    if i >= 0 and counts[i] == 0:
        i -= 1  # today isn't over, so an empty today doesn't break the streak
    while i >= 0 and counts[i]:
        current += 1
        i -= 1
    return current, longest


def top_languages(repos: list[dict], limit: int = 5) -> list[tuple[str, float, str]]:
    sizes: dict[str, int] = {}
    colors: dict[str, str] = {}
    for repo in repos:
        for edge in repo["languages"]["edges"]:
            name = edge["node"]["name"]
            if name in EXCLUDED_LANGUAGES:
                continue
            sizes[name] = sizes.get(name, 0) + edge["size"]
            colors[name] = edge["node"]["color"] or "#8b949e"
    total = sum(sizes.values())
    if not total:
        return []
    ranked = sorted(sizes.items(), key=lambda kv: -kv[1])
    result = [(name, size / total, colors[name]) for name, size in ranked[:limit]]
    rest = sum(size for _, size in ranked[limit:]) / total
    if rest >= 0.005:
        result.append(("Other", rest, "#3a4658"))
    return result


def mix(a: str, b: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def shade(color: str, factor: float) -> str:
    return mix("#000000", color, factor)


def poly(points, fill: str) -> str:
    return f'<polygon points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in points)}" fill="{fill}"/>'


def skyline(weeks: list[dict], right: float, base_y: float) -> tuple[str, int]:
    """Isometric-style towers, one per day: weeks run right and away, weekdays run toward the viewer."""
    step_week_x, step_week_up, step_day_x, step_day_down, fill = 11.6, 3.0, 6.8, 7.0, 0.8
    x0 = right - (len(weeks) * step_week_x + 7 * step_day_x)
    cells = [(wi, day["weekday"], day["contributionCount"]) for wi, week in enumerate(weeks) for day in week["contributionDays"]]
    peak = max((count for _, _, count in cells), default=0) or 1

    def pt(c: float, r: float, h: float = 0.0):
        return (x0 + c * step_week_x + r * step_day_x, base_y - c * step_week_up + r * step_day_down - h)

    shapes = []
    # Painter's order: cells higher on screen are farther away, so draw them first.
    for c, r, count in sorted(cells, key=lambda cell: -cell[0] * step_week_up + cell[1] * step_day_down):
        p00, p10, p11, p01 = pt(c, r), pt(c + fill, r), pt(c + fill, r + fill), pt(c, r + fill)
        if not count:
            shapes.append(poly([p00, p10, p11, p01], "#101a28"))
            continue
        t = math.sqrt(count / peak)
        h = 4 + 76 * t
        top = mix(TEAL_DIM, CYAN, t * 1.5)
        if t > 0.7:
            top = mix(top, VIOLET, (t - 0.7) / 0.3)
        q00, q10, q11, q01 = pt(c, r, h), pt(c + fill, r, h), pt(c + fill, r + fill, h), pt(c, r + fill, h)
        shapes.append(poly([p00, p01, q01, q00], shade(top, 0.42)))  # left face
        shapes.append(poly([p01, p11, q11, q01], shade(top, 0.62)))  # front face
        shapes.append(poly([q00, q10, q11, q01], top))
    return "\n".join(shapes), peak


def render(user: dict, today: dt.date) -> str:
    calendar = user["contributionsCollection"]["contributionCalendar"]
    weeks = calendar["weeks"]
    days = [day for week in weeks for day in week["contributionDays"]]
    current, longest = streaks(days)
    languages = top_languages(user["repositories"]["nodes"])

    stats = [
        (calendar["totalContributions"], "contributions, past year"),
        (user["contributionsCollection"]["totalCommitContributions"], "commits, past year"),
        (current, "day current streak"),
        (longest, "day longest streak"),
        (user["repositories"]["totalCount"], "public repositories"),
        (user["followers"]["totalCount"], "followers"),
    ]
    parts = []
    for i, (value, label) in enumerate(stats):
        x, y = 48 + (i % 2) * 210, 150 + (i // 2) * 68
        parts.append(f'<text x="{x}" y="{y}" font-family="{SANS}" font-size="34" font-weight="700" fill="{CYAN if i % 2 == 0 else INK}">{value:,}</text>')
        parts.append(f'<text x="{x}" y="{y + 20}" font-family="{MONO}" font-size="11" letter-spacing="1.2" fill="{MUTED}">{escape(label.upper())}</text>')

    bar_x, bar_w = 48.0, 400.0
    parts.append(f'<text x="48" y="354" font-family="{MONO}" font-size="11" letter-spacing="2" fill="{MUTED}">TOP LANGUAGES <tspan fill="{BODY}">BY CODE SIZE</tspan></text>')
    parts.append(f'<rect x="{bar_x}" y="366" width="{bar_w}" height="8" rx="4" fill="#101a28"/>')
    parts.append('<g clip-path="url(#barclip)">')
    offset = bar_x
    for name, share, color in languages:
        width = bar_w * share
        parts.append(f'<rect x="{offset:.1f}" y="366" width="{width + 0.6:.1f}" height="8" fill="{color}"/>')
        offset += width
    parts.append("</g>")
    for i, (name, share, color) in enumerate(languages):
        x, y = 48 + (i % 3) * 138, 396 + (i // 3) * 20
        parts.append(f'<circle cx="{x + 4}" cy="{y - 4}" r="4" fill="{color}"/>')
        parts.append(f'<text x="{x + 14}" y="{y}" font-family="{MONO}" font-size="11.5" fill="{BODY}">{escape(name)} <tspan fill="{MUTED}">{share * 100:.0f}%</tspan></text>')

    if days:
        towers, peak = skyline(weeks, right=1152, base_y=318)
        caption = f"PAST 12 MONTHS  ·  {calendar['totalContributions']:,} CONTRIBUTIONS  ·  BUSIEST DAY {peak:,}"
    else:
        towers, caption = "", "NO PUBLIC ACTIVITY IN THE PAST YEAR"

    style = "@media (prefers-reduced-motion: reduce) { .scan { display: none } }"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-labelledby="title desc">
<title id="title">GitHub activity for @{escape(user['login'])}</title>
<desc id="desc">{calendar['totalContributions']:,} contributions and a longest streak of {longest} days in the past year, {user['repositories']['totalCount']} public repositories. Refreshed {today.isoformat()}.</desc>
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#070b14"/><stop offset="1" stop-color="#0b1330"/></linearGradient>
  <linearGradient id="scan" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{CYAN}" stop-opacity="0"/><stop offset=".5" stop-color="{CYAN}" stop-opacity=".16"/><stop offset="1" stop-color="{CYAN}" stop-opacity="0"/></linearGradient>
  <clipPath id="barclip"><rect x="48" y="366" width="400" height="8" rx="4"/></clipPath>
  <clipPath id="skyclip"><rect x="490" y="30" width="690" height="370"/></clipPath>
  <style>{style}</style>
</defs>
<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="14" fill="url(#bg)" stroke="{CYAN}" stroke-opacity=".18"/>
<path d="M14 40V14H40M{W - 40} 14H{W - 14}V40M14 {H - 40}V{H - 14}H40M{W - 40} {H - 14}H{W - 14}V{H - 40}" fill="none" stroke="{CYAN}" stroke-opacity=".7" stroke-width="1.5"/>
<text x="48" y="66" font-family="{MONO}" font-size="12.5" letter-spacing="3" fill="{CYAN}">PROFILE TELEMETRY</text>
<text x="48" y="90" font-family="{MONO}" font-size="11.5" fill="{MUTED}">@{escape(user['login'])} · public activity · refreshed {today.isoformat()}</text>
<line x1="476" y1="56" x2="476" y2="{H - 56}" stroke="{CYAN}" stroke-opacity=".12"/>
{chr(10).join(parts)}
<g clip-path="url(#skyclip)">
{towers}
<rect class="scan" x="0" y="30" width="90" height="370" fill="url(#scan)"><animate attributeName="x" from="300" to="1180" dur="7s" repeatCount="indefinite"/></rect>
</g>
<text x="1152" y="{H - 34}" text-anchor="end" font-family="{MONO}" font-size="11" letter-spacing="1.5" fill="{MUTED}">{escape(caption)}</text>
</svg>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--login", default=os.environ.get("GITHUB_REPOSITORY_OWNER", "srikar0805"))
    parser.add_argument("--out", default="dist/stats.svg")
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        sys.exit("Set GITHUB_TOKEN (the Actions token is enough).")
    svg = render(fetch(args.login, token), dt.datetime.now(dt.timezone.utc).date())
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(svg, encoding="utf-8")
    print(f"wrote {out} ({len(svg.encode()):,} bytes)")


if __name__ == "__main__":
    main()
