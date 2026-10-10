"""Rebuild data/bugs.json from GitHub: every public issue and PR this user opened in other people's repos."""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LOGIN = os.environ.get("BUGS_LOGIN", "maharanay22")
TOKEN = os.environ["GITHUB_TOKEN"]
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "bugs.json"
OVERRIDES = json.loads((ROOT / "data" / "overrides.json").read_text(encoding="utf-8"))

MAX_LINES_PER_FILE = 30
MAX_LINES_PER_FIX = 60
SKIP_PATH = re.compile(r"(^|/)(tests?|testing|docs?)/|(^|/)test_|_test\.py$|whatsnew|\.md$|\.rst$|\.txt$|\.lock$|\.json$")
CLOSES = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#(\d+)", re.I)
TITLE_PREFIX = re.compile(r"^\s*(?:\[[^\]]*\]\s*:?\s*|(?:BUG|ENH|BUG FIX)\s*:\s*|[a-z]+(?:\([^)]*\))?!?:\s*)", re.I)
TRIAGE_HINT = re.compile(r"missing the `?ready`? label|needs? triage|awaiting triage", re.I)

REPO_FIELDS = """
  nameWithOwner name url description stargazerCount forkCount primaryLanguage { name }
  owner { login } repositoryTopics(first: 4) { nodes { topic { name } } }
"""
QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 50, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      __typename
      ... on PullRequest {
        number title url state merged mergedAt createdAt closedAt updatedAt body reviewDecision mergeable
        repository { %s }
        closingIssuesReferences(first: 5) { nodes { number url title state author { login } } }
        comments(last: 15) { nodes { body createdAt authorAssociation author { login __typename } } }
        latestReviews(first: 10) { nodes { state submittedAt authorAssociation author { login __typename } } }
        commits(last: 1) { nodes { commit { committedDate statusCheckRollup { state } } } }
      }
      ... on Issue {
        number title url state stateReason createdAt closedAt updatedAt
        repository { %s }
        labels(first: 10) { nodes { name } }
        timelineItems(itemTypes: [CLOSED_EVENT], last: 1) {
          nodes { ... on ClosedEvent { closer { __typename ... on PullRequest { number url merged author { login } } } } }
        }
      }
    }
  }
}
""" % (REPO_FIELDS, REPO_FIELDS)


def request(url, payload=None, accept="application/vnd.github+json"):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {TOKEN}", "Accept": accept, "User-Agent": f"{LOGIN}-bug-page",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def search(q):
    nodes, after = [], None
    while True:
        res = request("https://api.github.com/graphql", {"query": QUERY, "variables": {"q": q, "after": after}})
        if res.get("errors"):
            sys.exit(f"GraphQL error: {res['errors']}")
        page = res["data"]["search"]
        nodes += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return nodes
        after = page["pageInfo"]["endCursor"]


def clean_title(title):
    t = TITLE_PREFIX.sub("", title, count=1).strip()
    first_word = t.split(" ", 1)[0]
    return t[:1].upper() + t[1:] if first_word.isalpha() else t


def parse_patch(patch):
    rows = []
    for line in patch.splitlines():
        if line.startswith("@@"):
            rows.append(["h", re.sub(r"^(@@ [^@]+ @@).*", r"\1", line)])
        elif line.startswith("\\"):
            continue
        else:
            rows.append([{"+": "a", "-": "d"}.get(line[:1], "c"), line[1:]])
    return rows


def fix_diffs(repo, number):
    files = request(f"https://api.github.com/repos/{repo}/pulls/{number}/files?per_page=100")
    out, budget = [], MAX_LINES_PER_FIX
    for f in files:
        if budget <= 0 or SKIP_PATH.search(f["filename"]) or not f.get("patch"):
            continue
        rows = parse_patch(f["patch"])
        take = min(len(rows), MAX_LINES_PER_FILE, budget)
        out.append({"path": f["filename"], "lines": rows[:take], "truncated": take < len(rows),
                    "additions": f["additions"], "deletions": f["deletions"]})
        budget -= take
    return out


def repo_info(r):
    o = OVERRIDES.get("repos", {}).get(r["nameWithOwner"], {})
    topics = [n["topic"]["name"] for n in r["repositoryTopics"]["nodes"]]
    return {
        "full": r["nameWithOwner"], "name": o.get("name", r["name"]), "url": r["url"],
        "about": o.get("about") or (r["description"] or "")[:180],
        "icon": o.get("icon", "📦"), "category": o.get("category") or (r["primaryLanguage"] or {}).get("name", "open source"),
        "tags": o.get("tags", topics[:3]), "stars": r["stargazerCount"], "forks": r["forkCount"],
    }


MAINTAINER = {"OWNER", "MEMBER", "COLLABORATOR"}
STAGES = ["reported", "fix sent", "tests", "review", "merged"]
CHECKS = {"success": "passing", "failure": "failing", "error": "failing", "pending": "running",
          "expected": "running", "none": "not started"}


KNOWN_BOTS = {"claassistant", "codecov", "codspeed-hq", "netlify", "vercel", "dosubot", "copilot"}


def is_bot(author):
    if not author:
        return True
    login = author["login"].lower()
    return author.get("__typename") == "Bot" or login.endswith("[bot]") or login.endswith("-bot") or login in KNOWN_BOTS


def plan(state, waiting_on, next_step):
    return {"state": state, "waiting_on": waiting_on, "next": next_step}


def track_pr(pr, status):
    """Work out where a fix stands and who has to act next."""
    commit = (pr["commits"]["nodes"] or [{}])[0].get("commit") or {}
    checks = CHECKS.get(((commit.get("statusCheckRollup") or {}).get("state") or "NONE").lower(), "running")
    last_push = commit.get("committedDate") or pr["createdAt"]
    reviews = [r for r in pr["latestReviews"]["nodes"] if not is_bot(r["author"])]
    bot_ok = any(is_bot(r["author"]) and r["state"] == "APPROVED" for r in pr["latestReviews"]["nodes"])
    human = [c for c in pr["comments"]["nodes"] if not is_bot(c["author"])]
    mine = [c for c in human if c["author"]["login"] == LOGIN]
    others = [c for c in human if c["author"]["login"] != LOGIN]
    my_last = max([c["createdAt"] for c in mine] + [last_push])
    unanswered = [c for c in others if c["createdAt"] > my_last]
    approvals = [r for r in reviews if r["state"] == "APPROVED"]
    maint_ok = [r["author"]["login"] for r in approvals if r["authorAssociation"] in MAINTAINER]
    community_ok = [r["author"]["login"] for r in approvals if r["authorAssociation"] not in MAINTAINER]
    changes = [r for r in reviews if r["state"] == "CHANGES_REQUESTED"]
    review = ("approved" if maint_ok else "changes requested" if changes else
              "community approved" if community_ok else "bot approved" if bot_ok else
              "commented" if reviews or others else "waiting")
    last_other = max(others, key=lambda c: c["createdAt"]) if others else None
    info = {
        "stage": 4 if checks == "passing" else 3, "checks": checks, "review": review,
        "approved_by": maint_ok + community_ok, "last_push": last_push,
        "updated_at": pr["updatedAt"], "conflicts": pr.get("mergeable") == "CONFLICTING",
        "last_comment": {"who": last_other["author"]["login"], "at": last_other["createdAt"],
                         "maintainer": last_other["authorAssociation"] in MAINTAINER} if last_other else None,
    }
    if status == "merged":
        return {**info, "stage": 5, "checks": "passing", "review": "approved",
                **plan("done", "nobody", "Merged. It ships in the project's next release")}
    if status == "triage":
        return {**info, "stage": 2, **plan("waiting", "maintainers",
                "A bot closed the PR until maintainers label the issue `ready`. It gets reopened once they do")}
    if status == "closed":
        return {**info, "stage": 2, **plan("done", "nobody", "Closed without merging")}
    if changes:
        who = ", ".join(r["author"]["login"] for r in changes)
        if last_push > max(r["submittedAt"] for r in changes) or (mine and mine[-1]["createdAt"] > max(r["submittedAt"] for r in changes)):
            return {**info, **plan("waiting", "maintainers", f"Requested changes are pushed. Waiting for {who} to look again")}
        return {**info, **plan("action", "you", f"{who} asked for changes. Update the PR")}
    if unanswered:
        return {**info, **plan("action", "you", f"{unanswered[-1]['author']['login']} commented. Reply on the PR")}
    if info["conflicts"]:
        return {**info, **plan("action", "you", "The branch conflicts with main. Rebase it")}
    if checks == "failing":
        if mine and mine[-1]["createdAt"] > last_push:
            return {**info, **plan("waiting", "maintainers",
                    "A test job failed for a reason outside this change. Waiting for a maintainer to re-run it")}
        return {**info, **plan("action", "you", "Some tests failed. Check the logs")}
    if maint_ok:
        return {**info, **plan("waiting", "maintainers", f"Approved by {', '.join(maint_ok)}. Waiting to be merged")}
    if checks == "not started":
        return {**info, **plan("waiting", "maintainers", "Waiting for a maintainer to start the tests and review")}
    if checks == "running":
        return {**info, **plan("waiting", "nobody", "Tests are running")}
    extra = f". {', '.join(community_ok)} approved it as a community reviewer" if community_ok else ""
    return {**info, **plan("waiting", "maintainers", f"Tests pass. Waiting for a maintainer to review{extra}")}


def track_report(issue, status):
    base = {"stage": 1, "checks": None, "review": None, "approved_by": [], "last_push": None,
            "updated_at": issue["updatedAt"], "conflicts": False, "last_comment": None}
    if status == "scout":
        return {**base, "stage": 5, **plan("done", "nobody", "Another contributor's fix was merged")}
    if status == "closed":
        return {**base, **plan("done", "nobody", "Closed")}
    return {**base, **plan("waiting", "maintainers", "Reported with a reproduction. Waiting for maintainers")}


def main():
    prs = [n for n in search(f"author:{LOGIN} is:pr is:public -user:{LOGIN}") if n]
    issues = [n for n in search(f"author:{LOGIN} is:issue is:public -user:{LOGIN}") if n]
    repos = {n["repository"]["nameWithOwner"]: repo_info(n["repository"]) for n in prs + issues}
    my_issues = {(i["repository"]["nameWithOwner"], i["number"]): i for i in issues}

    items, linked = [], set()
    for pr in prs:
        repo = pr["repository"]["nameWithOwner"]
        refs = pr["closingIssuesReferences"]["nodes"]
        issue_num = refs[0]["number"] if refs else next((int(m) for m in CLOSES.findall(pr["body"] or "")), None)
        issue = my_issues.get((repo, issue_num)) if issue_num else None
        ref = next((r for r in refs if r["number"] == issue_num), None)
        if issue_num:
            linked.add((repo, issue_num))
        issue_open = (issue or ref or {}).get("state") == "OPEN"
        if pr["merged"]:
            status = "merged"
        elif pr["state"] == "OPEN":
            status = "review"
        elif issue_open and any(TRIAGE_HINT.search(c["body"] or "") for c in pr["comments"]["nodes"]):
            status = "triage"
        else:
            status = "closed"
        title_src = (issue or ref or {}).get("title") or pr["title"]
        items.append({
            "kind": "fix", "repo": repo, "status": status, "title": clean_title(title_src),
            "date": pr["createdAt"][:10], "created_at": pr["createdAt"], "merged_at": (pr["mergedAt"] or "")[:10] or None,
            "reported": issue_num is None or bool(issue) or ((ref or {}).get("author") or {}).get("login") == LOGIN,
            "pr": {"number": pr["number"], "url": pr["url"]},
            "issue": {"number": issue_num, "url": f"https://github.com/{repo}/issues/{issue_num}"} if issue_num else None,
            "proof": OVERRIDES.get("proofs", {}).get(pr["url"]),
            "files": fix_diffs(repo, pr["number"]),
            "track": track_pr(pr, status),
        })

    for (repo, num), issue in my_issues.items():
        if (repo, num) in linked:
            continue
        closer = next(iter(issue["timelineItems"]["nodes"]), {}).get("closer") or {}
        fixed_by_other = issue["state"] == "CLOSED" and closer.get("__typename") == "PullRequest" and closer.get("merged")
        status = "scout" if fixed_by_other else ("open" if issue["state"] == "OPEN" else "closed")
        items.append({
            "kind": "report", "repo": repo, "status": status, "title": clean_title(issue["title"]),
            "date": issue["createdAt"][:10], "created_at": issue["createdAt"], "merged_at": None, "reported": True,
            "pr": {"number": closer["number"], "url": closer["url"]} if fixed_by_other else None,
            "issue": {"number": num, "url": issue["url"]}, "proof": None, "files": [],
            "track": track_report(issue, status),
        })

    items.sort(key=lambda x: x["created_at"], reverse=True)
    for i, item in enumerate(reversed(items), 1):
        item["id"] = i
    data = {
        "login": LOGIN,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stages": STAGES,
        "repos": sorted(repos.values(), key=lambda r: -sum(1 for x in items if x["repo"] == r["full"])),
        "items": items,
    }
    new = json.dumps(data, ensure_ascii=False, indent=1)
    old = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
    strip = lambda s: re.sub(r'"generated_at": "[^"]*"', "", s)
    if strip(old) == strip(new):
        print("no changes")
        return
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(new, encoding="utf-8")
    print(f"wrote {len(items)} items across {len(repos)} repos")


if __name__ == "__main__":
    main()
