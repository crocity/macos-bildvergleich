#!/usr/bin/env python3
"""Bedienbruecke fuer den Leih-Mac (GitHub-Laeufer), 2. Fassung: fluessiger Bildstrom + Maus (klicken, ziehen in
Echtzeit, Mausrad) + Tasten + Text + Bildschirmfoto in voller Schaerfe. Laeuft auf dem Mac in der angemeldeten
Sitzung. Nimmt nur Anfragen mit der Kopfzeile X-Bruecke an. Schreibt nichts mit."""
import json, os, re, shutil, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

CLICLICK = next((p for p in ("/opt/homebrew/bin/cliclick", "/usr/local/bin/cliclick") if os.path.exists(p)), "cliclick")
FFMPEG = shutil.which("ffmpeg") or next((p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg") if os.path.exists(p)), None)
SCHLUESSEL = os.environ.get("BRUECKE_SCHLUESSEL", "")
sperre = threading.Lock()
try:
    import Quartz
except Exception:
    Quartz = None

def punkte():
    try:
        t = subprocess.run(["osascript", "-e", 'tell application "Finder" to get bounds of window of desktop'],
                           capture_output=True, text=True, timeout=10).stdout
        z = [int(x) for x in t.replace(" ", "").strip().split(",")]
        return z[2], z[3]
    except Exception:
        return 1920, 1080
BREITE, HOEHE = punkte()

class Strom:
    """Haelt immer das neueste JPEG-Bild bereit. Quelle: ffmpeg (fluessig) oder screencapture (langsam)."""
    def __init__(self):
        self.bild = b""; self.nr = 0; self.art = "?"; self.bps = 0.0; self.neu = threading.Condition()
        threading.Thread(target=self.lauf, daemon=True).start()
    def setze(self, b):
        with self.neu:
            self.bild = b; self.nr += 1; self.neu.notify_all()
    def lauf(self):
        if FFMPEG:
            try:
                self.ffmpeg(); return
            except Exception as e:
                print("ffmpeg-Strom gescheitert:", e, flush=True)
        self.langsam()
    def ffmpeg(self):
        l = subprocess.run([FFMPEG, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""], capture_output=True, text=True).stderr
        m = re.search(r"\[(\d+)\] Capture screen 0", l)
        if not m: raise RuntimeError("kein Bildschirm in der Geraeteliste")
        p = subprocess.Popen([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "avfoundation", "-capture_cursor", "1", "-framerate", "20",
                              "-pixel_format", "bgr0", "-i", m.group(1) + ":none", "-vf", "fps=15,scale=1920:-2", "-q:v", "7",
                              "-f", "image2pipe", "-c:v", "mjpeg", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.art = "ffmpeg"; puffer = b""; t0 = time.time(); n = 0
        while True:
            st = p.stdout.read(65536)
            if not st:
                raise RuntimeError("ffmpeg beendet: " + p.stderr.read().decode(errors="replace")[-300:])
            puffer += st
            while True:
                a = puffer.find(b"\xff\xd8"); e = puffer.find(b"\xff\xd9", a + 2)
                if a < 0 or e < 0: break
                self.setze(puffer[a:e + 2]); puffer = puffer[e + 2:]; n += 1
                if time.time() - t0 > 3: self.bps = n / (time.time() - t0); t0 = time.time(); n = 0
    def langsam(self):
        self.art = "screencapture"; t0 = time.time(); n = 0
        while True:
            subprocess.run(["screencapture", "-x", "-C", "-t", "jpg", "/tmp/macbruecke.jpg"], timeout=15)
            try: self.setze(open("/tmp/macbruecke.jpg", "rb").read())
            except Exception: pass
            n += 1
            if time.time() - t0 > 3: self.bps = n / (time.time() - t0); t0 = time.time(); n = 0
strom = Strom()

SEITE = """<!doctype html><meta charset=utf-8><title>Leih-Mac</title>
<style>*{box-sizing:border-box}body{margin:0;background:#101013;color:#ddd;font:13px system-ui;height:100vh;display:flex;flex-direction:column;overflow:hidden}
#leiste{display:flex;gap:6px;align-items:center;padding:6px 8px;background:#19191d;flex:none;flex-wrap:wrap}
button{font:inherit;padding:5px 9px;border-radius:7px;border:1px solid #3a3a40;background:#232328;color:#eee;cursor:pointer}button:hover{background:#2e2e35}
#st{padding:4px 9px;border-radius:7px;background:#1f3a2c;color:#9be7b7}#s{color:#888;margin-left:auto}
#rahmen{flex:1;min-height:0;display:grid;place-items:center;background:#000}
#b{max-width:100%;max-height:100%;cursor:crosshair;user-select:none;-webkit-user-drag:none;outline:none}</style>
<div id=leiste><b>Leih-Mac · macOS 27</b><span id=st>verbindet…</span>
<button onclick="taste('cmd+space')">Suche ⌘␣</button><button onclick="taste('cmd+tab')">Wechseln ⌘⇥</button>
<button onclick="taste('cmd+q')">Beenden ⌘Q</button><button onclick="taste('cmd+w')">Schließen ⌘W</button><button onclick="taste('esc')">Esc</button>
<button id=fb onclick="foto()">📷 Bildschirmfoto</button><span id=s>Klick, Ziehen, Mausrad und Tastatur gehen an den Mac. Strg+Taste = ⌘+Taste.</span></div>
<div id=rahmen><img id=b src=/strom draggable=false tabindex=0></div>
<script>
const b=document.getElementById('b'),s=document.getElementById('s'),st=document.getElementById('st');
let kette=Promise.resolve();const hin=u=>{kette=kette.then(()=>fetch(u).catch(()=>{}));return kette};
function pos(e){const r=b.getBoundingClientRect();return[Math.min(1,Math.max(0,(e.clientX-r.left)/r.width)),Math.min(1,Math.max(0,(e.clientY-r.top)/r.height))]}
let a=null,zieht=false,letzt=0;
b.addEventListener('contextmenu',e=>{e.preventDefault();const p=pos(e);hin(`/klick?x=${p[0]}&y=${p[1]}&art=rechts`)});
b.addEventListener('pointerdown',e=>{if(e.button!==0)return;b.focus();b.setPointerCapture(e.pointerId);a=pos(e);zieht=false;e.preventDefault()});
b.addEventListener('pointermove',e=>{const p=pos(e);if(a){if(!zieht&&Math.hypot((p[0]-a[0])*b.width,(p[1]-a[1])*b.height)>5){zieht=true;hin(`/maus?art=ab&x=${a[0]}&y=${a[1]}`)}
  if(zieht&&Date.now()-letzt>45){letzt=Date.now();hin(`/maus?art=zieh&x=${p[0]}&y=${p[1]}`)}}
  else if(Date.now()-letzt>90){letzt=Date.now();fetch(`/maus?art=hin&x=${p[0]}&y=${p[1]}`).catch(()=>{})}});
b.addEventListener('pointerup',e=>{if(e.button!==0||!a)return;const p=pos(e);if(zieht)hin(`/maus?art=los&x=${p[0]}&y=${p[1]}`);else hin(`/klick?x=${a[0]}&y=${a[1]}&art=${e.detail>=2?'doppelt':'links'}`);a=null;zieht=false});
let rad=0,radT=null;b.addEventListener('wheel',e=>{e.preventDefault();rad+=e.deltaY;clearTimeout(radT);radT=setTimeout(()=>{const p=pos(e);hin(`/rad?dy=${Math.round(-rad/3)}&x=${p[0]}&y=${p[1]}`);rad=0},40)},{passive:false});
function taste(k){return hin('/taste?k='+encodeURIComponent(k))}
let puffer='',t=null;function leeren(){const x=puffer;puffer='';if(!x)return;kette=kette.then(()=>fetch('/text',{method:'POST',body:x}).catch(()=>{}))}
const SONDER={Enter:'return',Backspace:'delete',Tab:'tab',Escape:'esc',ArrowLeft:'arrow-left',ArrowRight:'arrow-right',ArrowUp:'arrow-up',ArrowDown:'arrow-down',Delete:'fwd-delete',Home:'home',End:'end',PageUp:'page-up',PageDown:'page-down',F1:'f1',F2:'f2',F3:'f3',F4:'f4',F5:'f5'};
document.addEventListener('keydown',e=>{if(e.target.tagName==='BUTTON')return;
 const m=[];if(e.ctrlKey||e.metaKey)m.push('cmd');if(e.altKey)m.push('alt');if(e.shiftKey&&(e.ctrlKey||e.metaKey||e.altKey||SONDER[e.key]))m.push('shift');
 if(SONDER[e.key]){e.preventDefault();leeren();taste([...m,SONDER[e.key]].join('+'));return}
 if(e.key===' '&&m.length){e.preventDefault();leeren();taste([...m,'space'].join('+'));return}
 if(e.key.length===1&&(e.ctrlKey||e.metaKey)){e.preventDefault();leeren();taste([...m,e.key.toLowerCase()].join('+'));return}
 if(e.key.length===1){e.preventDefault();puffer+=e.key;clearTimeout(t);t=setTimeout(leeren,120)}});
let fotoLaeuft=false;async function foto(){if(fotoLaeuft)return;fotoLaeuft=true;fb.disabled=true;try{const r=await fetch('/foto');const bl=await r.blob();const u=URL.createObjectURL(bl);const l=document.createElement('a');l.href=u;l.download='leih-mac-'+new Date().toISOString().slice(11,19).replace(/:/g,'')+'.png';l.click();setTimeout(()=>URL.revokeObjectURL(u),4000);s.textContent='Bildschirmfoto gespeichert (Downloads)'}finally{setTimeout(()=>{fotoLaeuft=false;fb.disabled=false},800)}}
setInterval(async()=>{try{const j=await(await fetch('/stand')).json();st.textContent='verbunden · '+j.bps.toFixed(0)+' Bilder/s';st.style.background='#1f3a2c'}catch(e){st.textContent='keine Verbindung';st.style.background='#4a2222'}},4000);
</script>"""

def cc(*teile):
    with sperre:
        subprocess.run([CLICLICK, "-e", "20", *teile], timeout=30)

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _ok(self, body=b"ok", typ="text/plain"):
        self.send_response(200); self.send_header("Content-Type", typ); self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _darf(self):
        if not SCHLUESSEL or self.headers.get("X-Bruecke", "") == SCHLUESSEL: return True
        self.send_response(403); self.send_header("Content-Length", "0"); self.end_headers(); return False
    def do_GET(self):
        if not self._darf(): return
        u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/": return self._ok(SEITE.encode(), "text/html; charset=utf-8")
            if u.path == "/bild": return self._ok(strom.bild, "image/jpeg")
            if u.path == "/foto":
                with sperre: subprocess.run(["screencapture", "-x", "-C", "-t", "png", "/tmp/macbruecke-foto.png"], timeout=20)
                return self._ok(open("/tmp/macbruecke-foto.png", "rb").read(), "image/png")
            if u.path == "/strom":
                self.send_response(200); self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=bild")
                self.send_header("Cache-Control", "no-store"); self.send_header("Connection", "close"); self.end_headers(); nr = -1
                while True:
                    with strom.neu:
                        strom.neu.wait_for(lambda: strom.nr != nr, timeout=5); nr = strom.nr; b = strom.bild
                    if b: self.wfile.write(b"--bild\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(b)).encode() + b"\r\n\r\n" + b + b"\r\n"); self.wfile.flush()
            if u.path == "/stand": return self._ok(json.dumps({"breite": BREITE, "hoehe": HOEHE, "quelle": strom.art, "bps": strom.bps, "quartz": bool(Quartz), "ffmpeg": bool(FFMPEG)}).encode(), "application/json")
            def p(kx="x", ky="y"): return f"{int(float(q[kx]) * BREITE)},{int(float(q[ky]) * HOEHE)}"
            if u.path == "/klick":
                art = {"links": "c", "doppelt": "dc", "rechts": "rc"}.get(q.get("art", "links"), "c"); cc(f"{art}:{p()}"); return self._ok()
            if u.path == "/maus":
                bef = {"ab": "dd", "zieh": "dm", "los": "du", "hin": "m"}[q["art"]]; cc(f"{bef}:{p()}"); return self._ok()
            if u.path == "/rad":
                if not Quartz: return self._ok(b"kein Quartz")
                cc(f"m:{p()}"); dy = max(-40, min(40, int(q["dy"])))
                e = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, dy); Quartz.CGEventPost(Quartz.kCGHIDEventTap, e); return self._ok()
            if u.path == "/taste":
                k = q["k"].lower()
                if not all(c.isalnum() or c in "+-" for c in k) or len(k) > 40: raise ValueError
                teile = k.split("+"); taste = teile[-1]; halt = ",".join(teile[:-1]); bef = []
                if halt: bef.append(f"kd:{halt}")
                bef.append(f"kp:{taste}" if len(taste) > 1 else f"t:{taste}")
                if halt: bef.append(f"ku:{halt}")
                cc(*bef); return self._ok()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self.send_response(500); self.send_header("Content-Length", "0"); self.end_headers(); return
        self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()
    def do_POST(self):
        if not self._darf(): return
        n = int(self.headers.get("Content-Length") or 0)
        if self.path != "/text" or n > 600: self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
        cc("t:" + self.rfile.read(n).decode("utf-8", "replace")); self._ok()

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8790
    print("Bruecke auf Port", port, "Schirm", BREITE, HOEHE, "ffmpeg", FFMPEG, "Quartz", bool(Quartz), flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
