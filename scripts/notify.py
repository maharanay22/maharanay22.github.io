"""Send an hourly bug-fix status push to the phone through ntfy.sh.

Reads data/bugs.json (rebuilt by update_bugs.py), compares it with the
snapshot from the previous run in data/notify_state.json, and posts one
notification with what changed and what is waiting on Maha.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUGS = ROOT / "data" / "bugs.json"
STATE = ROOT / "data" / "notify_state.json"
PAGE = "https://maharanay22.github.io/status.html"
MAX_LINES = 12


def short(item):
    repo = item["repo"].split("/")[-1]
    number = (item.get("pr") or item.get("issue") or {}).get("number")
    return f"{repo} #{number}"


def snapshot(item):
    track = item.get("track") or {}
    last = track.get("last_comment") or {}
    return {
        "status": item.get("status"),
        "checks": track.get("checks"),
        "approved_by": sorted(track.get("approved_by") or []),
        "conflicts": bool(track.get("conflicts")),
        "waiting_on": track.get("waiting_on"),
        "last_comment": f"{last.get('who')}@{last.get('at')}" if last else None,
        "last_comment_who": last.get("who"),
    }


def actionable(feedback):
    if not feedback.get("maintainer") or feedback.get("answered"):
        return False
    return bool(feedback.get("path")) or "changes" in (feedback.get("kind") or "")


def author_action(item):
    track = item.get("track") or {}
    return any(actionable(f) for f in track.get("feedback") or []) or bool(track.get("conflicts"))


def changes(item, old, new):
    name = short(item)
    if old is None:
        return [f"New: {name} {item['title'][:60]}"]
    lines = []
    if new["status"] != old["status"]:
        if new["status"] == "merged":
            lines.append(f"MERGED: {name} {item['title'][:60]}")
        else:
            lines.append(f"{name}: {old['status']} -> {new['status']}")
    for who in sorted(set(new["approved_by"]) - set(old["approved_by"])):
        lines.append(f"Approved: {name} by {who}")
    if new["last_comment"] != old["last_comment"] and new["last_comment_who"] not in (None, "maharanay22"):
        lines.append(f"Comment: {name} from {new['last_comment_who']}")
    if new["conflicts"] and not old["conflicts"]:
        lines.append(f"Conflict: {name} needs a rebase")
    if new["checks"] == "failing" and old["checks"] != "failing":
        lines.append(f"Checks failing: {name}")
    return lines


def send(topic, title, body, priority):
    request = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Priority": priority, "Tags": "bug", "Click": PAGE},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        sys.exit("NTFY_TOPIC is not set")
    bugs = json.loads(BUGS.read_text(encoding="utf-8"))
    previous = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    fixes = [i for i in bugs["items"] if i.get("pr")]
    current = {i["pr"]["url"]: snapshot(i) for i in fixes}

    lines = []
    if previous:
        for item in fixes:
            lines += changes(item, previous.get(item["pr"]["url"]), current[item["pr"]["url"]])

    merged = sum(1 for i in fixes if i.get("status") == "merged")
    open_prs = sum(1 for i in fixes if i.get("status") == "review")
    approved = sum(1 for i in fixes if i.get("status") == "review" and (i.get("track") or {}).get("approved_by"))
    needs_you = [i for i in fixes if i.get("status") == "review" and author_action(i)]

    body = []
    body += lines[:MAX_LINES] if lines else ["No changes in the last hour"]
    if len(lines) > MAX_LINES:
        body.append(f"... and {len(lines) - MAX_LINES} more")
    body.append(f"Approved, waiting to merge: {approved}")
    if needs_you:
        body.append("Waiting on you: " + ", ".join(short(i) for i in needs_you[:6]))

    title = f"Bug fixes: {merged} merged, {open_prs} open"
    priority = "high" if any(l.startswith(("MERGED", "Approved", "Comment", "Needs you")) for l in lines) else "default"
    if os.environ.get("NOTIFY_ONLY_ON_CHANGE") == "1" and not lines and previous:
        print("No changes, skipping notification")
    else:
        send(topic, title, "\n".join(body), priority)
        print(title)
        print("\n".join(body))
    STATE.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
