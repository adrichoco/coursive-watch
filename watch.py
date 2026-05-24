#!/usr/bin/env python3
"""
Watcher for La Coursive ticket availability.

Renders https://la-coursive.notre-billetterie.com/billets?kld=2526 with headless
Chrome, extracts the per-show status, and alerts on any transition that exposes
new seats (warning -> beware/valid, beware -> valid, or a brand-new show
appearing in the listing).

Statuses come from the page's own CSS classes:
  valid    = green  = "Places disponibles"
  beware   = orange = "Dernieres places"
  warning  = red    = "Representation non vendue en ligne ou complete"

Two delivery modes, controlled by env vars:
  - macOS local mode (default when no SMTP env vars): native notification + sound,
    optional `open` to the booking URL.
  - Email mode (when SMTP_HOST is set): HTML email with a big clickable CTA to
    the booking URL. Used by the GitHub Actions cron.

Run modes:
  ./watch.py                # loop, default 60s interval (local)
  ./watch.py --once         # single poll then exit (used by CI)
  ./watch.py --reset        # delete the saved baseline
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import smtplib
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

URL = os.environ.get(
    "COURSIVE_URL",
    "https://la-coursive.notre-billetterie.com/billets?kld=2526",
)
STATE_FILE = Path(os.environ.get("COURSIVE_STATE", str(Path.home() / "coursive-watch" / "state.json")))
LOG_FILE = Path(os.environ.get("COURSIVE_LOG", str(STATE_FILE.parent / "watch.log")))

STATUS_LABEL = {
    "valid": "Places disponibles",
    "beware": "Dernieres places",
    "warning": "Complet / non vendu en ligne",
}
# Higher rank = more seats. Transitions toward higher rank = alert.
STATUS_RANK = {"warning": 0, "unknown": 0, "beware": 1, "valid": 2}

CHROME_CANDIDATES = [
    os.environ.get("CHROME_BIN"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    shutil.which("google-chrome"),
    shutil.which("chromium"),
    shutil.which("chromium-browser"),
]


def find_chrome() -> str:
    for c in CHROME_CANDIDATES:
        if c and Path(c).exists():
            return c
    raise RuntimeError("Chrome binary not found. Set CHROME_BIN or install Chrome/Chromium.")


def log(msg: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass  # logs are best-effort; never fail the run on log I/O


def render() -> str:
    chrome = find_chrome()
    cmd = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        "--disable-dev-shm-usage",
        "--virtual-time-budget=15000",
        "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "--dump-dom",
        URL,
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=90, text=True)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"Chrome failed rc={proc.returncode}: {proc.stderr[:200]}")
    return proc.stdout


# Each show card has somewhere inside it:
#   <h3 itemprop="name" ...>TITLE</h3>
#   <span class="date ...">...</span>
#   <button id="show_NNNN" class="... STATUS" data-sp="NNNN">
# We anchor on the button and walk back to the nearest preceding title/date.
BUTTON_RE = re.compile(
    r'<button[^>]*id="show_(?P<id>\d+)"[^>]*class="(?P<cls>[^"]+)"[^>]*data-sp="\d+"',
    re.IGNORECASE,
)
TITLE_RE = re.compile(
    r'<h3[^>]*itemprop="name"[^>]*>(?P<title>.*?)</h3>',
    re.IGNORECASE | re.DOTALL,
)
DATE_RE = re.compile(
    r'<span class="date[^"]*">(?P<date>.*?)</span>',
    re.IGNORECASE | re.DOTALL,
)
PAST_MARKER_RE = re.compile(
    r"(D[eé]j[aà] pass[eé]es|Les s[eé]ances sont pass[eé]es)",
    re.IGNORECASE,
)


def classify(cls_attr: str) -> str:
    tokens = cls_attr.split()
    for s in ("warning", "beware", "valid"):
        if s in tokens:
            return s
    return "unknown"


def strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_shows(html: str) -> list[dict]:
    cut = PAST_MARKER_RE.search(html)
    if cut:
        html = html[: cut.start()]
    shows = []
    for m in BUTTON_RE.finditer(html):
        sp_id = m.group("id")
        status = classify(m.group("cls"))
        title_m = None
        for tm in TITLE_RE.finditer(html, 0, m.start()):
            title_m = tm
        date_m = None
        for dm in DATE_RE.finditer(html, 0, m.start()):
            date_m = dm
        title = strip_tags(title_m.group("title")) if title_m else f"show_{sp_id}"
        date = strip_tags(date_m.group("date")) if date_m else ""
        shows.append({"id": sp_id, "title": title, "date": date, "status": status})
    return shows


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        return {}


def save_state(shows: list[dict]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    by_id = {s["id"]: s for s in shows}
    STATE_FILE.write_text(json.dumps(by_id, ensure_ascii=False, indent=2, sort_keys=True))


# ---------- alert delivery ----------

def macos_notify(subject: str, body: str, open_browser: bool) -> None:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{esc(body)}" with title "{esc(subject)}" sound name "Glass"'
    subprocess.run(["osascript", "-e", script], check=False)
    subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], check=False)
    if open_browser:
        subprocess.run(["open", URL], check=False)


def render_email_html(events: list[dict]) -> str:
    rows = []
    for e in events:
        rows.append(
            f"<tr>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee'><b>{e['title']}</b></td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee;color:#555'>{e['date']}</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee'>{e['transition']}</td>"
            f"</tr>"
        )
    body_rows = "".join(rows) or "<tr><td>(aucun)</td></tr>"
    return f"""\
<!doctype html><html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f7;margin:0;padding:24px">
  <div style="max-width:600px;margin:auto;background:#fff;border-radius:12px;padding:28px;box-shadow:0 1px 3px rgba(0,0,0,0.08)">
    <h2 style="margin:0 0 6px 0;color:#0fb155">Des places se sont liberees a La Coursive</h2>
    <p style="color:#666;margin:0 0 20px 0">Clique sur le bouton pour aller reserver tout de suite.</p>
    <a href="{URL}" style="display:inline-block;background:#0fb155;color:#fff;text-decoration:none;padding:14px 28px;border-radius:8px;font-weight:600;font-size:16px">Reserver maintenant</a>
    <table style="border-collapse:collapse;width:100%;margin-top:24px;font-size:14px">
      <thead><tr style="text-align:left;color:#888;font-weight:500">
        <th style="padding:8px 12px;border-bottom:2px solid #eee">Spectacle</th>
        <th style="padding:8px 12px;border-bottom:2px solid #eee">Date</th>
        <th style="padding:8px 12px;border-bottom:2px solid #eee">Changement</th>
      </tr></thead>
      <tbody>{body_rows}</tbody>
    </table>
    <p style="color:#999;font-size:12px;margin-top:24px">
      <a href="{URL}" style="color:#666">{URL}</a>
    </p>
  </div>
</body></html>"""


def send_email(events: list[dict]) -> bool:
    host = os.environ.get("SMTP_HOST")
    if not host:
        return False
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASS", "")
    mail_from = os.environ.get("MAIL_FROM", user)
    mail_to = [x.strip() for x in os.environ.get("MAIL_TO", "").split(",") if x.strip()]
    if not (mail_from and mail_to):
        log("WARN email skipped: MAIL_FROM / MAIL_TO missing")
        return False

    titles = ", ".join(sorted({e["title"] for e in events}))[:90]
    msg = EmailMessage()
    msg["Subject"] = f"Coursive — places liberees ({titles})"
    msg["From"] = mail_from
    msg["To"] = ", ".join(mail_to)
    text_body = (
        "Des places se sont liberees a La Coursive :\n\n"
        + "\n".join(f"- {e['title']} ({e['date']}) — {e['transition']}" for e in events)
        + f"\n\nReserver : {URL}\n"
    )
    msg.set_content(text_body)
    msg.add_alternative(render_email_html(events), subtype="html")

    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)
        log(f"email sent to {msg['To']}")
        return True
    except Exception as e:
        log(f"ERROR email send failed: {type(e).__name__}: {e}")
        return False


def send_ntfy(events: list[dict]) -> bool:
    """Push to ntfy.sh (or self-hosted ntfy). Returns True on success."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    token = os.environ.get("NTFY_TOKEN")  # optional, for self-hosted auth

    ok_any = False
    for e in events:
        body = f"{e['title']} ({e['date']})\n{e['transition']}\n\nTap pour reserver."
        req = urllib.request.Request(
            f"{server}/{topic}",
            data=body.encode("utf-8"),
            method="POST",
            headers={
                "Title": "Coursive — places liberees".encode("utf-8"),
                "Priority": "high",
                "Tags": "tada,ticket",
                "Click": URL,
                "Actions": f"view, Reserver, {URL}, clear=true",
            },
        )
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if 200 <= resp.status < 300:
                    ok_any = True
                else:
                    log(f"WARN ntfy non-2xx: {resp.status}")
        except Exception as ex:
            log(f"ERROR ntfy push failed: {type(ex).__name__}: {ex}")
    if ok_any:
        log(f"ntfy push sent ({len(events)} event(s)) topic={topic}")
    return ok_any


def deliver(events: list[dict], open_browser: bool) -> None:
    """Dispatch alerts via whatever channels are configured."""
    if not events:
        return
    sent_ntfy = send_ntfy(events)
    sent_email = send_email(events)
    if sys.platform == "darwin" and os.environ.get("CI") != "true":
        for e in events:
            macos_notify(
                "Coursive — places liberees",
                f"{e['title']} ({e['date']}) : {e['transition']}",
                open_browser=open_browser,
            )
    if not (sent_ntfy or sent_email) and os.environ.get("CI") == "true":
        # CI run with no remote channel succeeded — fail loudly so the workflow turns red.
        raise RuntimeError("alert(s) fired but no remote channel succeeded")


def diff(prev: dict, shows: list[dict]) -> list[dict]:
    """Return list of alert events (title, date, transition)."""
    events = []
    for s in shows:
        sid = s["id"]
        before = prev.get(sid)
        status = s["status"]
        if before is None:
            if status in ("valid", "beware"):
                events.append({
                    "title": s["title"],
                    "date": s["date"],
                    "transition": f"nouveau en vente — {STATUS_LABEL.get(status, status)}",
                })
            continue
        prev_status = before.get("status", "unknown")
        if STATUS_RANK.get(status, 0) > STATUS_RANK.get(prev_status, 0):
            events.append({
                "title": s["title"],
                "date": s["date"],
                "transition": f"{STATUS_LABEL.get(prev_status, prev_status)} -> {STATUS_LABEL.get(status, status)}",
            })
    return events


def run_once(prev: dict, *, baseline: bool, open_browser: bool) -> dict:
    html = render()
    shows = parse_shows(html)
    if not shows:
        log("WARN no shows parsed — page structure may have changed, skipping")
        return prev
    log(
        f"poll ok ({len(shows)} shows): "
        + " | ".join(f"{s['title']}={s['status']}" for s in shows)
    )
    if baseline:
        log("baseline captured, no alerts on first run")
    else:
        events = diff(prev, shows)
        for e in events:
            log(f"ALERT {e['title']} ({e['date']}) — {e['transition']}")
        deliver(events, open_browser=open_browser)
    save_state(shows)
    return {s["id"]: s for s in shows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=60, help="poll interval in seconds (default 60)")
    ap.add_argument("--once", action="store_true", help="single poll then exit")
    ap.add_argument("--reset", action="store_true", help="delete the saved baseline and exit")
    ap.add_argument("--open-browser", action="store_true", help="open the booking URL when an alert fires (local mode)")
    args = ap.parse_args()

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    if args.reset:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
            print(f"deleted {STATE_FILE}")
        else:
            print("no state to reset")
        return 0

    prev = load_state()
    baseline_needed = not prev
    log(f"start url={URL} state={STATE_FILE} baseline_needed={baseline_needed} CI={os.environ.get('CI','')}")

    try:
        run_once(prev, baseline=baseline_needed, open_browser=args.open_browser)
        if args.once:
            return 0
        while True:
            time.sleep(args.interval)
            try:
                prev = load_state()
                run_once(prev, baseline=False, open_browser=args.open_browser)
            except subprocess.TimeoutExpired:
                log("WARN render timed out, retrying next tick")
            except Exception as e:
                log(f"WARN poll failed: {type(e).__name__}: {e}")
    except KeyboardInterrupt:
        log("stopped (KeyboardInterrupt)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
