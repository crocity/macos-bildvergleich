#!/usr/bin/env python3
"""Bedienbruecke fuer den Leih-Mac (GitHub-Laeufer), 2. Fassung: fluessiger Bildstrom + Maus (klicken, ziehen in
Echtzeit, Mausrad) + Tasten + Text + Bildschirmfoto in voller Schaerfe. Laeuft auf dem Mac in der angemeldeten
Sitzung. Nimmt nur Anfragen mit der Kopfzeile X-Bruecke an. Schreibt nichts mit."""
import json, os, queue, re, shutil, subprocess, sys, threading, time
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
        p = subprocess.Popen([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "avfoundation", "-capture_cursor", "1", "-framerate", "60",
                              "-pixel_format", "bgr0", "-i", m.group(1) + ":none", "-vf", "fps=60,scale=1920:-2", "-q:v", "7",
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

# COS-Leih-Mac (24.11.2026): Hardware-H.264 (VideoToolbox) + WebCodecs statt MJPEG. Grund: ein
# Cloudflare-Schnelltunnel traegt NUR HTTP, kein UDP/TURN -- echtes WebRTC (RTCPeerConnection)
# verbindet darueber nie (siehe leih-mac.md "Cloudflare-Schnelltunnel traegt kein WebRTC/TURN").
# Dieser Weg bleibt auf HTTP, gewinnt die Geschwindigkeit aber ueber Hardware-Kodierung/-Dekodierung
# statt vieler einzelner JPEGs: ffmpeg liefert einen rohen Annex-B-H.264-Strom, hier in
# Zugriffseinheiten (SPS/PPS/Schnitt gehoeren zusammen) zerlegt, ueber eine eigene, schlichte
# Rahmung (1 Byte Art + 4 Byte Laenge) an /h264 ausgeliefert. Der Browser setzt sie per
# `VideoDecoder` (WebCodecs, ebenfalls Hardware) wieder zusammen -- siehe SEITE weiter unten.
NAL_START = re.compile(rb"\x00\x00\x01")

class H264Strom:
    """Verteilt Zugriffseinheiten an EINE Warteschlange je Verbindung (queue.Queue) -- nie an eine
    gemeinsame 'nur das Neueste'-Stelle wie bei Strom (MJPEG): bei H.264 haengt ein Schnittbild vom
    vorigen ab (-bf 0, IPPP), ein uebersprungenes Bild wuerde den Dekoder im Browser sichtbar
    verderben. Ein zu langsamer Betrachter (Warteschlange voll) wird abgeklemmt statt Bilder
    auszulassen -- der Browser verbindet neu und bekommt wieder einen frischen Keyframe."""
    def __init__(self):
        self.abonnenten = []; self.sperre_abo = threading.Lock()
        self.letzter_keyframe = None; self.art = "?"; self.codec = ""; self.bps = 0.0
        threading.Thread(target=self.lauf, daemon=True).start()
    def abonnieren(self):
        q = queue.Queue(maxsize=120)
        with self.sperre_abo:
            self.abonnenten.append(q)
            if self.letzter_keyframe is not None:
                q.put((True, self.letzter_keyframe))
        return q
    def abbestellen(self, q):
        with self.sperre_abo:
            if q in self.abonnenten: self.abonnenten.remove(q)
    def setze(self, keyframe, b):
        if keyframe: self.letzter_keyframe = b
        with self.sperre_abo:
            tote = []
            for q in self.abonnenten:
                try: q.put_nowait((keyframe, b))
                except queue.Full: tote.append(q)
            for q in tote: self.abonnenten.remove(q)
    def lauf(self):
        if not FFMPEG:
            print("h264Strom: kein ffmpeg", flush=True); return
        l = subprocess.run([FFMPEG, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""], capture_output=True, text=True).stderr
        m = re.search(r"\[(\d+)\] Capture screen 0", l)
        if not m:
            print("h264Strom: kein Bildschirm in der Geraeteliste", flush=True); return
        # Kein "-profile:v" erzwungen: Baseline ist bei 1080p60 eigentlich nur bis Level 3.0 definiert,
        # der Encoder waehlt sein natuerliches Profil (typischerweise High) selbst. Der Browser erfaehrt
        # das richtige Profil/Level ohnehin zur Laufzeit aus den echten SPS-Bytes (s. SEITE::nalPayload).
        gemeinsam = [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "avfoundation", "-capture_cursor", "1",
                     "-framerate", "60", "-pixel_format", "bgr0", "-i", m.group(1) + ":none",
                     "-vf", "fps=60,scale=1920:-2", "-pix_fmt", "yuv420p", "-g", "60", "-bf", "0",
                     "-b:v", "6M", "-f", "h264", "-"]
        for versuch, codec in ((["-c:v", "h264_videotoolbox", "-realtime", "1", "-allow_sw", "1"], "videotoolbox"),
                                (["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency"], "libx264")):
            idx = len(gemeinsam) - 1 - gemeinsam[::-1].index("-f")  # letztes "-f" (Ausgabeformat), nicht "-f avfoundation"
            cmd = gemeinsam[:idx] + versuch + gemeinsam[idx:]
            try:
                self._pipe(cmd, codec); return
            except Exception as e:
                print(f"h264Strom ({codec}) gescheitert:", e, flush=True)
        print("h264Strom: beide Kodierer gescheitert, nur MJPEG (/strom) verfuegbar", flush=True)
    def _pipe(self, cmd, codec):
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.art, self.codec = "ffmpeg", codec
        puffer = b""; aktuell = []; aktuell_key = False; t0 = time.time(); n = 0
        def ausliefern():
            nonlocal aktuell, aktuell_key
            if aktuell: self.setze(aktuell_key, b"".join(aktuell)); aktuell = []; aktuell_key = False
        while True:
            st = p.stdout.read(65536)
            if not st:
                raise RuntimeError(codec + " beendet: " + p.stderr.read().decode(errors="replace")[-300:])
            puffer += st
            start = [mm.start() for mm in NAL_START.finditer(puffer)]
            # die letzte gefundene Startmarke koennte noch unvollstaendige Folgedaten haben -- erst ab der naechsten Runde schneiden
            for i in range(len(start) - 1):
                nal = puffer[start[i]:start[i + 1]]
                typ = nal[3] & 0x1F if len(nal) > 3 else 0
                if typ in (7, 8):  # SPS/PPS -- gehoeren zur naechsten Zugriffseinheit
                    ausliefern(); aktuell.append(nal); aktuell_key = True
                elif typ in (1, 5):  # Schnittbild (IDR=5, sonst 1) -- schliesst die Zugriffseinheit ab
                    aktuell.append(nal); aktuell_key = aktuell_key or typ == 5; ausliefern()
                    n += 1
                    if time.time() - t0 > 3: self.bps = n / (time.time() - t0); t0 = time.time(); n = 0
                else:
                    aktuell.append(nal)
            if start: puffer = puffer[start[-1]:]
class _H264Aus:
    """Diagnose (25.11.2026, Lauf haengt seit der 60-fps/H264-Aenderung): BRUECKE_H264=0 startet den
    zweiten, GLEICHZEITIGEN Bildschirmgriff gar nicht erst -- testet, ob zwei parallele avfoundation-
    Erfassungen (Strom oben + H264Strom) sich auf dem virtualisierten Laeufer blockieren."""
    codec = ""; bps = 0.0
    def abonnieren(self): return queue.Queue()
    def abbestellen(self, q): pass
h264strom = H264Strom() if os.environ.get("BRUECKE_H264", "1") != "0" else _H264Aus()

SEITE = """<!doctype html><meta charset=utf-8><title>Leih-Mac</title>
<style>*{box-sizing:border-box}body{margin:0;background:#101013;color:#ddd;font:13px system-ui;height:100vh;display:flex;flex-direction:column;overflow:hidden}
#leiste{display:flex;gap:6px;align-items:center;padding:6px 8px;background:#19191d;flex:none;flex-wrap:wrap}
button{font:inherit;padding:5px 9px;border-radius:7px;border:1px solid #3a3a40;background:#232328;color:#eee;cursor:pointer}button:hover{background:#2e2e35}
#st{padding:4px 9px;border-radius:7px;background:#1f3a2c;color:#9be7b7}#s{color:#888;margin-left:auto}
#rahmen{flex:1;min-height:0;position:relative;background:#000;outline:none}
#b,#cv{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;cursor:crosshair;user-select:none;-webkit-user-drag:none}
#b{display:none}#cv{display:none}</style>
<div id=leiste><b>Leih-Mac · macOS 27</b><span id=st>verbindet…</span>
<button onclick="taste('cmd+space')">Suche ⌘␣</button><button onclick="taste('cmd+tab')">Wechseln ⌘⇥</button>
<button onclick="taste('cmd+q')">Beenden ⌘Q</button><button onclick="taste('cmd+w')">Schließen ⌘W</button><button onclick="taste('esc')">Esc</button>
<button id=fb onclick="foto()">📷 Bildschirmfoto</button><span id=s>Klick, Ziehen, Mausrad und Tastatur gehen an den Mac. Strg+Taste = ⌘+Taste.</span></div>
<div id=rahmen tabindex=0><img id=b draggable=false><canvas id=cv></canvas></div>
<script>
const rahmen=document.getElementById('rahmen'),b=document.getElementById('b'),cv=document.getElementById('cv'),s=document.getElementById('s'),st=document.getElementById('st');
let kette=Promise.resolve();const hin=u=>{kette=kette.then(()=>fetch(u).catch(()=>{}));return kette};
let groesse={w:1920,h:1080};
function pos(e){const r=rahmen.getBoundingClientRect(),W=groesse.w,H=groesse.h,s=Math.min(r.width/W,r.height/H),w=W*s,h=H*s,x0=r.left+(r.width-w)/2,y0=r.top+(r.height-h)/2;return[Math.min(1,Math.max(0,(e.clientX-x0)/w)),Math.min(1,Math.max(0,(e.clientY-y0)/h))]}
let a=null,zieht=false,letzt=0;
rahmen.addEventListener('contextmenu',e=>{e.preventDefault();const p=pos(e);hin(`/klick?x=${p[0]}&y=${p[1]}&art=rechts`)});
rahmen.addEventListener('pointerdown',e=>{if(e.button!==0)return;rahmen.focus();rahmen.setPointerCapture(e.pointerId);a=pos(e);zieht=false;e.preventDefault()});
rahmen.addEventListener('pointermove',e=>{const p=pos(e);if(a){if(!zieht&&Math.hypot((p[0]-a[0])*groesse.w,(p[1]-a[1])*groesse.h)>5){zieht=true;hin(`/maus?art=ab&x=${a[0]}&y=${a[1]}`)}
  if(zieht&&Date.now()-letzt>16){letzt=Date.now();hin(`/maus?art=zieh&x=${p[0]}&y=${p[1]}`)}}
  else if(Date.now()-letzt>25){letzt=Date.now();fetch(`/maus?art=hin&x=${p[0]}&y=${p[1]}`).catch(()=>{})}});
rahmen.addEventListener('pointerup',e=>{if(e.button!==0||!a)return;const p=pos(e);if(zieht)hin(`/maus?art=los&x=${p[0]}&y=${p[1]}`);else hin(`/klick?x=${a[0]}&y=${a[1]}&art=${e.detail>=2?'doppelt':'links'}`);a=null;zieht=false});
let rad=0,radT=null;rahmen.addEventListener('wheel',e=>{e.preventDefault();rad+=e.deltaY;clearTimeout(radT);radT=setTimeout(()=>{const p=pos(e);hin(`/rad?dy=${Math.round(-rad/3)}&x=${p[0]}&y=${p[1]}`);rad=0},40)},{passive:false});
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

// COS-Leih-Mac (24.11.2026): Hardware-H.264 (VideoToolbox) per WebCodecs, MJPEG (/strom, bisheriger Weg)
// NUR als Rueckfall, wenn WebCodecs fehlt oder der H.264-Weg scheitert (altes WebKitGTK, kaputter Strom o.ae.).
let modus='keins';
function mjpegStart(){
  if(modus==='mjpeg')return; modus='mjpeg';
  cv.style.display='none'; b.style.display='block'; b.src='/strom?t='+Date.now();
}
function nalPayload(puf, typ){ // sucht die erste NAL-Einheit vom Typ `typ` in einer Annex-B-Zugriffseinheit (3/4-Byte-Startcode)
  for(let i=0;i<puf.length-4;i++){
    if(puf[i]===0&&puf[i+1]===0&&puf[i+2]===1){
      const kopf=puf[i+3]; if((kopf&0x1F)===typ){
        let j=i+4; while(j<puf.length-2 && !(puf[j]===0&&puf[j+1]===0&&puf[j+2]===1)) j++;
        return puf.slice(i+4, j);
      }
    }
  }
  return null;
}
async function h264Start(){
  if(!window.VideoDecoder){ mjpegStart(); return; }
  let resp; try{ resp=await fetch('/h264'); }catch(e){ mjpegStart(); return; }
  if(!resp.ok||!resp.body){ mjpegStart(); return; }
  const ctx=cv.getContext('2d');
  let decoder=null, puf=new Uint8Array(0), zeit=0;
  const schliessen=()=>{try{decoder&&decoder.state!=='closed'&&decoder.close()}catch(e){} mjpegStart()};
  try{
    const reader=resp.body.getReader();
    while(true){
      const {value,done}=await reader.read();
      if(done) break;
      const neu=new Uint8Array(puf.length+value.length); neu.set(puf,0); neu.set(value,puf.length); puf=neu;
      while(puf.length>=5){
        const art=puf[0], laenge=(puf[1]<<24)|(puf[2]<<16)|(puf[3]<<8)|puf[4];
        if(puf.length<5+laenge) break;
        const einheit=puf.slice(5,5+laenge); puf=puf.slice(5+laenge);
        if(!decoder){
          if(!art) continue; // erste Einheit MUSS ein Keyframe sein (SPS+PPS+IDR), sonst warten
          const sps=nalPayload(einheit,7);
          if(!sps||sps.length<3){ schliessen(); return; }
          const codec='avc1.'+[sps[0],sps[1],sps[2]].map(x=>x.toString(16).padStart(2,'0')).join('').toUpperCase();
          decoder=new VideoDecoder({
            output(frame){
              if(cv.width!==frame.displayWidth||cv.height!==frame.displayHeight){cv.width=frame.displayWidth;cv.height=frame.displayHeight}
              groesse={w:frame.displayWidth,h:frame.displayHeight};
              ctx.drawImage(frame,0,0); frame.close();
              if(modus!=='h264'){modus='h264'; b.style.display='none'; cv.style.display='block'}
            },
            error(e){console.warn('VideoDecoder-Fehler:',e); schliessen()}
          });
          try{ decoder.configure({codec, optimizeForLatency:true}); }
          catch(e){ console.warn('Codec nicht unterstuetzt:',codec,e); schliessen(); return; }
        }
        decoder.decode(new EncodedVideoChunk({type:art?'key':'delta', timestamp:(zeit++)*16666, data:einheit}));
      }
    }
    schliessen();
  }catch(e){ console.warn('h264Start:',e); schliessen(); }
}
h264Start();
setInterval(()=>{if(modus==='mjpeg'&&!b.naturalWidth){b.src='/strom?t='+Date.now()}},3000);/* WebKitGTK bleibt beim ersten Laden des MJPEG-Stroms manchmal schwarz (naturalWidth 0) -- dann neu verbinden. */
setInterval(async()=>{try{const j=await(await fetch('/stand')).json();const n=modus==='h264'?j.h264_bps:j.bps;st.textContent='verbunden ('+(modus==='h264'?'H.264':modus==='mjpeg'?'MJPEG':'…')+') · '+(n||0).toFixed(0)+' Bilder/s';st.style.background='#1f3a2c'}catch(e){st.textContent='keine Verbindung';st.style.background='#4a2222'}},4000);
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
            if u.path == "/h264":
                self.send_response(200); self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Cache-Control", "no-store"); self.send_header("Connection", "close"); self.end_headers()
                q = h264strom.abonnieren(); gesehen_key = False
                try:
                    while True:
                        try: key, b = q.get(timeout=10)
                        except queue.Empty: break  # kein Bild seit 10s -- Strom tot, Browser verbindet selbst neu
                        if not gesehen_key:
                            if not key: continue  # vor dem ersten Keyframe nichts schicken, der Dekoder braucht ihn zuerst
                            gesehen_key = True
                        self.wfile.write(bytes([1 if key else 0]) + len(b).to_bytes(4, "big") + b); self.wfile.flush()
                finally:
                    h264strom.abbestellen(q)
            if u.path == "/stand": return self._ok(json.dumps({"breite": BREITE, "hoehe": HOEHE, "quelle": strom.art, "bps": strom.bps, "quartz": bool(Quartz), "ffmpeg": bool(FFMPEG), "h264_codec": h264strom.codec, "h264_bps": h264strom.bps}).encode(), "application/json")
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
    # Diagnose 25.11.: Server-Bau in einer eigenen, unverlierbaren Datei absichern -- der Socket
    # landete bisher im Zustand CLOSED statt LISTEN, bruecke.log zeigte aber nie eine Ausnahme
    # (Verdacht: Ausnahme beim Bau/serve_forever, Traceback ging in der Puffer-/Absturzreihenfolge
    # verloren). Diese Datei schreibt JEDE Zeile unverzueglich (buffering=1) und schliesst sofort.
    try:
        with open("/tmp/bruecke-fehler.txt", "w", buffering=1) as fehlerdatei:
            fehlerdatei.write("vor Server-Bau\n")
            srv = ThreadingHTTPServer(("0.0.0.0", port), H)
            fehlerdatei.write("Server gebaut, Socket: " + str(srv.socket.getsockname()) + "\n")
            fehlerdatei.write("serve_forever beginnt jetzt\n")
        srv.serve_forever()
    except BaseException as e:
        import traceback
        with open("/tmp/bruecke-fehler.txt", "a", buffering=1) as fehlerdatei:
            fehlerdatei.write("AUSNAHME: " + repr(e) + "\n")
            traceback.print_exc(file=fehlerdatei)
        raise
