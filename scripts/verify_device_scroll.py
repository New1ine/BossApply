"""Measure a connected phone's job list without opening jobs or sending messages.

XML and screenshots are collected sequentially, not as an atomic pair. This
script never installs the component, starts BOSS, changes settings, or taps UI.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import statistics
import sys
import time
import uuid
import zipfile


PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from bossapply.engine.adb_client import AdbClient, AdbTimeoutError, TransientUiDumpError
from bossapply.engine.ui_probe import ComponentReader
from bossapply.engine.uixml import node_id, query, rect
from bossapply.engine.workflow import BOSS_PACKAGE, Workflow


SHANGHAI = timezone(timedelta(hours=8))


def wall_time():
    return datetime.now(SHANGHAI).isoformat(timespec="milliseconds")


def held_gesture_command(x, start, end, duration):
    """Build a bounded shell experiment, with numeric arguments only.

    Separate input processes add unmeasured scheduling overhead to the nominal
    pauses. Their event downTime values are not a single continuous timestamp.
    """
    steps = max(4, min(24, (abs(start - end) + 79) // 80))
    pause = duration / (steps * 1000)
    cancel = f"input touchscreen motionevent CANCEL {x} {end} >/dev/null 2>&1"
    commands = ["set -e", f"trap '{cancel}' EXIT HUP INT TERM",
                f"input touchscreen motionevent DOWN {x} {start}"]
    for step in range(1, steps + 1):
        y = round(start + (end - start) * step / steps)
        commands.extend((f"input touchscreen motionevent MOVE {x} {y}", f"sleep {pause:.6f}"))
    commands.extend(("sleep 0.150", f"input touchscreen motionevent MOVE {x} {end}",
                     f"input touchscreen motionevent UP {x} {end}", "trap - EXIT HUP INT TERM"))
    return "; ".join(commands), steps


class RecordingClient(AdbClient):
    """Preserve the exact XML bytes before the normal parser consumes them."""

    last_raw = None

    def _parse_ui(self, raw):
        self.last_raw = raw
        return AdbClient._parse_ui(raw)


def duplicate_card_keys(window):
    listing = query(window, id="rv_list")
    parents = {child: parent for parent in listing.iter() for child in parent}
    seen = set()
    keys = []
    for title_node in listing.iter("node"):
        if node_id(title_node) != "tv_position_name":
            continue
        parent = title_node
        while parent is not listing and parent.get("clickable") != "true":
            parent = parents[parent]
        if parent is listing or id(parent) in seen:
            continue
        seen.add(id(parent))
        title = title_node.get("text", "").replace(" &@ ", "").strip()
        company = next((child.get("text", "").strip() for child in parent.iter("node")
                        if node_id(child) == "tv_company_name"), "")
        if title:
            keys.append((title, company))
    return [list(key) for key, count in Counter(keys).items() if count > 1]


def inspect_list(root):
    windows = root.findall("node")
    if len(windows) != 1 or windows[0].get("package") != BOSS_PACKAGE:
        raise RuntimeError("Current foreground must be one BOSS window; no gesture was issued.")
    window = windows[0]
    cards = Workflow.job_list_cards(window)
    if cards is None:
        raise RuntimeError("Current window is not the BOSS job list; no gesture was issued.")
    viewport = rect(query(window, id="rv_list"))
    if viewport is None:
        raise RuntimeError("Job-list viewport has no valid bounds.")
    decorated = []
    for card in cards:
        item = dict(card)
        item["production_complete"] = item.pop("complete")
        bounds = item["bounds"]
        item["touches_top"] = bounds[1] <= viewport[1] + 1
        item["touches_bottom"] = bounds[3] >= viewport[3] - 1
        item["fully_inside_observed_bounds"] = (
            viewport[0] <= bounds[0] < bounds[2] <= viewport[2]
            and viewport[1] < bounds[1] < bounds[3] < viewport[3]
        )
        decorated.append(item)
    return {
        "viewport": viewport,
        "window_id": root.get("window-id", window.get("window-id")),
        "scroll_sequence": root.get("scroll-sequence", window.get("scroll-sequence")),
        "cards": decorated,
        "duplicate_keys": duplicate_card_keys(window),
    }


def frame_signature(frame):
    return (frame["window_id"], frame["scroll_sequence"], tuple(frame["viewport"]),
            tuple((tuple(card["key"]), tuple(card["bounds"]), card["salary"],
                   tuple(card["requirements"]), card["production_complete"])
                  for card in frame["cards"]))


def compare_frames(before, after):
    """Report observed overlap, never claim a measured global omission rate."""
    result = {"coverage": "unknown", "common_cards": [], "content_up_px": None,
              "displacement_spread_px": None}
    if before["duplicate_keys"] or after["duplicate_keys"]:
        result["reason"] = "duplicate_title_company_keys"
        return result
    if before["viewport"] != after["viewport"]:
        result["reason"] = "viewport_changed"
        return result
    previous = {tuple(card["key"]): card for card in before["cards"]}
    movements = []
    common_order = []
    for card in after["cards"]:
        key = tuple(card["key"])
        old = previous.get(key)
        if old is None:
            continue
        common_order.append(key)
        # A bottom-clipped card still has an observable top. A top-clipped card
        # does not, so it cannot measure vertical displacement reliably.
        if old["touches_top"] or card["touches_top"]:
            continue
        if old["company"] != card["company"]:
            continue
        movement = old["bounds"][1] - card["bounds"][1]
        movements.append(movement)
        result["common_cards"].append({"key": list(key), "content_up_px": movement})
    expected_order = [tuple(card["key"]) for card in before["cards"]
                      if tuple(card["key"]) in common_order]
    if common_order != expected_order:
        result["reason"] = "common_cards_reordered"
        return result
    if movements:
        result["content_up_px"] = statistics.median(movements)
        result["displacement_spread_px"] = max(movements) - min(movements)
        if result["displacement_spread_px"] <= max(4, (before["viewport"][3] - before["viewport"][1]) * .01):
            result["coverage"] = "observed_overlap"
        else:
            result["reason"] = "inconsistent_common_card_displacement"
    else:
        result["reason"] = "no_common_card_with_observable_top"
    return result


class Measurement:
    def __init__(self, adb, args, output):
        self.adb, self.args, self.output = adb, args, output
        self.started = time.monotonic()
        self.native_remote = None
        self.native_losses = {"up": [], "down": []}
        self.report = {
            "started_at": wall_time(), "phase": args.phase, "reader": args.reader,
            "gesture_mode": args.gesture_mode,
            "status": "running", "reads": [], "screenshots": [], "gestures": [], "rounds": [],
            "native_transfers": [], "native_loss_samples": [],
            "notes": [
                "Screenshots and XML are sequential and non-atomic.",
                "Bounds may be clipped; production_complete is a heuristic, not visual ground truth.",
                "Stable means observed matching snapshots; delayed loading may occur later.",
                "Observed overlap cannot establish a global zero omission/duplicate rate.",
                "The script performs no UI taps, application launch, installation or delivery.",
                "Held mode is an experiment: per-input process overhead adds to nominal pauses; "
                "each input motionevent may use a different downTime timestamp.",
            ],
        }

    def save(self):
        (self.output / "summary.json").write_text(
            json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")

    def prepare_native(self):
        if self.args.gesture_mode != "native" or self.args.phase not in ("legacy", "anchor"):
            return
        jar = (self.args.gesture_jar or PROJECT / "build" / "android" / "BossApplyScrollGesture.jar").resolve()
        with zipfile.ZipFile(jar) as archive:
            if "classes.dex" not in archive.namelist():
                raise RuntimeError("Gesture jar must contain classes.dex; build_scroll_gesture.ps1 first.")
        self.native_remote = f"/data/local/tmp/bossapply-scroll-{uuid.uuid4().hex}.jar"
        event = {"operation": "push", "local_file": str(jar), "remote_file": self.native_remote,
                 "started_s": time.monotonic() - self.started}
        try:
            raw = self.adb.run("-s", self.adb.serial, "push", str(jar), self.native_remote,
                               timeout=self.args.timeout)
            event.update(status="ok", stdout=raw.decode("utf-8", errors="replace"))
        except Exception as exc:
            event.update(status="error", error=str(exc))
            raise
        finally:
            event["duration_s"] = time.monotonic() - self.started - event["started_s"]
            self.report["native_transfers"].append(event)
            self.save()

    def cleanup_native(self):
        if self.native_remote is None:
            return
        event = {"operation": "remove", "remote_file": self.native_remote,
                 "started_s": time.monotonic() - self.started}
        try:
            # This path is generated here, never read from a user-supplied path.
            self.adb.shell("rm", "-f", self.native_remote, timeout=5)
            event["status"] = "ok"
        except Exception as exc:
            event.update(status="error", error=str(exc))
        finally:
            event["duration_s"] = time.monotonic() - self.started - event["started_s"]
            self.report["native_transfers"].append(event)
            self.native_remote = None
            self.save()

    def native_loss(self, upward):
        samples = self.native_losses["up" if upward else "down"]
        return statistics.median(samples) if samples else 0

    def learn_native_loss(self, gesture, comparison, viewport):
        if self.args.gesture_mode != "native":
            return
        finger = gesture["finger_up_px"]
        content = comparison["content_up_px"]
        event = {"gesture_label": gesture["label"], "finger_up_px": finger,
                 "content_up_px": content, "direction": "up" if finger > 0 else "down",
                 "accepted": False}
        if comparison["coverage"] != "observed_overlap" or content is None or finger * content <= 0:
            event["reason"] = "unconfirmed_or_opposite_displacement"
        else:
            loss = abs(finger) - abs(content)
            event["loss_px"] = loss
            if 0 <= loss <= (viewport[3] - viewport[1]) * .1:
                self.native_losses[event["direction"]].append(loss)
                event.update(accepted=True, learned_median_px=self.native_loss(finger > 0))
            else:
                event["reason"] = "loss_outside_trust_range"
        self.report["native_loss_samples"].append(event)

    def read(self, label, *, scroll=False, timeout=None):
        index = len(self.report["reads"]) + 1
        filename = f"read-{index:04d}.xml"
        entry = {"index": index, "label": label, "started_at": wall_time(),
                 "started_s": time.monotonic() - self.started, "wait_for_scroll": scroll}
        self.adb.last_raw = None
        try:
            root = self.adb.dump_ui(timeout=timeout or self.args.timeout,
                                    wait_for_scroll=scroll and self.args.reader == "component")
            frame = inspect_list(root)
            entry.update(frame, status="ok")
            entry["index"] = index
            return entry
        except Exception as exc:
            entry.update(status="error", error_type=type(exc).__name__, error=str(exc))
            raise
        finally:
            entry["finished_at"] = wall_time()
            entry["finished_s"] = time.monotonic() - self.started
            entry["duration_s"] = entry["finished_s"] - entry["started_s"]
            if self.adb.last_raw is not None:
                (self.output / filename).write_bytes(self.adb.last_raw)
                entry["xml"] = filename
            self.report["reads"].append(entry)
            self.save()

    def stable(self, label, *, after_gesture_s=None):
        deadline = time.monotonic() + self.args.timeout
        signature = None
        unchanged_since = None
        first_success_s = None
        attempts = []
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                frame = self.read(label, scroll=after_gesture_s is not None,
                                  timeout=min(self.args.timeout, remaining))
            except (TransientUiDumpError, AdbTimeoutError):
                signature, unchanged_since = None, None
            else:
                attempts.append(frame["index"])
                if first_success_s is None:
                    first_success_s = frame["finished_s"]
                current = frame_signature(frame)
                if current == signature:
                    if time.monotonic() - unchanged_since >= self.args.stable_ms / 1000:
                        timing = {"read_indices": attempts, "first_readable_s": first_success_s,
                                  "observed_stable_s": frame["finished_s"]}
                        if after_gesture_s is not None:
                            timing["first_readable_after_command_s"] = first_success_s - after_gesture_s
                            timing["observed_stable_after_command_s"] = frame["finished_s"] - after_gesture_s
                        return frame, timing
                else:
                    signature, unchanged_since = current, time.monotonic()
            time.sleep(min(.04, max(0, deadline - time.monotonic())))
        raise RuntimeError("No matching stable job-list snapshots before measurement timeout.")

    def screenshot(self, frame, label):
        index = len(self.report["screenshots"]) + 1
        entry = {"label": label, "paired_read_index": frame["index"],
                 "started_at": wall_time(), "started_s": time.monotonic() - self.started,
                 "atomic_with_xml": False}
        try:
            raw = self.adb.run("-s", self.adb.serial, "exec-out", "screencap", "-p", timeout=self.args.timeout)
            if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError("screencap did not return PNG bytes")
            filename = f"screen-{index:04d}.png"
            (self.output / filename).write_bytes(raw)
            entry.update(status="ok", file=filename)
        except Exception as exc:
            entry.update(status="error", error=str(exc))
            raise
        finally:
            entry["finished_s"] = time.monotonic() - self.started
            entry["duration_s"] = entry["finished_s"] - entry["started_s"]
            self.report["screenshots"].append(entry)
            self.save()

    def gesture(self, before, *, x, start, end, duration, label):
        left, top, right, bottom = before["viewport"]
        if before["duplicate_keys"]:
            raise RuntimeError("Duplicate card keys prevent reliable measurement; refusing gesture.")
        if not before["cards"]:
            raise RuntimeError("No observable job cards; refusing gesture.")
        if not (left < x < right and top < start < bottom and top < end < bottom and start != end):
            raise RuntimeError("Planned gesture is outside the observed job-list safe area.")
        entry = {"label": label, "before_read_index": before["index"], "x": x,
                 "start_y": start, "end_y": end, "finger_up_px": start - end,
                 "requested_duration_ms": duration, "gesture_mode": self.args.gesture_mode,
                 "started_at": wall_time(),
                 "started_s": time.monotonic() - self.started}
        try:
            if self.args.gesture_mode == "native":
                if self.native_remote is None:
                    raise RuntimeError("Native gesture jar has not been staged.")
                command = (f"CLASSPATH={self.native_remote} app_process / "
                           f"com.bossapply.scrolltest.ScrollGesture {x} {start} {x} {end} {duration} 150")
                entry.update(shell_command=command, terminal_hold_ms=150)
                raw = self.adb.shell(command, timeout=self.args.timeout)
                entry["stdout"] = raw.decode("utf-8", errors="replace")
                native = json.loads(entry["stdout"].strip().splitlines()[-1])
                entry["native_result"] = native
                if native.get("ok") is not True:
                    raise RuntimeError(f"Native input injection failed: {native}")
            elif self.args.gesture_mode == "held":
                command, steps = held_gesture_command(x, start, end, duration)
                entry.update(shell_command=command, move_steps=steps, terminal_hold_ms=150,
                             nominal_duration_is_exact=False)
                self.adb.shell(command, timeout=self.args.timeout)
            else:
                command = ["input", "swipe", str(x), str(start), str(x), str(end), str(duration)]
                entry["shell_arguments"] = command
                self.adb.shell(*command, timeout=self.args.timeout)
            entry["status"] = "ok"
        except Exception as exc:
            entry.update(status="error", error=str(exc))
            raise
        finally:
            entry["finished_s"] = time.monotonic() - self.started
            entry["command_duration_s"] = entry["finished_s"] - entry["started_s"]
            self.report["gestures"].append(entry)
            self.save()
        return entry

    def run(self):
        if self.args.phase in ("snapshot", "baseline"):
            count = 1 if self.args.phase == "snapshot" else (
                self.args.samples or (10 if self.args.reader == "component" else 3))
            previous = None
            for index in range(count):
                frame, timing = self.stable("snapshot") if self.args.phase == "snapshot" else (
                    self.read(f"baseline-{index + 1}"), None)
                if previous is not None:
                    frame["comparison_to_previous"] = compare_frames(previous, frame)
                if timing is not None:
                    self.report["snapshot_stability"] = timing
                self.screenshot(frame, f"{self.args.phase}-{index + 1}")
                previous = frame
            return
        for index in range(self.args.rounds):
            if self.args.phase == "legacy":
                self.legacy_round(index + 1)
            else:
                self.anchor_round(index + 1)

    def legacy_round(self, number):
        before, _ = self.stable(f"legacy-{number}-before")
        self.screenshot(before, f"legacy-{number}-before")
        # Re-read after the screenshot so every gesture checks a current list.
        before = self.read(f"legacy-{number}-pre-gesture")
        width, height = self.adb.size
        start = min(round(height * .65), height - 1)
        end = max(0, int(start - height * .145))
        gesture = self.gesture(before, x=width // 2, start=start, end=end,
                               duration=self.args.duration_ms or 200, label=f"legacy-{number}")
        after, timing = self.stable(f"legacy-{number}-after", after_gesture_s=gesture["finished_s"])
        self.screenshot(after, f"legacy-{number}-after")
        self.report["rounds"].append({"number": number, "before_read_index": before["index"],
                                      "after_read_index": after["index"], "stability": timing,
                                      "comparison": compare_frames(before, after)})
        self.save()

    def anchor_round(self, number):
        initial, _ = self.stable(f"anchor-{number}-before")
        self.screenshot(initial, f"anchor-{number}-before")
        frame = self.read(f"anchor-{number}-pre-gesture")
        if not frame["cards"] or frame["duplicate_keys"]:
            raise RuntimeError("No unambiguous cards for an anchor measurement.")
        candidates = [card for card in frame["cards"] if card["touches_bottom"] and card["company"]]
        anchor = (candidates or [card for card in frame["cards"] if card["company"]])
        if not anchor:
            raise RuntimeError("No identifiable card with title and company for an anchor.")
        key = tuple(anchor[-1]["key"])
        result = {"number": number, "anchor_key": list(key),
                  "anchor_selection": "bottom_partial" if candidates else "last_card_overlap",
                  "before_read_index": frame["index"], "adjustments": [], "status": "adjustment_limit"}
        self.report["rounds"].append(result)
        for adjustment in range(self.args.max_adjustments):
            matches = [card for card in frame["cards"] if tuple(card["key"]) == key]
            if len(matches) != 1 or frame["duplicate_keys"]:
                result.update(status="anchor_missing_or_ambiguous", coverage="unknown")
                break
            card = matches[0]
            left, top, right, bottom = frame["viewport"]
            height = bottom - top
            safe_top = top + max(8, round(height * .015))
            error = card["bounds"][1] - safe_top
            result["target_top_y"] = safe_top
            if card["touches_top"]:
                result.update(status="anchor_top_clipped", coverage="unknown")
                break
            if abs(error) <= max(8, round(height * .01)):
                result["status"] = "aligned"
                break
            # Keep the entire gesture inside the list. Duration controls speed;
            # it is not assumed to produce a proportional content displacement.
            margin = max(12, round(height * .08))
            low, high = top + margin, bottom - margin
            learned_loss = self.native_loss(error > 0) if self.args.gesture_mode == "native" else 0
            distance = min(abs(error) + learned_loss, high - low)
            if distance < 2:
                result["status"] = "no_safe_gesture_distance"
                break
            start = high if error > 0 else low
            end = start - distance if error > 0 else start + distance
            duration = self.args.duration_ms or max(300, min(1500, round(distance / (height * .7) * 1000)))
            gesture = self.gesture(frame, x=(left + right) // 2, start=round(start), end=round(end),
                                   duration=duration, label=f"anchor-{number}-{adjustment + 1}")
            after, timing = self.stable(f"anchor-{number}-after-{adjustment + 1}",
                                       after_gesture_s=gesture["finished_s"])
            comparison = compare_frames(frame, after)
            self.learn_native_loss(gesture, comparison, frame["viewport"])
            result["adjustments"].append({"before_read_index": frame["index"],
                                           "after_read_index": after["index"],
                                           "anchor_error_before_px": error, "stability": timing,
                                           "learned_touch_loss_px": learned_loss,
                                           "comparison": comparison})
            frame = after
            self.save()
            if comparison["coverage"] == "unknown":
                result.update(status="coverage_unknown", coverage="unknown")
                break
            # Before a further correction, verify the current foreground again.
            frame = self.read(f"anchor-{number}-pre-correction")
        matches = [card for card in frame["cards"] if tuple(card["key"]) == key]
        if len(matches) == 1:
            height = frame["viewport"][3] - frame["viewport"][1]
            target = frame["viewport"][1] + max(8, round(height * .015))
            error = matches[0]["bounds"][1] - target
            result.update(final_anchor_error_px=error,
                          final_production_complete=matches[0]["production_complete"])
            if result["status"] == "adjustment_limit" and abs(error) <= max(8, round(height * .01)):
                result["status"] = "aligned"
        else:
            result.update(final_anchor_error_px=None, final_production_complete=None)
        result.update(after_read_index=frame["index"], comparison=compare_frames(initial, frame))
        self.screenshot(frame, f"anchor-{number}-after")
        self.save()
        if result["status"] != "aligned":
            # Preserve the evidence instead of moving further from an uncertain anchor.
            raise RuntimeError(f"Anchor round stopped: {result['status']}; see summary.json.")


def positive_int(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("snapshot", "baseline", "legacy", "anchor"), default="snapshot")
    parser.add_argument("--reader", choices=("command", "component"), default="command")
    parser.add_argument("--gesture-mode", choices=("swipe", "held", "native"), default="swipe",
                        help="held uses shell MOVE; native uses one continuous app_process gesture")
    parser.add_argument("--gesture-jar", type=Path, help="native dex jar; default build/android/BossApplyScrollGesture.jar")
    parser.add_argument("--serial", default="auto", help="select one connected authorized phone")
    parser.add_argument("--port", type=int, default=0, help="ADB server port; 0 discovers existing servers")
    parser.add_argument("--adb", default="auto", help="ADB executable path")
    parser.add_argument("--output", type=Path, help="new output directory; default build/verification/<timestamp>")
    parser.add_argument("--rounds", type=positive_int, default=3, help="swipe rounds for legacy/anchor")
    parser.add_argument("--samples", type=positive_int, help="baseline reads; default command=3, component=10")
    parser.add_argument("--duration-ms", type=positive_int, help="override gesture duration; legacy default=200")
    parser.add_argument("--max-adjustments", type=positive_int, default=3, help="maximum gestures per anchor round")
    parser.add_argument("--timeout", type=positive_int, default=18, help="per-read/stability safety deadline in seconds")
    parser.add_argument("--stable-ms", type=positive_int, default=120, help="minimum span of matching snapshots")
    return parser.parse_args()


def main():
    args = arguments()
    output = (args.output or PROJECT / "build" / "verification" /
              datetime.now(SHANGHAI).strftime("%Y%m%d-%H%M%S-%f")).resolve()
    output.mkdir(parents=True, exist_ok=False)
    adb = RecordingClient({"path": args.adb, "serial": args.serial, "port": args.port, "click_interval": 0})
    measurement = Measurement(adb, args, output)
    measurement.save()
    print(f"Output: {output}", flush=True)
    code = 0
    try:
        adb.connect()
        measurement.report["device"] = {"serial": adb.serial, "port": adb.port, "screen_size": adb.size}
        measurement.prepare_native()
        context = ComponentReader(adb) if args.reader == "component" else nullcontext()
        with context:
            measurement.run()
        measurement.report["status"] = "complete"
    except (Exception, KeyboardInterrupt) as exc:
        measurement.report.update(status="stopped", error_type=type(exc).__name__, error=str(exc))
        print(f"Stopped: {exc}", file=sys.stderr, flush=True)
        code = 1
    finally:
        measurement.cleanup_native()
        durations = [entry["duration_s"] for entry in measurement.report["reads"] if entry["status"] == "ok"]
        if durations:
            ordered = sorted(durations)
            measurement.report["successful_read_durations_s"] = {
                "count": len(durations), "median": statistics.median(durations), "max": max(durations),
                "p95_nearest_rank": ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)],
            }
        measurement.report.update(finished_at=wall_time(), elapsed_s=time.monotonic() - measurement.started)
        measurement.save()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
