#!/usr/bin/env python3
"""Claude Code hook: put matching claude-manager records in front of the model.

Registered for two events in ~/.claude/settings.json:

  SessionStart      -> one line: how many open ideas there are and how to name
                       one. It used to inject the whole open-idea index (~100
                       lines, re-sent after every /clear and compaction); the
                       per-prompt pass below already covers what that was for.
  UserPromptSubmit  -> injects the line of every `#id` the prompt names (open or
                       not), a bare project name's open ideas, and -- on the
                       first prompt of a session only -- the best fuzzy title
                       matches. An id already injected in this session is not
                       injected again. The model is told to get_idea the record
                       it needs, so full text is only fetched for that idea.

Per-session state (prompt count, injected ids) lives in STATE_DIR, keyed by the
harness's session_id; SessionStart clears the injected ids, since after a
compaction the earlier injections are no longer in context.

The harness runs this, not the model, so the lookup cannot be skipped. Any
failure (claude-manager down, bad JSON) degrades to a one-line note and exit 0
so a prompt is never blocked.
"""

import difflib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

# CM_API is the instance's ideas endpoint; set it in ~/.claude/settings.json "env".
API_BASE = os.environ.get("CM_API", "http://localhost:8000/api/ideas/")
# full=0: compact rows, not full records (~800 KB of plans and reports per prompt).
# An older server ignores the parameter and sends full records, which short()
# still reads, so the hook works on either side of a redeploy.
API = API_BASE + "?open=1&full=0"
STATE_DIR = Path(os.environ.get("CM_LOOKUP_STATE", Path.home() / ".cache" / "cm-lookup"))
STATE_MAX_AGE_SECONDS = 7 * 24 * 3600
TIMEOUT = 3
MAX_MATCHES = 5
FUZZY_THRESHOLD = 0.8

STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "into", "you", "your",
    "please", "can", "could", "should", "would", "let", "lets", "want", "need",
    "make", "add", "now", "then", "also", "just", "some", "all", "any", "one",
    "der", "die", "das", "und", "ein", "eine", "mit", "für", "von", "bitte",
    "ich", "wir", "mal", "noch", "auch", "bei", "auf", "zum", "zur",
}


def fetch_open_ideas():
    with urllib.request.urlopen(API, timeout=TIMEOUT) as resp:
        return json.load(resp)


def fetch_idea(idea_id):
    with urllib.request.urlopen(f"{API_BASE}{idea_id}/", timeout=TIMEOUT) as resp:
        return json.load(resp)


def words(text):
    return [w for w in re.split(r"[^\w]+", text.lower()) if w]


def stem(w):
    """Narrow English-plural stemmer -- just enough for the words seen in idea
    titles, applied identically to prompt tokens and haystack words. See the
    plan on claude-manager idea #104 for why each rule is shaped this way."""
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if (w.endswith("xes") or w.endswith("ches") or w.endswith("shes")) and len(w) > 4:
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def haystack(idea):
    parts = [idea.get("project_folder", ""), idea.get("project_name", ""), idea.get("title", "")]
    hay = set(words(" ".join(parts)))
    return hay, {stem(h) for h in hay}


def token_hit(tok, hay, hay_stems):
    """Graded per-token match against a haystack, tiered by token length so
    short tokens can't win on fuzzy grounds alone. A fuzzy hit is always < 1.0
    -- callers must keep that invariant, since score()'s threshold is always
    >= 1 and relies on fuzzy credit only topping up exact/stem hits, never
    satisfying a threshold by itself."""
    if len(tok) <= 2:
        return 1.0 if tok in hay else 0.0
    if len(tok) == 3:
        return 1.0 if stem(tok) in hay_stems else 0.0
    if stem(tok) in hay_stems:
        return 1.0
    best = max((difflib.SequenceMatcher(None, tok, h).ratio() for h in hay), default=0.0)
    return best if best >= FUZZY_THRESHOLD else 0.0


def score(tokens, idea):
    hay, hay_stems = haystack(idea)
    return sum(token_hit(tok, hay, hay_stems) for tok in tokens)


def has_plan(idea):
    if "has_plan" in idea:
        return idea["has_plan"]
    return bool((idea.get("plan") or "").strip())


def short(idea, with_goal=False):
    plan = ", has plan" if has_plan(idea) else ""
    line = f"- #{idea['id']} [{idea['project_folder']}] {idea['title']} ({idea['status']}{plan})"
    goal = " ".join((idea.get("goal") or "").split())
    if with_goal and goal:
        line += " — " + (goal if len(goal) <= 120 else goal[:117] + "...")
    return line


def emit(event, text):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))


def load_state(session_id):
    if not session_id:
        return {"prompts": 0, "injected": []}
    try:
        return json.loads((STATE_DIR / f"{session_id}.json").read_text())
    except Exception:
        return {"prompts": 0, "injected": []}


def save_state(session_id, state):
    if not session_id:
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / f"{session_id}.json").write_text(json.dumps(state))
    except Exception:
        pass


def prune_state():
    cutoff = time.time() - STATE_MAX_AGE_SECONDS
    try:
        for f in STATE_DIR.glob("*.json"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
    except Exception:
        pass


def session_start(ideas, session_id=None):
    # A compaction keeps the session id but drops what was injected earlier, so
    # the dedupe set starts over; the prompt count does not (the conversation
    # has not restarted, so fuzzy matching stays off).
    state = load_state(session_id)
    state["injected"] = []
    save_state(session_id, state)
    prune_state()
    projects = {i["project_folder"] for i in ideas}
    emit(
        "SessionStart",
        f"claude-manager: {len(ideas)} open ideas in {len(projects)} projects. A `#id` in a message "
        "injects that idea's line (then get_idea it); a bare project name lists its open ideas.",
    )


def prompt_submit(ideas, prompt, session_id=None):
    state = load_state(session_id)
    text = prompt_context(ideas, prompt, state, fetch_idea)
    save_state(session_id, state)
    if text:
        emit("UserPromptSubmit", text)


def prompt_context(ideas, prompt, state, fetch):
    """The text to inject for one prompt, or None. Updates `state` in place:
    counts the prompt and records every id it injects."""
    stripped = prompt.strip()
    if not stripped or stripped.startswith("/"):
        return None
    first = state.get("prompts", 0) == 0
    state["prompts"] = state.get("prompts", 0) + 1
    injected = set(state.get("injected", []))
    norm = re.sub(r"[\s_]+", "-", stripped.lower())
    folders = {i["project_folder"].lower(): i["project_folder"] for i in ideas}
    names = {i["project_name"].lower(): i["project_folder"] for i in ideas}
    # Initials of a hyphenated folder ("cm" -> claude-manager), only when unambiguous.
    acronyms = {}
    for folder in set(folders.values()):
        parts = folder.split("-")
        if len(parts) >= 2 and all(parts):
            acronyms.setdefault("".join(p[0] for p in parts).lower(), set()).add(folder)
    acronym = acronyms.get(norm)
    bare = folders.get(norm) or names.get(stripped.lower()) or (
        next(iter(acronym)) if acronym and len(acronym) == 1 else None
    )
    if bare:
        mine = [i for i in ideas if i["project_folder"] == bare]
        out = [
            f"claude-manager: `{bare}` has {len(mine)} open idea(s). Bare project name = pick one: "
            "present these with AskUserQuestion now (multi-select, `#id title` as label, goal as "
            "description, split across questions if more than 4); no get_idea until one is chosen; "
            "do not start implementing.",
        ]
        out.extend(short(i, with_goal=True) for i in mine)
        return "\n".join(out)

    ids = sorted({int(m) for m in re.findall(r"#(\d+)", stripped)})
    if ids:
        # An explicit #id is the reference; fuzzy title matches next to it are noise.
        by_id = {i["id"]: i for i in ideas}
        matches = []
        for iid in ids:
            if iid in injected:
                continue
            try:
                matches.append(by_id.get(iid) or fetch(iid))
            except Exception:
                pass
    elif first:
        matches = fuzzy_matches(ideas, stripped)
    else:
        return None
    matches = [i for i in matches if i["id"] not in injected]
    if not matches:
        return None
    state["injected"] = sorted(injected | {i["id"] for i in matches})
    header = (
        f"claude-manager: {len(matches)} idea(s) match this message. If one is what it means, "
        "call get_idea on it and work on that record; if none fits, say so."
    )
    return "\n".join([header] + [short(i) for i in matches])


def fuzzy_matches(ideas, text):
    tokens = [w for w in words(text) if w not in STOPWORDS and not w.isdigit()]
    scored = [(s, i) for i in ideas if (s := score(tokens, i))]
    if not scored:
        return []
    n = len(tokens)
    threshold = _prompt_threshold(tokens)
    scored.sort(key=lambda t: (-t[0], t[1]["id"]))
    best = scored[0][0]
    # A full match (every token hit) makes weaker partial matches noise; otherwise keep near-ties.
    floor = best if best >= n else best - 1
    return [i for s, i in scored if s >= threshold and s >= floor][:MAX_MATCHES]


def _prompt_threshold(tokens):
    n = len(tokens)
    return 1 if n <= 2 else min(n, 2 + n // 5)


def selftest():
    """Regression net for score()/stem()/token_hit(), run offline against
    fixed haystacks. See the verification section of claude-manager idea #104
    for what each case is checking."""
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    check(stem("cases") == stem("case"), "stem(cases) != stem(case)")
    check(stem("releases") == stem("release"), "stem(releases) != stem(release)")
    check(stem("boxes") == stem("box"), "stem(boxes) != stem(box)")
    check(stem("proxies") == stem("proxy"), "stem(proxies) != stem(proxy)")
    check(stem("not") != stem("notes"), "stem(not) == stem(notes)")

    idea17 = {"project_folder": "claude-manager", "project_name": "Claude Idea Manager", "title": "Per-project notes"}
    tokens1 = [w for w in words("Oh why did the Claude Manager hook not work?") if w not in STOPWORDS and not w.isdigit()]
    s1 = score(tokens1, idea17)
    check(s1 < _prompt_threshold(tokens1), f"repro1 (false positive): score {s1} still clears threshold")

    idea97 = {"project_folder": "MusicBox", "project_name": "MusicBox", "title": "List of proxies"}
    tokens2 = words("Proxy")
    s2 = score(tokens2, idea97)
    check(s2 >= _prompt_threshold(tokens2), f"repro2 (false negative): score {s2} below threshold")

    idea_logging = {"project_folder": "router", "project_name": "Router", "title": "Logging"}
    s3 = score(words("login"), idea_logging)
    check(s3 < 1, f"fuzzy invariant: login/logging scored {s3} (must stay < 1)")

    idea_hooks = {"project_folder": "x", "project_name": "x", "title": "hooks feature"}
    s4 = score(words("hook"), idea_hooks)
    check(s4 >= 1, f"stem match: hook/hooks scored {s4} (should be >= 1)")

    hay, hay_stems = haystack(idea17)
    hit_not = token_hit("not", hay, hay_stems)
    hit_claude = token_hit("claude", hay, hay_stems)
    check(hit_not == 0.0, f"not/notes should contribute 0, got {hit_not}")
    check(hit_claude == 1.0, f"claude/claude should contribute 1.0, got {hit_claude}")
    check(hit_not < hit_claude, "not/notes must score lower than an exact hit")

    # #244: when the per-prompt pass injects anything.
    def row(iid, title, folder="shopping"):
        return {"id": iid, "project_folder": folder, "project_name": folder, "title": title,
                "status": "new", "has_plan": False, "goal": ""}

    open_ideas = [row(241, "Case for the ESP32 Zigbee board"), row(244, "Reduce context", "claude-manager")]
    fetched = []

    def fake_fetch(iid):
        fetched.append(iid)
        return row(iid, "Closed idea")

    state = {"prompts": 0, "injected": []}
    out = prompt_context(open_ideas, "the esp32 zigbee case", state, fake_fetch)
    check(out and "#241" in out, f"first prompt: fuzzy match expected, got {out!r}")
    out = prompt_context(open_ideas, "now the zigbee case again", state, fake_fetch)
    check(out is None, f"later prompt: no fuzzy matching expected, got {out!r}")
    out = prompt_context(open_ideas, "#244 please", state, fake_fetch)
    check(out and "#244" in out and "#241" not in out, f"#id on a later prompt: expected #244 only, got {out!r}")
    out = prompt_context(open_ideas, "back to #244", state, fake_fetch)
    check(out is None, f"repeated #id: expected dedupe, got {out!r}")
    out = prompt_context(open_ideas, "what about #12", state, fake_fetch)
    check(out and "#12" in out and fetched == [12], f"closed #id: expected fetch by id, got {out!r} {fetched}")
    out = prompt_context(open_ideas, "#241 esp32 zigbee", {"prompts": 0, "injected": []}, fake_fetch)
    check(out and out.count("\n- #") == 1, f"#id suppresses fuzzy extras, got {out!r}")
    out = prompt_context(open_ideas, "shopping", {"prompts": 5, "injected": [241]}, fake_fetch)
    check(out and "#241" in out and "AskUserQuestion" in out, f"bare project name always lists, got {out!r}")
    check(has_plan({"plan": "x"}) and not has_plan({"has_plan": False, "plan": "x"}),
          "has_plan must prefer the row flag and fall back to a full record's plan")

    if failures:
        for f in failures:
            print(f"SELFTEST FAIL: {f}", file=sys.stderr)
        sys.exit(1)
    print("selftest OK")
    sys.exit(0)


def main():
    if "--selftest" in sys.argv[1:]:
        selftest()
        return
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    event = payload.get("hook_event_name") or ("UserPromptSubmit" if "prompt" in payload else "SessionStart")
    try:
        ideas = fetch_open_ideas()
    except Exception as exc:  # never block the prompt
        emit(event, f"claude-manager lookup hook: could not reach {API} ({exc.__class__.__name__}); "
                    "query the claude-manager MCP tools yourself before starting.")
        return
    session_id = payload.get("session_id")
    if event == "UserPromptSubmit":
        prompt_submit(ideas, payload.get("prompt", ""), session_id)
    else:
        session_start(ideas, session_id)


if __name__ == "__main__":
    main()
