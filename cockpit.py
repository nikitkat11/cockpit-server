#!/usr/bin/env python3
"""
Cockpit — a single-file local control center for a folder of markdown notes.

Reads an Obsidian-style vault (frontmatter, headings, checklists, wikilinks),
builds a live dashboard from it, and serves it over HTTP on localhost.

    python3 cockpit.py                 # serves the folder it lives in
    python3 cockpit.py --vault ~/notes # or point it somewhere else
    open http://127.0.0.1:8090

473 lines of Python + a 1,341-line embedded single-page UI. Stdlib only —
no framework, no build step, no node_modules. Binds 127.0.0.1 and is never
exposed to the network.
"""
import json
import os
import re
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = 8090
VAULT = os.path.dirname(os.path.abspath(__file__))
if len(sys.argv) > 2 and sys.argv[1] == "--vault":
    VAULT = os.path.abspath(sys.argv[2])

PROXY_URL = "http://127.0.0.1:8082"

# ---------------------------------------------------------------- parsing

def read_note(relpath):
    path = os.path.join(VAULT, relpath)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def frontmatter(text):
    fm = {}
    if text and text.startswith("---"):
        m = re.match(r"^---\n(.*?)\n---\n?", text, re.S)
        if m:
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
    return fm


def strip_fm(text):
    return re.sub(r"^---\n.*?\n---\n?", "", text, flags=re.S) if text else ""


def md_files(folder):
    d = os.path.join(VAULT, folder)
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.endswith(".md"))


def section(text, heading_re):
    """Return lines of the section whose heading matches, up to next heading of same/higher level."""
    lines = text.splitlines()
    out, level, active = [], None, False
    for ln in lines:
        h = re.match(r"^(#{1,6})\s+(.*)", ln)
        if h:
            if active and len(h.group(1)) <= level:
                break
            if re.search(heading_re, h.group(2)):
                active, level = True, len(h.group(1))
                continue
            if active:
                out.append(ln)  # deeper subheading inside the section
        elif active:
            out.append(ln)
    return out


def checklist(lines):
    items = []
    for ln in lines:
        m = re.match(r"^\s*-\s*\[( |x)\]\s*(.*)", ln)
        if m:
            items.append({"done": m.group(1) == "x", "text": m.group(2).strip()})
    return items


def parse_projects():
    projects = []
    for fname in md_files("05 Projects"):
        text = read_note(os.path.join("05 Projects", fname))
        fm = frontmatter(text)
        if fm.get("type") != "project":
            continue
        body = strip_fm(text)
        status_line = next((l for l in body.splitlines() if l.startswith("Status:")), "")
        goal = deadline = ""
        m = re.search(r"Goal:\s*([^|]+)", status_line)
        if m:
            goal = m.group(1).strip()
        m = re.search(r"(?:Deadline|Budget):\s*(.+)$", status_line)
        if m:
            deadline = m.group(1).strip()
        top = ("#1 PRIORITY" in body) or ("USER'S #1" in body)
        log = checklist_last_log(body)
        projects.append({
            "name": fname[:-3],
            "status": fm.get("status", "?"),
            "goal": goal,
            "deadline": deadline,
            "top": top,
            "lastLog": log,
        })
    projects.sort(key=lambda p: (not p["top"], p["name"]))
    return projects


def checklist_last_log(body):
    lines = section(body, r"^Log$")
    entries = [l.lstrip("- ").strip() for l in lines if l.strip().startswith("-")]
    return entries[-1] if entries else ""


def parse_agents():
    agents = []
    for fname in md_files("01 Agents"):
        text = read_note(os.path.join("01 Agents", fname))
        body = strip_fm(text)
        m = re.search(r"\*\*Role:\*\*\s*(.+)", body)
        role = m.group(1).strip() if m else ""
        m = re.search(r"\*\*Toolkit:\*\*\s*(.+)", body)
        toolkit = m.group(1).strip() if m else ""
        m = re.search(r"\*\*Area:\*\*\s*(.+)", body)
        area = m.group(1).strip() if m else ""
        rep = re.search(r"Reports to \[\[(.+?)\]\]", role)
        agents.append({
            "name": fname[:-3],
            "role": role,
            "toolkit": toolkit,
            "area": area,
            "reportsTo": rep.group(1) if rep else "",
        })
    return agents


def parse_handoff():
    text = read_note("00 System/Handoff.md") or ""
    body = strip_fm(text)
    nxt = [i for i in checklist(section(body, r"Next up")) if not i["done"]]
    done = [i for i in checklist(section(body, r"Done")) if i["done"]]
    log_lines = section(body, r"^Session log$")
    headings = [re.sub(r"^#+\s*", "", l) for l in log_lines if l.startswith("###")]
    latest = headings[-1] if headings else ""
    return {"next": nxt, "doneCount": len(done), "latestSession": latest}


def parse_memory():
    text = read_note("00 System/Memory.md") or ""
    body = strip_fm(text)
    qs = [l.lstrip("- ").strip() for l in section(body, r"Open questions") if l.strip().startswith("-")]
    state = [l.lstrip("- ").strip() for l in section(body, r"Current state") if l.strip().startswith("-")]
    return {"openQuestions": qs, "currentState": state}


def parse_decisions(n=8):
    text = read_note("00 System/Decisions.md") or ""
    body = strip_fm(text)
    entries = [l.lstrip("- ").strip() for l in section(body, r"^Log$") if l.strip().startswith("-")]
    return entries[-n:][::-1]


def parse_inbox():
    return [f for f in md_files("04 Inbox") if f != "README.md"]


def vault_payload():
    return {
        "vault": VAULT,
        "projects": parse_projects(),
        "agents": parse_agents(),
        "skills": [f[:-3] for f in md_files("02 Skills")],
        "workflows": [f[:-3] for f in md_files("03 Workflows")],
        "handoff": parse_handoff(),
        "memory": parse_memory(),
        "decisions": parse_decisions(),
        "inbox": parse_inbox(),
    }


QUEUE_PATH = "00 System/Operator Queue.md"
QUEUE_ITEM = re.compile(r"^- \[( |x)\] \((.+?)\) (\d{4}-\d{2}-\d{2}) — (.+)$")


def parse_queue():
    """Operator Queue items + derived blockers (Handoff/Memory) the agents are waiting on."""
    import datetime
    text = read_note(QUEUE_PATH) or ""
    items, answers = [], {}
    lines = text.splitlines()
    for idx, ln in enumerate(lines):
        m = QUEUE_ITEM.match(ln)
        if m:
            parts = m.group(4).split(" — ", 1)
            ans = ""
            if idx + 1 < len(lines):
                am = re.match(r"^\s+- A (\d{4}-\d{2}-\d{2}): (.+)$", lines[idx + 1])
                if am:
                    ans = am.group(2)
            items.append({
                "done": m.group(1) == "x", "agent": m.group(2), "date": m.group(3),
                "q": parts[0].strip(), "ctx": parts[1].strip() if len(parts) > 1 else "",
                "line": ln, "answer": ans,
            })
    derived = []
    today = datetime.date.today().isoformat()
    for i in parse_handoff()["next"]:
        if re.search(r"blocked on (the )?user|on user\b", i["text"], re.I):
            derived.append({"agent": "Handoff roadmap", "date": today, "q": i["text"], "ctx": ""})
    for q in parse_memory()["openQuestions"]:
        if not q.startswith("✅"):
            derived.append({"agent": "Memory — open question", "date": today, "q": q, "ctx": ""})
    return {"items": items, "derived": derived,
            "open": sum(1 for i in items if not i["done"]) + len(derived)}


def answer_queue(payload):
    """Write an answer back into the Operator Queue note (the only file the cockpit writes)."""
    import datetime
    q = str(payload.get("q", "")).strip()
    answer = str(payload.get("answer", "")).strip()
    line = payload.get("line", "")
    agent = str(payload.get("agent", "Operator"))[:60]
    if not answer or not q:
        return {"ok": False, "error": "empty answer"}
    today = datetime.date.today().isoformat()
    path = os.path.join(VAULT, QUEUE_PATH)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = "---\ntype: system\nstatus: active\ncreated: " + today + "\n---\n# Operator Queue\n\n## Queue\n"
    entry_answer = "\n  - A " + today + ": " + answer.replace("\n", " ")
    if line and line in text:
        new_line = line.replace("- [ ]", "- [x]", 1) + entry_answer
        text = text.replace(line, new_line, 1)
    else:  # derived / ad-hoc: append as an answered record
        record = "- [x] (" + agent + ") " + today + " — " + q.replace("\n", " ") + entry_answer
        m = re.search(r"^## Queue\s*$", text, re.M)
        if m:
            text = text[:m.end()] + "\n" + record + text[m.end():]
        else:
            text = text.rstrip() + "\n" + record + "\n"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


def parse_graph():
    """All vault notes as nodes, wikilinks as edges."""
    nodes, index = [], {}
    for root, dirs, files in os.walk(VAULT):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in sorted(files):
            if not fn.endswith(".md"):
                continue
            rel = os.path.relpath(os.path.join(root, fn), VAULT)
            folder = rel.split(os.sep)[0] if os.sep in rel else "root"
            name = fn[:-3]
            if name not in index:
                index[name] = len(nodes)
                nodes.append({"name": name, "folder": folder, "rel": rel})
    edges = set()
    for i, n in enumerate(nodes):
        text = read_note(n["rel"]) or ""
        for link in re.findall(r"\[\[([^\]|#]+)", text):
            j = index.get(link.strip())
            if j is not None and j != i:
                edges.add((min(i, j), max(i, j)))
    for n in nodes:
        n.pop("rel")
    return {"nodes": nodes, "edges": sorted(edges)}


def proxy_token():
    """Read ANTHROPIC_AUTH_TOKEN from the free-claude-code .env files."""
    candidates = [
        os.path.expanduser(os.environ.get("COCKPIT_PROXY_ENV", "~/.config/cockpit/.env")),
        os.path.expanduser("~/.fcc/.env"),
    ]
    for p in candidates:
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    m = re.match(r'\s*ANTHROPIC_AUTH_TOKEN\s*=\s*"?([^"\n]+)"?', line)
                    if m and m.group(1).strip():
                        return m.group(1).strip()
        except OSError:
            continue
    return ""


LANE_MODELS = {
    "opus": "claude-opus-4-20250514",
    "sonnet": "claude-sonnet-4-20250514",
    "haiku": "claude-haiku-4-20250514",
}


def system_context():
    """Fresh vault snapshot injected into every Uplink chat — the model always knows the OS."""
    import datetime
    mem = strip_fm(read_note("00 System/Memory.md") or "")[:4500]
    handoff = parse_handoff()
    nxt = "\n".join("- " + i["text"] for i in handoff["next"][:12])
    dec = "\n".join("- " + d for d in parse_decisions(8))
    projs = "\n".join(f"- {p['name']}: {p['goal']}" for p in parse_projects())
    return (
        "You are the Uplink channel of this vault — a personal knowledge and automation system "
        "running as a markdown vault (Obsidian). You are already booted: the live "
        "vault snapshot is below. NEVER ask the user to paste Kernel/Memory/files — you have them. "
        "Act as the Chief of Staff: concise, direct, no fluff, no emoji walls. "
        "You CANNOT read or write files or execute workflows from this chat; when a task needs vault writes "
        "or skills, say it belongs in a Claude Code session (free lane `os-free` / heavy lane `os-pro`) and give "
        "the exact instruction to paste there. Today: " + datetime.date.today().isoformat() + "\n\n"
        "[MEMORY]\n" + mem + "\n\n[ROADMAP — NEXT UP]\n" + nxt +
        "\n\n[RECENT DECISIONS]\n" + dec + "\n\n[ACTIVE PROJECTS]\n" + projs +
        "\n\n[OPERATOR QUEUE — open questions agents are waiting on]\n" +
        ("\n".join("- (" + i["agent"] + ") " + i["q"]
                   for i in parse_queue()["items"] if not i["done"]) or "- none") +
        "\n\n[LATEST SESSION] " + handoff["latestSession"]
    )


def chat(payload):
    """Relay a chat turn to the free-lane proxy (Anthropic /v1/messages format)."""
    lane = payload.get("lane", "sonnet")
    messages = payload.get("messages", [])
    dept = str(payload.get("dept", ""))[:60]
    system = system_context()
    if dept:
        system += (
            "\n\n[DEPARTMENT CONTEXT] For this conversation act as the " + dept +
            " of this vault: stay in that department's lane, use its Toolkit and rules "
            "from `01 Agents/" + dept + ".md`, and apply its output standards."
        )
    body = json.dumps({
        "model": LANE_MODELS.get(lane, LANE_MODELS["sonnet"]),
        "max_tokens": 2048,
        "stream": False,
        "system": system,
        "messages": messages,
    }).encode()
    req = urllib.request.Request(
        PROXY_URL + "/v1/messages",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": proxy_token() or "unset",
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode(errors="replace")
        try:
            data = json.loads(raw)
            text = "".join(b.get("text", "") for b in data.get("content", []))
        except json.JSONDecodeError:
            # SSE stream fallback: accumulate text deltas
            parts = []
            for line in raw.splitlines():
                if line.startswith("data:"):
                    try:
                        ev = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    d = ev.get("delta", {})
                    if d.get("type") == "text_delta":
                        parts.append(d.get("text", ""))
                    for b in ev.get("content", []) if ev.get("type") == "message" else []:
                        parts.append(b.get("text", ""))
            text = "".join(parts)
        if not text:
            return {"ok": False, "error": "empty reply from proxy (check its terminal for errors)"}
        return {"ok": True, "text": text}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        return {"ok": False, "error": f"proxy {e.code}: {detail}"}
    except Exception as e:
        return {"ok": False, "error": f"proxy unreachable ({e}) — is fcc-server running?"}


def proxy_status():
    try:
        req = urllib.request.Request(
            PROXY_URL + "/health", method="GET",
            headers={"x-api-key": proxy_token() or "unset"},
        )
        with urllib.request.urlopen(req, timeout=1.5) as r:
            return {"up": True, "code": r.status}
    except urllib.error.HTTPError as e:
        return {"up": True, "code": e.code}  # server responded at all = running
    except Exception:
        return {"up": False}


# ---------------------------------------------------------------- UI

HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AGENTIC//OS</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"></script>
<style>
:root{
  --p:255,59,77;--s:255,158,44;
  --bg:#080405;
  --cy:rgb(var(--p));--mg:rgb(var(--s));--gn:#39ff88;--am:#ffc24d;--rd:#ff4d5e;
  --cy-dim:rgba(var(--p),.5);--line:rgba(var(--p),.18);--line2:rgba(var(--p),.38);
  --panel:rgba(18,6,9,.84);--card:rgba(var(--p),.05);
  --txt:#f2d9dc;--dim:#8a5d64;
  --mono:"SF Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
body[data-theme="ember"]{--p:255,176,46;--s:255,94,58;--bg:#0a0703;--panel:rgba(20,13,4,.84);--txt:#f5e7cf;--dim:#8a7a5d}
body[data-theme="cyan"]{--p:0,240,255;--s:255,59,77;--bg:#040608;--panel:rgba(6,12,18,.84);--txt:#cfe9f2;--dim:#5d7a8a}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  background:
    radial-gradient(900px 500px at 85% -5%, rgba(var(--s),.07), transparent 55%),
    radial-gradient(800px 600px at -5% 105%, rgba(var(--p),.07), transparent 55%),
    linear-gradient(rgba(var(--p),.030) 1px, transparent 1px),
    linear-gradient(90deg, rgba(var(--p),.030) 1px, transparent 1px),
    var(--bg);
  background-size:auto,auto,44px 44px,44px 44px,auto;
  color:var(--txt);font:13.5px/1.55 var(--mono);
  display:flex;overflow:hidden;
}
body::after{ /* scanlines */
  content:"";position:fixed;inset:0;pointer-events:none;z-index:99;
  background:repeating-linear-gradient(0deg, rgba(255,255,255,.018) 0 1px, transparent 1px 3px);
}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-thumb{background:rgba(var(--p),.25);border-radius:0}
::-webkit-scrollbar-track{background:transparent}
::selection{background:rgba(var(--p),.3)}

/* ------- HUD corner brackets ------- */
.hud{position:relative;background:var(--panel);border:1px solid var(--line)}
.hud::before,.hud::after{content:"";position:absolute;width:14px;height:14px;pointer-events:none}
.hud::before{top:-1px;left:-1px;border-top:2px solid var(--cy);border-left:2px solid var(--cy)}
.hud::after{bottom:-1px;right:-1px;border-bottom:2px solid var(--cy);border-right:2px solid var(--cy)}

/* ------- sidebar ------- */
aside{
  width:200px;min-width:200px;height:100vh;display:flex;flex-direction:column;
  background:rgba(3,6,10,.92);border-right:1px solid var(--line);padding:16px 10px;
}
.brand{padding:2px 8px 16px;user-select:none}
.brand .t{
  font:700 16px var(--mono);letter-spacing:.08em;color:#fff;
  text-shadow:0 0 8px var(--cy),0 0 22px rgba(var(--p),.5);
  animation:flicker 6s infinite;
}
.brand .t em{color:var(--cy);font-style:normal}
.brand .sub{font-size:9.5px;color:var(--dim);letter-spacing:.28em;text-transform:uppercase}
@keyframes flicker{0%,93%,100%{opacity:1}94%{opacity:.6}95%{opacity:1}97%{opacity:.75}98%{opacity:1}}
nav{display:flex;flex-direction:column;gap:3px}
.nav-item{
  display:flex;align-items:center;gap:9px;padding:9px 10px;cursor:pointer;user-select:none;
  color:var(--dim);font-size:11.5px;letter-spacing:.14em;text-transform:uppercase;
  border-left:2px solid transparent;transition:all .15s;
}
.nav-item .idx{font-size:9.5px;opacity:.6}
.nav-item:hover{color:var(--txt);background:rgba(var(--p),.05);padding-left:14px}
.nav-item.on{
  color:var(--cy);border-left-color:var(--cy);background:linear-gradient(90deg,rgba(var(--p),.12),transparent);
  text-shadow:0 0 8px rgba(var(--p),.6);
}
.nav-item .n{margin-left:auto;font-size:9.5px;color:var(--dim);border:1px solid var(--line);padding:0 6px;border-radius:2px}
.side-foot{margin-top:auto;border-top:1px solid var(--line);padding-top:10px}
.lane-mini{display:flex;align-items:center;gap:8px;padding:5px 8px;font-size:10.5px;color:var(--dim);letter-spacing:.06em}
.lane-mini b{color:var(--txt);font-weight:600;display:block;font-size:10.5px;letter-spacing:.12em}
.dot{width:7px;height:7px;flex:none;transform:rotate(45deg)}
.dot.ok{background:var(--gn);box-shadow:0 0 10px var(--gn);animation:pulse 2s infinite}
.dot.bad{background:var(--rd);box-shadow:0 0 10px var(--rd)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}

/* ------- main ------- */
main{flex:1;height:100vh;display:flex;flex-direction:column;overflow:hidden}
.topbar{
  display:flex;align-items:center;gap:12px;padding:12px 24px;
  border-bottom:1px solid var(--line);background:rgba(3,6,10,.7);
}
.topbar h1{
  font-size:13px;font-weight:700;letter-spacing:.3em;text-transform:uppercase;color:var(--cy);
  text-shadow:0 0 10px rgba(var(--p),.5);
}
.topbar h1::before{content:"// ";color:var(--dim);text-shadow:none}
.chip{font-size:10px;color:var(--dim);border:1px solid var(--line);padding:3px 9px;letter-spacing:.08em;transition:all .15s}
.chip:hover{color:var(--txt);border-color:var(--line2)}
.chip.hot{color:var(--gn);border-color:rgba(57,255,136,.3)}
.content{flex:1;overflow-y:auto;padding:20px 24px}
.tab{display:none;animation:boot .3s ease}
.tab.on{display:block}
#tab-chat.on{display:flex;flex-direction:column;height:100%}
@keyframes boot{from{opacity:0;transform:translateY(6px);clip-path:inset(0 0 40% 0)}to{opacity:1;transform:none;clip-path:inset(0)}}

/* ------- stats ------- */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.stat{padding:13px 16px}
.stat .v{font:700 26px var(--mono);color:var(--cy);text-shadow:0 0 12px rgba(var(--p),.6)}
.stat .k{font-size:9.5px;color:var(--dim);letter-spacing:.22em;text-transform:uppercase;margin-top:3px}
.bar{height:5px;background:rgba(var(--p),.1);margin-top:9px;overflow:hidden}
.bar i{
  display:block;height:100%;background:linear-gradient(90deg,var(--cy),var(--mg));
  box-shadow:0 0 10px rgba(var(--p),.6);transition:width .8s ease;
}

/* ------- panels/cards ------- */
.cols{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(330px,1fr))}
.panel{padding:15px 17px;margin-bottom:14px}
.panel h2{
  font-size:10px;letter-spacing:.26em;text-transform:uppercase;color:var(--mg);
  margin-bottom:11px;font-weight:700;text-shadow:0 0 10px rgba(var(--s),.4);
}
.panel h2::before{content:"▸ "}
.card{
  background:var(--card);border:1px solid var(--line);border-left:2px solid var(--cy-dim);
  padding:11px 13px;margin-bottom:9px;transition:all .15s;
}
.card:hover{border-left-color:var(--cy);background:rgba(var(--p),.08);box-shadow:0 0 18px rgba(var(--p),.12)}
.card b{font-size:13.5px;color:#fff}
.small{font-size:11.5px;color:var(--dim)}
.pill{font-size:9px;padding:2px 7px;margin-left:7px;vertical-align:middle;letter-spacing:.12em;text-transform:uppercase;border:1px solid}
.pill.active{color:var(--gn);border-color:rgba(57,255,136,.4);background:rgba(57,255,136,.07)}
.pill.top{color:#fff;border-color:var(--mg);background:rgba(var(--s),.2);text-shadow:0 0 6px var(--mg)}
ul{list-style:none}
li{padding:6px 0 6px 2px;border-bottom:1px dashed rgba(var(--p),.1);font-size:12px;transition:all .15s}
li:last-child{border-bottom:none}
li:hover{background:rgba(var(--p),.04);padding-left:6px}
li::before{content:"› ";color:var(--cy)}
.wl{color:var(--cy)}
button{
  background:transparent;color:var(--cy);border:1px solid var(--line2);
  padding:7px 14px;font:11px var(--mono);letter-spacing:.1em;text-transform:uppercase;
  cursor:pointer;transition:all .15s;
  clip-path:polygon(8px 0,100% 0,100% calc(100% - 8px),calc(100% - 8px) 100%,0 100%,0 8px);
}
button:hover{background:rgba(var(--p),.12);box-shadow:0 0 14px rgba(var(--p),.35);text-shadow:0 0 6px var(--cy)}
button.primary{background:rgba(var(--p),.16);border-color:var(--cy);font-weight:700}
button:disabled{opacity:.35;cursor:default}
select{
  background:rgba(0,20,30,.8);color:var(--cy);border:1px solid var(--line2);
  padding:7px 10px;font:11px var(--mono);letter-spacing:.06em;outline:none;
}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.empty{color:var(--dim);font-size:11.5px;padding:6px 0}

/* ------- AGENTS department ------- */
.org{display:flex;flex-direction:column;align-items:center}
.agent-card{
  background:var(--card);border:1px solid var(--line);padding:14px;transition:all .18s;position:relative;
}
.agent-card:hover{border-color:var(--line2);box-shadow:0 0 24px rgba(var(--p),.16);transform:translateY(-2px)}
.chief{
  width:min(430px,100%);border-color:rgba(var(--s),.45);text-align:center;padding:18px;
  box-shadow:0 0 30px rgba(var(--s),.12);
}
.chief:hover{box-shadow:0 0 34px rgba(var(--s),.3)}
.trunk{width:2px;height:26px;background:linear-gradient(180deg,var(--mg),var(--cy));box-shadow:0 0 8px var(--cy)}
.branch{width:min(92%,900px);height:2px;background:linear-gradient(90deg,transparent,var(--cy),transparent);box-shadow:0 0 8px rgba(var(--p),.5);margin-bottom:26px}
.squad{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:13px;width:100%}
.avatar{
  width:46px;height:46px;display:grid;place-items:center;flex:none;
  font:700 15px var(--mono);color:#021016;
  background:linear-gradient(135deg,var(--cy),#0090aa);
  clip-path:polygon(50% 0,93% 25%,93% 75%,50% 100%,7% 75%,7% 25%);
  box-shadow:0 0 16px rgba(var(--p),.5);
}
.chief .avatar{margin:0 auto 8px;width:56px;height:56px;font-size:18px;background:linear-gradient(135deg,var(--mg),#8b2fff);box-shadow:0 0 20px rgba(var(--s),.55)}
.agent-head{display:flex;gap:12px;align-items:center;margin-bottom:8px}
.agent-name{font-weight:700;color:#fff;font-size:13.5px;letter-spacing:.04em}
.agent-status{font-size:9px;letter-spacing:.18em;color:var(--gn)}
.agent-status::before{content:"●";margin-right:5px;animation:pulse 2s infinite}
.agent-role{font-size:11.5px;color:var(--dim);line-height:1.5}
.agent-rep{font-size:9.5px;color:var(--dim);letter-spacing:.1em;margin-top:8px;text-transform:uppercase}
.agent-rep b{color:var(--cy);font-weight:600}
.proto{margin-top:16px;width:100%}

/* ------- DEPARTMENTS ------- */
.dept-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:14px}
.dept-card{
  background:var(--card);border:1px solid var(--line);padding:16px;position:relative;
  display:flex;flex-direction:column;gap:11px;transition:all .18s;
}
.dept-card:hover{border-color:var(--line2);box-shadow:0 0 26px rgba(var(--p),.16);transform:translateY(-2px)}
.dept-head{display:flex;align-items:center;gap:11px}
.dept-ico{
  width:40px;height:40px;display:grid;place-items:center;flex:none;
  font:700 14px var(--mono);color:#021016;
  background:linear-gradient(135deg,var(--cy),#0090aa);
  clip-path:polygon(50% 0,93% 25%,93% 75%,50% 100%,7% 75%,7% 25%);
  box-shadow:0 0 14px rgba(var(--p),.5);
}
.dept-name{font-weight:700;color:#fff;font-size:14px;letter-spacing:.05em}
.dept-agent{font-size:9.5px;color:var(--dim);letter-spacing:.16em;text-transform:uppercase}
.dept-on{font-size:9px;letter-spacing:.18em;color:var(--gn);margin-left:auto;flex:none}
.dept-on::before{content:"●";margin-right:5px;animation:pulse 2s infinite}
.dept-on.off{color:var(--rd)}
.tkit{display:flex;flex-wrap:wrap;gap:5px}
.tkit .chip{font-size:9px;padding:2px 7px;color:var(--cy);transition:all .15s;cursor:default}
.tkit .chip:hover{border-color:var(--cy);text-shadow:0 0 6px var(--cy)}
.dept-actions{display:flex;flex-wrap:wrap;gap:7px;margin-top:auto;padding-top:2px}
button.heavy{color:var(--mg);border-color:rgba(var(--s),.4)}
button.heavy:hover{background:rgba(var(--s),.12);box-shadow:0 0 14px rgba(var(--s),.35);text-shadow:0 0 6px var(--mg)}
button.heavy::after{content:" ⚡";opacity:.7}
.dept-activity{margin-bottom:14px}
.ctx-chip{font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--mg);border:1px solid rgba(var(--s),.4);padding:3px 9px}
.ctx-chip:empty{display:none}

/* ------- YOUR INPUT / operator queue ------- */
.hot-n:not(:empty){color:#fff!important;border-color:var(--mg)!important;background:rgba(var(--s),.25);animation:pulse 2s infinite}
.q-card{
  background:var(--card);border:1px solid var(--line);border-left:2px solid var(--mg);
  padding:13px 15px;margin-bottom:11px;transition:all .18s;
}
.q-card:hover{box-shadow:0 0 18px rgba(var(--s),.14)}
.q-card.answered{border-left-color:var(--gn);opacity:.6}
.q-meta{font-size:9.5px;color:var(--dim);letter-spacing:.16em;text-transform:uppercase;margin-bottom:5px}
.q-meta b{color:var(--mg);font-weight:600}
.q-text{font-size:13px;color:#fff;line-height:1.5}
.q-ctx{font-size:11px;color:var(--dim);margin-top:3px}
.q-answer{display:flex;gap:8px;margin-top:10px;align-items:flex-end}
.q-answer textarea{
  flex:1;min-height:38px;max-height:110px;background:rgba(0,15,22,.85);color:var(--txt);
  border:1px solid var(--line2);padding:8px 11px;font:12px var(--mono);resize:none;outline:none;
}
.q-answer textarea:focus{border-color:var(--cy);box-shadow:0 0 12px rgba(var(--p),.25)}
.q-done{font-size:11px;color:var(--gn);margin-top:8px}
.q-mic.rec{color:#fff;border-color:var(--rd);background:rgba(255,77,94,.25);box-shadow:0 0 18px rgba(255,77,94,.7);animation:pulse 1s infinite}
#vs-status{font-size:9px;letter-spacing:.18em;text-transform:uppercase;padding:3px 10px;border:1px solid transparent}
#vs-status.speaking{color:var(--mg);border-color:rgba(var(--s),.4)}
#vs-status.listening{color:var(--rd);border-color:rgba(255,77,94,.5);animation:pulse 1.2s infinite}
#vs-status.thinking{color:var(--gn);border-color:rgba(57,255,136,.4)}
#vs-transcript{font-size:12px;color:var(--cy);min-height:0;padding:0 2px}
#vs-transcript:not(:empty){padding:6px 2px;text-shadow:0 0 8px rgba(var(--p),.4)}
#vs-transcript:not(:empty)::before{content:"» "}

/* ------- chat / uplink ------- */
#chat-log{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:9px;padding:4px 2px}
.msg{max-width:78%;padding:9px 13px;font-size:12.5px;white-space:pre-wrap;line-height:1.55;animation:boot .2s ease;border:1px solid}
.msg.you{
  align-self:flex-end;color:#ffe3e6;background:rgba(var(--p),.10);border-color:rgba(var(--p),.35);
  clip-path:polygon(0 0,100% 0,100% 100%,10px 100%,0 calc(100% - 10px));
}
.msg.ai{
  align-self:flex-start;color:#dcffe9;background:rgba(57,255,136,.06);border-color:rgba(57,255,136,.3);
  clip-path:polygon(0 0,100% 0,100% calc(100% - 10px),calc(100% - 10px) 100%,0 100%);
}
.msg.err{border-color:rgba(255,77,94,.5);background:rgba(255,77,94,.08);color:#ffd9dc}
.msg .who{display:block;font-size:8.5px;color:var(--dim);margin-bottom:3px;letter-spacing:.22em;text-transform:uppercase}
.typing i{display:inline-block;width:5px;height:11px;background:var(--gn);margin-right:4px;animation:blink 1s infinite}
.typing i:nth-child(2){animation-delay:.18s}.typing i:nth-child(3){animation-delay:.36s}
@keyframes blink{0%,100%{opacity:.15}50%{opacity:1}}
.chat-head{display:flex;gap:10px;align-items:center;margin-bottom:10px}
.chat-input{display:flex;gap:9px;align-items:flex-end;padding-top:12px;border-top:1px solid var(--line);margin-top:10px}
#chat-text{
  flex:1;min-height:46px;max-height:140px;background:rgba(0,15,22,.85);color:var(--txt);
  border:1px solid var(--line2);padding:11px 13px;font:12.5px var(--mono);resize:none;outline:none;
}
#chat-text:focus{border-color:var(--cy);box-shadow:0 0 14px rgba(var(--p),.25)}
#ptt.rec{color:#fff;border-color:var(--rd);background:rgba(255,77,94,.25);box-shadow:0 0 18px rgba(255,77,94,.7);animation:pulse 1s infinite}

/* ------- ops-floor simulation ------- */
.simchip{font-size:8px;border:1px solid var(--line);color:var(--dim);padding:1px 6px;margin-left:8px;letter-spacing:.18em;vertical-align:middle}
#net{width:100%;height:400px;display:block;image-rendering:pixelated}
.streams{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:10px}
.stream-card{background:var(--card);border:1px solid var(--line);border-left:2px solid var(--cy-dim);padding:10px 12px;transition:all .15s}
.stream-card:hover{border-left-color:var(--cy);box-shadow:0 0 14px rgba(var(--p),.12)}
.stream-card b{font-size:12px;color:#fff}
.stream-card .dept{font-size:8.5px;color:var(--cy);letter-spacing:.16em;text-transform:uppercase;display:block;margin-bottom:3px}
.stream-card .log{font-size:10.5px;color:var(--dim);margin-top:4px;line-height:1.45}
.pill.blocked{color:#fff;border-color:var(--rd);background:rgba(255,77,94,.18)}
#brain{width:100%;height:520px;display:block;cursor:grab}
#brain.grabbing{cursor:grabbing}
.lgd{display:inline-flex;align-items:center;gap:6px;margin-right:8px;margin-bottom:5px;border:1px solid var(--line);padding:2px 9px;transition:all .15s}
.lgd:hover{border-color:var(--line2)}
.lgd em{color:var(--dim);font-style:normal;font-size:9px}
.lgd i{width:8px;height:8px;border-radius:50%;display:inline-block}
#feed{font-size:10.5px;line-height:1.9;max-height:198px;overflow:hidden}
#feed>div{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;animation:boot .25s ease}
#feed .t{color:var(--cy);opacity:.7}
#feed .a{color:#fff;font-weight:700}
#feed .v{color:var(--gn)}
#feed>div:nth-child(n+8){opacity:.45}
.agent-card.active{border-color:rgba(57,255,136,.5);box-shadow:0 0 22px rgba(57,255,136,.22)}
.agent-card.active .agent-status{color:var(--am)}
.task{font-size:10px;color:var(--gn);margin-top:7px;min-height:15px;letter-spacing:.04em}
.task:not(:empty)::after{content:"▊";animation:blink 1s infinite;margin-left:2px}
</style></head><body>

<aside>
  <div class="brand">
    <div class="t">AGENTIC<em>//</em>OS</div>
    <div class="sub">control center</div>
  </div>
  <nav>
    <div class="nav-item on" data-tab="overview"><span class="idx">01</span>Command</div>
    <div class="nav-item" data-tab="projects"><span class="idx">02</span>Operations<span class="n" id="n-projects"></span></div>
    <div class="nav-item" data-tab="depts"><span class="idx">03</span>Departments<span class="n" id="n-agents"></span></div>
    <div class="nav-item" data-tab="agents"><span class="idx">04</span>Agent Town</div>
    <div class="nav-item" data-tab="input"><span class="idx">05</span>Your Input<span class="n hot-n" id="n-queue"></span></div>
    <div class="nav-item" data-tab="brain"><span class="idx">06</span>Brain<span class="n" id="n-notes"></span></div>
    <div class="nav-item" data-tab="chat"><span class="idx">07</span>Uplink</div>
    <div class="nav-item" data-tab="system"><span class="idx">08</span>Core</div>
  </nav>
  <div class="side-foot">
    <div class="lane-mini"><span class="dot" id="free-dot"></span><div><b>FREE LANE</b><span id="free-note">scanning…</span></div></div>
    <div class="lane-mini"><span class="dot ok" style="animation:none"></span><div><b>HEAVY LANE</b><span>subscription</span></div></div>
    <div class="lane-mini" id="theme-btn" style="cursor:pointer" title="cycle theme"><span class="dot" style="background:var(--cy);box-shadow:0 0 8px var(--cy)"></span><div><b>THEME</b><span id="theme-name">CRIMSON</span></div></div>
  </div>
</aside>

<main>
  <div class="topbar">
    <h1 id="tab-title">Command</h1>
    <span class="chip" id="clock"></span>
    <span class="chip" id="vault-path"></span>
    <span class="chip hot" id="latest-session" style="margin-left:auto"></span>
  </div>
  <div class="content">

    <div class="tab on" id="tab-overview">
      <div class="stats">
        <div class="stat hud"><div class="v" id="s-projects">–</div><div class="k">Active ops</div></div>
        <div class="stat hud"><div class="v" id="s-agents">–</div><div class="k">Agents online</div></div>
        <div class="stat hud"><div class="v" id="s-skills">–</div><div class="k">Skills loaded</div></div>
        <div class="stat hud"><div class="v" id="s-next">–</div><div class="k">Roadmap open</div><div class="bar"><i id="s-bar" style="width:0%"></i></div></div>
      </div>
      <div class="cols">
        <div class="panel hud"><h2>Roadmap — next up</h2><ul id="roadmap"></ul></div>
        <div>
          <div class="panel hud"><h2>Activity feed<span class="simchip">SIM</span></h2><div id="feed"></div></div>
          <div class="panel hud"><h2>Operator blockers</h2><ul id="questions"></ul></div>
          <div class="panel hud"><h2>Decision log</h2><ul id="decisions"></ul></div>
        </div>
      </div>
    </div>

    <div class="tab" id="tab-projects">
      <div id="projects"></div>
      <div class="panel hud"><h2>Launch lanes</h2>
        <div class="card"><b>FREE LANE</b> <span class="small">proxy → NVIDIA NIM · triage, drafts, journaling</span>
          <div class="small" id="free-note2" style="margin:4px 0 8px"></div>
          <button onclick="copyCmd('free',this)">copy launch cmd</button></div>
        <div class="card"><b>HEAVY LANE</b> <span class="small">real Claude · money, research, publishing</span>
          <div style="margin-top:8px"><button onclick="copyCmd('pro',this)">copy launch cmd</button></div></div>
      </div>
    </div>

    <div class="tab" id="tab-depts">
      <div class="panel hud dept-activity"><h2>Latest activity</h2><div class="small" id="dept-latest">–</div></div>
      <div class="dept-grid" id="depts"></div>
    </div>

    <div class="tab" id="tab-agents">
      <div class="panel hud"><h2>Agent town<span class="simchip">SIM · ZERO COST · NO MODEL CALLS</span></h2>
        <canvas id="net"></canvas>
      </div>
      <div class="panel hud"><h2>Business streams<span class="simchip">LIVE · FROM 05 PROJECTS</span></h2>
        <div class="streams" id="streams"></div>
      </div>
      <div class="org" id="org"></div>
    </div>

    <div class="tab" id="tab-input">
      <div class="panel hud">
        <h2>Command center — what your agents need from you</h2>
        <div class="row" style="margin-bottom:8px">
          <button class="primary" id="vs-btn" onclick="voiceSession()">🎧 voice session</button>
          <button onclick="speakQueue()" title="just reads all items aloud">🔊 read all</button>
          <button onclick="speechSynthesis.cancel()" title="stop speaking">■</button>
          <select id="voice-sel" title="voice" style="max-width:190px"></select>
          <span id="vs-status"></span>
        </div>
        <div id="vs-transcript"></div>
        <div class="small" style="margin-bottom:12px">voice session: it reads item 1 → you answer by speaking → it asks follow-ups if needed → saves → next item. Say "skip", "repeat" or "stop" anytime. Answers are written back into the vault and picked up by the next session. Typing + hold-🎙 per card still work.</div>
        <div id="queue-list"></div>
      </div>
      <div class="panel hud">
        <h2>Also parsed from Handoff & Memory</h2>
        <div id="queue-derived"></div>
      </div>
    </div>

    <div class="tab" id="tab-brain">
      <div class="panel hud">
        <h2>Vault neural map<span class="simchip">LIVE · PARSED FROM WIKILINKS</span></h2>
        <canvas id="brain"></canvas>
        <div class="row small" id="brain-legend" style="padding-top:8px"></div>
      </div>
    </div>

    <div class="tab" id="tab-chat">
      <div class="chat-head">
        <select id="chat-sess" title="chat sessions"></select>
        <button onclick="newChat()">+ new</button>
        <button onclick="delChat()">del</button>
        <select id="chat-lane">
          <option value="opus">OPUS SLOT · GLM 4.7</option>
          <option value="sonnet">SONNET SLOT · KIMI K2</option>
          <option value="haiku">HAIKU SLOT · STEP 3.5</option>
        </select>
        <span class="ctx-chip" id="chat-ctx" title="department context — this chat is briefed as that agent"></span>
        <span class="small" style="margin-left:auto">vault-aware · read-only — writes happen in Claude Code</span>
      </div>
      <div id="chat-log"></div>
      <div class="chat-input">
        <button id="ptt" title="hold to talk (or F9)">🎙</button>
        <textarea id="chat-text" placeholder="> transmit to free model — Enter to send"></textarea>
        <button class="primary" id="send" onclick="sendChat()">Send</button>
      </div>
      <div class="small" id="ptt-status" style="padding-top:4px"></div>
    </div>

    <div class="tab" id="tab-system">
      <div class="cols">
        <div class="panel hud"><h2>Memory — current state</h2><ul id="state"></ul></div>
        <div>
          <div class="panel hud"><h2>Skills & workflows</h2><div class="row small" id="skills"></div></div>
          <div class="panel hud"><h2>Inbox</h2><div class="small" id="inbox"></div></div>
        </div>
      </div>
    </div>

  </div>
</main>

<script>
const $=id=>document.getElementById(id);
const esc=s=>s.replace(/&/g,"&amp;").replace(/</g,"&lt;");
const wl=s=>esc(s).replace(/\[\[([^\]]+)\]\]/g,'<span class="wl">$1</span>').replace(/~~([^~]+)~~/g,'<s>$1</s>').replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>');

// ---- themes
const THEMES={
 crimson:{p:'255,59,77',s:'255,158,44',pc:'#ff3b4d',sc:'#ff9e2c',
  shirts:['#ff3b4d','#ff9e2c','#ffd166','#ff6b6b','#c9184a','#ff85a1','#f77f00','#e85d75']},
 ember:{p:'255,176,46',s:'255,94,58',pc:'#ffb02e',sc:'#ff5e3a',
  shirts:['#ffb02e','#ff5e3a','#ffd166','#e8a13c','#ff8552','#d4a017','#ff9e2c','#f2c14e']},
 cyan:{p:'0,240,255',s:'255,59,77',pc:'#00f0ff',sc:'#ff3b4d',
  shirts:['#00f0ff','#39ff88','#ffc24d','#8b5cff','#4da3ff','#ff6b6b','#66ffd9','#ff9de6']}
};
let CT=THEMES.crimson;
const pa=a=>`rgba(${CT.p},${a})`,sa=a=>`rgba(${CT.s},${a})`;
function applyTheme(t){
  if(!THEMES[t])t='crimson';
  document.body.dataset.theme=t;
  CT=THEMES[t];localStorage.setItem('cockpit-theme',t);
  const n=$('theme-name');if(n)n.textContent=t.toUpperCase();
  if(SIM)SIM.agents.forEach((a,i)=>a.color=CT.shirts[i%CT.shirts.length]);
}

// ---- tabs
const TITLES={overview:'Command',projects:'Operations',depts:'Departments',agents:'Agent Town',input:'Your Input',brain:'Neural Map',chat:'Uplink',system:'Core'};
document.querySelectorAll('.nav-item').forEach(el=>el.addEventListener('click',()=>{
  document.querySelectorAll('.nav-item').forEach(x=>x.classList.remove('on'));
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));
  el.classList.add('on');
  $('tab-'+el.dataset.tab).classList.add('on');
  $('tab-title').textContent=TITLES[el.dataset.tab];
  localStorage.setItem('cockpit-tab',el.dataset.tab);
  if(el.dataset.tab==='brain')initBrain();
}));
const saved=localStorage.getItem('cockpit-tab');
if(saved&&saved!=='overview'){const el=document.querySelector(`[data-tab="${saved}"]`);if(el)el.click();}

const CMDS={
  free:'cd $VAULT && ANTHROPIC_AUTH_TOKEN="pick-your-own-secret-here" ANTHROPIC_BASE_URL="http://localhost:8082" claude',
  pro:'cd $VAULT && claude'
};
function copyCmd(k,btn){navigator.clipboard.writeText(CMDS[k]).then(()=>flash(btn));}
function flash(btn){const t=btn.textContent;btn.textContent='copied ✓';setTimeout(()=>btn.textContent=t,1200);}
const initials=n=>n.split(/\s+/).filter(w=>/^[A-Za-z]/.test(w)).map(w=>w[0]).join('').slice(0,2).toUpperCase();

// ---- departments
const HEAVY_BOOT='cd $VAULT && claude "Boot from 00 System/Kernel.md, then ';
const DEPTS=[
 {name:'Chief of Staff',agent:'Chief of Staff',ico:'CS',
  quick:[{label:'brief me',prompt:'Brief me on where everything stands: top priorities, blockers, and anything needing my decision. Keep it tight.'}],
  heavy:[{label:'full heavy session',cmd:'cd $VAULT && claude "Boot from 00 System/Kernel.md and resume from Handoff — Chief of Staff mode."'}]},
 {name:'Finance',agent:'Finance Analyst',ico:'FI',
  quick:[{label:'portfolio pulse',prompt:'Quick portfolio pulse: current state per Memory, open finance questions, what the weekly brief should watch.'}],
  heavy:[{label:'suggest a stock',cmd:HEAVY_BOOT+'as the Finance Analyst run the stock-screener skill with the full team pattern."'}]},
 {name:'Content',agent:'Content Director',ico:'CO',
  quick:[{label:'channel status',prompt:'Status of P - Docu Channel: what is done, what is next, what is blocked on me?'}],
  heavy:[{label:'next video step',cmd:HEAVY_BOOT+'as the Content Director continue the content project: next production step."'}]},
 {name:'Business Ops',agent:'Business Ops Agent',ico:'BO',
  quick:[{label:'probe status',prompt:'Status of the venture probes and summer data collectors: what is live, collecting, or blocked?'}],
  heavy:[{label:'run bot verdicts',cmd:HEAVY_BOOT+'as the Business Ops Agent run each summer bot\'s analyze.py and log the verdicts."'}]},
 {name:'Daily Ops',agent:'Daily Ops Agent',ico:'DO',
  quick:[{label:'run the daily check',prompt:'Run the daily check: today\'s events, top 3 tasks, deadlines within 7 days, anything blocked. Max 150 words.'}],
  heavy:[]},
 {name:'Research',agent:'Research Agent',ico:'RE',
  quick:[{label:'what should I look into?',prompt:'Given my roadmap and open questions, what should I look into next? 3 candidates, one-line why each.'}],
  heavy:[]}
];
function renderDepts(agents){
  const byName=Object.fromEntries(agents.map(a=>[a.name,a]));
  $('depts').innerHTML=DEPTS.map((d,i)=>{
    const a=byName[d.agent];
    const links=a?((a.toolkit||'').match(/\[\[([^\]|#]+)\]\]/g)||[]).map(s=>s.slice(2,-2)):[];
    return `<div class="dept-card hud">
      <div class="dept-head">
        <div class="dept-ico">${d.ico}</div>
        <div><div class="dept-name">${esc(d.name)}</div><div class="dept-agent">${esc(d.agent)}</div></div>
        <div class="dept-on${a?'':' off'}">${a?'ONLINE':'MISSING'}</div>
      </div>
      <div class="tkit">${links.map(l=>`<span class="chip">${esc(l)}</span>`).join('')||'<span class="small">no toolkit line</span>'}</div>
      <div class="dept-actions">
        <button class="primary" onclick="deptChat(${i})">work with dept</button>
        ${d.quick.map((q,j)=>`<button title="runs on free lane" onclick="deptQuick(${i},${j})">${esc(q.label)}</button>`).join('')}
        ${d.heavy.map((h,j)=>`<button class="heavy" title="runs on heavy lane — copies the Claude command" onclick="deptHeavy(${i},${j},this)">${esc(h.label)}</button>`).join('')}
      </div>
    </div>`;
  }).join('');
}
function gotoTab(t){const el=document.querySelector(`[data-tab="${t}"]`);if(el)el.click();}
function deptOpen(i,title){
  const d=DEPTS[i],id=++CHATS.seq;
  CHATS.list.push({id,title,dept:d.agent,msgs:[]});CHATS.cur=id;
  saveChats();renderSess();renderLog();gotoTab('chat');
}
function deptChat(i){deptOpen(i,DEPTS[i].name);$('chat-text').focus();}
function deptQuick(i,j){
  const q=DEPTS[i].quick[j];
  deptOpen(i,DEPTS[i].name+' · '+q.label.slice(0,16));
  $('chat-text').value=q.prompt;sendChat();
}
function deptHeavy(i,j,btn){navigator.clipboard.writeText(DEPTS[i].heavy[j].cmd).then(()=>flash(btn));}

// ---- YOUR INPUT: operator queue (answers write back into the vault)
let QUEUE={items:[],derived:[],open:0};
async function loadQueue(){
  try{QUEUE=await (await fetch('/api/queue')).json();}catch(e){return;}
  $('n-queue').textContent=QUEUE.open||'';
  const card=(it,i,derived)=>{
    const done=!!it.done;
    return `<div class="q-card${done?' answered':''}">
      <div class="q-meta"><b>${esc(it.agent)}</b> · ${esc(it.date)}${derived?' · parsed':''}</div>
      <div class="q-text">${wl(it.q)}</div>
      ${it.ctx?`<div class="q-ctx">${wl(it.ctx)}</div>`:''}
      ${done?`<div class="q-done">✓ answered${it.answer?': '+esc(it.answer):''}</div>`
        :`<div class="q-answer">
          <textarea id="qa-${derived?'d':''}${i}" placeholder="> your answer — typed or dictated"></textarea>
          <button class="q-mic" data-mic="qa-${derived?'d':''}${i}" title="hold to dictate">🎙</button>
          <button class="primary" onclick="saveAnswer(${i},${derived},this)">save</button>
        </div>`}
    </div>`;};
  const open=QUEUE.items.filter(x=>!x.done),doneN=QUEUE.items.length-open.length;
  $('queue-list').innerHTML=QUEUE.items.map((it,i)=>card(it,i,false)).join('')
    ||'<div class="empty">queue clear — nothing needs you ✓</div>';
  $('queue-derived').innerHTML=QUEUE.derived.map((it,i)=>card(it,i,true)).join('')
    ||'<div class="empty">nothing parsed</div>';
}
async function saveAnswer(i,derived,btn){
  const it=derived?QUEUE.derived[i]:QUEUE.items[i];
  const ta=$('qa-'+(derived?'d':'')+i);
  const answer=(ta.value||'').trim();if(!answer)return;
  btn.disabled=true;
  const r=await (await fetch('/api/answer',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({line:derived?'':(it.line||''),q:it.q,agent:it.agent,answer})})).json();
  if(r.ok){speak('Saved.');loadQueue();}
  else{btn.disabled=false;btn.textContent='error';}
}
// ---- voice engine: best-available voice + conversational session
let VOICES=[];
function loadVoices(){
  VOICES=speechSynthesis.getVoices().filter(v=>v.lang.toLowerCase().startsWith('en'));
  const score=v=>(/premium|enhanced|natural|neural/i.test(v.name)?8:0)
    +(/samantha|ava|zoe|allison|serena|karen|moira/i.test(v.name)?4:0)
    +(/google/i.test(v.name)?3:0)+(/siri/i.test(v.name)?3:0)-(/compact|fred|albert|bells|zarvox/i.test(v.name)?9:0);
  VOICES.sort((a,b)=>score(b)-score(a));
  const sel=$('voice-sel');if(!sel||!VOICES.length)return;
  const saved=localStorage.getItem('cockpit-voice')||'';
  sel.innerHTML=VOICES.map(v=>`<option value="${esc(v.name)}"${v.name===saved?' selected':''}>${esc(v.name.replace(/\(.*\)/,'').trim())}</option>`).join('');
  if(!saved&&VOICES[0])localStorage.setItem('cockpit-voice',VOICES[0].name);
}
speechSynthesis.onvoiceschanged=loadVoices;
setTimeout(loadVoices,300);
document.addEventListener('change',e=>{
  if(e.target.id!=='voice-sel')return;
  localStorage.setItem('cockpit-voice',e.target.value);
  speak('This is my voice now.');
});
function speak(t){ // resolves when done speaking
  return new Promise(res=>{
    try{
      const u=new SpeechSynthesisUtterance(t);
      const v=VOICES.find(x=>x.name===localStorage.getItem('cockpit-voice'))||VOICES[0];
      if(v)u.voice=v;
      u.rate=1.04;
      u.onend=res;u.onerror=res;
      speechSynthesis.speak(u);
    }catch(e){res();}
  });
}
function speakQueue(){
  speechSynthesis.cancel();
  const open=[...QUEUE.items.filter(x=>!x.done),...QUEUE.derived];
  if(!open.length){speak('Queue is clear. Nothing needs your input.');return;}
  speak(`You have ${open.length} item${open.length>1?'s':''} waiting on you.`);
  open.forEach((it,k)=>speak(`${k+1}. From ${it.agent.replace(/Agent$/,'')}: ${it.q}`));
}

// ---- guided voice session: read item → listen → model asks follow-ups → save → next
let VS=null;
function vsStatus(s){const el=$('vs-status');el.className=s;el.textContent=s?('● '+s):'';}
function vsListen(){ // one spoken utterance; resolves with transcript on ~1.6s silence
  return new Promise(res=>{
    const SRC=window.SpeechRecognition||window.webkitSpeechRecognition;
    if(!SRC){res('');return;}
    const r=new SRC();r.lang='en-US';r.continuous=true;r.interimResults=true;
    let fin='',timer=null,done=false;
    const finish=()=>{if(done)return;done=true;clearTimeout(timer);try{r.stop();}catch(e){}res(fin.trim());};
    r.onresult=e=>{
      let interim='';
      for(const x of e.results){if(x.isFinal)fin+=x[0].transcript+' ';else interim+=x[0].transcript;}
      $('vs-transcript').textContent=(fin+' '+interim).trim();
      clearTimeout(timer);timer=setTimeout(finish,1600);
    };
    r.onerror=finish;r.onend=finish;
    if(VS)VS.rec=r;
    try{r.start();}catch(e){finish();}
    timer=setTimeout(finish,10000); // silence budget if nothing said
  });
}
async function vsAsk(item,transcript,history){ // free-lane model round: SAVE or one follow-up
  try{
    const r=await (await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({lane:$('chat-lane')?$('chat-lane').value:'sonnet',dept:'Chief of Staff',messages:[
        {role:'user',content:`VOICE SESSION over the Operator Queue. Item from ${item.agent}: "${item.q}"${item.ctx?' — context: '+item.ctx:''}. I answer by voice (may be messy speech-to-text). If my answer resolves the item, reply EXACTLY "SAVE: <my answer consolidated into one clear line>". If something essential is missing or ambiguous, ask exactly ONE short clarifying question (max 15 words) and nothing else.`},
        {role:'assistant',content:'Understood. Give me your answer.'},
        ...history,{role:'user',content:transcript}]})})).json();
    if(r.ok){
      const m=r.text.trim();
      if(/^SAVE:/i.test(m))return{save:m.replace(/^SAVE:\s*/i,'').split('\n')[0].trim()};
      return{question:m.split('\n')[0].slice(0,220)};
    }
  }catch(e){}
  return{save:transcript}; // proxy down → save the raw transcript, still useful
}
async function voiceSession(){
  if(VS){vsStop();return;}
  await loadQueue();
  const items=[...QUEUE.items.map((it)=>({...it,derived:false})).filter(x=>!x.done),
               ...QUEUE.derived.map((it)=>({...it,derived:true}))];
  if(!items.length){speak('Queue is clear. Nothing needs your input.');return;}
  VS={on:true};$('vs-btn').textContent='■ end session';
  vsStatus('speaking');
  await speak(`Voice session. ${items.length} item${items.length>1?'s':''} waiting.`);
  for(let k=0;k<items.length&&VS;k++){
    const it=items[k];
    let history=[],rounds=0,resolved=false,repeat=true;
    while(VS&&repeat){
      repeat=false;
      vsStatus('speaking');
      await speak(`Item ${k+1}. From ${it.agent.replace(/Agent$/,'').trim()}. ${it.q}`);
      while(VS&&!resolved&&rounds<4){
        vsStatus('listening');$('vs-transcript').textContent='';
        const t=(await vsListen()).trim();
        if(!VS)break;
        const low=t.toLowerCase();
        if(!t){vsStatus('speaking');await speak('Nothing heard. Moving on.');break;}
        if(/^(skip|next)\b/.test(low)){vsStatus('speaking');await speak('Skipped.');break;}
        if(/^(repeat|again|say again)\b/.test(low)){repeat=true;break;}
        if(/^(stop|exit|end|quit)\b/.test(low)){vsStop();break;}
        vsStatus('thinking');
        const out=await vsAsk(it,t,history);
        if(!VS)break;
        if(out.save!==undefined){
          await fetch('/api/answer',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({line:it.derived?'':(it.line||''),q:it.q,agent:it.agent,answer:out.save})});
          vsStatus('speaking');await speak('Saved. '+(k<items.length-1?'Next.':''));resolved=true;
        }else{
          history.push({role:'user',content:t},{role:'assistant',content:out.question});
          vsStatus('speaking');await speak(out.question);rounds++;
        }
      }
    }
  }
  if(VS)await speak('Session complete. All answers are in the vault.');
  vsStop();loadQueue();
}
function vsStop(){
  if(VS&&VS.rec){try{VS.rec.stop();}catch(e){}}
  VS=null;speechSynthesis.cancel();
  $('vs-btn').textContent='🎧 voice session';vsStatus('');$('vs-transcript').textContent='';
}
// hold-to-dictate on any queue mic (event delegation; shared recognizer)
if(window.SpeechRecognition||window.webkitSpeechRecognition){
  const QR=new (window.SpeechRecognition||window.webkitSpeechRecognition)();
  QR.lang='en-US';QR.continuous=true;QR.interimResults=true;
  let qTarget=null,qBtn=null;
  QR.onresult=e=>{if(!qTarget)return;let fin='';
    for(const r of e.results)if(r.isFinal)fin+=r[0].transcript+' ';
    if(fin)qTarget.value=(qTarget.value+' '+fin).trim();};
  const qStop=()=>{try{QR.stop();}catch(e){}if(qBtn)qBtn.classList.remove('rec');qBtn=null;qTarget=null;};
  document.addEventListener('mousedown',e=>{
    const b=e.target.closest('.q-mic');if(!b)return;
    qTarget=$(b.dataset.mic);qBtn=b;b.classList.add('rec');
    try{QR.start();}catch(err){}
  });
  document.addEventListener('mouseup',()=>{if(qBtn)qStop();});
}

// ---- vault data
async function load(){
  const v=await (await fetch('/api/vault')).json();
  $('vault-path').textContent=v.vault.replace(/^\/Users\/[^/]+/,'~');
  $('latest-session').textContent=v.handoff.latestSession;
  $('n-projects').textContent=v.projects.length;
  $('n-agents').textContent=v.agents.length;
  $('s-projects').textContent=v.projects.length;
  $('s-agents').textContent=v.agents.length;
  $('s-skills').textContent=v.skills.length+v.workflows.length;
  $('s-next').textContent=v.handoff.next.length;
  const total=v.handoff.next.length+v.handoff.doneCount;
  $('s-bar').style.width=(total?Math.round(100*v.handoff.doneCount/total):0)+'%';

  $('projects').innerHTML=v.projects.map(p=>`
    <div class="card"><b>${esc(p.name)}</b>
      ${p.top?'<span class="pill top">★ Priority 1</span>':''}
      <span class="pill active">${esc(p.status)}</span>
      <div class="small" style="margin-top:4px">${wl(p.goal||'')}${p.deadline?' · '+wl(p.deadline):''}</div>
      ${p.lastLog?`<div class="small" style="margin-top:4px;opacity:.75">↳ ${wl(p.lastLog)}</div>`:''}</div>`).join('');

  // ---- agent department (org view)
  const chief=v.agents.find(a=>a.name==='Chief of Staff');
  const proto=v.agents.filter(a=>/pattern/i.test(a.name));
  const squad=v.agents.filter(a=>a!==chief&&!proto.includes(a));
  let org='';
  if(chief)org+=`
    <div class="agent-card chief hud">
      <div class="avatar">${initials(chief.name)}</div>
      <div class="agent-name">${esc(chief.name)}</div>
      <div class="agent-status">ORCHESTRATOR · ONLINE</div>
      <div class="agent-role" style="margin-top:6px">${wl(chief.role)}</div>
    </div>
    <div class="trunk"></div><div class="branch"></div>`;
  org+=`<div class="squad">${squad.map(a=>`
    <div class="agent-card" data-agent="${esc(a.name)}">
      <div class="agent-head">
        <div class="avatar">${initials(a.name)}</div>
        <div><div class="agent-name">${esc(a.name)}</div><div class="agent-status" data-st>STANDBY</div></div>
      </div>
      <div class="agent-role">${wl(a.role.replace(/\s*Reports to \[\[.+?\]\]\.?/,''))}</div>
      <div class="task" data-task></div>
      ${a.reportsTo?`<div class="agent-rep">↳ reports to <b>${esc(a.reportsTo)}</b></div>`:''}
    </div>`).join('')}</div>`;
  if(proto.length)org+=`<div class="panel hud proto"><h2>Protocols</h2>${proto.map(p=>`
    <div class="card"><b>${esc(p.name)}</b><div class="small" style="margin-top:3px">${wl(p.role)}</div></div>`).join('')}</div>`;
  $('org').innerHTML=org;
  initSim(squad.map(a=>a.name),[...v.projects.map(p=>p.name),...v.skills,'Handoff','Memory','04 Inbox']);

  // business streams board (real project data, mapped to owner departments)
  const streamDept=n=>/finance/i.test(n)?'Finance':/docu|content|channel/i.test(n)?'Content'
    :/flip|venture|product|demand|smart/i.test(n)?'Business Ops':/job/i.test(n)?'Research'
    :/control center|cockpit/i.test(n)?'Chief of Staff':'Daily Ops';
  $('streams').innerHTML=v.projects.map(p=>{
    const blocked=/blocked/i.test(p.goal+' '+p.lastLog);
    return `<div class="stream-card">
      <span class="dept">${streamDept(p.name)}</span><b>${esc(p.name)}</b>
      <span class="pill active">${esc(p.status)}</span>
      ${blocked?'<span class="pill blocked">⏸ needs you</span>':''}
      ${p.top?'<span class="pill top">★</span>':''}
      ${p.lastLog?`<div class="log">↳ ${wl(p.lastLog)}</div>`:''}
    </div>`;}).join('')||'<div class="empty">no active projects</div>';

  renderDepts(v.agents);
  $('dept-latest').innerHTML='<b style="color:var(--txt)">'+esc(v.handoff.latestSession)+'</b>'+
    (v.handoff.next.length?'<div style="margin-top:4px">next → '+wl(v.handoff.next[0].text)+'</div>':'');

  $('roadmap').innerHTML=v.handoff.next.slice(0,12).map(i=>`<li>${wl(i.text)}</li>`).join('');
  $('questions').innerHTML=v.memory.openQuestions.map(q=>`<li>${wl(q)}</li>`).join('')||'<div class="empty">none</div>';
  $('state').innerHTML=v.memory.currentState.map(q=>`<li>${wl(q)}</li>`).join('');
  $('inbox').textContent=v.inbox.length?v.inbox.join(', '):'EMPTY — nothing to triage';
  $('decisions').innerHTML=v.decisions.map(d=>`<li>${wl(d)}</li>`).join('');
  $('skills').innerHTML=[...v.skills,...v.workflows].map(s=>`<span class="chip">${esc(s)}</span>`).join('');
}
async function proxy(){
  try{const p=await (await fetch('/api/proxy')).json();
    $('free-dot').className='dot '+(p.up?'ok':'bad');
    $('free-note').textContent=p.up?'proxy :8082 up':'proxy offline';
    const n2=$('free-note2');if(n2)n2.textContent=p.up?'✓ PROXY ONLINE :8082':'✗ PROXY OFFLINE — start fcc-server (Setup Guide step 5)';
  }catch(e){}
  setTimeout(proxy,10000);
}
function clock(){$('clock').textContent=new Date().toLocaleString('en-GB',{weekday:'short',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});setTimeout(clock,30000);}

// ---- uplink chat (multi-session, persisted in localStorage)
let CHATS=JSON.parse(localStorage.getItem('cockpit-chats')||'null')||{seq:1,cur:1,list:[{id:1,title:'chat 1',msgs:[]}]};
const curChat=()=>CHATS.list.find(c=>c.id===CHATS.cur)||CHATS.list[0];
const saveChats=()=>localStorage.setItem('cockpit-chats',JSON.stringify(CHATS));
function renderSess(){
  $('chat-sess').innerHTML=CHATS.list.map(c=>`<option value="${c.id}"${c.id===CHATS.cur?' selected':''}>${esc(c.title)}</option>`).join('');
}
function renderLog(){
  const c=curChat();$('chat-log').innerHTML='';
  $('chat-ctx').textContent=c.dept?('» '+c.dept):'';
  if(!c.msgs.length){$('chat-log').innerHTML='<div class="empty">UPLINK READY — vault-aware channel. It already knows your OS; just talk.</div>';return;}
  c.msgs.forEach(m=>bubble(m.role==='user'?'you':'ai',esc(m.content),false,''));
}
function newChat(){
  const id=++CHATS.seq;CHATS.list.push({id,title:'chat '+id,msgs:[]});CHATS.cur=id;
  saveChats();renderSess();renderLog();
}
function delChat(){
  CHATS.list=CHATS.list.filter(c=>c.id!==CHATS.cur);
  if(!CHATS.list.length){CHATS.seq++;CHATS.list=[{id:CHATS.seq,title:'chat '+CHATS.seq,msgs:[]}];}
  CHATS.cur=CHATS.list[CHATS.list.length-1].id;
  saveChats();renderSess();renderLog();
}
function bubble(role,html,remember,text){
  const e=$('chat-log').querySelector('.empty');if(e)e.remove();
  if(remember)curChat().msgs.push({role:role==='you'?'user':'assistant',content:text});
  const d=document.createElement('div');d.className='msg '+(role==='you'?'you':'ai');
  d.innerHTML=`<span class="who">${role==='you'?'operator':(curChat().dept||'free model').toLowerCase()}</span>${html}`;
  $('chat-log').appendChild(d);$('chat-log').scrollTop=1e9;
  return d;
}
async function sendChat(){
  const t=$('chat-text').value.trim();if(!t)return;
  const c=curChat();
  $('chat-text').value='';
  bubble('you',esc(t),true,t);
  if(c.msgs.length===1){c.title=t.slice(0,26)+(t.length>26?'…':'');renderSess();}
  saveChats();
  const tip=bubble('ai','<span class="typing"><i></i><i></i><i></i></span>',false,'');
  $('send').disabled=true;
  try{
    const r=await (await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({lane:$('chat-lane').value,dept:c.dept||'',messages:c.msgs})})).json();
    if(r.ok){tip.innerHTML=`<span class="who">${esc((c.dept||'free model').toLowerCase())}</span>${esc(r.text)}`;c.msgs.push({role:'assistant',content:r.text});}
    else{tip.classList.add('err');tip.innerHTML=`<span class="who">error</span>⚠ ${esc(r.error)}`;c.msgs.pop();$('chat-text').value=t;}
  }catch(e){tip.classList.add('err');tip.innerHTML=`<span class="who">error</span>⚠ ${esc(e.message)}`;c.msgs.pop();$('chat-text').value=t;}
  saveChats();
  $('send').disabled=false;$('chat-log').scrollTop=1e9;
}
$('chat-sess').addEventListener('change',e=>{CHATS.cur=+e.target.value;saveChats();renderLog();});
renderSess();renderLog();
$('chat-text').addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat();}
});

// ---- push-to-talk (Web Speech API; Chrome/Edge best)
const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
if(SR){
  const rec=new SR();rec.lang='en-US';rec.continuous=true;rec.interimResults=true;
  rec.onresult=e=>{let fin='';for(const r of e.results)if(r.isFinal)fin+=r[0].transcript+' ';
    if(fin)$('chat-text').value=($('chat-text').value+' '+fin).trim();
    $('ptt-status').textContent=[...e.results].filter(r=>!r.isFinal).map(r=>r[0].transcript).join('')||'';};
  rec.onerror=e=>$('ptt-status').textContent='mic error: '+e.error;
  const b=$('ptt');
  const start=()=>{try{rec.start();b.classList.add('rec');}catch(e){}};
  const stop=()=>{rec.stop();b.classList.remove('rec');$('ptt-status').textContent='';};
  b.addEventListener('mousedown',start);b.addEventListener('touchstart',start);
  b.addEventListener('mouseup',stop);b.addEventListener('mouseleave',stop);b.addEventListener('touchend',stop);
  window.addEventListener('keydown',e=>{if(e.code==='F9'&&!e.repeat)start();});
  window.addEventListener('keyup',e=>{if(e.code==='F9')stop();});
}else{
  $('ptt').disabled=true;$('ptt-status').textContent='Speech API not available — use Chrome';
}

// ---- AGENT TOWN simulation (cosmetic only — zero model calls)
const VERBS=['scanning','indexing','drafting','reviewing','triaging','syncing','compiling','evaluating','cross-checking','archiving'];
let SIM=null;
function initSim(names,targets){
  if(SIM)return; // once
  const cv=$('net'),ctx=cv.getContext('2d');
  SIM={W:0,H:400,targets,agents:[],last:performance.now(),roadY:196};
  const BKIND=n=>/finance/i.test(n)?'tower':/content/i.test(n)?'studio':/business/i.test(n)?'factory'
    :/research/i.test(n)?'lab':/daily/i.test(n)?'post':/review/i.test(n)?'gate':'house';
  function layout(){
    SIM.W=Math.max(cv.clientWidth||800,560);
    cv.width=SIM.W*devicePixelRatio;cv.height=SIM.H*devicePixelRatio;
    cv.style.height=SIM.H+'px';
    ctx.setTransform(devicePixelRatio,0,0,devicePixelRatio,0,0);
    SIM.chief={x:SIM.W*.56,y:150,w:118,h:78,bkind:'hq'};
    SIM.operator={x:SIM.W*.30,y:150,w:76,h:46,bkind:'op'};
    SIM.coffee={x:64,y:150};
    SIM.plant={x:SIM.W-70,y:150};
    SIM.lights=[SIM.W*.14,SIM.W*.43,SIM.W*.72,SIM.W*.92].map(x=>({x,y:SIM.roadY}));
    if(!SIM.drone)SIM.drone={x:-40,active:false,next:performance.now()+6000};
    const n=names.length;
    names.forEach((nm,i)=>{
      const bx=SIM.W*(i+1)/(n+1);
      const a=SIM.agents[i]||{name:nm,color:CT.shirts[i%CT.shirts.length],state:'idle',frame:0,busy:0,path:null,task:'',bkind:BKIND(nm)};
      a.home={x:bx,y:352,w:Math.min(92,SIM.W/(n+1)-16),h:a.bkind==='tower'?96:64,bkind:a.bkind};
      a.door={x:bx,y:352};
      if(a.state==='walk'||a.state==='meet'){a.state='idle';a.path=null;}
      if(a.state==='idle'||a.state==='work')a.pos={x:bx,y:352};
      SIM.agents[i]=a;
    });
  }
  layout();window.addEventListener('resize',layout);

  function setCard(name,on,task){
    document.querySelectorAll('.agent-card[data-agent]').forEach(c=>{
      if(c.dataset.agent!==name)return;
      c.classList.toggle('active',on);
      const st=c.querySelector('[data-st]'),tk=c.querySelector('[data-task]');
      if(st)st.textContent=on?'PROCESSING':'STANDBY';
      if(tk)tk.textContent=on?task:'';
    });
  }
  function feed(name,verb,target){
    const f=$('feed');if(!f)return;
    const d=document.createElement('div');
    d.innerHTML=`<span class="t">[${new Date().toLocaleTimeString('en-GB')}]</span> <span class="a">${esc(name.toUpperCase())}</span> <span class="v">${verb}</span> ${esc(target)}`;
    f.prepend(d);while(f.children.length>12)f.lastChild.remove();
  }
  function pathTo(a,dest){ // route via the main street
    return [{x:a.pos.x,y:SIM.roadY},{x:dest.x,y:SIM.roadY},{x:dest.x,y:dest.y}];
  }
  function tick(){
    const idle=SIM.agents.filter(a=>a.state==='idle');
    if(idle.length){
      const a=idle[Math.floor(Math.random()*idle.length)];
      const r=Math.random();
      const verb=VERBS[Math.floor(Math.random()*VERBS.length)];
      const target=SIM.targets[Math.floor(Math.random()*SIM.targets.length)];
      const go=(dest,task,fverb,ftar,mkind)=>{
        a.state='walk';a.dest=dest;a.after='meet';a.mkind=mkind;
        a.path=pathTo(a,dest);a.task=task;
        setCard(a.name,true,task);feed(a.name,fverb,ftar);
      };
      const askOp=QUEUE&&QUEUE.open>0;
      if(r<.5){ // work inside own building
        a.state='work';a.mkind='work';a.busy=4200+Math.random()*3600;a.task=`${verb} :: ${target}`;
        setCard(a.name,true,a.task);feed(a.name,verb,target);
      }else if(r<.68){ // brief the chief at HQ
        go({x:SIM.chief.x+20,y:SIM.chief.y},'briefing :: Chief of Staff','briefing','Chief of Staff','chief');
      }else if(askOp&&r<.82){ // queue open → ask the operator
        go({x:SIM.operator.x-16,y:SIM.operator.y},'waiting :: YOUR input','requesting input from','OPERATOR (Your Input tab)','ask');
      }else if(r<.9){ // café
        go({x:SIM.coffee.x+18,y:SIM.coffee.y},'coffee break :: recharging','☕ coffee break','recharging','coffee');
      }else{ // power plant maintenance
        go({x:SIM.plant.x-20,y:SIM.plant.y},'maintaining :: power plant','maintaining','power plant','plant');
      }
    }
    setTimeout(tick,2400+Math.random()*2600);
  }
  setTimeout(tick,900);

  function update(dt){
    SIM.agents.forEach(a=>{
      if(a.state==='work'){
        a.busy-=dt;
        if(a.busy<=0){a.state='idle';setCard(a.name,false,'');}
      }else if(a.state==='walk'){
        a.frame+=dt/90;
        const wp=a.path[0];
        const dx=wp.x-a.pos.x,dy=wp.y-a.pos.y,d=Math.hypot(dx,dy),step=dt*.055;
        if(d<=step){a.pos={...wp};a.path.shift();
          if(!a.path.length){
            if(a.after==='meet'){a.state='meet';a.busy=(a.mkind==='coffee'?3400:(a.mkind==='ask'?2600:1800))+Math.random()*1600;}
            else{a.state='idle';a.pos={...a.door};setCard(a.name,false,'');}
          }
        }else{a.pos.x+=dx/d*step;a.pos.y+=dy/d*step;}
      }else if(a.state==='meet'){
        a.busy-=dt;
        if(a.busy<=0){a.state='walk';a.after='home';a.path=pathTo(a,a.door);}
      }
    });
  }

  function person(x,y,color,opts={}){
    const {walking=false,frame=0,seated=false,carry=false,hair=null,longHair=false}=opts;
    ctx.fillStyle='#1b2b38';
    if(seated){ctx.fillRect(x-4,y-6,8,3);}
    else if(walking){const k=Math.sin(frame*2*Math.PI)*3;
      ctx.fillRect(x-3+k,y-8,3,8);ctx.fillRect(x+1-k,y-8,3,8);}
    else{ctx.fillRect(x-3,y-8,3,8);ctx.fillRect(x+1,y-8,3,8);}
    ctx.fillStyle=color;ctx.shadowColor=color;ctx.shadowBlur=6;
    ctx.fillRect(x-4,y-17,8,10);ctx.shadowBlur=0;
    ctx.fillStyle='#e8f4ff';ctx.fillRect(x-3,y-24,6,6);
    ctx.fillStyle=hair||pa(.8);
    ctx.fillRect(x-3,y-24,6,2.5);
    if(longHair){ctx.fillRect(x-4.5,y-24,1.8,9);ctx.fillRect(x+2.7,y-24,1.8,9);}
    if(carry){ctx.fillStyle='#fff';ctx.fillRect(x+5,y-14,5,7);}
  }

  function windows(x,y,w,h,busy,now){
    const cols=Math.max(2,Math.floor(w/18)),rows=Math.max(2,Math.floor((h-18)/16));
    for(let r=0;r<rows;r++)for(let c=0;c<cols;c++){
      const wx=x-w/2+8+c*(w-20)/Math.max(cols-1,1),wy=y-h+7+r*(h-30)/Math.max(rows-1,1);
      const lit=busy&&Math.sin(now/230+r*3.1+c*5.7)>-.25;
      ctx.fillStyle=lit?'rgba(255,214,120,.95)':'rgba(255,255,255,.07)';
      ctx.fillRect(wx,wy,6,7);
    }
  }
  function smoke(x,y,now,n){
    for(let s=0;s<n;s++){
      const ph=(now/1100+s*.45)%1;
      ctx.fillStyle=`rgba(200,200,210,${.5*(1-ph)})`;
      const r=1.6+ph*3;
      ctx.fillRect(x+Math.sin(now/500+s*2.2)*(2+ph*6)-r/2,y-ph*26-r/2,r,r);
    }
  }
  function building(b,label,busy,now){
    const{x,y,w,h}=b;
    ctx.strokeStyle=pa(.10);ctx.beginPath();ctx.moveTo(x,SIM.roadY);ctx.lineTo(x,y-2);ctx.stroke();
    ctx.fillStyle='rgba(10,6,10,.95)';ctx.strokeStyle=busy?pa(.75):pa(.3);
    if(busy){ctx.shadowColor=`rgb(${CT.p})`;ctx.shadowBlur=12;}
    ctx.fillRect(x-w/2,y-h,w,h);ctx.strokeRect(x-w/2,y-h,w,h);ctx.shadowBlur=0;
    windows(x,y,w,h,busy,now);
    ctx.fillStyle=busy?sa(.95):pa(.3);ctx.fillRect(x-5,y-12,10,12); // door
    switch(b.bkind){
      case 'factory':{
        ctx.fillStyle='#2a151a';ctx.fillRect(x+w/2-15,y-h-16,9,16);
        ctx.strokeStyle=pa(.35);ctx.strokeRect(x+w/2-15,y-h-16,9,16);
        smoke(x+w/2-10,y-h-17,now,busy?4:1);
        // conveyor belt with boxes
        ctx.strokeStyle=pa(.4);ctx.strokeRect(x-w/2-2,y-5,w+4,5);
        if(busy)for(let bx0=0;bx0<3;bx0++){
          const bp=((now/28+bx0*((w+4)/3))%(w+4));
          ctx.fillStyle='#c9a24e';ctx.fillRect(x-w/2-2+bp-3,y-10,6,5);
        }
        break;}
      case 'studio':{
        ctx.strokeStyle=pa(.5);ctx.beginPath();ctx.moveTo(x,y-h);ctx.lineTo(x,y-h-18);ctx.stroke();
        ctx.fillStyle=(Math.sin(now/(busy?160:600))>0)?'#ff4d5e':'#4a1d24';
        ctx.beginPath();ctx.arc(x,y-h-19,2.4,0,7);ctx.fill();
        break;}
      case 'tower':{
        ctx.strokeStyle=pa(.5);ctx.beginPath();ctx.moveTo(x-8,y-h);ctx.lineTo(x,y-h-12);ctx.lineTo(x+8,y-h);ctx.stroke();
        if(busy){ctx.fillStyle='#39ff88';ctx.font='9px SF Mono,monospace';
          for(let g=0;g<2;g++){const gp=(now/900+g*.5)%1;
            ctx.globalAlpha=1-gp;ctx.fillText('$',x+(g?9:-9),y-h-4-gp*18);ctx.globalAlpha=1;}
          ctx.font='8px SF Mono,monospace';}
        break;}
      case 'lab':{
        ctx.strokeStyle=pa(.5);ctx.beginPath();ctx.arc(x,y-h,10,Math.PI,0);ctx.stroke();
        if(busy)for(let g=0;g<3;g++){const gp=(now/800+g*.33)%1;
          ctx.fillStyle=`rgba(57,255,136,${.7*(1-gp)})`;
          ctx.beginPath();ctx.arc(x+Math.sin(g*4+now/400)*5,y-h-4-gp*14,1.5+gp,0,7);ctx.fill();}
        break;}
      case 'post':{
        ctx.strokeStyle=pa(.5);ctx.beginPath();ctx.moveTo(x-w/2+4,y-h);ctx.lineTo(x-w/2+4,y-h-14);ctx.stroke();
        ctx.fillStyle=busy?'#ffc24d':'#7a5d2e';ctx.fillRect(x-w/2+4,y-h-14,8,5);
        ctx.fillStyle='#fff';ctx.fillRect(x+w/2-13,y-h+4,9,6);
        ctx.strokeStyle='#0a141c';ctx.beginPath();ctx.moveTo(x+w/2-13,y-h+4);ctx.lineTo(x+w/2-8.5,y-h+8);ctx.lineTo(x+w/2-4,y-h+4);ctx.stroke();
        break;}
      case 'gate':{
        if(busy&&Math.sin(now/300)>0){ctx.fillStyle='#39ff88';ctx.fillText('✓',x+w/2-8,y-h+10);}
        ctx.fillStyle='#2a151a';
        for(let st=0;st<4;st++){ctx.fillStyle=st%2?sa(.7):'#1a0d10';ctx.fillRect(x-w/2+st*6,y-h-4,6,4);}
        break;}
      case 'hq':{
        ctx.fillStyle=sa(.9);ctx.font='700 10px SF Mono,monospace';ctx.fillText('HQ',x,y-h+13);ctx.font='8px SF Mono,monospace';
        ctx.strokeStyle=sa(.6);ctx.beginPath();ctx.moveTo(x-w/2+6,y-h);ctx.lineTo(x-w/2+6,y-h-16);ctx.stroke();
        ctx.fillStyle=sa(.8);ctx.fillRect(x-w/2+6,y-h-16,10,6);
        break;}
      case 'op':{
        // cozy home: pitched roof + chimney smoke
        ctx.fillStyle='#2a151a';ctx.strokeStyle=pa(.55);
        ctx.beginPath();ctx.moveTo(x-w/2-6,y-h);ctx.lineTo(x,y-h-18);ctx.lineTo(x+w/2+6,y-h);ctx.closePath();ctx.fill();ctx.stroke();
        ctx.fillStyle='#1a0d10';ctx.fillRect(x+w/2-16,y-h-13,7,13);
        ctx.strokeStyle=pa(.4);ctx.strokeRect(x+w/2-16,y-h-13,7,13);
        smoke(x+w/2-12,y-h-14,now,2);
        // small hearts drifting up from the house
        for(let hh=0;hh<3;hh++){
          const hp=(now/1900+hh*.34)%1;
          ctx.fillStyle=`rgba(255,120,150,${.85*(1-hp)})`;
          ctx.font=`700 ${6+hp*4}px SF Mono,monospace`;
          ctx.fillText('♥',x+Math.sin(now/620+hh*2.1)*(3+hp*7),y-h-6-hp*30);
        }
        ctx.font='8px SF Mono,monospace';
        // the residents, out front receiving reports
        person(x+16,y,'#4da3ff',{hair:'#2a1c12'});
        person(x+27,y,'#ff8fb3',{hair:'#ffd678',longHair:true});
        // operator mailbox + waiting "?" when queue open
        ctx.fillStyle='#1a2a38';ctx.fillRect(x-w/2-14,y-10,9,7);
        ctx.strokeStyle=pa(.4);ctx.strokeRect(x-w/2-14,y-10,9,7);
        ctx.strokeStyle=pa(.3);ctx.beginPath();ctx.moveTo(x-w/2-10,y-3);ctx.lineTo(x-w/2-10,y);ctx.stroke();
        if(QUEUE&&QUEUE.open>0){
          ctx.fillStyle='#ffc24d';ctx.font='700 11px SF Mono,monospace';
          ctx.fillText('?',x,y-b.h-26+Math.sin(now/300)*2);ctx.font='8px SF Mono,monospace';
          ctx.fillStyle='#ffc24d';ctx.fillText(QUEUE.open+' WAITING',x,y+22);
        }
        break;}
    }
    ctx.fillStyle=busy?'#ffe3c9':'#8a5d64';ctx.fillText(label,x,y+12);
  }

  function draw(){
    const now=performance.now(),dt=Math.min(now-SIM.last,80);SIM.last=now;update(dt);
    ctx.clearRect(0,0,SIM.W,SIM.H);
    // ground grid
    ctx.strokeStyle=pa(.05);
    for(let y=40;y<SIM.H;y+=40){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(SIM.W,y);ctx.stroke();}
    for(let x=40;x<SIM.W;x+=40){ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,SIM.H);ctx.stroke();}
    ctx.font='8px SF Mono,monospace';ctx.textAlign='center';
    // main street
    ctx.fillStyle='rgba(255,255,255,.03)';ctx.fillRect(0,SIM.roadY-13,SIM.W,26);
    ctx.strokeStyle=pa(.25);
    ctx.beginPath();ctx.moveTo(0,SIM.roadY-13);ctx.lineTo(SIM.W,SIM.roadY-13);ctx.stroke();
    ctx.beginPath();ctx.moveTo(0,SIM.roadY+13);ctx.lineTo(SIM.W,SIM.roadY+13);ctx.stroke();
    ctx.setLineDash([8,10]);ctx.strokeStyle=pa(.2);
    ctx.beginPath();ctx.moveTo(0,SIM.roadY);ctx.lineTo(SIM.W,SIM.roadY);ctx.stroke();ctx.setLineDash([]);
    // streetlights
    SIM.lights.forEach(l=>{
      ctx.strokeStyle=pa(.3);ctx.beginPath();ctx.moveTo(l.x,l.y-13);ctx.lineTo(l.x,l.y-30);ctx.stroke();
      ctx.fillStyle='rgba(255,214,120,.9)';ctx.shadowColor='#ffd678';ctx.shadowBlur=10;
      ctx.fillRect(l.x-2,l.y-33,4,3);ctx.shadowBlur=0;
    });
    // café (top-left kiosk)
    const cf=SIM.coffee;
    ctx.fillStyle='#1a0d10';ctx.strokeStyle=pa(.45);
    ctx.fillRect(cf.x-14,cf.y-34,28,34);ctx.strokeRect(cf.x-14,cf.y-34,28,34);
    ctx.fillStyle=sa(.9);ctx.fillRect(cf.x-9,cf.y-28,18,6);
    ctx.fillStyle='#fff';ctx.fillRect(cf.x-3,cf.y-14,6,5);
    const atCoffee=SIM.agents.some(a=>a.state==='meet'&&a.mkind==='coffee');
    smoke(cf.x,cf.y-15,now,atCoffee?3:1);
    ctx.fillStyle='#8a5d64';ctx.fillText('CAFÉ',cf.x,cf.y+12);
    // operator HQ
    building(SIM.operator,'HOME',SIM.agents.some(a=>a.state==='meet'&&a.mkind==='ask'),now);
    // chief HQ
    const meeting=SIM.agents.some(a=>a.state==='meet'&&a.mkind==='chief');
    building(SIM.chief,'CHIEF OF STAFF · HQ',true&&meeting,now);
    person(SIM.chief.x-20,SIM.chief.y-2,CT.sc,{seated:false});
    // power plant
    const rk=SIM.plant;
    ctx.fillStyle='#140a0c';ctx.strokeStyle=pa(.5);
    ctx.fillRect(rk.x-18,rk.y-52,36,52);ctx.strokeRect(rk.x-18,rk.y-52,36,52);
    for(let ry=0;ry<4;ry++){
      ctx.strokeStyle=pa(.25);ctx.strokeRect(rk.x-14,rk.y-48+ry*12,28,9);
      for(let lx=0;lx<3;lx++){
        const on=Math.sin(now/130+ry*2.4+lx*4.1)>((ry+lx)%2?0:.4);
        ctx.fillStyle=on?(lx===2?'#39ff88':CT.pc):'#2a1518';
        ctx.fillRect(rk.x-11+lx*9,rk.y-46+ry*12,4,3);
      }
    }
    ctx.fillStyle='#ffd678';ctx.font='700 10px SF Mono,monospace';ctx.fillText('⚡',rk.x,rk.y-55);ctx.font='8px SF Mono,monospace';
    ctx.fillStyle='#8a5d64';ctx.fillText('POWER PLANT',rk.x,rk.y+12);
    // trees along the street
    [[SIM.W*.08,SIM.roadY+34],[SIM.W*.5,SIM.roadY+34],[SIM.W*.97,SIM.roadY+34]].forEach(([px,py])=>{
      ctx.fillStyle='#3a1518';ctx.fillRect(px-1.5,py-8,3,8);
      ctx.fillStyle='#2f9e5f';ctx.beginPath();ctx.arc(px,py-12,6,0,7);ctx.fill();
    });
    // delivery drone
    const dr=SIM.drone;
    if(!dr.active&&now>dr.next){dr.active=true;dr.x=-40;}
    if(dr.active){
      dr.x+=dt*.09;
      if(dr.x>SIM.W+40){dr.active=false;dr.next=now+14000+Math.random()*18000;}
      const dy=16+Math.sin(now/260)*2.5;
      ctx.fillStyle='#33161a';ctx.fillRect(dr.x-7,dy,14,5);
      const sp=Math.sin(now/40)>0;
      ctx.strokeStyle='rgba(255,255,255,.5)';
      ctx.beginPath();ctx.moveTo(dr.x-(sp?11:9),dy-2);ctx.lineTo(dr.x-3,dy-2);ctx.moveTo(dr.x+3,dy-2);ctx.lineTo(dr.x+(sp?11:9),dy-2);ctx.stroke();
      ctx.fillStyle=Math.sin(now/180)>0?CT.pc:'#5a1d24';ctx.fillRect(dr.x-1,dy+1,2,2);
      ctx.fillStyle='#fff';ctx.fillRect(dr.x-3,dy+7,6,5);
      ctx.strokeStyle='rgba(255,255,255,.4)';ctx.beginPath();ctx.moveTo(dr.x-3,dy+5);ctx.lineTo(dr.x-3,dy+7);ctx.moveTo(dr.x+3,dy+5);ctx.lineTo(dr.x+3,dy+7);ctx.stroke();
    }
    // dept buildings + agents (painter's order by y)
    SIM.agents.forEach(a=>building(a.home,a.name.toUpperCase(),a.state==='work',now));
    const sorted=[...SIM.agents].sort((a,b)=>a.pos.y-b.pos.y);
    sorted.forEach(a=>{
      if(a.state==='work')return; // inside the building
      if(a.state==='idle'){
        person(a.door.x+12,a.door.y,a.color,{});
        return;
      }
      const carry=a.mkind==='chief'||a.mkind==='ask';
      person(a.pos.x,a.pos.y,a.color,{walking:a.state==='walk',frame:a.frame,carry});
      if(a.state==='meet'){
        if(a.mkind==='coffee'){
          ctx.fillStyle='#fff';ctx.fillRect(a.pos.x+5,a.pos.y-15,4,4);
          ctx.fillStyle=`rgba(255,255,255,${.4+.3*Math.sin(now/300)})`;
          ctx.fillRect(a.pos.x+6,a.pos.y-20,1.5,1.5);
        }else if(a.mkind==='ask'){
          ctx.fillStyle='#ffc24d';ctx.font='700 10px SF Mono,monospace';
          ctx.fillText('?',a.pos.x,a.pos.y-30+Math.sin(now/280)*1.5);ctx.font='8px SF Mono,monospace';
        }else{
          ctx.fillStyle='rgba(255,255,255,.85)';
          for(let b=0;b<3;b++){
            const on=Math.floor(now/350)%3===b;
            ctx.globalAlpha=on?1:.3;
            ctx.fillRect(a.pos.x-5+b*5,a.pos.y-32,2.5,2.5);
          }
          ctx.globalAlpha=1;
        }
      }
      ctx.fillStyle='#ffe3e6';ctx.fillText(a.name.toUpperCase(),a.pos.x,a.pos.y+11);
    });
    requestAnimationFrame(draw);
  }
  requestAnimationFrame(draw);
}

// ---- brain: force-directed vault graph
let BRAIN=null;
async function initBrain(){
  if(BRAIN)return;
  BRAIN={nodes:[],edges:[],adj:{},hover:-1,drag:-1,W:0,H:520};
  const g=await (await fetch('/api/graph')).json();
  const cv=$('brain'),ctx=cv.getContext('2d');
  const folders=[...new Set(g.nodes.map(n=>n.folder))];
  const fcolor=f=>CT.shirts[folders.indexOf(f)%CT.shirts.length];
  $('n-notes').textContent=g.nodes.length;
  $('brain-legend').innerHTML=folders.map(f=>`<span class="lgd"><i style="background:${fcolor(f)};box-shadow:0 0 6px ${fcolor(f)}"></i>${esc(f)} <em>${g.nodes.filter(n=>n.folder===f).length}</em></span>`).join('');
  function size(){
    BRAIN.W=cv.clientWidth||900;
    cv.width=BRAIN.W*devicePixelRatio;cv.height=BRAIN.H*devicePixelRatio;
    cv.style.height=BRAIN.H+'px';
    ctx.setTransform(devicePixelRatio,0,0,devicePixelRatio,0,0);
  }
  size();window.addEventListener('resize',size);
  BRAIN.now=0;
  BRAIN.nodes=g.nodes.map((n,i)=>({...n,deg:0,ph:Math.random()*6.283,
    x:BRAIN.W/2+Math.cos(i*2.399)*(30+i*2.2),y:BRAIN.H/2+Math.sin(i*2.399)*(24+i*1.6),vx:0,vy:0}));
  BRAIN.edges=g.edges;
  g.edges.forEach(([a,b])=>{
    BRAIN.nodes[a].deg++;BRAIN.nodes[b].deg++;
    (BRAIN.adj[a]=BRAIN.adj[a]||new Set()).add(b);
    (BRAIN.adj[b]=BRAIN.adj[b]||new Set()).add(a);
  });
  BRAIN.view={s:1,tx:0,ty:0}; // auto-fit camera
  const scr=n=>({ // gentle idle drift keeps the map feeling alive (visual only)
    x:n.x*BRAIN.view.s+BRAIN.view.tx+Math.sin(BRAIN.now/1400+n.ph)*1.6,
    y:n.y*BRAIN.view.s+BRAIN.view.ty+Math.cos(BRAIN.now/1700+n.ph)*1.6});
  const pos=e=>{const r=cv.getBoundingClientRect();return{x:e.clientX-r.left,y:e.clientY-r.top};};
  const pick=p=>{let best=-1,bd=220;BRAIN.nodes.forEach((n,i)=>{const s=scr(n),d=(s.x-p.x)**2+(s.y-p.y)**2;if(d<bd){bd=d;best=i;}});return best;};
  cv.addEventListener('mousemove',e=>{
    const p=pos(e);
    if(BRAIN.drag>=0){const n=BRAIN.nodes[BRAIN.drag],v=BRAIN.view;
      const wx=(p.x-v.tx)/v.s,wy=(p.y-v.ty)/v.s;
      n.x=wx;n.y=wy;n.vx=n.vy=0;
      if(BRAIN.sim){n.fx=wx;n.fy=wy;BRAIN.sim.alpha(Math.max(BRAIN.sim.alpha(),.25));}
      else BRAIN.alpha=Math.max(BRAIN.alpha,.25);}
    else BRAIN.hover=pick(p);
  });
  cv.addEventListener('mousedown',e=>{BRAIN.drag=pick(pos(e));cv.classList.add('grabbing');});
  window.addEventListener('mouseup',()=>{
    if(BRAIN.drag>=0&&BRAIN.sim){const n=BRAIN.nodes[BRAIN.drag];n.fx=n.fy=null;}
    BRAIN.drag=-1;cv.classList.remove('grabbing');});
  cv.addEventListener('mouseleave',()=>{BRAIN.hover=-1;});
  // d3-force when available (natural Obsidian-style layout); custom fallback otherwise
  if(window.d3){
    BRAIN.links=BRAIN.edges.map(([a,b])=>({source:a,target:b}));
    BRAIN.sim=d3.forceSimulation(BRAIN.nodes)
      .force('link',d3.forceLink(BRAIN.links).distance(l=>50+Math.min((l.source.deg+l.target.deg)*1.2,40)).strength(.5))
      .force('charge',d3.forceManyBody().strength(-160).distanceMax(320))
      .force('x',d3.forceX(BRAIN.W/2).strength(.06))
      .force('y',d3.forceY(BRAIN.H/2).strength(.11))
      .force('collide',d3.forceCollide(n=>9+Math.min(n.deg*.9,8)))
      .stop();
  }
  BRAIN.alpha=1; // cooling: sim settles and freezes
  function step(){
    if(BRAIN.sim){if(BRAIN.sim.alpha()>BRAIN.sim.alphaMin())BRAIN.sim.tick();return;}
    if(BRAIN.alpha<=0.003)return; // frozen — no jitter
    const al=BRAIN.alpha;
    const N=BRAIN.nodes,cx=BRAIN.W/2,cy=BRAIN.H/2;
    for(let i=0;i<N.length;i++){
      const a=N[i];
      a.vx+=(cx-a.x)*.0028*al;a.vy+=(cy-a.y)*.0028*al; // strong centering
      for(let j=i+1;j<N.length;j++){ // repulsion (short range)
        const b=N[j];let dx=a.x-b.x,dy=a.y-b.y,d2=dx*dx+dy*dy||1;
        if(d2<19600){const f=850*al/d2;dx*=f;dy*=f;a.vx+=dx;a.vy+=dy;b.vx-=dx;b.vy-=dy;}
      }
    }
    BRAIN.edges.forEach(([i,j])=>{ // springs
      const a=N[i],b=N[j],dx=b.x-a.x,dy=b.y-a.y,d=Math.hypot(dx,dy)||1;
      const f=(d-70)*.006*al;
      a.vx+=dx/d*f;a.vy+=dy/d*f;b.vx-=dx/d*f;b.vy-=dy/d*f;
    });
    N.forEach(n=>{
      n.vx*=.7;n.vy*=.7; // heavy damping
      const sp=Math.hypot(n.vx,n.vy);if(sp>3){n.vx*=3/sp;n.vy*=3/sp;} // speed cap
      n.x+=n.vx;n.y+=n.vy; // unbounded — camera auto-fits
    });
    BRAIN.alpha*=.99;
  }
  function draw(){
    if(!$('tab-brain').classList.contains('on')){requestAnimationFrame(draw);return;}
    BRAIN.now=performance.now();
    step();
    // auto-fit camera (smoothed)
    const xs=BRAIN.nodes.map(n=>n.x),ys=BRAIN.nodes.map(n=>n.y);
    const x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys),y1=Math.max(...ys);
    const ts=Math.min((BRAIN.W-110)/Math.max(x1-x0,1),(BRAIN.H-90)/Math.max(y1-y0,1),1.5);
    const ttx=BRAIN.W/2-ts*(x0+x1)/2,tty=BRAIN.H/2-ts*(y0+y1)/2;
    const v=BRAIN.view;v.s+=(ts-v.s)*.08;v.tx+=(ttx-v.tx)*.08;v.ty+=(tty-v.ty)*.08;
    ctx.clearRect(0,0,BRAIN.W,BRAIN.H);
    // faint radial backdrop
    const bgr=ctx.createRadialGradient(BRAIN.W/2,BRAIN.H/2,40,BRAIN.W/2,BRAIN.H/2,Math.max(BRAIN.W,BRAIN.H)*.7);
    bgr.addColorStop(0,pa(.055));bgr.addColorStop(1,'rgba(0,0,0,0)');
    ctx.fillStyle=bgr;ctx.fillRect(0,0,BRAIN.W,BRAIN.H);
    const hov=BRAIN.drag>=0?BRAIN.drag:BRAIN.hover;
    const nb=hov>=0?(BRAIN.adj[hov]||new Set()):null;
    BRAIN.edges.forEach(([i,j])=>{
      const hot=hov>=0&&(i===hov||j===hov);
      const A=scr(BRAIN.nodes[i]),B=scr(BRAIN.nodes[j]);
      const ex=B.x-A.x,ey=B.y-A.y,el=Math.hypot(ex,ey)||1;
      const off=Math.min(el*.13,24); // curved edges
      ctx.strokeStyle=hot?pa(.8):pa(.14);
      ctx.lineWidth=hot?1.5:1;
      if(hot){ctx.shadowColor=`rgb(${CT.p})`;ctx.shadowBlur=6;}
      ctx.beginPath();ctx.moveTo(A.x,A.y);
      ctx.quadraticCurveTo((A.x+B.x)/2-ey/el*off,(A.y+B.y)/2+ex/el*off,B.x,B.y);
      ctx.stroke();ctx.shadowBlur=0;
    });
    ctx.font='9px SF Mono,monospace';ctx.textAlign='center';
    BRAIN.nodes.forEach((n,i)=>{
      const s=scr(n);
      const pulse=n.deg>=3?Math.sin(BRAIN.now/750+n.ph)*.7:0; // hubs breathe
      const r=3.5+Math.min(n.deg*1.15,11)+pulse;
      const lit=hov<0||i===hov||(nb&&nb.has(i));
      ctx.globalAlpha=lit?1:.18;
      ctx.fillStyle=fcolor(n.folder);
      ctx.shadowColor=fcolor(n.folder);ctx.shadowBlur=i===hov?20:(n.deg>=5?14:8);
      ctx.beginPath();ctx.arc(s.x,s.y,i===hov?r+2:r,0,7);ctx.fill();ctx.shadowBlur=0;
      if(i===hov){ // halo ring on hover
        ctx.strokeStyle=fcolor(n.folder);ctx.globalAlpha=.5;
        ctx.beginPath();ctx.arc(s.x,s.y,r+6,0,7);ctx.stroke();ctx.globalAlpha=lit?1:.18;
      }
      if(n.deg>=3||i===hov||(nb&&nb.has(i))){
        ctx.fillStyle=i===hov?'#fff':'rgba(255,255,255,.55)';
        ctx.fillText(n.name,s.x,s.y-r-6);
      }
      ctx.globalAlpha=1;
    });
    requestAnimationFrame(draw);
  }
  requestAnimationFrame(draw);
}

$('theme-btn').addEventListener('click',()=>{
  const order=['crimson','ember','cyan'];
  const cur=localStorage.getItem('cockpit-theme')||'crimson';
  applyTheme(order[(order.indexOf(cur)+1)%order.length]);
});
applyTheme(localStorage.getItem('cockpit-theme')||'crimson');
load();proxy();clock();initBrain();loadQueue();setInterval(loadQueue,30000);
</script>
</body></html>"""


# ---------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/":
            # hot-reload: re-read this file so UI edits show on browser refresh
            # (no server restart needed); falls back to the baked-in HTML.
            html = HTML
            try:
                with open(os.path.abspath(__file__), encoding="utf-8") as f:
                    src = f.read()
                i = src.index('HTML = r"""') + len('HTML = r"""')
                html = src[i:src.index('"""', i)]
            except (OSError, ValueError):
                pass
            self.send(200, html, "text/html")
        elif p == "/api/vault":
            self.send(200, json.dumps(vault_payload()))
        elif p == "/api/proxy":
            self.send(200, json.dumps(proxy_status()))
        elif p == "/api/agents":
            self.send(200, json.dumps(parse_agents()))
        elif p == "/api/queue":
            self.send(200, json.dumps(parse_queue()))
        elif p == "/api/graph":
            self.send(200, json.dumps(parse_graph()))
        else:
            self.send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        p = urlparse(self.path).path
        if p in ("/api/chat", "/api/answer"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self.send(400, json.dumps({"ok": False, "error": "bad request"}))
                return
            fn = chat if p == "/api/chat" else answer_queue
            self.send(200, json.dumps(fn(payload)))
        else:
            self.send(404, json.dumps({"error": "not found"}))


if __name__ == "__main__":
    url = f"http://127.0.0.1:{PORT}"
    if os.environ.get("COCKPIT_NO_BROWSER") != "1":
        import subprocess
        import threading
        import webbrowser

        def open_ui():
            chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            if os.path.exists(chrome):  # standalone app window, no tabs/URL bar
                subprocess.Popen([chrome, f"--app={url}"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                webbrowser.open(url)
        threading.Timer(0.6, open_ui).start()
    print(f"Cockpit → {url}   (vault: {VAULT})")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except OSError:
        print("Cockpit already running; opened browser.")
