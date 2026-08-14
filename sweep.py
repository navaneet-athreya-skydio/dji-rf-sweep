#!/usr/bin/env python3
"""
DJI O3 attenuation sweep.

Sweeps Mini-Circuits attenuator ATTEN_START → ATTEN_END → ATTEN_START,
collects bitrate/frequency/bandwidth/chunk stats from adb logcat,
saves CSV + HTML plot.

Usage:
    python3 sweep.py
"""

import subprocess
import threading
import time
import csv
import re
import statistics
import webbrowser
from datetime import datetime
from pathlib import Path
from collections import defaultdict

# ── Config (change these) ─────────────────────────────────────────────────────
ATTEN_START   = 45    # dB
ATTEN_END     = 72    # dB
ATTEN_STEP    = 1     # dB
SETTLE_TIME   = 3.0   # seconds to wait after setting attenuation (not recorded)
MEASURE_TIME  = 3.0   # seconds to record after settling
ONE_WAY       = False # True = up sweep only, no return
TX_POWER_DBM = 21.0    # dBm
PATH_LOSS_DB = 57.0     # dB
MC_ATTEN     = Path(__file__).parent / "mc_atten.py"
LOG_DIR      = Path(__file__).parent / "logs"
# ─────────────────────────────────────────────────────────────────────────────

LOG_DIR.mkdir(parents=True, exist_ok=True)

_lock    = threading.Lock()
_records = []
_latest  = {
    "bitrate":          None,
    "video_feed_bw":    None,
    "video_bitrate":    None,
    "chunk_mbps":       None,
    "chunk_avg_bytes":  None,
    "chunk_keyframes":  None,
    "chunk_dropped":    None,
    "chunk_fps":        None,
    "chunk_res":        None,
    "frequency":        None,
    "bandwidth":        None,
}
_chunk_updated_at = None   # wall time of last CHUNK log line
_CHUNK_STALE_SEC  = 3.0    # discard chunk data older than this
_state   = {"atten": None, "dir": "", "t0": None, "active": False, "measuring": False}


def _chunk_fresh():
    """Return True if a CHUNK log line arrived within the staleness window."""
    return (_chunk_updated_at is not None and
            time.time() - _chunk_updated_at <= _CHUNK_STALE_SEC)


def _record(trigger):
    if not _state["active"]:
        return
    atten = _state["atten"]
    rssi  = round(TX_POWER_DBM - PATH_LOSS_DB - atten, 2) if atten is not None else None
    fresh = _chunk_fresh()
    with _lock:
        _records.append({
            "timestamp":            datetime.now().isoformat(),
            "elapsed_sec":          round(time.time() - _state["t0"], 3),
            "direction":            _state["dir"],
            "phase":                "measure" if _state["measuring"] else "settle",
            "attenuation_db":       atten,
            "rssi_dbm":             rssi,
            "bitrate_mbps":         _latest["bitrate"],
            "video_feed_bw_mbps":   _latest["video_feed_bw"],
            "video_bitrate_mbps":   _latest["video_bitrate"],
            "chunk_mbps":           _latest["chunk_mbps"]      if fresh else None,
            "chunk_avg_bytes":      _latest["chunk_avg_bytes"] if fresh else None,
            "chunk_keyframes":      _latest["chunk_keyframes"] if fresh else None,
            "chunk_dropped":        _latest["chunk_dropped"]   if fresh else None,
            "chunk_fps":            _latest["chunk_fps"]       if fresh else None,
            "chunk_res":            _latest["chunk_res"]       if fresh else None,
            "frequency_mhz":        _latest["frequency"],
            "bandwidth_mhz":        _latest["bandwidth"],
            "trigger":              trigger,
        })


def parse_line(line):
    # ── KeyDynamicDataRate ────────────────────────────────────────────────────
    m = re.search(r"Bitrate: ([\d.]+)", line)
    if m:
        br = float(m.group(1))
        if br == 9.765625:
            return
        _latest["bitrate"] = br
        _record("bitrate")
        return

    # ── KeyPrimaryVideoFeedBandwidth ──────────────────────────────────────────
    m = re.search(r"VideoFeedBW: ([\d.]+)", line)
    if m:
        _latest["video_feed_bw"] = float(m.group(1))
        _record("video_feed_bw")
        return

    # ── KeyVideoDataRate ──────────────────────────────────────────────────────
    m = re.search(r"VideoDataRate: ([\d.]+)", line)
    if m:
        _latest["video_bitrate"] = float(m.group(1))
        _record("video_bitrate")
        return

    # ── CHUNK per-second summary ──────────────────────────────────────────────
    m = re.search(
        r"chunks=(\d+) totalBytes=(\d+) avg=(\d+)B.*keyframes=(\d+) dropped=(\d+) fps=(\d+) res=(\d+)x(\d+)",
        line
    )
    if m:
        global _chunk_updated_at
        total_bytes = int(m.group(2))
        mbps = round(total_bytes * 8 / 1_000_000, 3)
        # below 0.1 Mbps = SPS/PPS keepalive noise when link is dead, not real video
        if mbps < 0.03:
            return
        _latest["chunk_avg_bytes"] = int(m.group(3))
        _latest["chunk_mbps"]      = mbps
        _latest["chunk_keyframes"] = int(m.group(4))
        _latest["chunk_dropped"]   = int(m.group(5))
        _latest["chunk_fps"]       = int(m.group(6))
        _latest["chunk_res"]       = f"{m.group(7)}x{m.group(8)}"
        _chunk_updated_at          = time.time()
        _record("chunk")
        return

    # ── Frequency / Bandwidth ─────────────────────────────────────────────────
    m = re.search(r"Frequency Point: (\d+)", line)
    if m:
        _latest["frequency"] = int(m.group(1))
        _record("frequency")
        return
    m = re.search(r"Bandwidth: BANDWIDTH_(\d+)MHZ", line)
    if m:
        _latest["bandwidth"] = int(m.group(1))
        _record("bandwidth")


def logcat_thread():
    proc = subprocess.Popen(
        ["adb", "logcat", "DATA:D", "CHUNK:D", "WLM:D", "*:S", "-v", "raw"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1
    )
    for line in proc.stdout:
        parse_line(line.strip())


def set_atten(val):
    result = subprocess.run(
        ["sudo", "python3", str(MC_ATTEN), str(val)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    if result.returncode != 0:
        print(f"  WARNING: attenuator set failed for {val} dB")


def build_sweep():
    import math
    n = math.ceil((ATTEN_END - ATTEN_START) / ATTEN_STEP)
    up = [round(ATTEN_START + i * ATTEN_STEP, 4) for i in range(n + 1)]
    if up[-1] != ATTEN_END:
        up[-1] = ATTEN_END
    if ONE_WAY:
        return [("up", v) for v in up]
    down = list(reversed(up[:-1]))
    return [("up", v) for v in up] + [("down", v) for v in down]


def main(inline_js=False):
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path  = LOG_DIR / f"sweep_{ts}.csv"
    html_path = LOG_DIR / f"sweep_{ts}.html"

    print("Clearing stale logcat...")
    subprocess.run(["adb", "logcat", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)

    print("Starting logcat reader...")
    threading.Thread(target=logcat_thread, daemon=True).start()
    time.sleep(1)

    sweep = build_sweep()
    _state["t0"]     = time.time()
    _state["active"] = True

    total_steps = len(sweep)
    est_secs    = total_steps * (SETTLE_TIME + MEASURE_TIME)
    print(f"Sweep: {ATTEN_START}→{ATTEN_END}{'→'+str(ATTEN_START) if not ONE_WAY else ''} dB  "
          f"step={ATTEN_STEP} dB  settle={SETTLE_TIME}s  measure={MEASURE_TIME}s")
    print(f"Steps: {total_steps}  Est. time: {est_secs:.0f}s ({est_secs/60:.1f} min)\n")

    for i, (direction, atten) in enumerate(sweep, 1):
        _state["dir"]       = direction
        _state["atten"]     = atten
        _state["measuring"] = False
        elapsed = time.time() - _state["t0"]
        print(f"  [{i:2}/{total_steps}] [{direction:4}] {atten:3} dB  settling...  (t={elapsed:.0f}s)", flush=True)
        set_atten(atten)
        time.sleep(SETTLE_TIME)
        _state["measuring"] = True
        print(f"  [{i:2}/{total_steps}] [{direction:4}] {atten:3} dB  measuring...  (t={time.time()-_state['t0']:.0f}s)", flush=True)
        time.sleep(MEASURE_TIME)

    _state["measuring"] = False
    _state["active"]    = False

    with _lock:
        rows = list(_records)

    print(f"\nSweep complete. {len(rows)} rows collected.")

    fields = [
        "timestamp", "elapsed_sec", "direction", "phase", "attenuation_db", "rssi_dbm",
        "bitrate_mbps", "video_feed_bw_mbps", "video_bitrate_mbps", "chunk_mbps",
        "chunk_avg_bytes", "chunk_keyframes", "chunk_dropped", "chunk_fps", "chunk_res",
        "frequency_mhz", "bandwidth_mhz", "trigger",
    ]
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"CSV:  {csv_path}")

    summary_rows = _compute_summary(rows)
    summary_path = csv_path.with_name(csv_path.stem + "_summary.csv")
    summary_fields = [
        "attenuation_db", "rssi_dbm",
        "median_bitrate_up_mbps",        "median_bitrate_dn_mbps",
        "median_video_feed_bw_up_mbps",  "median_video_feed_bw_dn_mbps",
        "median_video_bitrate_up_mbps",  "median_video_bitrate_dn_mbps",
        "median_chunk_mbps_up",          "median_chunk_mbps_dn",
    ]
    with open(summary_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=summary_fields)
        w.writeheader()
        w.writerows(summary_rows)
    print(f"Summary: {summary_path}")

    make_plot(rows, html_path, summary_rows, inline_js=inline_js)


def _compute_summary(records):
    up_b, dn_b = defaultdict(list), defaultdict(list)
    up_f, dn_f = defaultdict(list), defaultdict(list)
    up_v, dn_v = defaultdict(list), defaultdict(list)
    up_c, dn_c = defaultdict(list), defaultdict(list)

    for r in records:
        if r.get("phase", "measure") != "measure":
            continue
        atten = r["attenuation_db"]
        if r.get("bitrate_mbps") is not None:
            (up_b if r["direction"] == "up" else dn_b)[atten].append(r["bitrate_mbps"])
        if r.get("video_feed_bw_mbps") is not None:
            (up_f if r["direction"] == "up" else dn_f)[atten].append(r["video_feed_bw_mbps"])
        if r.get("video_bitrate_mbps") is not None:
            (up_v if r["direction"] == "up" else dn_v)[atten].append(r["video_bitrate_mbps"])
        if r.get("chunk_mbps") is not None:
            (up_c if r["direction"] == "up" else dn_c)[atten].append(r["chunk_mbps"])

    steps = sorted(set(up_b.keys()) | set(dn_b.keys()))
    out = []
    for atten in steps:
        def med(d): return round(statistics.median(d[atten]), 3) if d.get(atten) else None
        out.append({
            "attenuation_db":                   atten,
            "rssi_dbm":                         round(TX_POWER_DBM - PATH_LOSS_DB - atten, 2),
            "median_bitrate_up_mbps":           med(up_b),
            "median_bitrate_dn_mbps":           med(dn_b),
            "median_video_feed_bw_up_mbps":     med(up_f),
            "median_video_feed_bw_dn_mbps":     med(dn_f),
            "median_video_bitrate_up_mbps":     med(up_v),
            "median_video_bitrate_dn_mbps":     med(dn_v),
            "median_chunk_mbps_up":             med(up_c),
            "median_chunk_mbps_dn":             med(dn_c),
        })
    return out


def make_plot(records, html_path, summary_rows=None, inline_js=False):
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
        import plotly.io as pio
    except ImportError:
        print("pip install plotly  — skipping plot")
        return

    if not records:
        print("No data to plot.")
        return

    up   = [r for r in records if r["direction"] == "up"]
    down = [r for r in records if r["direction"] == "down"]

    def t(rows):       return [r["elapsed_sec"] for r in rows]
    def rssi(rows):    return [r["rssi_dbm"]    for r in rows]
    def val(rows, k):  return [r.get(k)         for r in rows]

    C_UP      = "#00b4d8"
    C_DOWN    = "#f77f00"
    C_ATTEN   = "rgba(180,180,180,0.4)"
    C_FREQ    = "#80ffdb"
    C_BW      = "#c77dff"
    C_VIDEO   = "#06d6a0"
    C_CHUNK   = "#ffd166"
    TPL       = "plotly_dark"

    def rssi_traces(rows_up, rows_down):
        return [
            go.Scatter(x=t(rows_up),   y=rssi(rows_up),   name="RSSI (up)",
                       mode="lines", line=dict(color=C_ATTEN, dash="dot",  width=1), yaxis="y2"),
            go.Scatter(x=t(rows_down), y=rssi(rows_down), name="RSSI (down)",
                       mode="lines", line=dict(color=C_ATTEN, dash="dash", width=1), yaxis="y2"),
        ]

    def dual_axis_layout(title, y_title):
        return dict(
            title=title, template=TPL, height=520,
            xaxis=dict(title="Time (s)"),
            yaxis=dict(title=y_title),
            yaxis2=dict(title="RSSI (dBm)", overlaying="y", side="right", showgrid=False),
            legend=dict(x=1.08, y=1),
        )

    # ── Tab 1: Bitrate vs Time ────────────────────────────────────────────────
    fig1 = go.Figure([
        go.Scatter(x=t(up),   y=val(up,   "bitrate_mbps"), name="Dynamic (up)",   mode="lines", line=dict(color=C_UP)),
        go.Scatter(x=t(down), y=val(down, "bitrate_mbps"), name="Dynamic (down)", mode="lines", line=dict(color=C_DOWN)),
        *rssi_traces(up, down),
    ])
    fig1.update_layout(**dual_axis_layout("Bitrate vs Time (KeyDynamicDataRate)", "Bitrate (Mbps)"))

    # ── Tab 2: Frequency vs Time ──────────────────────────────────────────────
    GHZ24_THRESH = 4000
    all_rows = up + down
    has_24 = any(r.get("frequency_mhz") is not None and r["frequency_mhz"] < GHZ24_THRESH for r in all_rows)
    has_58 = any(r.get("frequency_mhz") is not None and r["frequency_mhz"] >= GHZ24_THRESH for r in all_rows)

    if has_24 and has_58:
        up_24   = [r for r in up   if r.get("frequency_mhz") is not None and r["frequency_mhz"] < GHZ24_THRESH]
        up_58   = [r for r in up   if r.get("frequency_mhz") is not None and r["frequency_mhz"] >= GHZ24_THRESH]
        down_24 = [r for r in down if r.get("frequency_mhz") is not None and r["frequency_mhz"] < GHZ24_THRESH]
        down_58 = [r for r in down if r.get("frequency_mhz") is not None and r["frequency_mhz"] >= GHZ24_THRESH]

        fig2 = make_subplots(
            rows=2, cols=1, shared_xaxes=True,
            row_heights=[0.55, 0.45], vertical_spacing=0.10,
            specs=[[{"secondary_y": True}], [{"secondary_y": False}]],
        )
        fig2.add_trace(go.Scatter(x=t(up_58),   y=val(up_58,   "frequency_mhz"), name="Freq up (5.8G)",   mode="lines", line=dict(color=C_FREQ)),         row=1, col=1, secondary_y=False)
        fig2.add_trace(go.Scatter(x=t(down_58), y=val(down_58, "frequency_mhz"), name="Freq down (5.8G)", mode="lines", line=dict(color=C_FREQ, dash="dash")), row=1, col=1, secondary_y=False)
        fig2.add_trace(go.Scatter(x=t(up),   y=rssi(up),   name="RSSI (up)",   mode="lines", line=dict(color=C_ATTEN, dash="dot",  width=1)), row=1, col=1, secondary_y=True)
        fig2.add_trace(go.Scatter(x=t(down), y=rssi(down), name="RSSI (down)", mode="lines", line=dict(color=C_ATTEN, dash="dash", width=1)), row=1, col=1, secondary_y=True)
        fig2.add_trace(go.Scatter(x=t(up_24),   y=val(up_24,   "frequency_mhz"), name="Freq up (2.4G)",   mode="lines", line=dict(color=C_FREQ),           showlegend=False), row=2, col=1)
        fig2.add_trace(go.Scatter(x=t(down_24), y=val(down_24, "frequency_mhz"), name="Freq down (2.4G)", mode="lines", line=dict(color=C_FREQ, dash="dash"), showlegend=False), row=2, col=1)
        fig2.update_layout(title="Frequency vs Time (broken axis: 5.8 GHz top / 2.4 GHz bottom)", template=TPL, height=600, legend=dict(x=1.08, y=1))
        fig2.update_yaxes(title_text="Frequency (MHz)", row=1, col=1, secondary_y=False)
        fig2.update_yaxes(title_text="RSSI (dBm)",      row=1, col=1, secondary_y=True, showgrid=False)
        fig2.update_yaxes(title_text="Frequency (MHz)", row=2, col=1)
        fig2.update_xaxes(title_text="Time (s)",        row=2, col=1)
        for xp in [0.0, 1.0]:
            fig2.add_shape(type="line", xref="paper", yref="paper", x0=xp-0.012, y0=0.455, x1=xp+0.012, y1=0.495, line=dict(color="#888", width=2))
            fig2.add_shape(type="line", xref="paper", yref="paper", x0=xp-0.012, y0=0.425, x1=xp+0.012, y1=0.465, line=dict(color="#888", width=2))
    else:
        fig2 = go.Figure([
            go.Scatter(x=t(up),   y=val(up,   "frequency_mhz"), name="Frequency (up)",   mode="lines", line=dict(color=C_FREQ)),
            go.Scatter(x=t(down), y=val(down, "frequency_mhz"), name="Frequency (down)", mode="lines", line=dict(color=C_FREQ, dash="dash")),
            *rssi_traces(up, down),
        ])
        fig2.update_layout(**dual_axis_layout("Frequency vs Time", "Frequency (MHz)"))

    # ── Tab 3: Bandwidth vs Time ──────────────────────────────────────────────
    fig3 = go.Figure([
        go.Scatter(x=t(up),   y=val(up,   "bandwidth_mhz"), name="Bandwidth (up)",   mode="lines+markers", line=dict(color=C_BW)),
        go.Scatter(x=t(down), y=val(down, "bandwidth_mhz"), name="Bandwidth (down)", mode="lines+markers", line=dict(color=C_BW, dash="dash")),
        *rssi_traces(up, down),
    ])
    fig3.update_layout(**dual_axis_layout("Bandwidth vs Time", "Bandwidth (MHz)"))
    fig3.update_yaxes(tickvals=[10, 20, 40], selector=dict(title_text="Bandwidth (MHz)"))

    # ── Tab 4: Median Bitrate vs RSSI (all 3 bitrates) ───────────────────────
    up_m   = [r for r in up   if r.get("phase", "measure") == "measure"]
    down_m = [r for r in down if r.get("phase", "measure") == "measure"]
    up_steps   = sorted(set(r["attenuation_db"] for r in up_m))
    down_steps = sorted(set(r["attenuation_db"] for r in down_m), reverse=True)
    n_up       = len(up_steps)
    sweep_path = up_steps + down_steps
    rssi_labels = [str(round(TX_POWER_DBM - PATH_LOSS_DB - a, 1)) for a in sweep_path]
    x_pos       = list(range(len(sweep_path)))

    def median_seq(rows, key, steps):
        buckets = defaultdict(list)
        for r in rows:
            v = r.get(key)
            if v is not None:
                buckets[r["attenuation_db"]].append(v)
        return [statistics.median(buckets[s]) if buckets.get(s) else None for s in steps]

    sweep_label = (f"{round(TX_POWER_DBM - PATH_LOSS_DB - ATTEN_START, 1)} → "
                   f"{round(TX_POWER_DBM - PATH_LOSS_DB - ATTEN_END, 1)} → "
                   f"{round(TX_POWER_DBM - PATH_LOSS_DB - ATTEN_START, 1)} dBm")

    def _x_axis(tickvals, ticktext, title="RSSI (dBm)"):
        return dict(tickmode="array", tickvals=tickvals, ticktext=ticktext, title=title)

    up_br  = median_seq(up_m,   "bitrate_mbps", up_steps)
    dn_br  = median_seq(down_m, "bitrate_mbps", down_steps)
    up_cbr = median_seq(up_m,   "chunk_mbps",   up_steps)
    dn_cbr = median_seq(down_m, "chunk_mbps",   down_steps)

    fig4a = go.Figure([
        go.Scatter(x=x_pos[:n_up], y=up_br,  name="Dynamic up",        mode="lines+markers", line=dict(color=C_UP)),
        go.Scatter(x=x_pos[n_up:], y=dn_br,  name="Dynamic down",      mode="lines+markers", line=dict(color=C_DOWN)),
        go.Scatter(x=x_pos[:n_up], y=up_cbr, name="Chunk-derived up",  mode="lines+markers", line=dict(color=C_CHUNK)),
        go.Scatter(x=x_pos[n_up:], y=dn_cbr, name="Chunk-derived down",mode="lines+markers", line=dict(color=C_CHUNK, dash="dash")),
    ])
    fig4a.update_layout(
        title=f"Median Bitrate Comparison vs RSSI ({sweep_label})",
        template=TPL, height=600,
        xaxis=_x_axis(x_pos, rssi_labels),
        yaxis=dict(title="Bitrate (Mbps)"),
        legend=dict(x=1.02, y=1),
        margin=dict(l=60, r=200, t=60, b=60),
    )

    # ── Tab 5: Median Freq & BW ───────────────────────────────────────────────
    fig4b = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.12,
                          subplot_titles=("Median Frequency (MHz)", "Median Bandwidth (MHz)"))
    for row_idx, (metric, color) in enumerate([("frequency_mhz", C_FREQ), ("bandwidth_mhz", C_BW)], start=1):
        up_med = median_seq(up_m,   metric, up_steps)
        dn_med = median_seq(down_m, metric, down_steps)
        show_leg = (row_idx == 1)
        fig4b.add_trace(go.Scatter(x=x_pos[:n_up], y=up_med, name="Up sweep",   mode="lines+markers", line=dict(color=C_UP),   showlegend=show_leg), row=row_idx, col=1)
        fig4b.add_trace(go.Scatter(x=x_pos[n_up:], y=dn_med, name="Down sweep", mode="lines+markers", line=dict(color=C_DOWN), showlegend=show_leg), row=row_idx, col=1)
    fig4b.update_layout(title=f"Median Freq & BW vs RSSI ({sweep_label})", template=TPL, height=600, legend=dict(x=1.02, y=1), margin=dict(l=60, r=160, t=60, b=60))
    for _row in [1, 2]:
        fig4b.update_xaxes(tickmode="array", tickvals=x_pos, ticktext=rssi_labels, row=_row, col=1)
    fig4b.update_xaxes(title_text="RSSI (dBm)", row=2, col=1)

    # ── Tab 6: Chunk Stats ────────────────────────────────────────────────────
    # filter to records that have chunk data
    up_ck   = [r for r in up   if r.get("chunk_avg_bytes") is not None]
    down_ck = [r for r in down if r.get("chunk_avg_bytes") is not None]

    C_DROP = "#ff6b6b"

    fig_chunk = make_subplots(
        rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.07,
        subplot_titles=(
            "Avg Chunk Size (bytes) — H.264/H.265 payload per callback",
            "Chunk-Derived Bitrate vs KeyDynamicDataRate (Mbps)",
            "Keyframes per Second",
            "Dropped Frames per Second (PTS gap detection)",
        )
    )
    # Panel 1: avg chunk size
    fig_chunk.add_trace(go.Scatter(x=t(up_ck),   y=val(up_ck,   "chunk_avg_bytes"), name="Avg chunk (up)",   mode="lines+markers", line=dict(color=C_CHUNK)), row=1, col=1)
    fig_chunk.add_trace(go.Scatter(x=t(down_ck), y=val(down_ck, "chunk_avg_bytes"), name="Avg chunk (down)", mode="lines+markers", line=dict(color=C_CHUNK, dash="dash")), row=1, col=1)

    # Panel 2: chunk Mbps vs dynamic Mbps
    fig_chunk.add_trace(go.Scatter(x=t(up_ck),   y=val(up_ck,   "chunk_mbps"), name="Chunk Mbps (up)",   mode="lines", line=dict(color=C_CHUNK)), row=2, col=1)
    fig_chunk.add_trace(go.Scatter(x=t(down_ck), y=val(down_ck, "chunk_mbps"), name="Chunk Mbps (down)", mode="lines", line=dict(color=C_CHUNK, dash="dash")), row=2, col=1)
    fig_chunk.add_trace(go.Scatter(x=t(up),   y=val(up,   "bitrate_mbps"), name="Dynamic (up)",   mode="lines", line=dict(color=C_UP,   width=1), opacity=0.6), row=2, col=1)
    fig_chunk.add_trace(go.Scatter(x=t(down), y=val(down, "bitrate_mbps"), name="Dynamic (down)", mode="lines", line=dict(color=C_DOWN, width=1), opacity=0.6), row=2, col=1)

    # Panel 3: keyframes/sec
    fig_chunk.add_trace(go.Scatter(x=t(up_ck),   y=val(up_ck,   "chunk_keyframes"), name="Keyframes (up)",   mode="lines+markers", line=dict(color=C_VIDEO)), row=3, col=1)
    fig_chunk.add_trace(go.Scatter(x=t(down_ck), y=val(down_ck, "chunk_keyframes"), name="Keyframes (down)", mode="lines+markers", line=dict(color=C_VIDEO, dash="dash")), row=3, col=1)

    # Panel 4: dropped frames/sec
    fig_chunk.add_trace(go.Scatter(x=t(up_ck),   y=val(up_ck,   "chunk_dropped"), name="Dropped (up)",   mode="lines+markers", line=dict(color=C_DROP)), row=4, col=1)
    fig_chunk.add_trace(go.Scatter(x=t(down_ck), y=val(down_ck, "chunk_dropped"), name="Dropped (down)", mode="lines+markers", line=dict(color=C_DROP, dash="dash")), row=4, col=1)

    fig_chunk.update_layout(title="Chunk Stats vs Time", template=TPL, height=1000, legend=dict(x=1.02, y=1), margin=dict(l=60, r=180, t=60, b=60))
    fig_chunk.update_xaxes(title_text="Time (s)", row=4, col=1)
    fig_chunk.update_yaxes(title_text="Bytes",        row=1, col=1)
    fig_chunk.update_yaxes(title_text="Mbps",         row=2, col=1)
    fig_chunk.update_yaxes(title_text="Keyframes/s",  row=3, col=1)
    fig_chunk.update_yaxes(title_text="Dropped/s",    row=4, col=1)

    # ── Tab 7: Bitrate Comparison Table ──────────────────────────────────────
    if summary_rows is None:
        summary_rows = _compute_summary(records)

    def _fmt(v): return f"{v:.3f}" if v is not None else "—"
    def _fmt_chunk(v): return f"{v:.3f}" if v is not None else "0.000"

    rows_html = "".join(
        f'<tr style="background:{"#1e1e1e" if i%2==0 else "#2a2a2a"}">'
        f'<td>{r["attenuation_db"]}</td>'
        f'<td>{r["rssi_dbm"]}</td>'
        f'<td style="color:{C_UP}">{_fmt(r.get("median_bitrate_up_mbps"))}</td>'
        f'<td style="color:{C_CHUNK}">{_fmt_chunk(r.get("median_chunk_mbps_up"))}</td>'
        f'<td style="color:{C_DOWN}">{_fmt(r.get("median_bitrate_dn_mbps"))}</td>'
        f'<td style="color:#e8c96d">{_fmt_chunk(r.get("median_chunk_mbps_dn"))}</td>'
        f'</tr>'
        for i, r in enumerate(summary_rows)
    )

    tab7_html = f"""
    <div style="padding:20px;font-family:sans-serif;overflow-x:auto">
      <p style="color:#aaa;margin:0 0 6px;font-size:13px">
        <b style="color:{C_UP}">Dynamic</b> = KeyDynamicDataRate (total RF link, incl. FEC &amp; telemetry) &nbsp;|&nbsp;
        <b style="color:{C_CHUNK}">Chunk</b> = H.264/H.265 payload bytes received by app (MediaDataCenter.cameraStreamManager)
      </p>
      <table style="border-collapse:collapse;font-size:12px;color:#eee;min-width:600px">
        <thead>
          <tr style="background:#1a3a5c;color:#fff">
            <th rowspan="2" style="padding:6px 12px;border:1px solid #444">Atten (dB)</th>
            <th rowspan="2" style="padding:6px 12px;border:1px solid #444">RSSI (dBm)</th>
            <th colspan="2" style="padding:6px 12px;border:1px solid #444;background:#1a4a2c">↑ Up Sweep (Mbps)</th>
            <th colspan="2" style="padding:6px 12px;border:1px solid #444;background:#4a2a1a">↓ Down Sweep (Mbps)</th>
          </tr>
          <tr style="background:#162e20">
            <th style="padding:6px 12px;border:1px solid #444;color:{C_UP}">Dynamic</th>
            <th style="padding:6px 12px;border:1px solid #444;color:{C_CHUNK}">Chunk</th>
            <th style="padding:6px 12px;border:1px solid #444;color:{C_DOWN}">Dynamic</th>
            <th style="padding:6px 12px;border:1px solid #444;color:#e8c96d">Chunk</th>
          </tr>
        </thead>
        <tbody style="text-align:center">
          {rows_html}
        </tbody>
      </table>
    </div>"""

    # ── Build tabbed HTML ─────────────────────────────────────────────────────
    tab_defs = [
        ("Bitrate vs Time",       fig1),
        ("Frequency vs Time",     fig2),
        ("Bandwidth vs Time",     fig3),
        ("Median Bitrates",       fig4a),
        ("Median Freq & BW",      fig4b),
        ("Chunk Stats",           fig_chunk),
        ("Bitrate Comparison",    tab7_html),
    ]

    first_plotly = True
    divs = []
    for i, (_, content) in enumerate(tab_defs):
        if isinstance(content, str):
            divs.append(content)
        else:
            inc_js = (inline_js and first_plotly)
            first_plotly = False
            divs.append(pio.to_html(content, full_html=False,
                                    include_plotlyjs=inc_js, div_id=f"tab{i}"))

    btn_html = "".join(
        f'<button onclick="showTab({i})" id="btn{i}">{label}</button>'
        for i, (label, _) in enumerate(tab_defs)
    )
    n_tabs = len(tab_defs)

    plotly_tag = ("" if inline_js else
                  '<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>')

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>DJI O3 Sweep {datetime.now().strftime("%Y-%m-%d %H:%M")}</title>
{plotly_tag}
<style>
  body  {{ background:#111; color:#eee; font-family:sans-serif; margin:0; padding:12px; }}
  h2    {{ margin:0 0 10px; font-weight:400; font-size:15px; color:#aaa; }}
  .bar  {{ display:flex; gap:4px; margin-bottom:0; flex-wrap:wrap; }}
  .bar button {{
    padding:8px 20px; border:1px solid #444; border-bottom:none;
    border-radius:6px 6px 0 0; background:#222; color:#888;
    cursor:pointer; font-size:13px; transition:background .15s;
  }}
  .bar button.active {{ background:#2d2d2d; color:#fff; border-color:#666; }}
  .panel {{ border:1px solid #444; border-radius:0 6px 6px 6px; padding:8px; background:#1a1a1a; }}
  td, th {{ padding:6px 14px; border:1px solid #333; }}
</style>
</head>
<body>
<h2>DJI O3 Attenuation Sweep — {datetime.now().strftime("%Y-%m-%d %H:%M")} &nbsp;|&nbsp;
Step: {ATTEN_STEP} dB &nbsp;|&nbsp; Settle: {SETTLE_TIME}s + Measure: {MEASURE_TIME}s &nbsp;|&nbsp; Rows: {len(records)}</h2>
<div class="bar">{btn_html}</div>
<div class="panel">{"".join(f'<div id="tc{i}" style="display:{"block" if i==0 else "none"}">{div}</div>' for i, div in enumerate(divs))}</div>
<script>
var N = {n_tabs};
function showTab(i) {{
  for (var j = 0; j < N; j++) {{
    document.getElementById("tc"  + j).style.display = j === i ? "block" : "none";
    document.getElementById("btn" + j).classList.toggle("active", j === i);
  }}
  var el = document.getElementById("tc" + i).querySelector(".plotly-graph-div");
  if (el) Plotly.Plots.resize(el);
}}
document.getElementById("btn0").classList.add("active");
</script>
</body>
</html>"""

    html_path.write_text(html)
    print(f"Plot: {html_path}")
    webbrowser.open(str(html_path))


if __name__ == "__main__":
    import sys
    inline_js = "--inline-js" in sys.argv
    _args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(_args) == 1 and _args[0].endswith(".csv"):
        import csv as _csv
        p = Path(_args[0])
        with open(p) as f:
            rows = list(_csv.DictReader(f))
        for r in rows:
            if "phase" not in r:
                r["phase"] = "measure"
            for k in ("elapsed_sec", "attenuation_db"):
                r[k] = float(r[k]) if r.get(k) else None
            for k in ("bitrate_mbps", "video_feed_bw_mbps", "video_bitrate_mbps", "chunk_mbps",
                       "chunk_avg_bytes", "chunk_keyframes", "chunk_dropped", "chunk_fps",
                       "frequency_mhz", "bandwidth_mhz"):
                r[k] = float(r[k]) if r.get(k) and r[k] not in ("None", "") else None
            if r["bitrate_mbps"] == 9.765625:
                r["bitrate_mbps"] = None
            if not r.get("rssi_dbm"):
                r["rssi_dbm"] = round(TX_POWER_DBM - PATH_LOSS_DB - r["attenuation_db"], 2) if r["attenuation_db"] is not None else None
            else:
                r["rssi_dbm"] = float(r["rssi_dbm"])
            if "direction" not in r:
                r["direction"] = "up"
        make_plot(rows, p.with_suffix(".html"), _compute_summary(rows), inline_js=inline_js)
    else:
        main(inline_js=inline_js)
