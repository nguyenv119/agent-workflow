#!/usr/bin/env bash
# Per-session concept graph: Mermaid source -> live HTML viewer.
# graph.mmd is the editable source of truth. viewer.html fetches it and
# re-renders ONLY when the text changed, so there's no reload/flicker and
# scroll+zoom are preserved. Served over http via python3's stdlib server
# (file:// can't fetch a sibling file); one server covers every session.
set -euo pipefail

PORT="${GRAPH_PORT:-7317}"
ROOT="$HOME/.claude/graphs"
SID="${CLAUDE_CODE_SESSION_ID:-default}"
DIR="$ROOT/$SID"
MMD="$DIR/graph.mmd"
HTML="$DIR/viewer.html"
URL="http://localhost:$PORT/$SID/viewer.html"
mkdir -p "$DIR"

seed() { [ -s "$MMD" ] || printf 'graph TD\n' > "$MMD"; }

build() {
  seed
  # Tab title + favicon come from optional comment lines in graph.mmd:
  #   %% title: <short summary>     %% icon: <emoji>
  local title icon
  title=$(grep -m1 '^%%[[:space:]]*title:' "$MMD" | sed 's/^%%[[:space:]]*title:[[:space:]]*//')
  icon=$(grep -m1 '^%%[[:space:]]*icon:' "$MMD" | sed 's/^%%[[:space:]]*icon:[[:space:]]*//')
  [ -n "$title" ] || title="Session concept graph"
  [ -n "$icon" ] || icon="🧠"
  {
    cat <<'A'
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
A
    printf '<link rel="icon" href="data:image/svg+xml,<svg xmlns=%%27http://www.w3.org/2000/svg%%27 viewBox=%%270 0 100 100%%27><text y=%%27.9em%%27 font-size=%%2790%%27>%s</text></svg>">\n' "$icon"
    printf '<title>%s</title>\n' "$title"
    cat <<'B'
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<style>
  :root{color-scheme:dark}
  html,body{margin:0;height:100%;background:#0d1117;color:#c9d1d9;
    font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
  header{padding:8px 14px;border-bottom:1px solid #21262d;color:#8b949e;
    display:flex;gap:12px;align-items:center;position:sticky;top:0;background:#0d1117}
  header b{color:#c9d1d9}
  #g{padding:20px;display:flex;justify-content:center}
  svg{max-width:none!important}
</style>
</head><body>
B
    printf '<header><b>%s&nbsp;%s</b><span>live · edit graph.mmd or say /graph …</span></header>\n' "$icon" "$title"
    cat <<'C'
<div id="g">loading…</div>
<script>
  mermaid.initialize({startOnLoad:false,theme:'dark',securityLevel:'loose'});
  let last=null,n=0;
  async function tick(){
    try{
      const txt=await (await fetch('graph.mmd',{cache:'no-store'})).text();
      if(txt!==last){
        last=txt;
        const {svg}=await mermaid.render('m'+(n++),txt);
        document.getElementById('g').innerHTML=svg;
      }
    }catch(e){/* transient: keep last render, retry next tick */}
  }
  tick(); setInterval(tick,1500);
</script>
</body></html>
C
  } > "$HTML"
  echo "$HTML"
}

serve() {
  if ! curl -sf "http://localhost:$PORT/" >/dev/null 2>&1; then
    command -v python3 >/dev/null || { echo "python3 not found; open file://$HTML" >&2; return 1; }
    nohup python3 -m http.server "$PORT" --directory "$ROOT" >/dev/null 2>&1 &
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      curl -sf "http://localhost:$PORT/" >/dev/null 2>&1 && break; sleep 0.2
    done
  fi
}

case "${1:-path}" in
  path)  seed; echo "$MMD" ;;
  init)  seed; echo "$MMD" ;;
  build) build >/dev/null; echo "$HTML" ;;
  serve) build >/dev/null; serve; echo "$URL" ;;
  open)  build >/dev/null; serve || true; open "$URL" 2>/dev/null || true; echo "$URL" ;;
  test)
    T="$(mktemp -d)"
    printf 'graph TD\n%%%% title: My Test Graph\n%%%% icon: 🚀\n  a-->b\n' \
      > /dev/null # (doc only)
    mkdir -p "$T/.claude/graphs/__t"
    printf 'graph TD\n%%%% title: My Test Graph\n%%%% icon: 🚀\n  a-->b\n' \
      > "$T/.claude/graphs/__t/graph.mmd"
    CLAUDE_CODE_SESSION_ID="__t" HOME="$T" bash "$0" build >/dev/null
    H="$T/.claude/graphs/__t/viewer.html"
    grep -q "fetch('graph.mmd'" "$H" \
      && grep -q 'mermaid@11' "$H" \
      && grep -q 'mermaid.render' "$H" \
      && grep -q '<title>My Test Graph</title>' "$H" \
      && grep -q '🚀' "$H" \
      && echo "ok: viewer fetches+re-renders, title+icon injected" || { echo "FAIL"; exit 1; }
    rm -rf "$T" ;;
  *) echo "usage: graph.sh {path|init|build|serve|open|test}" >&2; exit 1 ;;
esac
