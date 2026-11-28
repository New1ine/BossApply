"""Browse real job cards through Workflow.next_card without opening or applying.

This uses the production parser, scroll planner, reader and checkpoint. It never
calls Workflow.run, apply_one or any sending flow. Tap and Back are blocked.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import statistics
import sys
import time
import traceback


PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from bossapply.core import DEFAULTS
from bossapply.engine.adb_client import AdbClient
from bossapply.engine.ui_probe import ComponentReader
from bossapply.engine.workflow import Workflow


SHANGHAI = timezone(timedelta(hours=8))


def wall_time():
    return datetime.now(SHANGHAI).isoformat(timespec="milliseconds")


class RecordingClient(AdbClient):
    def __init__(self, config, report, output):
        super().__init__(config)
        self.report, self.output = report, output
        self.started = time.monotonic()
        self.last_raw = None
        self.last_successful_read = None

    def save(self):
        (self.output / "summary.json").write_text(
            json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")

    def offset(self):
        return time.monotonic() - self.started

    def tap(self, *_args, **_kwargs):
        raise RuntimeError("Read-only verification blocked a tap; no tap was issued.")

    def back(self, *_args, **_kwargs):
        raise RuntimeError("Read-only verification blocked Back; no key was issued.")

    def shell(self, *args, **kwargs):
        if args and args[0] == "input" and (len(args) < 2 or args[1] != "swipe"):
            raise RuntimeError("Read-only verification permits only swipe input.")
        return super().shell(*args, **kwargs)

    def _parse_ui(self, raw):
        self.last_raw = raw
        return AdbClient._parse_ui(raw)

    def dump_ui(self, timeout=30, *, wait_for_scroll=False):
        index = len(self.report["reads"]) + 1
        entry = {"index": index, "started_at": wall_time(), "started_s": self.offset(),
                 "timeout_s": timeout, "wait_for_scroll": wait_for_scroll}
        self.last_raw = None
        try:
            root = super().dump_ui(timeout, wait_for_scroll=wait_for_scroll)
            entry.update(status="ok", hierarchy_attributes=dict(root.attrib))
            windows = root.findall("node")
            entry["window_packages"] = [node.get("package") for node in windows]
            if len(windows) == 1:
                cards = Workflow.job_list_cards(windows[0])
                if cards is not None:
                    entry["cards"] = cards
                    entry["complete_cards"] = sum(bool(card["complete"]) for card in cards)
            self.last_successful_read = index
            return root
        except Exception as exc:
            entry.update(status="error", error_type=type(exc).__name__, error=str(exc))
            if hasattr(exc, "reason"):
                entry["reason"] = exc.reason
            raise
        finally:
            entry.update(finished_at=wall_time(), finished_s=self.offset())
            entry["duration_s"] = entry["finished_s"] - entry["started_s"]
            if self.last_raw is not None:
                filename = f"read-{index:04d}.xml"
                (self.output / filename).write_bytes(self.last_raw)
                entry["xml"] = filename
            self.report["reads"].append(entry)
            startup = self.report["startup"]
            if "first_dump_attempt_duration_s" not in startup:
                startup["first_dump_attempt_duration_s"] = entry["duration_s"]
            if entry["status"] == "ok" and "first_successful_dump_duration_s" not in startup:
                startup["first_successful_dump_duration_s"] = entry["duration_s"]
                startup["first_successful_dump_index"] = index
            self.save()

    def swipe(self, x1, y1, x2, y2, *, duration=600):
        entry = {"index": len(self.report["swipes"]) + 1, "x1": x1, "y1": y1,
                 "x2": x2, "y2": y2, "requested_duration_ms": duration,
                 "finger_up_px": y1 - y2, "before_read_index": self.last_successful_read,
                 "started_at": wall_time(), "started_s": self.offset()}
        try:
            result = super().swipe(x1, y1, x2, y2, duration=duration)
            entry["status"] = "ok"
            if isinstance(result, dict):
                entry["component_json"] = result
            elif isinstance(result, bytes):
                entry["command_stdout"] = result.decode("utf-8", errors="replace")
            else:
                entry["result"] = result
            return result
        except Exception as exc:
            entry.update(status="error", error_type=type(exc).__name__, error=str(exc))
            raise
        finally:
            entry.update(finished_at=wall_time(), finished_s=self.offset())
            entry["actual_duration_s"] = entry["finished_s"] - entry["started_s"]
            self.report["swipes"].append(entry)
            self.save()

    def screenshot(self, page):
        filename = f"page-{page['index']:04d}.png"
        event = {"file": filename, "started_at": wall_time(), "started_s": self.offset(),
                 "atomic_with_xml": False}
        page["screenshot"] = event
        try:
            raw = self.run("-s", self.serial, "exec-out", "screencap", "-p", timeout=15)
            if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError("Phone screenshot did not return PNG bytes.")
            (self.output / filename).write_bytes(raw)
            event["status"] = "ok"
        except Exception as exc:
            event.update(status="error", error=str(exc))
            raise
        finally:
            event["finished_s"] = self.offset()
            event["duration_s"] = event["finished_s"] - event["started_s"]
            self.save()


def positive_int(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def display_size(value):
    match = re.fullmatch(r"([1-9][0-9]{2,4})x([1-9][0-9]{2,4})", value)
    if match is None:
        raise argparse.ArgumentTypeError("use WIDTHxHEIGHT, for example 1080x2414")
    return value


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cards", type=positive_int, default=15, help="readable cards to consume in memory")
    parser.add_argument("--reader", choices=("component", "command"), default="component")
    parser.add_argument("--serial", default="auto")
    parser.add_argument("--port", type=int, default=0, help="0 discovers existing local ADB servers")
    parser.add_argument("--adb", default="auto", help="ADB executable path")
    parser.add_argument("--output", type=Path, help="new output directory; defaults to build/verification/<time>-fixed")
    parser.add_argument("--display-size", type=display_size,
                        help="temporarily override screen size and restore its exact previous override in finally")
    return parser.parse_args()


def main():
    args = arguments()
    output = (args.output or PROJECT / "build" / "verification" /
              (datetime.now(SHANGHAI).strftime("%Y%m%d-%H%M%S-%f") + "-fixed")).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {"started_at": wall_time(), "status": "running", "reader": args.reader,
              "requested_cards": args.cards, "startup": {}, "reads": [], "swipes": [],
              "pages": [], "selected_cards": [], "logs": [],
              "notes": [
                  "Uses production Workflow.next_card and in-memory checkpoint only.",
                  "No Workflow.run, job opening, application, messaging or image sending is executed.",
                  "Client tap and Back are blocked, including taps requested by quota-reminder handling.",
                  "One device connection and one component reader session are reused throughout the scan.",
                  "Each page is a distinct observed _card_view returned by the scanner, not a guaranteed screenful.",
                  "Screenshots are captured after XML and are not atomic with XML.",
                  "Observed card continuity does not establish a global zero omission rate.",
              ]}
    config = copy.deepcopy(DEFAULTS)
    config["device"].update(path=args.adb, serial=args.serial, port=args.port,
                            click_interval=0, ui_reader=args.reader)
    config["actions"].update(image=False, quick_phrase=False)
    adb = RecordingClient(config["device"], report, output)
    adb.save()
    print(f"Output: {output}", flush=True)
    display_changed = False
    old_override = None
    code = 0
    try:
        started = time.monotonic()
        adb.connect()
        report["startup"]["device_connection_s"] = time.monotonic() - started
        report["device"] = {"serial": adb.serial, "port": adb.port, "initial_screen_size": adb.size}
        if args.display_size:
            original = adb.shell("wm", "size", timeout=10).decode("utf-8", errors="replace")
            if re.search(r"Physical size: [1-9][0-9]*x[1-9][0-9]*", original) is None:
                raise RuntimeError("Cannot determine original display size; refusing temporary override.")
            old_override = re.search(r"Override size: ([1-9][0-9]*x[1-9][0-9]*)", original)
            report["display"] = {"original_wm_size": original.strip(),
                                 "requested_override": args.display_size, "restored": False}
            # A timed-out command might still take effect, so cleanup is armed first.
            display_changed = True
            adb.shell("wm", "size", args.display_size, timeout=10)
            current = adb.shell("wm", "size", timeout=10).decode("utf-8", errors="replace")
            sizes = re.findall(r"(?:Physical|Override) size: ([1-9][0-9]*)x([1-9][0-9]*)", current)
            if not sizes:
                raise RuntimeError("Cannot determine test display size.")
            adb.size = tuple(map(int, sizes[-1]))
            report["display"]["during_wm_size"] = current.strip()
        report["device"]["test_screen_size"] = adb.size
        context = ComponentReader(adb) if args.reader == "component" else nullcontext()
        started = time.monotonic()
        with context:
            report["startup"]["reader_session_startup_s"] = time.monotonic() - started
            report["startup"]["reader_ready_s"] = adb.offset()
            adb.save()

            def log(message):
                report["logs"].append({"at_s": adb.offset(), "message": message})
                adb.save()

            workflow = Workflow(adb, config, log=log)
            visible_cards = None
            previous_view = None
            for _ in range(args.cards):
                selected_at = adb.offset()
                card, visible_cards = workflow.next_card(visible_cards)
                selection_duration = adb.offset() - selected_at
                if card is None:
                    report["stop_reason"] = "no_more_readable_cards"
                    break
                if not card["complete"]:
                    raise RuntimeError("Production scanner returned an incomplete card.")
                if card["key"] in workflow.checkpoint.processed:
                    raise RuntimeError("Production scanner returned an already consumed card.")
                view = workflow._card_view(visible_cards)
                if view != previous_view:
                    page = {"index": len(report["pages"]) + 1, "at_s": adb.offset(),
                            "read_index": adb.last_successful_read,
                            "card_view": view, "cards": visible_cards,
                            "complete_card_count": sum(bool(item["complete"]) for item in visible_cards),
                            "processed_before_page": len(workflow.checkpoint.processed),
                            "swipes_before_page": len(report["swipes"])}
                    report["pages"].append(page)
                    adb.screenshot(page)
                    previous_view = view
                report["selected_cards"].append({"index": len(report["selected_cards"]) + 1,
                                                   "page_index": report["pages"][-1]["index"],
                                                   "read_index": adb.last_successful_read,
                                                   "selection_duration_s": selection_duration,
                                                   "card": card})
                workflow.checkpoint.anchor = card["key"]
                workflow.checkpoint.processed.add(card["key"])
                adb.save()
            report["status"] = "complete"
            report.setdefault("stop_reason", "requested_card_count_reached")
    except (Exception, KeyboardInterrupt) as exc:
        report.update(status="stopped", error_type=type(exc).__name__, error=str(exc),
                      traceback=traceback.format_exc())
        print(f"Stopped: {exc}", file=sys.stderr, flush=True)
        code = 1
    finally:
        if display_changed:
            restoration = {"started_at": wall_time(), "started_s": adb.offset()}
            report["display"]["restoration"] = restoration
            try:
                adb.shell("wm", "size", old_override.group(1) if old_override else "reset", timeout=10)
                restored = adb.shell("wm", "size", timeout=10).decode("utf-8", errors="replace").strip()
                restoration.update(status="ok", wm_size=restored)
                report["display"]["restored"] = restored == report["display"]["original_wm_size"]
                if not report["display"]["restored"]:
                    raise RuntimeError("Display restoration did not match its original state.")
            except Exception as exc:
                restoration.update(status="error", error=str(exc))
                report["status"] = "stopped"
                report["restoration_error"] = str(exc)
                print(f"Display restoration failed: {exc}", file=sys.stderr, flush=True)
                code = 1
            restoration["duration_s"] = adb.offset() - restoration["started_s"]
        durations = [entry["duration_s"] for entry in report["reads"] if entry["status"] == "ok"]
        if durations:
            report["read_timing_s"] = {"count": len(durations), "first": durations[0],
                                       "median": statistics.median(durations), "max": max(durations)}
        report.update(finished_at=wall_time(), elapsed_s=adb.offset(),
                      selected_card_count=len(report["selected_cards"]),
                      distinct_page_count=len(report["pages"]), swipe_count=len(report["swipes"]))
        adb.save()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
