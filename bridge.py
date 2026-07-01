#!/usr/bin/env python3
"""
Claude Code -> companion bridge (Stop + PreToolUse hook).

ONE script, wired into two Claude Code hook slots:

  * Stop        at the end of every assistant turn, mirror the *conversational*
                lines of the reply (skipping code / paths / commands), and prepend
                any context event that just happened: a model switch, or an /effort
                or /goal change. The companion shows them one by one, then idles.

  * PreToolUse  the moment any tool starts, flash a live activity line (reading a
                file, running a command, searching, editing, a workflow...) so the
                screen narrates the work in parallel while the turn is still going.

So nothing is pushed by hand: the screen mirrors the chat and reacts to what you do.
Events are detected automatically from the session transcript (message.model for the
model, <command-name> markers for /effort and /goal) -- no extra config.

Dedup: conversational lines by source-message uuid; events by tiny state keys kept in
feed.json (last model / last command) so a change is announced once, not every turn.

push.py stays available for sending an explicit line off-cycle. Never throws into
the session: any error just exits 0 quietly.
"""
import sys, json, os, re, time, unicodedata

# feed.json lives next to this script; override with COMPANION_FEED if you serve it elsewhere
HERE = os.path.dirname(os.path.abspath(__file__))
FEED = os.environ.get("COMPANION_FEED", os.path.join(HERE, "feed.json"))
MAX_LINES = 4
MAX_EVENTS = 2

# what to say while a given tool runs (PreToolUse) -> (bubble line, dedup tag).
# the same tag repeated back-to-back is shown once, so a run of Reads doesn't spam.
TOOL_ACTIVITY = {
    "Read":         ("\U0001F4D6 reading a file…",    "evt:read"),
    "Grep":         ("\U0001F50D searching the code…", "evt:search"),
    "Glob":         ("\U0001F50D searching the code…", "evt:search"),
    "Bash":         ("⚙️ running a command…", "evt:bash"),
    "Edit":         ("✏️ editing a file…",   "evt:edit"),
    "Write":        ("✏️ writing a file…",   "evt:edit"),
    "NotebookEdit": ("✏️ editing a file…",   "evt:edit"),
    "WebFetch":     ("\U0001F310 checking the web…",   "evt:web"),
    "WebSearch":    ("\U0001F310 searching the web…",  "evt:web"),
    "Workflow":     ("\U0001F527 a workflow is running…", "evt:wf"),
    "Task":         ("\U0001F527 put an agent on it…", "evt:wf"),
    "Agent":        ("\U0001F527 put an agent on it…", "evt:wf"),
}
TOOL_DEFAULT = ("\U0001F527 working on something…", "evt:tool")

# lines that look like code / shell / paths / data, not something to "say"
CODEY = re.compile(
    r"^\s*(```|\$ |sudo |cd |npm |npx |python|node |git |curl |wget |rm |ls |cat |mkdir |"
    r"export |const |let |var |function |import |from |def |class |return |print\(|"
    r"\{|\}|\[|<|/|\./|#!/|http)"
)

# pretty names for the model-switch line
MODEL_NAMES = {
    "claude-opus-4-8": "Opus 4.8", "claude-opus-4-7": "Opus 4.7", "claude-opus-4-6": "Opus 4.6",
    "claude-sonnet-4-6": "Sonnet 4.6", "claude-haiku-4-5": "Haiku 4.5", "claude-fable-5": "Fable 5",
}


def pretty_model(m):
    if not m:
        return ""
    if m in MODEL_NAMES:
        return MODEL_NAMES[m]
    for k, v in MODEL_NAMES.items():            # date-suffixed ids: claude-haiku-4-5-2025...
        if m.startswith(k):
            return v
    s = re.sub(r"^claude-", "", m)              # generic fallback: claude-foo-1-2 -> "Foo 1.2"
    s = re.sub(r"-\d{6,}$", "", s)
    parts = [p for p in s.split("-") if p]
    if parts:
        return (parts[0].capitalize() + " " + ".".join(parts[1:])).strip()
    return m


def read_entries(path):
    """Parse a .jsonl transcript into a list of objects, tolerating a half-written line."""
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return out


def last_assistant(entries):
    """(text, uuid) of the last assistant entry that carries non-empty text."""
    for o in reversed(entries):
        if o.get("type") != "assistant":
            continue
        content = o.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        joined = "\n".join(p for p in parts if p).strip()
        if joined:
            return joined, o.get("uuid")
    return None, None


def latest_model(entries):
    """Most recent real model id used by the assistant (ignores '<synthetic>')."""
    for o in reversed(entries):
        if o.get("type") != "assistant":
            continue
        m = o.get("message", {}).get("model")
        if m and m != "<synthetic>":
            return m
    return ""


def latest_command(entries):
    """Newest /model, /effort or /goal slash command -> (cmd, args), else (None, '')."""
    for o in reversed(entries):
        if o.get("type") != "user":
            continue
        c = o.get("message", {}).get("content")
        s = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
        m = re.search(r"<command-name>\s*/?(\w+)", s)
        if not m:
            continue
        cmd = m.group(1).lower()
        if cmd not in ("model", "effort", "goal"):
            continue
        a = re.search(r"<command-args>(.*?)</command-args>", s, re.S)
        return cmd, (a.group(1).strip() if a else "")
    return None, ""


def detect_events(entries, prev_model, prev_cmd):
    """Context events for this turn -> (event_lines, new_model_state, new_cmd_state).

    prev_model / prev_cmd are None on a fresh install: we record the baseline then,
    but announce nothing, so installing the hook doesn't spit out a burst of events.
    """
    events = []
    cur_model = latest_model(entries)
    cmd, args = latest_command(entries)
    cmd_sig = (cmd + "|" + args) if cmd else ""

    announced_model = False
    if cmd and prev_cmd is not None and cmd_sig != prev_cmd:    # a *new* slash command
        if cmd == "model":
            mm = pretty_model(args.strip() or cur_model) or "a new model"
            events.append("\U0001F9E0 switched to " + mm)
            announced_model = True
        elif cmd == "effort":
            lvl = args.strip()
            events.append("⚡ effort set to " + lvl if lvl else "⚡ changed the effort")
        elif cmd == "goal":
            g = re.sub(r"\s+", " ", args.strip())
            if len(g) > 64:
                g = g[:61].rstrip() + "…"
            events.append("\U0001F3AF new goal: " + g if g else "\U0001F3AF set a new goal")

    if not announced_model and cur_model and prev_model is not None and cur_model != prev_model:
        events.append("\U0001F9E0 now running on " + (pretty_model(cur_model) or cur_model))

    new_model = cur_model if cur_model else (prev_model if prev_model is not None else "")
    new_cmd = cmd_sig if cmd else (prev_cmd if prev_cmd is not None else "")
    return events[:MAX_EVENTS], new_model, new_cmd


def looks_codey(s):
    if CODEY.search(s):
        return True
    if " " not in s and ("/" in s or "_" in s or "(" in s):   # bare path/identifier
        return True
    # count letters + Devanagari matras (Mn/Mc marks) so Hindi isn't mis-flagged as codey
    letters = sum(1 for ch in s if ch.isalpha() or unicodedata.category(ch) in ("Mn", "Mc"))
    return letters < max(4, len(s) * 0.4)


def clean(s):
    s = re.sub(r"^[#>\-\*\d\.\)\s]+", "", s)          # list / heading markers
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)              # bold
    s = re.sub(r"`([^`]+)`", r"\1", s)                  # inline code ticks
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)      # links -> text
    return s.strip()


def select_lines(text, maxn=MAX_LINES):
    """Full reply -> a few short, sentence-like partner lines (skip code/paths)."""
    out = []
    in_code = False
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("```"):          # toggle fenced code block, skip its lines entirely
            in_code = not in_code
            continue
        if not s or in_code:
            continue
        s = clean(s)
        if len(s) < 6 or " " not in s or looks_codey(s):
            continue
        # break a multi-sentence line into separate bubble lines
        for sent in re.split(r"(?<=[.!?।])\s+", s):
            sent = sent.strip()
            if len(sent) < 6 or " " not in sent or looks_codey(sent):
                continue
            if len(sent) > 120:
                sent = sent[:117].rstrip() + "..."
            if out and sent == out[-1]:
                continue
            out.append(sent)
            if len(out) >= maxn:
                return out
    return out


def write_feed(d_id, lines, src, model, cmd):
    try:
        with open(FEED, "w", encoding="utf-8") as f:
            json.dump({"id": d_id, "lines": lines, "src": src, "model": model, "cmd": cmd},
                      f, ensure_ascii=False)
    except Exception:
        pass


def main():
    # Claude Code passes the hook payload (incl. hook_event_name / transcript_path) on stdin
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    event = payload.get("hook_event_name") or ""
    transcript = payload.get("transcript_path")

    prev_id, prev_src, prev_model, prev_cmd = -1, "", None, None
    try:
        with open(FEED, "r", encoding="utf-8") as f:
            d = json.load(f)
        prev_id = d.get("id", -1)
        prev_src = d.get("src", "")
        prev_model = d.get("model")          # None when absent (fresh install)
        prev_cmd = d.get("cmd")
    except Exception:
        pass

    # --- PreToolUse: a tool is about to run -> flash a live activity line ---
    if event == "PreToolUse":
        tool = payload.get("tool_name", "")
        line, tag = TOOL_ACTIVITY.get(tool, TOOL_DEFAULT)
        if prev_src != tag:            # debounce a run of the same activity
            write_feed(prev_id + 1, [line], tag,
                       prev_model if prev_model is not None else "",
                       prev_cmd if prev_cmd is not None else "")
        return

    # --- Stop: mirror the reply + any context event ---
    if not transcript:
        return
    # The Stop hook can fire before this turn's message is flushed. Wait only while the
    # newest assistant uuid is still the one we already processed; decide once otherwise.
    for _ in range(8):
        entries = read_entries(transcript)
        text, uuid = last_assistant(entries)
        if text is None or (uuid and uuid == prev_src):
            time.sleep(0.25)
            continue
        events, new_model, new_cmd = detect_events(entries, prev_model, prev_cmd)
        lines = []
        for ln in events + select_lines(text):       # events first, then the reply
            if ln and (not lines or ln != lines[-1]):
                lines.append(ln)
        lines = lines[:MAX_LINES]
        if lines:
            write_feed(prev_id + 1, lines, uuid, new_model, new_cmd)
        return


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
