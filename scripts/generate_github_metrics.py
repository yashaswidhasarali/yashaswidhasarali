#!/usr/bin/env python3
"""Generate GitHub contribution and language charts from real API data.

This script uses the GitHub GraphQL API to collect contribution data from
2020-01-01 onward and the REST API to fetch repository language breakdowns.
It then writes JSON and SVG artifacts under `generated/`.

Required environment variables:
  - GITHUB_TOKEN: Personal access token with `read:user` and `repo` if private
    repositories should be included.

Optional environment variables:
  - GITHUB_USERNAME: GitHub login to analyze. Defaults to yashaswidhasarali.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple


ROOT = Path(__file__).resolve().parents[1]
GENERATED_DIR = ROOT / "generated"
USERNAME = os.getenv("GITHUB_USERNAME", "yashaswidhasarali")
TOKEN = os.getenv("GITHUB_TOKEN")
START_DATE = date(2020, 1, 1)


class GithubMetricsError(RuntimeError):
    """Raised when GitHub API auth or permissions are not sufficient."""


def github_graphql(query: str, variables: dict) -> dict:
    if not TOKEN:
        raise GithubMetricsError(
            "Missing GITHUB_TOKEN. Add a repo Actions secret named "
            "PROFILE_GH_TOKEN and pass it as GITHUB_TOKEN."
        )

    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "github-metrics-generator",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise GithubMetricsError(
                "GitHub rejected GITHUB_TOKEN with HTTP 401 Unauthorized. "
                "The token is invalid, expired, or pasted incorrectly."
            ) from exc
        if exc.code == 403:
            raise GithubMetricsError(
                "GitHub returned HTTP 403 Forbidden. The token is valid but "
                "is missing required access, has not been approved for the org, "
                "or has hit an API restriction."
            ) from exc
        raise
    if payload.get("errors"):
        message = "GitHub GraphQL returned errors."
        rendered_errors = json.dumps(payload["errors"], indent=2)
        if any(
            "Resource not accessible by personal access token"
            in error.get("message", "")
            for error in payload["errors"]
        ):
            message = (
                "The token cannot access some contribution data. If you want "
                "private or org repository activity included, grant the token "
                "repo access and org approval where needed."
            )
        raise GithubMetricsError(f"{message}\n{rendered_errors}")
    return payload["data"]


def github_rest(path: str) -> dict:
    if not TOKEN:
        raise GithubMetricsError(
            "Missing GITHUB_TOKEN. Add a repo Actions secret named "
            "PROFILE_GH_TOKEN and pass it as GITHUB_TOKEN."
        )

    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-metrics-generator",
        },
    )
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise GithubMetricsError(
                "GitHub rejected GITHUB_TOKEN with HTTP 401 Unauthorized. "
                "The token is invalid, expired, or pasted incorrectly."
            ) from exc
        if exc.code == 403:
            raise GithubMetricsError(
                "GitHub returned HTTP 403 Forbidden while reading repository data. "
                "The token likely lacks repo access or org approval."
            ) from exc
        raise


def fetch_yearly_contributions(username: str, start_year: int, end_year: int) -> dict:
    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!, $maxRepos: Int!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          contributionCalendar {
            totalContributions
            weeks {
              contributionDays {
                contributionCount
                date
              }
            }
          }
          commitContributionsByRepository(maxRepositories: $maxRepos) {
            repository {
              nameWithOwner
              isPrivate
              url
            }
            contributions(first: 1) {
              totalCount
            }
          }
        }
      }
    }
    """

    years = {}
    repo_commits: Counter[str] = Counter()

    for year in range(start_year, end_year + 1):
        from_iso = f"{year}-01-01T00:00:00Z"
        to_iso = f"{year}-12-31T23:59:59Z"
        data = github_graphql(
            query,
            {
                "login": username,
                "from": from_iso,
                "to": to_iso,
                "maxRepos": 100,
            },
        )
        collection = data["user"]["contributionsCollection"]
        calendar = collection["contributionCalendar"]
        days = [
            day
            for week in calendar["weeks"]
            for day in week["contributionDays"]
            if day["date"].startswith(str(year))
        ]
        years[str(year)] = {
            "total_contributions": calendar["totalContributions"],
            "days": days,
        }
        for repo_entry in collection["commitContributionsByRepository"]:
            repo_name = repo_entry["repository"]["nameWithOwner"]
            repo_commits[repo_name] += repo_entry["contributions"]["totalCount"]

    return {"years": years, "repo_commit_counts": dict(repo_commits)}


def fetch_repo_languages(repo_name: str) -> dict:
    owner, repo = repo_name.split("/", 1)
    safe_repo = urllib.parse.quote(repo, safe="")
    try:
        return github_rest(f"/repos/{owner}/{safe_repo}/languages")
    except urllib.error.HTTPError as exc:
        if exc.code in {403, 404}:
            return {}
        raise


def aggregate_languages(repo_commit_counts: Dict[str, int]) -> List[Tuple[str, float]]:
    weighted = defaultdict(float)
    for repo_name, commit_count in repo_commit_counts.items():
        if commit_count <= 0:
            continue
        languages = fetch_repo_languages(repo_name)
        total_bytes = sum(languages.values())
        if total_bytes <= 0:
            continue
        for language, byte_count in languages.items():
            weighted[language] += commit_count * (byte_count / total_bytes)
    ranked = sorted(weighted.items(), key=lambda item: item[1], reverse=True)
    return ranked


def safe_metrics_payload(
    username: str,
    yearly_data: dict,
    ranked_languages: List[Tuple[str, float]],
    generated_at: str,
    today: date,
) -> dict:
    return {
        "username": username,
        "generated_at": generated_at,
        "from_year": START_DATE.year,
        "to_year": today.year,
        "summary": {
            "repositories_with_commit_activity": len(yearly_data["repo_commit_counts"]),
            "total_commit_contributions_observed": sum(
                yearly_data["repo_commit_counts"].values()
            ),
        },
        "yearly_contributions": {
            year: data["total_contributions"] for year, data in yearly_data["years"].items()
        },
        "languages_weighted_by_commit_activity": [
            {"language": language, "score": round(score, 4)}
            for language, score in ranked_languages
        ],
        "methodology": {
            "description": "Repo language bytes are weighted by commit contribution counts per repository.",
            "notes": [
                "This is an approximation, not exact changed lines by language.",
                "Private repositories are included only when the token can access them.",
                "Public artifacts intentionally omit repository names to avoid leaking private org details.",
            ],
        },
    }


def ensure_generated_dir() -> None:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def svg_header(width: int, height: int) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img">'
    )


def placeholder_svg(title: str, subtitle: str) -> str:
    return f"""{svg_header(1200, 320)}
<rect width="1200" height="320" rx="24" fill="#0d1117"/>
<text x="56" y="96" fill="#e6edf3" font-size="36" font-family="ui-sans-serif, system-ui, sans-serif" font-weight="700">{title}</text>
<text x="56" y="150" fill="#8b949e" font-size="22" font-family="ui-sans-serif, system-ui, sans-serif">{subtitle}</text>
<text x="56" y="210" fill="#58a6ff" font-size="20" font-family="ui-sans-serif, system-ui, sans-serif">Run scripts/generate_github_metrics.py with GITHUB_TOKEN set.</text>
</svg>
"""


def build_languages_svg(username: str, ranked_languages: List[Tuple[str, float]]) -> str:
    width, height = 1200, 320
    left = 56
    top = 100
    max_bar_width = 520
    row_gap = 34
    colors = ["#58a6ff", "#3fb950", "#f78166", "#d2a8ff", "#ffa657", "#79c0ff"]
    top_languages = ranked_languages[:6]
    total_value = sum(score for _, score in top_languages) or 1
    max_value = top_languages[0][1] if top_languages else 1

    parts = [
        svg_header(width, height),
        '<rect width="1200" height="320" rx="24" fill="#0d1117"/>',
        f'<text x="{left}" y="56" fill="#e6edf3" font-size="30" font-family="ui-sans-serif, system-ui, sans-serif" font-weight="700">Commit language mix</text>',
        f'<text x="{left}" y="82" fill="#8b949e" font-size="16" font-family="ui-sans-serif, system-ui, sans-serif">@{username} | Estimated from repository language bytes weighted by commit activity</text>',
    ]

    for index, (language, score) in enumerate(top_languages):
        y = top + index * row_gap
        color = colors[index % len(colors)]
        bar_width = 0 if max_value == 0 else max_bar_width * (score / max_value)
        percentage = (score / total_value) * 100
        parts.extend(
            [
                f'<circle cx="{left + 8}" cy="{y - 6}" r="6" fill="{color}"/>',
                f'<text x="{left + 24}" y="{y}" fill="#c9d1d9" font-size="18" font-family="ui-sans-serif, system-ui, sans-serif">{language}</text>',
                f'<rect x="{left + 240}" y="{y - 16}" width="{max_bar_width}" height="14" rx="7" fill="#21262d"/>',
                f'<rect x="{left + 240}" y="{y - 16}" width="{bar_width:.2f}" height="14" rx="7" fill="{color}"/>',
                f'<text x="{left + 800}" y="{y}" text-anchor="end" fill="#c9d1d9" font-size="17" font-family="ui-sans-serif, system-ui, sans-serif">{percentage:.1f}%</text>',
            ]
        )

    parts.append("</svg>")
    return "\n".join(parts)


def build_yearly_svg(username: str, years: Dict[str, dict]) -> str:
    width, height = 1200, 360
    left = 70
    bottom = 300
    chart_height = 180
    bar_width = 84
    gap = 40
    colors = ["#1f6feb", "#2ea043", "#a371f7", "#db6d28", "#d29922", "#bf3989", "#58a6ff"]
    ordered_years = sorted(years.items())
    max_total = max((year_data["total_contributions"] for _, year_data in ordered_years), default=1)

    parts = [
        svg_header(width, height),
        '<rect width="1200" height="360" rx="24" fill="#0d1117"/>',
        f'<text x="{left}" y="58" fill="#e6edf3" font-size="34" font-family="ui-sans-serif, system-ui, sans-serif" font-weight="700">Contribution totals by year</text>',
        f'<text x="{left}" y="90" fill="#8b949e" font-size="18" font-family="ui-sans-serif, system-ui, sans-serif">@{username} | GitHub contributionsCollection from 2020 onward</text>',
        f'<line x1="{left}" y1="{bottom}" x2="{width - 70}" y2="{bottom}" stroke="#30363d" stroke-width="1"/>',
    ]

    for index, (year, year_data) in enumerate(ordered_years):
        x = left + index * (bar_width + gap)
        total = year_data["total_contributions"]
        bar_height = 0 if max_total == 0 else chart_height * (total / max_total)
        y = bottom - bar_height
        color = colors[index % len(colors)]
        parts.extend(
            [
                f'<rect x="{x}" y="{y:.2f}" width="{bar_width}" height="{bar_height:.2f}" rx="10" fill="{color}"/>',
                f'<text x="{x + bar_width/2:.1f}" y="{y - 10:.2f}" text-anchor="middle" fill="#c9d1d9" font-size="18" font-family="ui-sans-serif, system-ui, sans-serif">{total}</text>',
                f'<text x="{x + bar_width/2:.1f}" y="{bottom + 28}" text-anchor="middle" fill="#8b949e" font-size="18" font-family="ui-sans-serif, system-ui, sans-serif">{year}</text>',
            ]
        )

    parts.append("</svg>")
    return "\n".join(parts)


def generate() -> None:
    ensure_generated_dir()

    if not TOKEN:
        (GENERATED_DIR / "github-languages-by-commit.svg").write_text(
            placeholder_svg(
                "Languages weighted by commit activity",
                "A GitHub token is required before real metrics can be generated.",
            ),
            encoding="utf-8",
        )
        (GENERATED_DIR / "github-contributions-by-year.svg").write_text(
            placeholder_svg(
                "Contribution totals by year",
                "A GitHub token is required before real metrics can be generated.",
            ),
            encoding="utf-8",
        )
        write_json(
            GENERATED_DIR / "github-metrics.json",
            {
                "username": USERNAME,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "status": "waiting_for_github_token",
                "message": "Add PROFILE_GH_TOKEN as a GitHub Actions secret, then rerun the workflow.",
            },
        )
        return

    today = date.today()
    yearly_data = fetch_yearly_contributions(USERNAME, START_DATE.year, today.year)
    ranked_languages = aggregate_languages(yearly_data["repo_commit_counts"])
    generated_at = datetime.now(timezone.utc).isoformat()
    payload = safe_metrics_payload(
        USERNAME,
        yearly_data,
        ranked_languages,
        generated_at,
        today,
    )
    write_json(GENERATED_DIR / "github-metrics.json", payload)
    (GENERATED_DIR / "github-languages-by-commit.svg").write_text(
        build_languages_svg(USERNAME, ranked_languages),
        encoding="utf-8",
    )
    (GENERATED_DIR / "github-contributions-by-year.svg").write_text(
        build_yearly_svg(USERNAME, yearly_data["years"]),
        encoding="utf-8",
    )


def main() -> int:
    try:
        generate()
        print(f"Wrote metrics under {GENERATED_DIR}")
        return 0
    except GithubMetricsError as exc:  # pragma: no cover - used in automation
        print("GitHub metrics generation failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - used in automation
        print("Unexpected error while generating GitHub metrics.", file=sys.stderr)
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
